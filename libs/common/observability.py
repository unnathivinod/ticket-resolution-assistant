"""Shared logging, request IDs and metrics. Every service calls add_observability(app, name).

What a service gets from this one call:
  - JSON logs, one line per request, each carrying a request ID
  - the same request ID passed between services (header: X-Request-ID), so one
    customer request can be followed through the whole system
  - a /metrics page that Prometheus reads (request counts and latency)
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
import uuid
from contextvars import ContextVar

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

REQUEST_ID_HEADER = "x-request-id"
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_QUIET_PATHS = {"/health", "/ready", "/metrics"}  # too noisy to log or count

# Holds the current request's ID so any log line written during the request includes it.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

HTTP_REQUESTS = Counter(
    "http_requests_total", "Number of HTTP requests", ["service", "method", "path", "status"]
)
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds",
    "Time taken to answer an HTTP request",
    ["service", "method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)


class JsonFormatter(logging.Formatter):
    """Writes each log record as one line of JSON, which log tools can search and filter."""

    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "service": self.service,
            "request_id": request_id_var.get(),
            "message": record.getMessage(),
        }
        payload.update(getattr(record, "fields", {}))
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def setup_logging(service: str, level: str = "INFO") -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    return logging.getLogger(service)


def add_observability(app: FastAPI, service: str) -> logging.Logger:
    """Attach logging, request IDs and /metrics to a FastAPI app."""
    log = setup_logging(service)

    @app.middleware("http")
    async def observe(request: Request, call_next):
        # Reuse the caller's request ID if it looks safe, otherwise make a new one.
        incoming = request.headers.get(REQUEST_ID_HEADER, "")
        request_id = incoming if _SAFE_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers[REQUEST_ID_HEADER] = request_id
            return response
        finally:
            elapsed = time.perf_counter() - start
            route = request.scope.get("route")
            # Use the route pattern, not the raw URL, so metrics do not explode into many labels.
            path = getattr(route, "path", "unmatched")
            if path not in _QUIET_PATHS:
                HTTP_REQUESTS.labels(service, request.method, path, str(status)).inc()
                HTTP_LATENCY.labels(service, request.method, path).observe(elapsed)
                log.info(
                    "request",
                    extra={
                        "fields": {
                            "method": request.method,
                            "path": path,
                            "status": status,
                            "duration_ms": round(elapsed * 1000, 1),
                        }
                    },
                )
            request_id_var.reset(token)

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return log
