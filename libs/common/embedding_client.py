"""Client that other services use to talk to the embedding service."""

from __future__ import annotations

from libs.common.service_client import ServiceClient, ServiceError


class EmbeddingServiceError(ServiceError):
    """The embedding service could not be reached or kept failing."""


class EmbeddingClient(ServiceClient):
    name = "embedding"
    error_class = EmbeddingServiceError

    def __init__(self, base_url: str, timeout: float = 30.0, retries: int = 3, batch_size: int = 64) -> None:
        super().__init__(base_url, timeout, retries)
        self._batch_size = batch_size

    def embed(self, texts: list[str], kind: str = "document") -> dict:
        """Return {"model", "dim", "dense": [...], "sparse": [...]} for any number of texts."""
        dense: list = []
        sparse: list = []
        info: dict = {}
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            info = self._post("/embed", {"texts": batch, "kind": kind, "sparse": True})
            dense.extend(info["dense"])
            sparse.extend(info["sparse"])
        return {"model": info.get("model"), "dim": info.get("dim"), "dense": dense, "sparse": sparse}

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        return self._post("/rerank", {"query": query, "documents": documents})["scores"]
