"""Who is signed in on this page, and which menu items their role gets.

The page only decides what is worth SHOWING. What data a person may actually get is decided by
the gateway, from the signed token it issued at sign-in (services/gateway/auth.py). Hiding a
menu item here is a convenience, never the protection.
"""

from __future__ import annotations

import streamlit as st

from ui.api import GatewayError, call_gateway
from ui.roles import ROLE_NAMES, role_may

__all__ = ["ROLE_NAMES", "may", "sign_in", "sign_out", "user"]


def user() -> dict | None:
    """The signed-in person: {"username", "name", "role", "token"}. None before sign-in."""
    return st.session_state.get("user")


def may(what: str) -> bool:
    person = user()
    return bool(person) and role_may(person["role"], what)


def sign_in(username: str, password: str) -> str | None:
    """Ask the gateway to check the password. Returns None on success, or the message to show."""
    try:
        body = call_gateway("/v1/login", {"username": username, "password": password}, timeout=20)
    except GatewayError as error:
        return error.detail if error.status == 401 else str(error)
    st.session_state["user"] = {**body["user"], "token": body["token"]}
    return None


def sign_out() -> None:
    """Forget the person and everything they had on the page."""
    st.session_state.clear()
