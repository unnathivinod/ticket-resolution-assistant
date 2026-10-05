"""Endpoints for people: signing in, the list of cases, and how each case ended.

  POST /v1/login                        username and password -> a session token
  GET  /v1/cases                        the complaints the signed-in person may see
  POST /v1/cases/{request_id}/decision  record that a case was resolved or escalated

Who sees what is decided here, in the gateway, and not in the page:
  agent                only the cases they handled themselves
  expert, engineer     every case, with the name of the person who handled it
An agent who asks for someone else's case gets "not found", the same answer as for a case that
does not exist, so the reply does not even reveal that the case is there.
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from prometheus_client import Counter
from pydantic import BaseModel, Field

from services.gateway.auth import SEES_ALL_CASES, issue_token, verify_password
from services.gateway.config import Settings

LOGINS = Counter("gateway_logins_total", "Sign-in attempts, by result", ["result"])
# followed = the person did what the assistant suggested. A falling share means falling trust.
DECISIONS = Counter(
    "gateway_case_decisions_total", "How cases ended, as recorded by people", ["decision", "followed"]
)
for _result in ("ok", "failed"):
    LOGINS.labels(_result)
for _decision in ("resolved", "escalated"):
    for _followed in ("true", "false"):
        DECISIONS.labels(_decision, _followed)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=60)
    password: str = Field(min_length=1, max_length=200)


class DecisionRequest(BaseModel):
    decision: Literal["resolved", "escalated"]


def add_case_routes(app: FastAPI, any_key, signed_in, settings: Settings, log) -> None:
    """Attach the endpoints. `any_key` checks the API key, `signed_in` returns the signed-in person."""

    def database(call, *args, **options):
        """Run a database call. If the database is down, say so clearly instead of crashing."""
        try:
            return call(*args, **options)
        except HTTPException:
            raise
        except Exception as error:  # noqa: BLE001
            log.error("database unavailable", extra={"fields": {"error": str(error)}})
            raise HTTPException(status_code=503, detail="The database is unavailable. Try again.") from error

    @app.post("/v1/login", tags=["people"])
    def login(body: LoginRequest, request: Request, caller: str = Depends(any_key)) -> dict:
        username = body.username.strip().lower()
        user = database(request.app.state.store.get_user, username)
        # The password is checked even when the name is unknown, so both cases take the same time
        # and get the same answer. Otherwise the reply would reveal which usernames exist.
        if not verify_password(body.password, user["password_hash"] if user else None):
            LOGINS.labels("failed").inc()
            log.warning("sign-in failed", extra={"fields": {"username": username, "caller": caller}})
            raise HTTPException(status_code=401, detail="Wrong username or password.")
        LOGINS.labels("ok").inc()
        log.info("signed in", extra={"fields": {"username": username, "role": user["role"]}})
        seconds = settings.session_minutes * 60
        return {
            "token": issue_token(user, settings.token_secret, seconds),
            "expires_in": seconds,
            "user": {"username": user["username"], "name": user["display_name"], "role": user["role"]},
        }

    @app.get("/v1/cases", tags=["people"])
    def cases(
        request: Request,
        status: Literal["all", "open", "resolved", "escalated"] = "all",
        hours: int = Query(24, ge=1, le=24 * 90, description="How far back to look"),
        limit: int = Query(100, ge=1, le=500),
        caller: str = Depends(any_key),
        user: dict = Depends(signed_in),
    ) -> dict:
        sees_all = user["role"] in SEES_ALL_CASES
        found = database(
            request.app.state.store.list_cases,
            None if sees_all else user["username"],
            hours=hours,
            status=status,
            limit=limit,
        )
        totals = found["totals"]
        decided = totals["resolved"] + totals["escalated"]
        # Share of decided cases in which the person did what the assistant suggested.
        totals["followed_rate"] = round(totals.pop("followed") / decided, 3) if decided else None
        return {"scope": "all" if sees_all else "mine", "hours": hours, **found}

    @app.post("/v1/cases/{request_id}/decision", tags=["people"])
    def decide(
        request_id: str,
        body: DecisionRequest,
        request: Request,
        caller: str = Depends(any_key),
        user: dict = Depends(signed_in),
    ) -> dict:
        try:
            uuid.UUID(request_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="request_id is not a valid ID") from None
        saved = database(
            request.app.state.store.decide_case,
            request_id,
            body.decision,
            user["username"],
            user["role"] in SEES_ALL_CASES,
        )
        if saved is None:
            raise HTTPException(status_code=404, detail="No such case.")
        DECISIONS.labels(body.decision, str(bool(saved["followed"])).lower()).inc()
        log.info(
            "case decided",
            extra={
                "fields": {
                    "resolve_id": request_id,
                    "decision": body.decision,
                    "decided_by": user["username"],
                    "followed": bool(saved["followed"]),
                }
            },
        )
        return {**saved, "decided_by_name": user["name"]}
