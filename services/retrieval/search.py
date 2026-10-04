"""The search logic: embed the query, search Qdrant, rerank, tidy up.

Pipeline for one request:
  1. Embed the query once (dense + keyword vectors).
  2. For tickets and for KB articles, fetch the top candidates from Qdrant:
       "dense"  = meaning only        "sparse" = keywords only (BM25)
       "hybrid" = both, merged with Reciprocal Rank Fusion (RRF)
  3. Rerank all candidates with the cross-encoder (a slower, more careful model).
  4. Keep the best chunk per document, apply limits, return with scores and timings.
"""

from __future__ import annotations

import math
import time
from typing import Literal

from prometheus_client import Counter, Histogram
from pydantic import BaseModel, Field
from qdrant_client import QdrantClient, models

from services.ingestion.indexer import DENSE_VECTOR, SPARSE_VECTOR
from services.retrieval.config import Settings

STAGE_LATENCY = Histogram(
    "retrieval_stage_seconds",
    "Time spent in each retrieval stage",
    ["stage"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
TOP_SIMILARITY = Histogram(
    "retrieval_top_similarity",
    "Similarity of the closest result (a drop over time means the data or the questions have changed)",
    buckets=(0.3, 0.4, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.0),
)
EMPTY_RESULTS = Counter("retrieval_empty_results_total", "Searches that returned nothing")

Mode = Literal["hybrid", "dense", "sparse"]


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    top_k_tickets: int = Field(3, ge=0, description="How many past tickets to return")
    top_k_kb: int = Field(2, ge=0, description="How many knowledge-base articles to return")
    # The defaults below were chosen from measurements, see evals/results/ and docs/DESIGN_DECISIONS.md:
    # hybrid with the meaning search counting 3x matched the best accuracy and still finds exact codes,
    # and the reranker added about 900 ms per search without improving accuracy on our data.
    mode: Mode = "hybrid"
    dense_weight: float = Field(
        3.0, gt=0, le=10, description="Hybrid only: how much more the meaning search counts than keywords"
    )
    rerank: bool = False
    score_relevance: bool = Field(
        False,
        description="Also score each returned result with the cross-encoder (0 to 1), without "
        "changing the order. Used as a second 'does this really fit?' check",
    )
    product_hint: str | None = Field(
        None,
        description="Product guessed by triage. Used only with rerank: matching results get a small boost",
    )


class SearchResult(BaseModel):
    id: str
    source_type: Literal["ticket", "kb"]
    title: str
    text: str = Field(description="The part that matched the query")
    content: str = Field(description="The full problem + resolution, for the LLM to read")
    score: float = Field(description="Ranking score. Its scale depends on the search mode (see score_type)")
    similarity: float = Field(
        description="How close the meaning is to the query, from -1 to 1. Comparable across searches, "
        "so it is the right number for a 'no confident match' threshold"
    )
    category: str | None = None
    product: str | None = None
    severity: str | None = None
    scenario_id: str | None = None
    relevance: float | None = Field(
        None, description="Cross-encoder score from 0 to 1, only when score_relevance was asked for"
    )


class SearchResponse(BaseModel):
    results: list[SearchResult]
    mode: Mode
    reranked: bool
    score_type: str
    candidates_considered: int
    timings_ms: dict[str, float]


def _sigmoid(value: float) -> float:
    """Squash a raw reranker score into the range 0 to 1."""
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, value))))


class Searcher:
    def __init__(self, qdrant: QdrantClient, embedder, settings: Settings) -> None:
        self._qdrant = qdrant
        self._embedder = embedder
        self._settings = settings

    def ready(self) -> bool:
        return self._qdrant.collection_exists(self._settings.collection) and self._embedder.ready()

    def _fetch(
        self,
        mode: Mode,
        dense: list[float],
        sparse: dict,
        source_type: str,
        limit: int,
        dense_weight: float = 1.0,
    ) -> list:
        """One Qdrant query for one source type. Inactive (outdated) documents are always excluded."""
        only_active = models.Filter(
            must=[
                models.FieldCondition(key="is_active", match=models.MatchValue(value=True)),
                models.FieldCondition(key="source_type", match=models.MatchValue(value=source_type)),
            ]
        )
        sparse_vector = models.SparseVector(indices=sparse["indices"], values=sparse["values"])
        collection = self._settings.collection
        if mode == "dense":
            response = self._qdrant.query_points(
                collection,
                query=dense,
                using=DENSE_VECTOR,
                query_filter=only_active,
                limit=limit,
                with_payload=True,
                with_vectors=[DENSE_VECTOR],
            )
        elif mode == "sparse":
            response = self._qdrant.query_points(
                collection,
                query=sparse_vector,
                using=SPARSE_VECTOR,
                query_filter=only_active,
                limit=limit,
                with_payload=True,
                with_vectors=[DENSE_VECTOR],
            )
        else:
            # Reciprocal Rank Fusion merges the two result lists by rank. With a dense weight
            # above 1, a good rank in the meaning search counts for more than in the keyword search.
            fusion = (
                models.FusionQuery(fusion=models.Fusion.RRF)
                if dense_weight == 1.0
                else models.RrfQuery(rrf=models.Rrf(weights=[dense_weight, 1.0]))
            )
            response = self._qdrant.query_points(
                collection,
                prefetch=[
                    models.Prefetch(query=dense, using=DENSE_VECTOR, filter=only_active, limit=limit),
                    models.Prefetch(
                        query=sparse_vector, using=SPARSE_VECTOR, filter=only_active, limit=limit
                    ),
                ],
                query=fusion,
                limit=limit,
                with_payload=True,
                with_vectors=[DENSE_VECTOR],
            )
        return response.points

    def search(self, request: SearchRequest) -> SearchResponse:
        settings = self._settings
        timings: dict[str, float] = {}
        started = time.perf_counter()

        # 1. Embed the query once and reuse it for every Qdrant query.
        stage = time.perf_counter()
        vectors = self._embedder.embed([request.query], kind="query")
        dense, sparse = vectors["dense"][0], vectors["sparse"][0]
        timings["embed"] = time.perf_counter() - stage

        # 2. Fetch candidates per source type, so tickets cannot crowd out KB articles.
        stage = time.perf_counter()
        limits = {"ticket": request.top_k_tickets, "kb": request.top_k_kb}
        candidates = []
        for source_type, top_k in limits.items():
            if top_k > 0:
                fetch = max(settings.candidates, top_k * 2)
                candidates.extend(
                    self._fetch(request.mode, dense, sparse, source_type, fetch, request.dense_weight)
                )
        timings["search"] = time.perf_counter() - stage

        # 3. Rerank: the cross-encoder reads the query and each candidate together.
        scores = [point.score for point in candidates]
        reranked = bool(request.rerank and candidates)
        if reranked:
            stage = time.perf_counter()
            raw = self._embedder.rerank(request.query, [point.payload["text"] for point in candidates])
            scores = [_sigmoid(value) for value in raw]
            if request.product_hint:
                scores = [
                    min(1.0, score + settings.product_boost)
                    if point.payload.get("product") == request.product_hint
                    else score
                    for point, score in zip(candidates, scores, strict=True)
                ]
            timings["rerank"] = time.perf_counter() - stage

        # 4. Best chunk per document, best first, then apply the per-type limits.
        best: dict[str, tuple[float, object]] = {}
        for point, score in zip(candidates, scores, strict=True):
            doc_id = point.payload["doc_id"]
            if doc_id not in best or score > best[doc_id][0]:
                best[doc_id] = (score, point)
        ranked = sorted(best.values(), key=lambda item: item[0], reverse=True)

        def similarity(point) -> float:
            # Both vectors have length 1, so their dot product is the cosine similarity.
            return sum(a * b for a, b in zip(dense, point.vector[DENSE_VECTOR], strict=True))

        results: list[SearchResult] = []
        taken = {"ticket": 0, "kb": 0}
        for score, point in ranked:
            payload = point.payload
            source_type = payload["source_type"]
            if taken[source_type] >= limits[source_type]:
                continue
            taken[source_type] += 1
            results.append(
                SearchResult(
                    id=payload["doc_id"],
                    source_type=source_type,
                    title=payload["title"],
                    text=payload["text"],
                    content=payload["content"],
                    score=round(score, 4),
                    similarity=round(similarity(point), 4),
                    category=payload.get("category"),
                    product=payload.get("product"),
                    severity=payload.get("severity"),
                    scenario_id=payload.get("scenario_id"),
                )
            )

        # 5. Optional second opinion on the few results we return. The cross-encoder reads the
        #    complaint and one source together, which is slower but a different kind of evidence
        #    than vector similarity. The order is NOT changed (reranking did not help, see evals).
        if request.score_relevance and results:
            stage = time.perf_counter()
            raw = self._embedder.rerank(request.query, [result.text for result in results])
            for result, value in zip(results, raw, strict=True):
                result.relevance = round(_sigmoid(value), 4)
            timings["relevance"] = time.perf_counter() - stage

        timings["total"] = time.perf_counter() - started
        for name, seconds in timings.items():
            STAGE_LATENCY.labels(name).observe(seconds)
        if results:
            TOP_SIMILARITY.observe(max(result.similarity for result in results))
        else:
            EMPTY_RESULTS.inc()

        score_type = (
            "reranker (0 to 1)"
            if reranked
            else {"hybrid": "rrf", "dense": "cosine", "sparse": "bm25"}[request.mode]
        )
        return SearchResponse(
            results=results,
            mode=request.mode,
            reranked=reranked,
            score_type=score_type,
            candidates_considered=len(candidates),
            timings_ms={name: round(seconds * 1000, 1) for name, seconds in timings.items()},
        )
