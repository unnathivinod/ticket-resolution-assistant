"""Ingestion worker: keeps the search index in step with PostgreSQL.

How a new or changed document reaches the search index:

  gateway  saves the document in PostgreSQL, then puts a small note on a queue
           (a Redis Stream): "ticket T-123 changed"
  worker   (this file) picks up the note, reads the CURRENT document from PostgreSQL,
           turns it into vectors and writes them to Qdrant, then marks it as indexed

Things that go wrong, and what happens:
  the embedding service or Qdrant is down   the note stays on the queue and is tried again
  a note keeps failing                      after a few tries it moves to a "dead letter" list
  the note was never written (Redis down)   a regular sweep compares PostgreSQL with the index
                                            and repairs anything that was missed
  the worker crashes half-way               another worker (or the restart) takes over the note

Run:  python -m services.ingestion.worker
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import sys
import time

import redis
from prometheus_client import Counter, Gauge, Histogram, start_http_server
from qdrant_client import QdrantClient

from libs.common.embedding_client import EmbeddingClient
from libs.common.observability import JsonFormatter
from services.ingestion.config import Settings
from services.ingestion.indexer import (
    collection_dim,
    ensure_collection,
    index_chunks,
    kb_chunks,
    remove_document,
    remove_stale_chunks,
    ticket_chunks,
)
from services.ingestion.store import DocumentStore

INDEX_VERSION_KEY = "index:version"  # goes up with every change, so cached answers go stale

EVENTS = Counter("ingestion_events_total", "Queue events handled, by result", ["result"])
SECONDS = Histogram(
    "ingestion_seconds",
    "Time to index one document",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)
DEAD = Counter("ingestion_dead_letter_total", "Events given up on after too many failed tries")
RECOVERED = Counter("ingestion_sweep_recovered_total", "Documents repaired by the safety sweep")
WAITING = Gauge("ingestion_queue_waiting", "Events on the queue that are not finished yet")
LAST_SUCCESS = Gauge("ingestion_last_success_timestamp", "When a document was last indexed (Unix time)")
for _result in ("indexed", "removed", "missing", "retry", "dead_letter", "invalid"):
    EVENTS.labels(_result)  # start at 0 so the dashboard shows a flat line, not nothing

log = logging.getLogger("ingestion")


class Worker:
    def __init__(self, redis_client, store, qdrant, embedder, settings: Settings, consumer: str = "") -> None:
        self._redis = redis_client
        self._store = store
        self._qdrant = qdrant
        self._embedder = embedder
        self._settings = settings
        # Every worker needs its own name on the queue, so several can run side by side.
        self._consumer = consumer or f"{socket.gethostname()}-{os.getpid()}"
        self._last_sweep = 0.0
        self.running = True

    # ---- one document -----------------------------------------------------------------------

    def process(self, doc_type: str, doc_id: str) -> str:
        """Make the index match PostgreSQL for one document. Safe to repeat."""
        row, read_at = self._store.load(doc_type, doc_id)
        if row is None:
            return "missing"
        if not row["is_active"]:
            remove_document(self._qdrant, doc_id)  # retired: it must no longer be found
            result = "removed"
        else:
            chunks = ticket_chunks(row) if doc_type == "ticket" else kb_chunks(row)
            index_chunks(self._qdrant, self._embedder, chunks)
            remove_stale_chunks(self._qdrant, doc_id, keep=len(chunks))
            result = "indexed"
        self._store.mark_indexed(doc_type, doc_id, read_at)
        return result

    def _bump_index_version(self) -> None:
        try:
            self._redis.incr(INDEX_VERSION_KEY)
        except redis.RedisError:
            log.warning("could not bump the index version")

    # ---- the queue --------------------------------------------------------------------------

    def setup(self) -> None:
        """Create the queue's consumer group and, if needed, the search collection."""
        try:
            self._redis.xgroup_create(self._settings.stream, self._settings.group, id="0", mkstream=True)
        except redis.ResponseError as error:
            if "BUSYGROUP" not in str(error):  # "already exists" is fine
                raise
        if collection_dim(self._qdrant) is None:
            ensure_collection(self._qdrant, self._embedder.embed(["dimension check"])["dim"])

    def _handle(self, message_id: str, fields: dict) -> None:
        settings = self._settings
        doc_type, doc_id = fields.get("doc_type"), fields.get("doc_id")
        details = {"doc_type": doc_type, "doc_id": doc_id, "event": message_id}
        if doc_type not in ("ticket", "kb") or not doc_id:
            log.error("unreadable event dropped", extra={"fields": details})
            EVENTS.labels("invalid").inc()
            self._redis.xack(settings.stream, settings.group, message_id)
            return

        started = time.perf_counter()
        try:
            result = self.process(doc_type, doc_id)
        except Exception as error:  # noqa: BLE001 - any failure is retried, then dead-lettered
            attempts = self._redis.hincrby(f"{settings.stream}:attempts", message_id, 1)
            details |= {"attempt": attempts, "error": str(error)}
            if attempts >= settings.max_attempts:
                self._redis.xadd(settings.dead_letter_stream, {**fields, "error": str(error)[:500]})
                self._redis.xack(settings.stream, settings.group, message_id)
                self._redis.hdel(f"{settings.stream}:attempts", message_id)
                DEAD.inc()
                EVENTS.labels("dead_letter").inc()
                log.error("event moved to the dead-letter list", extra={"fields": details})
            else:
                EVENTS.labels("retry").inc()  # not acknowledged, so it will be picked up again
                log.warning("indexing failed, will retry", extra={"fields": details})
            return

        self._redis.xack(settings.stream, settings.group, message_id)
        self._redis.hdel(f"{settings.stream}:attempts", message_id)
        SECONDS.observe(time.perf_counter() - started)
        EVENTS.labels(result).inc()
        if result != "missing":
            LAST_SUCCESS.set_to_current_time()
        details |= {"result": result, "ms": round((time.perf_counter() - started) * 1000, 1)}
        log.info("event handled", extra={"fields": details})

    def run_once(self, block: bool = True) -> int:
        """Handle one batch: first events that got stuck, then new ones. Returns how many.

        With block=True it waits a few seconds for new events when there is nothing to do.
        """
        settings = self._settings
        stream, group = settings.stream, settings.group

        # Events another worker took but never finished (it crashed, or the attempt failed).
        claimed = self._redis.xautoclaim(
            stream, group, self._consumer, min_idle_time=settings.retry_after_ms, count=settings.batch_size
        )
        messages = list(claimed[1])
        if len(messages) < settings.batch_size:
            fresh = self._redis.xreadgroup(
                group,
                self._consumer,
                {stream: ">"},
                count=settings.batch_size - len(messages),
                # Only wait for new events when there is nothing else to do right now.
                block=settings.block_ms if block and not messages else None,
            )
            for _stream, entries in fresh or []:
                messages.extend(entries)

        handled = 0
        for message_id, fields in messages:
            if fields is None:  # the event was trimmed from the queue while it was waiting
                self._redis.xack(stream, group, message_id)
                continue
            self._handle(message_id, fields)
            handled += 1
        if handled:
            self._bump_index_version()
        return handled

    # ---- the safety net ---------------------------------------------------------------------

    def sweep(self) -> int:
        """Index every document PostgreSQL says is behind. Catches anything the queue lost."""
        repaired, failures_in_a_row = 0, 0
        for doc_type, doc_id in self._store.pending(self._settings.sweep_grace_seconds):
            try:
                self.process(doc_type, doc_id)
                repaired += 1
                failures_in_a_row = 0
            except Exception as error:  # noqa: BLE001 - it stays "behind" and is retried next sweep
                details = {"doc_type": doc_type, "doc_id": doc_id, "error": str(error)}
                log.warning("sweep could not index a document", extra={"fields": details})
                failures_in_a_row += 1
                if failures_in_a_row >= 3:
                    break  # a dependency is probably down: stop and try again at the next sweep
        if repaired:
            RECOVERED.inc(repaired)
            self._bump_index_version()
            log.info("sweep repaired documents", extra={"fields": {"count": repaired}})
        return repaired

    def _update_gauges(self) -> None:
        settings = self._settings
        try:
            pending = self._redis.xpending(settings.stream, settings.group)["pending"]
            groups = self._redis.xinfo_groups(settings.stream)
            lag = next((g.get("lag") or 0 for g in groups if g["name"] == settings.group), 0)
            WAITING.set(pending + lag)
        except (redis.RedisError, KeyError, TypeError):
            pass  # a missing number on a dashboard must never stop the worker

    def run_forever(self) -> None:
        while self.running:
            try:
                self.run_once()
                if time.monotonic() - self._last_sweep >= self._settings.sweep_every_seconds:
                    self._last_sweep = time.monotonic()
                    self.sweep()
                    self._update_gauges()
            except (redis.RedisError, OSError) as error:
                log.error("queue unavailable, waiting", extra={"fields": {"error": str(error)}})
                time.sleep(5)
            except Exception:  # noqa: BLE001 - the loop must survive anything
                log.exception("unexpected error in the worker loop")
                time.sleep(5)


def main() -> None:
    settings = Settings()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter("ingestion"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    store = DocumentStore(settings.database_url)
    worker = Worker(
        redis.Redis.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=5),
        store,
        QdrantClient(url=settings.qdrant_url, timeout=30),
        EmbeddingClient(settings.embedding_url, timeout=120),
        settings,
    )

    def stop(*_) -> None:  # finish the current batch, then exit cleanly
        worker.running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    start_http_server(settings.metrics_port)
    while worker.running:
        try:
            worker.setup()
            break
        except Exception as error:  # noqa: BLE001 - dependencies may still be starting
            log.warning("waiting for dependencies", extra={"fields": {"error": str(error)}})
            time.sleep(5)
    log.info("worker started", extra={"fields": {"stream": settings.stream, "group": settings.group}})
    worker.run_forever()
    store.close()
    log.info("worker stopped")


if __name__ == "__main__":
    main()
