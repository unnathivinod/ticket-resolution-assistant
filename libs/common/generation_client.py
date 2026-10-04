"""Client that other services use to talk to the generation service."""

from __future__ import annotations

from libs.common.service_client import ServiceClient, ServiceError


class GenerationServiceError(ServiceError):
    """The generation service could not be reached or kept failing."""


class GenerationClient(ServiceClient):
    name = "generation"
    error_class = GenerationServiceError

    def __init__(self, base_url: str, timeout: float = 240.0) -> None:
        # One attempt only: a model call can take a minute, so retrying would double the wait.
        super().__init__(base_url, timeout, retries=1)

    def generate(self, complaint: str, sources: list[dict], triage: dict | None = None) -> dict:
        return self._post("/generate", {"complaint": complaint, "sources": sources, "triage": triage})

    def reply(
        self,
        complaint: str,
        steps: list[str],
        already_tried: list[str] | None = None,
        sentiment: str | None = None,
        severity: str | None = None,
        escalated: bool = False,
    ) -> dict:
        """The message for the customer, written from steps that were already checked."""
        return self._post(
            "/reply",
            {
                "complaint": complaint,
                "steps": steps,
                "already_tried": already_tried or [],
                "sentiment": sentiment,
                "severity": severity,
                "escalated": escalated,
            },
        )
