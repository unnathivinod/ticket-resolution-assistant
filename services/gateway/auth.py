"""Signing in: checking a password, and the signed pass ("token") the page gets in return.

Two different questions are kept apart on purpose:
  the API key      answers "which application is calling?"   (the agent page, a script, an eval)
  the session token answers "which person is using it?"       (Priya, an agent)

Passwords are never stored. What is stored is a salted PBKDF2 hash, which cannot be turned back
into the password and is slow on purpose, so guessing many passwords takes a long time.

The token is a small piece of JSON (who, which role, until when) plus a signature made with a
secret only the gateway knows. Anyone can read the JSON, but nobody can change it or make a new
one without the secret. Nothing is stored for it, so any copy of the gateway can check it.

Plain functions with no database and no network, so they are easy to test.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

ROLES = ("agent", "expert", "engineer")
SEES_ALL_CASES = ("expert", "engineer")  # an agent sees only the cases they handled themselves
ALGORITHM = "pbkdf2_sha256"
ROUNDS = 200_000


def hash_password(password: str, salt: str | None = None, rounds: int = ROUNDS) -> str:
    """Turn a password into the text that is stored: algorithm$rounds$salt$hash."""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), rounds)
    return f"{ALGORITHM}${rounds}${salt}${digest.hex()}"


# Checked when the username does not exist, so "no such user" takes as long as "wrong password"
# and the response time does not reveal which usernames exist.
_NOBODY = hash_password("no-such-user", salt="00" * 16)


def verify_password(password: str, stored: str | None) -> bool:
    """True when the password matches the stored hash. Never raises on a damaged hash."""
    try:
        algorithm, rounds, salt, expected = (stored or _NOBODY).split("$")
        if algorithm != ALGORITHM:
            return False
        actual = hash_password(password, salt, int(rounds)).split("$")[3]
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected) and stored is not None


def _encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: str, secret: str) -> str:
    return _encode(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())


def issue_token(user: dict, secret: str, ttl_seconds: int, now: float | None = None) -> str:
    """The pass handed to the page after a successful sign-in."""
    claims = {
        "username": user["username"],
        "name": user["display_name"],
        "role": user["role"],
        "exp": int((now if now is not None else time.time()) + ttl_seconds),
    }
    payload = _encode(json.dumps(claims, separators=(",", ":")).encode())
    return f"{payload}.{_sign(payload, secret)}"


def read_token(token: str | None, secret: str, now: float | None = None) -> dict | None:
    """Who a token belongs to, or None when it is missing, changed, made with another secret or expired."""
    try:
        payload, signature = (token or "").split(".")
        if not hmac.compare_digest(signature, _sign(payload, secret)):
            return None
        claims = json.loads(_decode(payload))
        if claims["exp"] < (now if now is not None else time.time()) or claims["role"] not in ROLES:
            return None
        return {"username": claims["username"], "name": claims["name"], "role": claims["role"]}
    except (ValueError, KeyError, TypeError):
        return None
