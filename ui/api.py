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
SIGN_IN_PATH = "/v1/login"


class GatewayError(RuntimeError):
    """The gateway could not be reached or refused. `detail` is its own message, `status` its code."""

    def __init__(self, message: str, detail: str = "", status: int | None = None) -> None:
        super().__init__(message)
        self.detail, self.status = detail or message, status


def _send(
    method: str, path: str, payload: dict | None, timeout: float, key: str, as_person: bool = True
) -> dict:
    # Two things travel with a request: the key says "the agent page is calling",
    # the token says which signed-in person is using it.
    headers = {"X-API-Key": key}
    token = (st.session_state.get("user") or {}).get("token") if as_person else None
    if token:
        headers["X-User-Token"] = token
    try:
        response = httpx.request(
            method, f"{GATEWAY_URL}{path}", json=payload, headers=headers, timeout=timeout
        )
    except httpx.HTTPError as error:
        raise GatewayError(f"The assistant could not be reached ({error.__class__.__name__}).") from error
    if response.status_code == 429:
        raise GatewayError("Too many requests. Please wait a moment and try again.", status=429)
    if response.status_code == 401 and token and path != SIGN_IN_PATH:
        # The session ended (a working day has passed). Back to the sign-in page, with a reason.
        st.session_state.clear()
        st.session_state["signin_note"] = "Your session has ended. Please sign in again."
        st.rerun()
    if response.status_code not in (200, 202):
        try:
            detail = str(response.json().get("detail", response.text))
        except ValueError:
            detail = response.text or response.reason_phrase
        raise GatewayError(f"The assistant returned an error: {detail}", detail, response.status_code)
    return response.json()


def call_gateway(path: str, payload: dict, timeout: float = 300, key: str = API_KEY) -> dict:
    return _send("POST", path, payload, timeout, key)


def read_gateway(path: str, timeout: float = 15, key: str = API_KEY, as_person: bool = True) -> dict:
    return _send("GET", path, None, timeout, key, as_person)


def record_decision(request_id: str, decision: str) -> str:
    """Tell the gateway how a case ended ("resolved" or "escalated") and remember it for the page."""
    saved = call_gateway(f"/v1/cases/{request_id}/decision", {"decision": decision}, timeout=30)
    st.session_state.setdefault("decisions", {})[request_id] = saved["decision"]
    return saved["decision"]


@st.cache_data(ttl=300)
def taxonomy() -> dict[str, list[str]]:
    """The ticket classes in use, read from the gateway so new ones appear without a code change."""
    try:
        body = read_gateway("/v1/taxonomy", timeout=10, as_person=False)  # the same for everyone
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
