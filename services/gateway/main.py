"""API gateway: the single front door of the system.

Endpoints
  POST /v1/resolve    complaint -> labels, sources and a cited resolution   (needs X-API-Key)
  POST /v1/feedback   thumbs up/down on an answer                           (needs X-API-Key)
  GET  /health        is the process alive?
  GET  /ready         which dependencies are reachable?
  GET  /metrics       numbers for Prometheus
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from contextlib import asynccontextmanager

import redis
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from prometheus_client import Counter
from pydantic import BaseModel, Field

from libs.common.generation_client import GenerationClient
from libs.common.observability import add_observability
from libs.common.retrieval_client import RetrievalClient
from libs.common.triage_client import TriageClient
from services.gateway.config import Settings
from services.gateway.infra import PostgresStore, RedisCache, RedisRateLimiter
from services.gateway.orchestrator import Orchestrator, SearchUnavailableError

SERVICE_NAME = "gateway"

REJECTED = Counter("gateway_rejected_total", "Requests refused before any work was done", ["reason"])
FEEDBACK = Counter("gateway_feedback_total", "Feedback received from agents", ["helpful"])


class ResolveRequest(BaseModel):
    complaint: str = Field(min_length=1, description="The customer's complaint, as written")
    generate: bool = Field(True, description="False = only labels and sources, skip the slow drafting step")


class FeedbackRequest(BaseModel):
    request_id: str
    helpful: bool
    comment: str | None = Field(None, max_length=2000)
    edited_resolution: str | None = Field(None, max_length=8000)


def create_app(
    orchestrator: Orchestrator | None = None, limiter=None, settings: Settings | None = None
) -> FastAPI:
    """Build the app. Tests pass in an Orchestrator and a limiter wired to in-memory stand-ins."""
    settings = settings or Settings()
    api_keys = settings.key_set()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.orchestrator is None:
            client = redis.Redis.from_url(settings.redis_url, socket_timeout=2, socket_connect_timeout=2)
            app.state.limiter = RedisRateLimiter(client, settings.rate_limit_per_minute)
            app.state.orchestrator = Orchestrator(
                TriageClient(settings.triage_url, timeout=30),
                RetrievalClient(settings.retrieval_url, timeout=30),
                GenerationClient(settings.generation_url, timeout=settings.generation_timeout_seconds),
                RedisCache(client, settings.cache_ttl_seconds),
                PostgresStore(settings.database_url),
                settings,
            )
        yield

    app = FastAPI(title="Support Ticket Resolution Assistant", version="0.1.0", lifespan=lifespan)
    app.state.orchestrator = orchestrator
    app.state.limiter = limiter
    log = add_observability(app, SERVICE_NAME)

    def authorised(request: Request, x_api_key: str | None = Header(None)) -> str:
        """Check the API key, then the rate limit. Returns a short fingerprint of the key for logs."""
        if x_api_key is None or not any(hmac.compare_digest(x_api_key, key) for key in api_keys):
            REJECTED.labels("bad_api_key").inc()
            raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key header")
        caller = hashlib.sha256(x_api_key.encode()).hexdigest()[:12]  # never log the key itself
        allowed, retry_after = request.app.state.limiter.allow(caller)
        if not allowed:
            REJECTED.labels("rate_limited").inc()
            raise HTTPException(
                status_code=429,
                detail="Too many requests. Try again shortly.",
                headers={"Retry-After": str(retry_after)},
            )
        return caller

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/ready")
    def ready(request: Request) -> dict:
        checks = request.app.state.orchestrator.readiness() if request.app.state.orchestrator else {}
        # Retrieval is the one dependency the gateway cannot work without.
        if not checks.get("retrieval"):
            raise HTTPException(status_code=503, detail={"status": "not ready", "checks": checks})
        return {"status": "ready", "checks": checks}

    @app.post("/v1/resolve")
    def resolve(body: ResolveRequest, request: Request, caller: str = Depends(authorised)) -> dict:
        if not body.complaint.strip():
            raise HTTPException(status_code=422, detail="complaint is empty")
        if len(body.complaint) > settings.max_complaint_chars:
            raise HTTPException(
                status_code=422, detail=f"complaint is longer than {settings.max_complaint_chars} characters"
            )
        try:
            response = request.app.state.orchestrator.resolve(body.complaint, body.generate)
        except SearchUnavailableError as error:
            log.error("search unavailable", extra={"fields": {"error": str(error)}})
            raise HTTPException(status_code=503, detail="Search is unavailable. Please try again.") from error
        log.info(
            "resolved",
            extra={
                "fields": {
                    "resolve_id": response["request_id"],
                    "caller": caller,
                    "cached": response["meta"]["cached"],
                    "escalate": response["escalate"],
                    "degraded": response["meta"]["degraded"],
                    "top_similarity": response["meta"]["top_similarity"],
                    "latency_ms": response["meta"]["latency_ms"],
                }
            },
        )
        return response

    @app.post("/v1/feedback")
    def feedback(body: FeedbackRequest, request: Request, caller: str = Depends(authorised)) -> dict:
        try:
            uuid.UUID(body.request_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="request_id is not a valid ID") from None
        try:
            saved = request.app.state.orchestrator.feedback(
                body.request_id, body.helpful, body.comment, body.edited_resolution
            )
        except Exception as error:  # noqa: BLE001 - the database is down: say so instead of crashing
            log.error("feedback not saved", extra={"fields": {"error": str(error), "caller": caller}})
            raise HTTPException(status_code=503, detail="Feedback could not be saved. Try again.") from error
        if not saved:
            raise HTTPException(status_code=404, detail="Unknown request_id")
        FEEDBACK.labels(str(body.helpful).lower()).inc()
        return {"status": "saved"}

    return app


app = create_app()
