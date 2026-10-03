"""Embedding service: the one place in the system that turns text into numbers.

Endpoints
  POST /embed    texts -> dense vectors (meaning) and sparse vectors (keywords)
  POST /rerank   query + documents -> a relevance score for each document
  GET  /health   is the process alive?
  GET  /ready    are the models loaded?
  GET  /metrics  numbers for Prometheus
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from prometheus_client import Histogram
from pydantic import BaseModel, Field

from libs.common.observability import add_observability
from services.embedding.config import Settings

SERVICE_NAME = "embedding"

MODEL_LATENCY = Histogram(
    "embedding_model_seconds",
    "Time spent inside a model",
    ["operation"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
TEXTS_PER_REQUEST = Histogram(
    "embedding_texts_per_request",
    "How many texts arrive in one request",
    ["operation"],
    buckets=(1, 2, 5, 10, 25, 50, 100, 256),
)


# ---- Request and response shapes (FastAPI checks these automatically) ---------------------


class EmbedRequest(BaseModel):
    texts: list[str] = Field(min_length=1, description="The texts to embed")
    kind: Literal["document", "query"] = Field(
        "document", description="'document' for things we store, 'query' for a search"
    )
    sparse: bool = Field(True, description="Also return keyword (BM25) vectors")


class SparseVector(BaseModel):
    indices: list[int]
    values: list[float]


class EmbedResponse(BaseModel):
    model: str
    dim: int
    dense: list[list[float]]
    sparse: list[SparseVector] | None = None


class RerankRequest(BaseModel):
    query: str = Field(min_length=1)
    documents: list[str] = Field(min_length=1)


class RerankResponse(BaseModel):
    model: str
    scores: list[float] = Field(description="One score per document, same order. Higher is better.")


# ---- The app ---------------------------------------------------------------------------------


def create_app(models=None, settings: Settings | None = None) -> FastAPI:
    """Build the app. Tests pass in small stand-in models; in production the real ones are loaded."""
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.models is None:
            from services.embedding.models import EmbeddingModels

            log.info("loading models")
            app.state.models = EmbeddingModels(settings)
            log.info("models loaded", extra={"fields": {"dense_dim": app.state.models.dense_dim}})
        yield

    app = FastAPI(title="Embedding Service", version="0.1.0", lifespan=lifespan)
    app.state.models = models
    log = add_observability(app, SERVICE_NAME)

    def loaded_models(request: Request):
        if request.app.state.models is None:
            raise HTTPException(status_code=503, detail="Models are still loading")
        return request.app.state.models

    def check_texts(texts: list[str], limit: int, what: str) -> None:
        if len(texts) > limit:
            raise HTTPException(status_code=422, detail=f"Too many {what}: {len(texts)} (limit {limit})")
        for position, text in enumerate(texts):
            if not text.strip():
                raise HTTPException(status_code=422, detail=f"{what} item {position} is empty")
            if len(text) > settings.max_chars_per_text:
                raise HTTPException(
                    status_code=422,
                    detail=f"{what} item {position} is longer than {settings.max_chars_per_text} characters",
                )

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/ready")
    def ready(request: Request) -> dict:
        models = loaded_models(request)
        return {
            "status": "ready",
            "dense_model": models.dense_name,
            "dim": models.dense_dim,
            "reranker_model": models.reranker_name,
        }

    # Plain "def" (not "async def") on purpose: the model work is heavy CPU work, and FastAPI
    # runs plain functions in a worker thread so the server can keep accepting other requests.
    @app.post("/embed", response_model=EmbedResponse)
    def embed(body: EmbedRequest, request: Request) -> EmbedResponse:
        models = loaded_models(request)
        check_texts(body.texts, settings.max_texts_per_request, "texts")
        TEXTS_PER_REQUEST.labels("embed").observe(len(body.texts))

        start = time.perf_counter()
        dense = models.embed_dense(body.texts)
        MODEL_LATENCY.labels("dense").observe(time.perf_counter() - start)

        sparse = None
        if body.sparse:
            start = time.perf_counter()
            sparse = models.embed_sparse(body.texts, body.kind)
            MODEL_LATENCY.labels("sparse").observe(time.perf_counter() - start)

        return EmbedResponse(model=models.dense_name, dim=models.dense_dim, dense=dense, sparse=sparse)

    @app.post("/rerank", response_model=RerankResponse)
    def rerank(body: RerankRequest, request: Request) -> RerankResponse:
        models = loaded_models(request)
        check_texts([body.query], 1, "query")
        check_texts(body.documents, settings.max_rerank_documents, "documents")
        TEXTS_PER_REQUEST.labels("rerank").observe(len(body.documents))

        start = time.perf_counter()
        scores = models.rerank(body.query, body.documents)
        MODEL_LATENCY.labels("rerank").observe(time.perf_counter() - start)
        return RerankResponse(model=models.reranker_name, scores=scores)

    return app


app = create_app()
