"""Recent complaints: how many customers reported the same thing in the last few minutes?

One complaint is one customer's problem. Several complaints that mean the same, arriving close
together, are usually one fault that affects many customers (an outage). This module keeps the
complaints of the last hours in their own small Qdrant collection and answers one question:
"how many recent complaints mean the same as this one?"

What record() does:
  1. Turn the complaint into a meaning vector.
  2. Save it with the current time. The ID is made from the text, so the same text sent twice
     (a retry, or the page's quick call followed by the full call) is still one complaint.
  3. Count the saved complaints inside the time window that are close enough in meaning.
  4. Now and then, delete complaints older than the retention time.

The text arrives already masked by the gateway. It is masked again here as a second layer.
"""

from __future__ import annotations

import threading
import time
import uuid

from prometheus_client import Counter, Histogram
from pydantic import BaseModel, Field
from qdrant_client import QdrantClient, models

from libs.common.pii import mask_pii
from services.retrieval.config import Settings

RECENT_SECONDS = Histogram(
    "retrieval_recent_seconds",
    "Time to save one complaint and count the similar recent ones",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5),
)
RECENT_REMOVED = Counter("retrieval_recent_cleanups_total", "Times old complaints were cleared out")

_ID_NAMESPACE = uuid.UUID("5a0e3f6c-7d1b-4c1e-9a55-2f1f6f0c9b11")
CLEANUP_EVERY_SECONDS = 300
MAX_COUNTED = 50  # a group larger than this is reported as this size: it is an incident either way


class RecentRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000, description="The complaint, personal details masked")
    window_minutes: int = Field(30, ge=1, le=1440, description="How far back 'recent' goes")
    min_similarity: float = Field(
        0.875, ge=0, le=1, description="How close in meaning two complaints must be to count as the same"
    )


class SimilarComplaint(BaseModel):
    text: str
    minutes_ago: int
    similarity: float


class RecentResponse(BaseModel):
    count: int = Field(description="This complaint plus the similar ones inside the window")
    others: list[SimilarComplaint] = Field(description="A few of the similar ones, closest first")
    window_minutes: int
    min_similarity: float


def complaint_id(text: str) -> str:
    """The same words always give the same ID, whatever the spacing or the capital letters."""
    return str(uuid.uuid5(_ID_NAMESPACE, " ".join(text.lower().split())))


class RecentComplaints:
    def __init__(self, qdrant: QdrantClient, embedder, settings: Settings, clock=time.time) -> None:
        self._qdrant = qdrant
        self._embedder = embedder
        self._settings = settings
        self._clock = clock  # tests pass their own clock to move time forward
        self._collection_ready = False
        self._last_cleanup = 0.0
        self._lock = threading.Lock()

    def _ensure_collection(self, dim: int) -> None:
        """Create the collection the first time it is needed."""
        if self._collection_ready:
            return
        name = self._settings.recent_collection
        with self._lock:
            if not self._qdrant.collection_exists(name):
                try:
                    self._qdrant.create_collection(
                        name, vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE)
                    )
                    # An index on the time, so "the last 30 minutes" stays a fast filter.
                    self._qdrant.create_payload_index(
                        name, field_name="ts", field_schema=models.PayloadSchemaType.FLOAT
                    )
                except Exception:
                    if not self._qdrant.collection_exists(name):
                        raise  # a real failure, not another copy of this service creating it first
            self._collection_ready = True

    def _cleanup(self, now: float) -> None:
        """Delete complaints older than the retention time. Runs at most every few minutes."""
        if now - self._last_cleanup < CLEANUP_EVERY_SECONDS:
            return
        self._last_cleanup = now
        too_old = now - self._settings.recent_retention_minutes * 60
        self._qdrant.delete(
            self._settings.recent_collection,
            points_selector=models.FilterSelector(
                filter=models.Filter(must=[models.FieldCondition(key="ts", range=models.Range(lt=too_old))])
            ),
        )
        RECENT_REMOVED.inc()

    def record(self, request: RecentRequest) -> RecentResponse:
        settings = self._settings
        started = time.perf_counter()
        now = self._clock()
        text = mask_pii(request.text)
        vector = self._embedder.embed([text], kind="document")["dense"][0]
        self._ensure_collection(len(vector))

        this_id = complaint_id(text)
        self._qdrant.upsert(
            settings.recent_collection,
            points=[
                models.PointStruct(
                    id=this_id,
                    vector=vector,
                    payload={"ts": now, "text": text[: settings.recent_text_chars]},
                )
            ],
            wait=True,
        )
        inside_window = models.Filter(
            must=[models.FieldCondition(key="ts", range=models.Range(gte=now - request.window_minutes * 60))]
        )
        found = self._qdrant.query_points(
            settings.recent_collection,
            query=vector,
            query_filter=inside_window,
            score_threshold=request.min_similarity,
            limit=MAX_COUNTED,
            with_payload=True,
        ).points
        others = [point for point in found if str(point.id) != this_id]
        self._cleanup(now)
        RECENT_SECONDS.observe(time.perf_counter() - started)
        return RecentResponse(
            count=len(others) + 1,
            others=[
                SimilarComplaint(
                    text=point.payload["text"],
                    minutes_ago=max(0, round((now - point.payload["ts"]) / 60)),
                    similarity=round(point.score, 3),
                )
                for point in others[: settings.recent_examples]
            ],
            window_minutes=request.window_minutes,
            min_similarity=request.min_similarity,
        )
