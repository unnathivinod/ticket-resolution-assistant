"""Client that other services use to talk to the triage service."""

from __future__ import annotations

from libs.common.service_client import ServiceClient, ServiceError


class TriageServiceError(ServiceError):
    """The triage service could not be reached or kept failing."""


class TriageClient(ServiceClient):
    name = "triage"
    error_class = TriageServiceError

    def classify(self, complaint: str) -> dict:
        return self._post("/classify", {"complaint": complaint})
