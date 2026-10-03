"""Client that other services use to talk to the retrieval service."""

from __future__ import annotations

from libs.common.service_client import ServiceClient, ServiceError


class RetrievalServiceError(ServiceError):
    """The retrieval service could not be reached or kept failing."""


class RetrievalClient(ServiceClient):
    name = "retrieval"
    error_class = RetrievalServiceError

    def search(self, query: str, top_k_tickets: int = 3, top_k_kb: int = 2, **options) -> dict:
        """Return the retrieval service's response: {"results": [...], "timings_ms": {...}, ...}."""
        payload = {"query": query, "top_k_tickets": top_k_tickets, "top_k_kb": top_k_kb, **options}
        return self._post("/search", payload)
