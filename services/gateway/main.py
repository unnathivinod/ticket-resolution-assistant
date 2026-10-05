"""API gateway: the single front door of the system.

Endpoints
  POST /v1/resolve    complaint -> labels, sources and a cited resolution   (needs X-API-Key)
  POST /v1/reply      request ID of an earlier answer -> a message for the customer, ready to edit
  POST /v1/feedback   thumbs up/down, and the right category if ours was wrong
  GET  /health       is the process alive?
  GET  /ready         which dependencies are reachable?
  GET  /metrics       numbers for Prometheus

The endpoints for adding tickets, articles and ticket classes are in data_api.py.
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
from services.gateway.data_api import add_data_routes
from services.gateway.infra import NONE_OF_THESE, IngestQueue, PostgresStore, RedisCache, RedisRateLimiter
from services.gateway.orchestrator import Orchestrator, SearchUnavailableError

SERVICE_NAME = "gateway"

REJECTED = Counter("gateway_rejected_total", "Requests refused before any work was done", ["reason"])
FEEDBACK = Counter("gateway_feedback_total", "Feedback received from agents", ["helpful"])
# Corrections divided by feedback is a live estimate of how often triage picks the wrong category.
CORRECTIONS = Counter("gateway_category_corrections_total", "Categories corrected by agents", ["kind"])
# Start every known label at 0, so dashboards show a zero instead of nothing and the
# first event is counted. (Prometheus cannot see a rise from "does not exist" to 1.)
for _reason in ("bad_api_key", "not_admin", "rate_limited"):
    REJECTED.labels(_reason)
for _helpful in ("true", "false"):
    FEEDBACK.labels(_helpful)
for _kind in ("relabelled", NONE_OF_THESE):
    CORRECTIONS.labels(_kind)


class ResolveRequest(BaseModel):
    complaint: str = Field(min_length=1, description="The customer's complaint, as written")
    generate: bool = Field(True, description="False = only labels and sources, skip the slow drafting step")
    use_cache: bool = Field(True, description="False = always draft a fresh answer (used by the evals)")
    track_incident: bool = Field(
        True, description="False = do not count this complaint towards incident detection (evals, tests)"
    )


class ReplyRequest(BaseModel):
    request_id: str = Field(description="The request_id returned by /v1/resolve")


class FeedbackRequest(BaseModel):
    request_id: str
    helpful: bool
    comment: str | None = Field(None, max_length=2000)
    edited_resolution: str | None = Field(None, max_length=8000)
    correct_category: str | None = Field(
        None,
        max_length=60,
        description=f"The right category if ours was wrong, or '{NONE_OF_THESE}' if no category fits",
    )


def create_app(
    orchestrator: Orchestrator | None = None, limiter=None, settings: Settings | None = None, queue=None
) -> FastAPI:
    """Build the app. Tests pass in an Orchestrator, a limiter and a queue wired to in-memory stand-ins."""
    settings = settings or Settings()
    api_keys = settings.key_set()
    admin_keys = settings.admin_key_set()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.orchestrator is None:
            client = redis.Redis.from_url(settings.redis_url, socket_timeout=2, socket_connect_timeout=2)
            store = PostgresStore(settings.database_url)
            app.state.limiter = RedisRateLimiter(client, settings.rate_limit_per_minute)
            app.state.queue = IngestQueue(client, settings.ingest_stream)
            app.state.store = store
            app.state.orchestrator = Orchestrator(
                TriageClient(settings.triage_url, timeout=30),
                RetrievalClient(settings.retrieval_url, timeout=30),
                GenerationClient(settings.generation_url, timeout=settings.generation_timeout_seconds),
                RedisCache(client, settings.cache_ttl_seconds),
                store,
                settings,
            )
            yield
            store.close()  # give the database connections back on shutdown
        else:
            yield

    app = FastAPI(title="Support Ticket Resolution Assistant", version="0.1.0", lifespan=lifespan)
    app.state.orchestrator = orchestrator
    app.state.limiter = limiter
    app.state.queue = queue
    app.state.store = orchestrator.store if orchestrator else None
    log = add_observability(app, SERVICE_NAME)

    def matches(candidate: str | None, keys: set[str]) -> bool:
        # compare_digest takes the same time whether the key is nearly right or completely wrong
        return candidate is not None and any(hmac.compare_digest(candidate, key) for key in keys)

    def check(request: Request, x_api_key: str | None, admin_only: bool) -> str:
        """Check the API key, then the rate limit. Returns a short fingerprint of the key for logs."""
        if not matches(x_api_key, api_keys):
            REJECTED.labels("bad_api_key").inc()
            raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key header")
        is_admin = matches(x_api_key, admin_keys)
        if admin_only and not is_admin:
            REJECTED.labels("not_admin").inc()
            raise HTTPException(status_code=403, detail="This API key may not change data")
        caller = hashlib.sha256(x_api_key.encode()).hexdigest()[:12]  # never log the key itself
        limit = settings.admin_rate_limit_per_minute if is_admin else settings.rate_limit_per_minute
        allowed, retry_after = request.app.state.limiter.allow(caller, limit)
        if not allowed:
            REJECTED.labels("rate_limited").inc()
            raise HTTPException(
                status_code=429,
                detail="Too many requests. Try again shortly.",
                headers={"Retry-After": str(retry_after)},
            )
        return caller

    def authorised(request: Request, x_api_key: str | None = Header(None)) -> str:
        return check(request, x_api_key, admin_only=False)

    def admin(request: Request, x_api_key: str | None = Header(None)) -> str:
        return check(request, x_api_key, admin_only=True)

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
            response = request.app.state.orchestrator.resolve(
                body.complaint, body.generate, body.use_cache, body.track_incident
            )
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
                    "similar_recent": (response.get("incident") or {}).get("similar_recent"),
                    "incident": (response.get("incident") or {}).get("detected", False),
                    "latency_ms": response["meta"]["latency_ms"],
                }
            },
        )
        return response

    @app.post("/v1/reply")
    def reply(body: ReplyRequest, request: Request, caller: str = Depends(authorised)) -> dict:
        try:
            uuid.UUID(body.request_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="request_id is not a valid ID") from None
        try:
            response = request.app.state.orchestrator.reply(body.request_id)
        except Exception as error:  # noqa: BLE001 - the database is down: say so instead of crashing
            log.error("reply not drafted", extra={"fields": {"error": str(error), "caller": caller}})
            raise HTTPException(
                status_code=503, detail="The earlier answer could not be read. Try again."
            ) from error
        if response is None:
            raise HTTPException(status_code=404, detail="Unknown request_id")
        log.info(
            "reply drafted",
            extra={
                "fields": {
                    "resolve_id": body.request_id,
                    "caller": caller,
                    "mode": response["mode"],
                    "model": response["model"],
                    "steps_used": response["steps_used"],
                    "steps_left_out": response["steps_left_out"],
                    "escalated": response["escalated"],
                    "degraded": response["degraded"],
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
        orchestrator = request.app.state.orchestrator
        try:
            if body.correct_category not in (None, NONE_OF_THESE):
                known = {item["name"] for item in orchestrator.store.taxonomy()["category"]}
                if body.correct_category not in known:
                    raise HTTPException(
                        status_code=422,
                        detail=f"correct_category must be an existing category or '{NONE_OF_THESE}'",
                    )
            saved = orchestrator.feedback(
                body.request_id, body.helpful, body.comment, body.edited_resolution, body.correct_category
            )
        except HTTPException:
            raise
        except Exception as error:  # noqa: BLE001 - the database is down: say so instead of crashing
            log.error("feedback not saved", extra={"fields": {"error": str(error), "caller": caller}})
            raise HTTPException(status_code=503, detail="Feedback could not be saved. Try again.") from error
        if not saved:
            raise HTTPException(status_code=404, detail="Unknown request_id")
        FEEDBACK.labels(str(body.helpful).lower()).inc()
        if body.correct_category:
            kind = NONE_OF_THESE if body.correct_category == NONE_OF_THESE else "relabelled"
            CORRECTIONS.labels(kind).inc()
        return {"status": "saved"}

    add_data_routes(app, any_key=authorised, admin_key=admin, log=log)
    return app


app = create_app()
