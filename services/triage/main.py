"""Triage service: reads a complaint and labels it.

Endpoints
  POST /classify   complaint -> category, product, severity, sentiment (with confidence and reasons)
  GET  /health     is the process alive?
  GET  /ready      can it reach the retrieval and embedding services?
  GET  /metrics    numbers for Prometheus
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from prometheus_client import Counter, Histogram
from pydantic import BaseModel, Field

from libs.common.embedding_client import EmbeddingClient
from libs.common.observability import add_observability
from libs.common.retrieval_client import RetrievalClient
from libs.common.service_client import ServiceError
from services.triage.config import Settings
from services.triage.logic import UNKNOWN, Params, load_signals
from services.triage.service import Triage

SERVICE_NAME = "triage"

LABELS = Counter("triage_labels_total", "Labels given, per field", ["field", "label"])
CONFIDENCE = Histogram(
    "triage_confidence",
    "Share of the neighbour vote won by the chosen label",
    ["field"],
    buckets=(0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)
SIGNALS = Counter("triage_signals_total", "Urgency and tone signals detected", ["signal"])
# A rising unknown rate is the alarm that customers are reporting a problem type we have no class for.
UNKNOWN_TOTAL = Counter("triage_unknown_total", "Complaints that could not be labelled", ["field"])


class ClassifyRequest(BaseModel):
    complaint: str = Field(min_length=1)


class VotedLabel(BaseModel):
    label: str = Field(description="The chosen label, or 'unknown' when the system is not sure")
    confidence: float = Field(description="Share of the neighbour vote won by the best guess, 0 to 1")
    best_guess: str | None = Field(description="The top label even when it was not confident enough to use")


class Severity(BaseModel):
    label: str
    baseline: str = Field(description="Severity of similar past tickets before urgency signals")
    reasons: list[str] = Field(description="Urgency signals that moved the severity")


class Sentiment(BaseModel):
    label: str
    reasons: list[str]


class SignalHit(BaseModel):
    name: str
    similarity: float
    matched_text: str


class ClassifyResponse(BaseModel):
    category: VotedLabel
    product: VotedLabel
    severity: Severity
    sentiment: Sentiment
    signals: list[SignalHit]
    neighbours_used: int
    top_similarity: float
    needs_review: bool = Field(description="True when the category is unknown: a human should label it")
    timings_ms: dict[str, float]


def create_app(triage: Triage | None = None, settings: Settings | None = None) -> FastAPI:
    """Build the app. Tests pass in a Triage wired to stand-ins."""
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.triage is None:
            app.state.triage = Triage(
                RetrievalClient(settings.retrieval_url, timeout=30),
                EmbeddingClient(settings.embedding_url, timeout=30),
                load_signals(settings.signals_path),
                Params.from_file(settings.params_path),
                settings.max_neighbours,
            )
        yield

    app = FastAPI(title="Triage Service", version="0.1.0", lifespan=lifespan)
    app.state.triage = triage
    log = add_observability(app, SERVICE_NAME)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/ready")
    def ready(request: Request) -> dict:
        triage = request.app.state.triage
        if triage is not None and triage.ready():
            return {"status": "ready", "params": triage.params.to_dict()}
        raise HTTPException(status_code=503, detail="Retrieval or embedding service is not available")

    @app.post("/classify", response_model=ClassifyResponse)
    def classify(body: ClassifyRequest, request: Request) -> dict:
        if not body.complaint.strip():
            raise HTTPException(status_code=422, detail="complaint is empty")
        if len(body.complaint) > settings.max_complaint_chars:
            raise HTTPException(
                status_code=422, detail=f"complaint is longer than {settings.max_complaint_chars} characters"
            )
        try:
            result = request.app.state.triage.classify(body.complaint)
        except ServiceError as error:
            log.error("dependency unavailable", extra={"fields": {"error": str(error)}})
            raise HTTPException(
                status_code=503, detail="A service that triage depends on is unavailable"
            ) from error

        for name in ("category", "product"):
            LABELS.labels(name, result[name]["label"]).inc()
            CONFIDENCE.labels(name).observe(result[name]["confidence"])
            if result[name]["label"] == UNKNOWN:
                UNKNOWN_TOTAL.labels(name).inc()
        LABELS.labels("severity", result["severity"]["label"]).inc()
        LABELS.labels("sentiment", result["sentiment"]["label"]).inc()
        for hit in result["signals"]:
            SIGNALS.labels(hit["name"]).inc()
        log.info(
            "classified",
            extra={
                "fields": {
                    "category": result["category"]["label"],
                    "product": result["product"]["label"],
                    "severity": result["severity"]["label"],
                    "sentiment": result["sentiment"]["label"],
                    "top_similarity": result["top_similarity"],
                    "timings_ms": result["timings_ms"],
                }
            },
        )
        return result

    return app


app = create_app()
