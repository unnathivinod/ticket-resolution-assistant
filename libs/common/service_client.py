"""Base class for calling another service over HTTP.

It puts three production habits in one place: timeouts, retries with a growing wait,
and passing the request ID along so one request can be traced across services.
"""

from __future__ import annotations

import time

import httpx

from libs.common.observability import REQUEST_ID_HEADER, request_id_var


class ServiceError(RuntimeError):
    """Another service could not be reached, kept failing, or rejected the request."""


class ServiceClient:
    name = "service"  # used in error messages
    error_class: type[ServiceError] = ServiceError

    def __init__(self, base_url: str, timeout: float = 30.0, retries: int = 3) -> None:
        self._http = httpx.Client(base_url=base_url, timeout=timeout)
        self._retries = retries

    def _headers(self) -> dict[str, str]:
        request_id = request_id_var.get()
        return {REQUEST_ID_HEADER: request_id} if request_id != "-" else {}

    def _post(self, path: str, payload: dict) -> dict:
        last_error: Exception | None = None
        for attempt in range(self._retries):
            try:
                response = self._http.post(path, json=payload, headers=self._headers())
                if response.status_code < 500:
                    response.raise_for_status()  # a 4xx is our mistake: retrying will not help
                    return response.json()
                last_error = self.error_class(f"{path} returned {response.status_code}")
            except httpx.HTTPStatusError as error:
                raise self.error_class(
                    f"The {self.name} service rejected {path}: {error.response.text}"
                ) from error
            except httpx.TransportError as error:  # connection refused, timeout, ...
                last_error = error
            time.sleep(0.3 * 2**attempt)  # wait 0.3s, 0.6s, 1.2s ... before trying again
        raise self.error_class(f"The {self.name} service failed after {self._retries} tries") from last_error

    def ready(self) -> bool:
        try:
            return self._http.get("/ready", timeout=3).status_code == 200
        except httpx.HTTPError:
            return False

    def close(self) -> None:
        self._http.close()
