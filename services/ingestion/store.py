"""The ingestion worker's view of PostgreSQL: read a document, mark it as indexed, find stragglers."""

from __future__ import annotations

from datetime import datetime

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

TABLES = {"ticket": "tickets", "kb": "kb_articles"}


class DocumentStore:
    def __init__(self, database_url: str) -> None:
        self._pool = ConnectionPool(
            database_url, min_size=1, max_size=3, open=False, timeout=10, kwargs={"row_factory": dict_row}
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

    def load(self, doc_type: str, doc_id: str) -> tuple[dict | None, datetime | None]:
        """The current version of a document, and the database time at which it was read."""
        table = TABLES[doc_type]
        with self._connection() as conn:
            row = conn.execute(
                f"SELECT *, now() AS read_at FROM {table} WHERE id = %s",  # noqa: S608 - fixed table names
                (doc_id,),
            ).fetchone()
        if row is None:
            return None, None
        read_at = row.pop("read_at")
        for name in ("created_at", "updated_at"):
            row[name] = row[name].isoformat() if row.get(name) else None
        row.pop("indexed_at", None)
        return row, read_at

    def mark_indexed(self, doc_type: str, doc_id: str, read_at: datetime) -> None:
        """Record that the index holds the version read at `read_at`.

        If the document was changed again after it was read, updated_at is newer than
        read_at, so it still counts as "behind" and gets indexed once more.
        """
        table = TABLES[doc_type]
        with self._connection() as conn:
            conn.execute(
                f"UPDATE {table} SET indexed_at = %s WHERE id = %s",  # noqa: S608 - fixed table names
                (read_at, doc_id),
            )

    def pending(self, grace_seconds: int, limit: int = 200) -> list[tuple[str, str]]:
        """Documents whose index entry is missing or out of date, and that have waited a while."""
        found: list[tuple[str, str]] = []
        with self._connection() as conn:
            for doc_type, table in TABLES.items():
                rows = conn.execute(
                    f"""SELECT id FROM {table}
                        WHERE (indexed_at IS NULL OR indexed_at < updated_at)
                          AND updated_at < now() - make_interval(secs => %s)
                        ORDER BY updated_at LIMIT %s""",  # noqa: S608 - fixed table names
                    (grace_seconds, limit),
                ).fetchall()
                found.extend((doc_type, row["id"]) for row in rows)
        return found
