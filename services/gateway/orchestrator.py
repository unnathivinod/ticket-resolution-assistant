"""The steps behind POST /v1/resolve, in order.

  1. Mask personal details.
  2. Return a cached answer if this exact complaint was resolved recently.
  3. Triage: label the complaint (if triage is down, carry on without labels).
  4. Retrieval: find similar tickets and articles (required; without it there is nothing to answer from).
  5. Checkpoint: if nothing found is similar enough, do not ask the model. Recommend escalation.
  6. Generation: draft the cited resolution.
  7. Write the audit log, cache the answer.

Every dependency is passed in, so tests can run this logic with in-memory stand-ins.
"""

from __future__ import annotations

import hashlib
import time
import uuid

from prometheus_client import Counter, Histogram

from libs.common.pii import mask_pii
from libs.common.service_client import ServiceError
from services.gateway.config import Settings

OUTCOMES = Counter("gateway_resolve_total", "Resolve requests by outcome", ["outcome"])
CACHE = Counter("gateway_cache_total", "Cache lookups", ["result"])
STAGE_SECONDS = Histogram(
    "gateway_stage_seconds",
    "Time spent in each stage of a resolve request",
    ["stage"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 45, 60, 90, 120, 240),
)
DEGRADED = Counter("gateway_degraded_total", "Requests served without one of the services", ["service"])
TOP_SIMILARITY = Histogram(
    "gateway_top_similarity",
    "Similarity of the closest source per request (falling values mean unfamiliar complaints)",
    buckets=(0.3, 0.4, 0.5, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.0),
)


NO_MATCH_REASON = (
    "No past ticket or article is similar enough to this complaint. Escalate to second-line support."
)


class SearchUnavailableError(RuntimeError):
    """Retrieval is down, so the request cannot be served at all."""


class Orchestrator:
    def __init__(self, triage, retrieval, generation, cache, store, settings: Settings) -> None:
        self._triage = triage
        self._retrieval = retrieval
        self._generation = generation
        self._cache = cache
        self._store = store
        self._settings = settings

    def readiness(self) -> dict[str, bool]:
        return {
            "triage": self._triage.ready(),
            "retrieval": self._retrieval.ready(),
            "generation": self._generation.ready(),
            "database": self._store.ready(),
        }

    @staticmethod
    def _cache_key(masked_complaint: str) -> str:
        normalised = " ".join(masked_complaint.lower().split())
        return "resolve:" + hashlib.sha256(normalised.encode()).hexdigest()

    @staticmethod
    def _triage_view(result: dict | None) -> dict | None:
        """The parts of the triage result the agent needs to see."""
        if result is None:
            return None
        return {
            "category": {key: result["category"][key] for key in ("label", "confidence", "best_guess")},
            "product": {key: result["product"][key] for key in ("label", "confidence")},
            "severity": {key: result["severity"][key] for key in ("label", "reasons")},
            "sentiment": {"label": result["sentiment"]["label"]},
            "needs_review": result["needs_review"],
        }

    def resolve(self, complaint: str, generate: bool = True) -> dict:
        settings = self._settings
        started = time.perf_counter()
        timings: dict[str, float] = {}
        degraded: list[str] = []
        request_id = str(uuid.uuid4())
        masked = mask_pii(complaint)
        cache_key = self._cache_key(masked)

        # 2. Cache
        if generate:
            cached = self._cache.get(cache_key)
            CACHE.labels("hit" if cached else "miss").inc()
            if cached:
                response = {**cached, "request_id": request_id}
                response["meta"] = {**cached["meta"], "cached": True}
                response["meta"]["latency_ms"] = {"total": round((time.perf_counter() - started) * 1000, 1)}
                self._audit(request_id, masked, response)
                OUTCOMES.labels("cached").inc()
                return response

        # 3. Triage (optional: the answer is still useful without labels)
        stage = time.perf_counter()
        try:
            triage = self._triage.classify(masked)
        except ServiceError:
            triage = None
            degraded.append("triage")
        timings["triage"] = time.perf_counter() - stage

        # 4. Retrieval (required)
        stage = time.perf_counter()
        try:
            found = self._retrieval.search(
                masked, top_k_tickets=settings.top_k_tickets, top_k_kb=settings.top_k_kb
            )["results"]
        except ServiceError as error:
            raise SearchUnavailableError(str(error)) from error
        timings["retrieval"] = time.perf_counter() - stage

        top_similarity = max((result["similarity"] for result in found), default=0.0)
        TOP_SIMILARITY.observe(top_similarity)
        confident = top_similarity >= settings.min_similarity

        response = {
            "request_id": request_id,
            "triage": self._triage_view(triage),
            "sources": [
                {key: result[key] for key in ("id", "source_type", "title", "text", "similarity")}
                for result in found
            ],
            "resolution": None,
            "escalate": False,
            "escalation_reason": "",
            "meta": {
                "cached": False,
                "degraded": degraded,
                "confident_match": confident,
                "top_similarity": round(top_similarity, 4),
                "model": None,
                "prompt_version": None,
            },
        }

        if generate:
            if not confident:
                # 5. Checkpoint: a wrong fix costs more than no fix.
                response["escalate"] = True
                response["escalation_reason"] = NO_MATCH_REASON
            else:
                # 6. Generation
                stage = time.perf_counter()
                labels = None
                if triage:
                    labels = {
                        name: triage[name]["label"]
                        for name in ("category", "product", "severity", "sentiment")
                    }
                    labels = {name: (None if label == "unknown" else label) for name, label in labels.items()}
                sources = [
                    {key: result[key] for key in ("id", "source_type", "title", "content")}
                    for result in found
                ]
                try:
                    answer = self._generation.generate(masked, sources, labels)
                    response["resolution"] = {
                        key: answer[key]
                        for key in (
                            "mode",
                            "summary",
                            "already_tried",
                            "steps",
                            "grounded",
                            "fallback_reason",
                        )
                    }
                    response["escalate"] = answer["escalate"]
                    response["escalation_reason"] = answer["escalation_reason"]
                    response["meta"]["model"] = answer["model"]
                    response["meta"]["prompt_version"] = answer["prompt_version"]
                except ServiceError:
                    degraded.append("generation")
                    response["escalate"] = True
                    response["escalation_reason"] = "The answer could not be drafted. Use the sources below."
                timings["generation"] = time.perf_counter() - stage

        timings["total"] = time.perf_counter() - started
        response["meta"]["latency_ms"] = {name: round(seconds * 1000, 1) for name, seconds in timings.items()}
        for name, seconds in timings.items():
            STAGE_SECONDS.labels(name).observe(seconds)
        for name in degraded:
            DEGRADED.labels(name).inc()

        if generate:
            # 7. Audit log and cache. Only complete, healthy answers are cached.
            self._audit(request_id, masked, response)
            if response["resolution"] is not None and not degraded:
                self._cache.set(cache_key, response)
            outcome = (
                "degraded" if degraded else "answered" if response["resolution"] else "escalated_no_match"
            )
            OUTCOMES.labels(outcome).inc()
        else:
            OUTCOMES.labels("analyzed_only").inc()
        return response

    def _audit(self, request_id: str, masked: str, response: dict) -> None:
        resolution = response["resolution"]
        self._store.save_request(
            {
                "request_id": request_id,
                "complaint_masked": masked,
                "triage": response["triage"],
                "source_ids": [source["id"] for source in response["sources"]],
                "resolution": resolution,
                "grounded": resolution["grounded"] if resolution else None,
                "escalated": response["escalate"],
                "latency_ms": response["meta"]["latency_ms"],
                "llm_model": response["meta"]["model"],
                "prompt_version": response["meta"]["prompt_version"],
                "index_version": None,
            }
        )

    def feedback(self, request_id: str, helpful: bool, comment: str | None, edited: str | None) -> bool:
        return self._store.save_feedback(request_id, helpful, comment, edited)
