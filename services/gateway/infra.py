"""The gateway's connections to Redis (cache, rate limit) and PostgreSQL (audit log, feedback).

Each class has a small interface, so the tests can swap in in-memory versions.
A failing cache or rate limiter never fails a request: the gateway carries on without it.
"""

from __future__ import annotations

import json
import logging
import time
import uuid

import redis
from prometheus_client import Counter
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

log = logging.getLogger("gateway")
INFRA_ERRORS = Counter("gateway_infra_errors_total", "Failures talking to Redis or PostgreSQL", ["component"])


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


class RedisRateLimiter:
    """Allows each API key a fixed number of requests per minute."""

    def __init__(self, client: redis.Redis, limit_per_minute: int) -> None:
        self._redis = client
        self._limit = limit_per_minute

    def allow(self, identity: str) -> tuple[bool, int]:
        """Returns (allowed, seconds until the limit resets)."""
        now = time.time()
        window = int(now // 60)
        retry_after = 60 - int(now % 60)
        try:
            key = f"ratelimit:{identity}:{window}"
            count = self._redis.incr(key)
            if count == 1:
                self._redis.expire(key, 120)
            return count <= self._limit, retry_after
        except redis.RedisError:
            # Fail open: if Redis is down we prefer serving agents over blocking everyone.
            INFRA_ERRORS.labels("rate_limiter").inc()
            return True, retry_after


class PostgresStore:
    """Writes the audit log and the agents' feedback."""

    def __init__(self, database_url: str) -> None:
        self._pool = ConnectionPool(database_url, min_size=1, max_size=5, open=False, timeout=10)
        self._opened = False

    def _connection(self):
        if not self._opened:
            self._pool.open()
            self._opened = True
        return self._pool.connection()

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

    def save_feedback(self, request_id: str, helpful: bool, comment: str | None, edited: str | None) -> bool:
        """Store feedback. Returns False when the request ID is not known."""
        with self._connection() as conn:
            found = conn.execute(
                "SELECT 1 FROM resolve_requests WHERE request_id = %s", (uuid.UUID(request_id),)
            ).fetchone()
            if not found:
                return False
            conn.execute(
                """INSERT INTO feedback (request_id, helpful, comment, edited_resolution)
                   VALUES (%s, %s, %s, %s)""",
                (uuid.UUID(request_id), helpful, comment, edited),
            )
            return True
