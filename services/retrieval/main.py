"""Retrieval service: finds the most similar past tickets and knowledge-base articles.

Endpoints
  POST /search   query -> ranked tickets and KB articles
  GET  /health   is the process alive?
  GET  /ready    can it reach Qdrant and the embedding service?
  GET  /metrics  numbers for Prometheus
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from qdrant_client import QdrantClient

from libs.common.embedding_client import EmbeddingClient, EmbeddingServiceError
from libs.common.observability import add_observability
from services.retrieval.config import Settings
from services.retrieval.search import Searcher, SearchRequest, SearchResponse

SERVICE_NAME = "retrieval"


def create_app(searcher: Searcher | None = None, settings: Settings | None = None) -> FastAPI:
    """Build the app. Tests pass in a Searcher wired to in-memory stand-ins."""
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.searcher is None:
            qdrant = QdrantClient(url=settings.qdrant_url, timeout=10)
            embedder = EmbeddingClient(settings.embedding_url, timeout=30)
            app.state.searcher = Searcher(qdrant, embedder, settings)
        yield

    app = FastAPI(title="Retrieval Service", version="0.1.0", lifespan=lifespan)
    app.state.searcher = searcher
    log = add_observability(app, SERVICE_NAME)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/ready")
    def ready(request: Request) -> dict:
        try:
            if request.app.state.searcher.ready():
                return {"status": "ready", "collection": settings.collection}
        except Exception as error:  # noqa: BLE001 - any failure means "not ready"
            log.warning("readiness check failed", extra={"fields": {"error": str(error)}})
        raise HTTPException(status_code=503, detail="Qdrant collection or embedding service is not available")

    @app.post("/search", response_model=SearchResponse)
    def search(body: SearchRequest, request: Request) -> SearchResponse:
        if not body.query.strip():
            raise HTTPException(status_code=422, detail="query is empty")
        if body.top_k_tickets + body.top_k_kb == 0:
            raise HTTPException(status_code=422, detail="Ask for at least one ticket or KB article")
        if max(body.top_k_tickets, body.top_k_kb) > settings.max_top_k:
            raise HTTPException(status_code=422, detail=f"top_k values may not exceed {settings.max_top_k}")
        try:
            response = request.app.state.searcher.search(body)
        except EmbeddingServiceError as error:
            log.error("embedding service unavailable", extra={"fields": {"error": str(error)}})
            raise HTTPException(status_code=503, detail="Embedding service is unavailable") from error
        except Exception as error:  # noqa: BLE001 - Qdrant client raises several error types
            log.exception("search failed")
            raise HTTPException(status_code=503, detail="Search backend is unavailable") from error
        log.info(
            "search",
            extra={
                "fields": {
                    "mode": response.mode,
                    "reranked": response.reranked,
                    "results": len(response.results),
                    "top_score": response.results[0].score if response.results else None,
                    "timings_ms": response.timings_ms,
                }
            },
        )
        return response

    return app


app = create_app()
