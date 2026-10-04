"""Generation service: drafts a step-by-step resolution with citations (the "G" in RAG).

Endpoints
  POST /generate   complaint + retrieved sources -> cited, checked resolution
  POST /reply      complaint + checked steps -> the message the agent sends to the customer
  GET  /health    is the process alive?
  GET  /ready      is the embedding service reachable? (also reports whether the models are available)
  GET  /metrics    numbers for Prometheus
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from libs.common.embedding_client import EmbeddingClient
from libs.common.llm_client import LLMClient
from libs.common.observability import add_observability
from libs.common.service_client import ServiceError
from services.generation.config import Settings
from services.generation.service import Generator

SERVICE_NAME = "generation"


class Source(BaseModel):
    id: str
    source_type: Literal["ticket", "kb"]
    title: str
    content: str = Field(min_length=1)


class Triage(BaseModel):
    category: str | None = None
    product: str | None = None
    severity: str | None = None
    sentiment: str | None = None


class GenerateRequest(BaseModel):
    complaint: str = Field(min_length=1)
    sources: list[Source] = Field(min_length=1, max_length=20)
    triage: Triage | None = None


class Step(BaseModel):
    n: int
    text: str
    citations: list[str] = Field(description="IDs of the sources that back this step")
    support: float = Field(description="How close the step is to its cited source, 0 to 1")
    verified: bool = Field(description="True when the cited source really supports the step")
    repeats_already_tried: bool = Field(description="True when the step repeats something the customer did")


class GenerateResponse(BaseModel):
    mode: Literal["llm", "extractive"] = Field(description="'extractive' = quoted from a source, no model")
    summary: str
    already_tried: list[str]
    steps: list[Step]
    escalate: bool
    escalation_reason: str
    grounded: bool = Field(description="True when every step is backed by its cited source")
    dropped_steps: int = Field(description="Steps removed because they cited no real source")
    fallback_reason: str | None
    sources_used: list[str]
    model: str | None = Field(description="The model that wrote the answer. None when it was quoted")
    failover_from: str | None = Field(
        default=None, description="The first-choice model, when it failed and the backup model answered"
    )
    prompt_version: str
    usage: dict[str, int]
    timings_ms: dict[str, float]


class ReplyRequest(BaseModel):
    complaint: str = Field(min_length=1)
    steps: list[str] = Field(default_factory=list, max_length=20, description="Checked steps only")
    already_tried: list[str] = Field(default_factory=list, max_length=20)
    sentiment: str | None = None
    severity: str | None = None
    escalated: bool = False


class ReplyResponse(BaseModel):
    reply: str = Field(description="The message for the customer, ready for the agent to edit")
    mode: Literal["llm", "template"] = Field(description="'template' = filled in without a model")
    model: str | None = Field(description="The model that wrote the reply. None for a template")
    failover_from: str | None = Field(
        default=None, description="The first-choice model, when it failed and the backup model answered"
    )
    fallback_reason: str | None
    escalated: bool = Field(description="True when the reply hands the case to the specialist team")
    prompt_version: str
    usage: dict[str, int]
    timings_ms: dict[str, float]


def create_app(generator: Generator | None = None, settings: Settings | None = None) -> FastAPI:
    """Build the app. Tests pass in a Generator wired to stand-ins."""
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.generator is None:
            llm, fallback_llm = None, None
            if settings.llm_enabled:
                llm = LLMClient(
                    settings.llm_base_url,
                    settings.llm_model,
                    settings.llm_api_key,
                    settings.llm_timeout_seconds,
                    reasoning_effort=settings.llm_reasoning_effort,
                    extra_tokens=settings.llm_extra_tokens,
                    rate_limit_wait=settings.llm_rate_limit_wait_seconds,
                )
                if settings.fallback_llm_base_url and settings.fallback_llm_model:
                    fallback_llm = LLMClient(
                        settings.fallback_llm_base_url,
                        settings.fallback_llm_model,
                        settings.fallback_llm_api_key,
                        settings.fallback_llm_timeout_seconds,
                    )
            app.state.generator = Generator(
                llm, EmbeddingClient(settings.embedding_url, timeout=30), settings, fallback_llm
            )
        yield

    app = FastAPI(title="Generation Service", version="0.1.0", lifespan=lifespan)
    app.state.generator = generator
    log = add_observability(app, SERVICE_NAME)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/ready")
    def ready(request: Request) -> dict:
        generator = request.app.state.generator
        if generator is None or not generator.embedder_ready():
            raise HTTPException(status_code=503, detail="Embedding service is not available")
        # The service still works without the model (it quotes the sources), so that is not a failure.
        return {
            "status": "ready",
            "llm_available": generator.llm_ready(),
            "llm_model": settings.llm_model,
            "fallback_llm_model": generator.fallback_model,
            "fallback_llm_available": generator.fallback_llm_ready(),
        }

    # Plain "def": the model call blocks for seconds, so FastAPI runs it in a worker thread.
    @app.post("/generate", response_model=GenerateResponse)
    def generate(body: GenerateRequest, request: Request) -> dict:
        if not body.complaint.strip():
            raise HTTPException(status_code=422, detail="complaint is empty")
        if len(body.complaint) > settings.max_complaint_chars:
            raise HTTPException(
                status_code=422, detail=f"complaint is longer than {settings.max_complaint_chars} characters"
            )
        try:
            result = request.app.state.generator.generate(
                body.complaint,
                [source.model_dump() for source in body.sources],
                body.triage.model_dump() if body.triage else None,
            )
        except ServiceError as error:
            log.error("dependency unavailable", extra={"fields": {"error": str(error)}})
            raise HTTPException(status_code=503, detail="The embedding service is unavailable") from error
        log.info(
            "generated",
            extra={
                "fields": {
                    "mode": result["mode"],
                    "model": result["model"],
                    "failover_from": result["failover_from"],
                    "steps": len(result["steps"]),
                    "grounded": result["grounded"],
                    "escalate": result["escalate"],
                    "fallback_reason": result["fallback_reason"],
                    "usage": result["usage"],
                    "timings_ms": result["timings_ms"],
                }
            },
        )
        return result

    @app.post("/reply", response_model=ReplyResponse)
    def reply(body: ReplyRequest, request: Request) -> dict:
        if not body.complaint.strip():
            raise HTTPException(status_code=422, detail="complaint is empty")
        if len(body.complaint) > settings.max_complaint_chars:
            raise HTTPException(
                status_code=422, detail=f"complaint is longer than {settings.max_complaint_chars} characters"
            )
        result = request.app.state.generator.draft_reply(
            body.complaint, body.steps, body.already_tried, body.sentiment, body.severity, body.escalated
        )
        log.info(
            "reply drafted",
            extra={
                "fields": {
                    "mode": result["mode"],
                    "model": result["model"],
                    "failover_from": result["failover_from"],
                    "steps": len(body.steps),
                    "escalated": result["escalated"],
                    "fallback_reason": result["fallback_reason"],
                    "usage": result["usage"],
                    "timings_ms": result["timings_ms"],
                }
            },
        )
        return result

    return app


app = create_app()
