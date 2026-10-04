"""The gateway's connections to Redis (cache, rate limit, ingestion queue) and PostgreSQL
(audit log, feedback, documents, ticket classes).

Each class has a small interface, so the tests can swap in in-memory versions.
A failing cache, rate limiter or queue never fails a request: the gateway carries on without it.
"""

from __future__ import annotations

import json
import logging
import time
import uuid

import redis
from prometheus_client import Counter
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

log = logging.getLogger("gateway")
INDEX_VERSION_KEY = "index:version"  # raised by the ingestion worker (services/ingestion/worker.py)
NONE_OF_THESE = "none_of_these"  # the agent's way of saying "no existing category fits"
TABLES = {"ticket": "tickets", "kb": "kb_articles"}
INFRA_ERRORS = Counter("gateway_infra_errors_total", "Failures talking to Redis or PostgreSQL", ["component"])
for _component in ("cache", "rate_limiter", "queue", "audit_log"):
    INFRA_ERRORS.labels(_component)  # start at 0 so the dashboard shows a flat line, not nothing


def _json(value: dict | None) -> Jsonb | None:
    """A missing value must be stored as SQL NULL, not as the JSON text "null"."""
    return None if value is None else Jsonb(value)


class RedisCache:
    """Remembers finished answers for a while, so a repeated complaint returns instantly."""

    def __init__(self, client: redis.Redis, ttl_seconds: int) -> None:
        self._redis = client
        self._ttl = ttl_seconds

    def get(self, key: str) -> dict | None:
        try:
            value = self._redis.get(key)
            return json.loads(value) if value else None
        except (redis.RedisError, ValueError):
            INFRA_ERRORS.labels("cache").inc()
            return None

    def set(self, key: str, value: dict) -> None:
        try:
            self._redis.set(key, json.dumps(value), ex=self._ttl)
        except redis.RedisError:
            INFRA_ERRORS.labels("cache").inc()

    def index_version(self) -> str:
        """A counter the ingestion worker raises whenever the search index changes.

        It is part of every cache key, so an answer cached before a knowledge-base
        update is never served after it.
        """
        try:
            value = self._redis.get(INDEX_VERSION_KEY)
            return (value.decode() if isinstance(value, bytes) else value) or "0"
        except redis.RedisError:
            INFRA_ERRORS.labels("cache").inc()
            return "0"


class IngestQueue:
    """Tells the ingestion worker that a document changed (a Redis Stream)."""

    def __init__(self, client: redis.Redis, stream: str = "ingest:events", max_length: int = 100_000) -> None:
        self._redis = client
        self._stream = stream
        self._max_length = max_length

    def publish(self, doc_type: str, doc_id: str) -> bool:
        """Returns False if the note could not be queued. The document is still saved in
        PostgreSQL, and the worker's regular sweep will index it a little later."""
        try:
            self._redis.xadd(
                self._stream,
                {"doc_type": doc_type, "doc_id": doc_id},
                maxlen=self._max_length,
                approximate=True,
            )
            return True
        except redis.RedisError:
            INFRA_ERRORS.labels("queue").inc()
            return False


class RedisRateLimiter:
    """Allows each API key a fixed number of requests per minute."""

    def __init__(self, client: redis.Redis, limit_per_minute: int) -> None:
        self._redis = client
        self._limit = limit_per_minute

    def allow(self, identity: str, limit: int | None = None) -> tuple[bool, int]:
        """Returns (allowed, seconds until the limit resets)."""
        limit = limit or self._limit
        now = time.time()
        window = int(now // 60)
        retry_after = 60 - int(now % 60)
        try:
            key = f"ratelimit:{identity}:{window}"
            count = self._redis.incr(key)
            if count == 1:
                self._redis.expire(key, 120)
            return count <= limit, retry_after
        except redis.RedisError:
            # Fail open: if Redis is down we prefer serving agents over blocking everyone.
            INFRA_ERRORS.labels("rate_limiter").inc()
            return True, retry_after


class PostgresStore:
    """Audit log and feedback, plus the documents and ticket classes that can change over time."""

    def __init__(self, database_url: str) -> None:
        self._pool = ConnectionPool(
            database_url, min_size=1, max_size=5, open=False, timeout=10, kwargs={"row_factory": dict_row}
        )
        self._opened = False

    def _connection(self):
        if not self._opened:
            self._pool.open()
            self._opened = True
        return self._pool.connection()

    def close(self) -> None:
        if self._opened:
            self._pool.close()
            self._opened = False

    def ready(self) -> bool:
        try:
            with self._connection() as conn:
                conn.execute("SELECT 1")
            return True
        except Exception:  # noqa: BLE001 - any failure means "not ready"
            return False

    def save_request(self, record: dict) -> bool:
        """Store one /v1/resolve request. Returns False (and logs) if the database is unavailable."""
        try:
            with self._connection() as conn:
                conn.execute(
                    """INSERT INTO resolve_requests
                           (request_id, complaint_masked, triage, source_ids, resolution, grounded,
                            escalated, latency_ms, llm_model, prompt_version, index_version)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        record["request_id"],
                        record["complaint_masked"],
                        _json(record["triage"]),
                        record["source_ids"],
                        _json(record["resolution"]),
                        record["grounded"],
                        record["escalated"],
                        Jsonb(record["latency_ms"]),
                        record["llm_model"],
                        record["prompt_version"],
                        record["index_version"],
                    ),
                )
            return True
        except Exception:  # noqa: BLE001 - the answer is still returned to the agent
            INFRA_ERRORS.labels("audit_log").inc()
            log.exception("could not write the audit log")
            return False

    def save_feedback(
        self,
        request_id: str,
        helpful: bool,
        comment: str | None,
        edited: str | None,
        correct_category: str | None = None,
    ) -> bool:
        """Store feedback. Returns False when the request ID is not known."""
        with self._connection() as conn:
            found = conn.execute(
                "SELECT 1 FROM resolve_requests WHERE request_id = %s", (uuid.UUID(request_id),)
            ).fetchone()
            if not found:
                return False
            conn.execute(
                """INSERT INTO feedback (request_id, helpful, comment, edited_resolution, correct_category)
                   VALUES (%s, %s, %s, %s, %s)""",
                (uuid.UUID(request_id), helpful, comment, edited, correct_category),
            )
            return True

    def recent_feedback(self, limit: int = 50) -> dict:
        """The latest agent ratings with the complaint they were about, plus the totals."""
        with self._connection() as conn:
            totals = conn.execute(
                """SELECT count(*) FILTER (WHERE helpful) AS helpful,
                          count(*) FILTER (WHERE NOT helpful) AS not_helpful,
                          count(*) FILTER (WHERE correct_category IS NOT NULL) AS category_corrections
                   FROM feedback"""
            ).fetchone()
            rows = conn.execute(
                """SELECT f.created_at, f.helpful, f.comment, f.correct_category,
                          r.complaint_masked AS complaint,
                          r.triage -> 'category' ->> 'label' AS category,
                          r.escalated, r.llm_model
                   FROM feedback f JOIN resolve_requests r USING (request_id)
                   ORDER BY f.created_at DESC LIMIT %s""",
                (limit,),
            ).fetchall()
        for row in rows:
            row["created_at"] = row["created_at"].isoformat()
        return {"totals": dict(totals), "items": rows}

    # ---- ticket classes -----------------------------------------------------------------------

    def taxonomy(self) -> dict[str, list[dict]]:
        """The classes in use right now: {"category": [{name, description}], "product": [...]}."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT kind, name, description FROM taxonomy WHERE status = 'active' ORDER BY kind, name"
            ).fetchall()
        result: dict[str, list[dict]] = {"category": [], "product": []}
        for row in rows:
            result[row["kind"]].append({"name": row["name"], "description": row["description"]})
        return result

    def add_class(self, kind: str, name: str, description: str | None) -> None:
        """Add a class, or bring a retired one back."""
        with self._connection() as conn:
            conn.execute(
                """INSERT INTO taxonomy (kind, name, description, status) VALUES (%s, %s, %s, 'active')
                   ON CONFLICT (kind, name) DO UPDATE SET
                       status = 'active',
                       description = COALESCE(EXCLUDED.description, taxonomy.description)""",
                (kind, name, description),
            )

    def retire_class(self, kind: str, name: str) -> bool:
        with self._connection() as conn:
            cursor = conn.execute(
                "UPDATE taxonomy SET status = 'retired' WHERE kind = %s AND name = %s AND status = 'active'",
                (kind, name),
            )
            return cursor.rowcount > 0

    # ---- documents ----------------------------------------------------------------------------

    def save_ticket(self, ticket: dict) -> None:
        """Insert or update a resolved ticket. The search index is brought up to date by the worker."""
        with self._connection() as conn:
            conn.execute(
                """INSERT INTO tickets (id, subject, description, resolution_steps, category, product,
                                        severity, sentiment, scenario_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (id) DO UPDATE SET
                       subject = EXCLUDED.subject, description = EXCLUDED.description,
                       resolution_steps = EXCLUDED.resolution_steps, category = EXCLUDED.category,
                       product = EXCLUDED.product, severity = EXCLUDED.severity,
                       sentiment = EXCLUDED.sentiment, scenario_id = EXCLUDED.scenario_id,
                       is_active = TRUE, updated_at = now()""",
                (
                    ticket["id"],
                    ticket["subject"],
                    ticket["description"],
                    Jsonb(ticket["resolution_steps"]),
                    ticket["category"],
                    ticket["product"],
                    ticket["severity"],
                    ticket.get("sentiment"),
                    ticket.get("scenario_id"),
                ),
            )

    def recorded_tickets(self, limit: int = 50) -> list[dict]:
        """Fixes that people recorded through the API, newest first.

        Every ticket of the generated dataset carries a scenario_id (its answer key for the
        evals). A fix recorded by a person has none, which is how the two are told apart.
        """
        with self._connection() as conn:
            rows = conn.execute(
                """SELECT id, subject, category, product, is_active, created_at,
                          jsonb_array_length(resolution_steps) AS steps,
                          (indexed_at IS NOT NULL AND indexed_at >= updated_at) AS searchable
                   FROM tickets WHERE scenario_id IS NULL
                   ORDER BY created_at DESC LIMIT %s""",
                (limit,),
            ).fetchall()
        for row in rows:
            row["created_at"] = row["created_at"].isoformat()
        return rows

    def save_article(self, article: dict) -> int:
        """Insert or update a knowledge-base article. Returns its new version number."""
        with self._connection() as conn:
            row = conn.execute(
                """INSERT INTO kb_articles (id, title, body, category, product, scenario_id)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (id) DO UPDATE SET
                       title = EXCLUDED.title, body = EXCLUDED.body, category = EXCLUDED.category,
                       product = EXCLUDED.product, scenario_id = EXCLUDED.scenario_id,
                       version = kb_articles.version + 1, is_active = TRUE, updated_at = now()
                   RETURNING version""",
                (
                    article["id"],
                    article["title"],
                    article["body"],
                    article.get("category"),
                    article.get("product"),
                    article.get("scenario_id"),
                ),
            ).fetchone()
            return row["version"]

    def retire_document(self, doc_type: str, doc_id: str) -> bool:
        """Hide a document from search. It stays in PostgreSQL, so old answers can still be traced."""
        table = TABLES[doc_type]
        with self._connection() as conn:
            cursor = conn.execute(
                f"UPDATE {table} SET is_active = FALSE, updated_at = now() WHERE id = %s",  # noqa: S608
                (doc_id,),
            )
            return cursor.rowcount > 0

    def document_status(self, doc_type: str, doc_id: str) -> dict | None:
        table = TABLES[doc_type]
        with self._connection() as conn:
            row = conn.execute(
                f"""SELECT id, is_active, updated_at, indexed_at,
                           (indexed_at IS NOT NULL AND indexed_at >= updated_at) AS index_up_to_date
                    FROM {table} WHERE id = %s""",  # noqa: S608 - fixed table names
                (doc_id,),
            ).fetchone()
        if row is None:
            return None
        for name in ("updated_at", "indexed_at"):
            row[name] = row[name].isoformat() if row[name] else None
        return {"type": doc_type, **row}

    def documents_waiting(self) -> int:
        """How many documents the search index has not caught up with yet."""
        with self._connection() as conn:
            row = conn.execute(
                """SELECT (SELECT count(*) FROM tickets
                           WHERE indexed_at IS NULL OR indexed_at < updated_at)
                        + (SELECT count(*) FROM kb_articles
                           WHERE indexed_at IS NULL OR indexed_at < updated_at) AS waiting"""
            ).fetchone()
            return row["waiting"]

    # ---- proposals for new classes ----------------------------------------------------------------

    def proposals(self, status: str = "pending") -> list[dict]:
        with self._connection() as conn:
            rows = conn.execute(
                """SELECT id, kind, suggested_name, keywords, cluster_size, examples, status,
                          approved_name, created_at, decided_at
                   FROM class_proposals WHERE status = %s ORDER BY cluster_size DESC, id""",
                (status,),
            ).fetchall()
        for row in rows:
            for name in ("created_at", "decided_at"):
                row[name] = row[name].isoformat() if row[name] else None
        return rows

    def decide_proposal(
        self, proposal_id: int, approve: bool, name: str | None = None, description: str | None = None
    ) -> dict | None:
        """Approve (which adds the class) or reject a pending proposal. None if there is no such one."""
        with self._connection() as conn:
            row = conn.execute(
                """UPDATE class_proposals
                   SET status = %s, approved_name = %s, decided_at = now()
                   WHERE id = %s AND status = 'pending'
                   RETURNING id, kind, suggested_name, status, approved_name""",
                ("approved" if approve else "rejected", name if approve else None, proposal_id),
            ).fetchone()
            if row is None:
                return None
            if approve:
                conn.execute(
                    """INSERT INTO taxonomy (kind, name, description, status) VALUES (%s, %s, %s, 'active')
                       ON CONFLICT (kind, name) DO UPDATE SET status = 'active'""",
                    (row["kind"], name, description),
                )
            return row
