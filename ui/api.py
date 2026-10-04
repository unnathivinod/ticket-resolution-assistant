"""Everything the pages ask the gateway for. The pages hold no business logic of their own."""

from __future__ import annotations

import os

import httpx
import streamlit as st

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://gateway:8000")
API_KEY = os.environ.get("GATEWAY_API_KEY", "dev-local-key")
# Only set for people who may add knowledge (second-line support). Without it that page is read-only.
ADMIN_KEY = os.environ.get("GATEWAY_ADMIN_API_KEY", "")
# Opened by the browser, not by this container, so it is the address on the user's machine.
GRAFANA_URL = os.environ.get("GRAFANA_PUBLIC_URL", "http://localhost:3000")


class GatewayError(RuntimeError):
    pass


def _send(method: str, path: str, payload: dict | None, timeout: float, key: str) -> dict:
    try:
        response = httpx.request(
            method, f"{GATEWAY_URL}{path}", json=payload, headers={"X-API-Key": key}, timeout=timeout
        )
    except httpx.HTTPError as error:
        raise GatewayError(f"The assistant could not be reached ({error.__class__.__name__}).") from error
    if response.status_code == 429:
        raise GatewayError("Too many requests. Please wait a moment and try again.")
    if response.status_code not in (200, 202):
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text or response.reason_phrase
        raise GatewayError(f"The assistant returned an error: {detail}")
    return response.json()


def call_gateway(path: str, payload: dict, timeout: float = 300, key: str = API_KEY) -> dict:
    return _send("POST", path, payload, timeout, key)


def read_gateway(path: str, timeout: float = 15, key: str = API_KEY) -> dict:
    return _send("GET", path, None, timeout, key)


@st.cache_data(ttl=300)
def taxonomy() -> dict[str, list[str]]:
    """The ticket classes in use, read from the gateway so new ones appear without a code change."""
    try:
        body = read_gateway("/v1/taxonomy", timeout=10)
        return {kind: [item["name"] for item in body[kind]] for kind in ("category", "product")}
    except (GatewayError, KeyError):
        return {"category": [], "product": []}


@st.cache_data(ttl=20)
def service_status() -> tuple[str, str]:
    """A short health line for the side menu: (ok | warn | bad, text)."""
    try:
        response = httpx.get(f"{GATEWAY_URL}/ready", timeout=5)
        body = response.json()
    except (httpx.HTTPError, ValueError):
        return "bad", "The assistant cannot be reached"
    checks = body.get("checks") or (body.get("detail") or {}).get("checks") or {}
    down = sorted(name for name, healthy in checks.items() if not healthy)
    if down:
        return "warn", "Not available: " + ", ".join(down)
    return ("ok", "All services connected") if response.status_code == 200 else ("warn", "Starting up")
