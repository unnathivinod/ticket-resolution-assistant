"""Checks against the REAL gateway, with every service, Redis and PostgreSQL behind it.

These do not wait for the language model: they use the quick path (labels + sources)
and questions that are stopped before the model is asked.

Run with:  docker compose run --rm tools pytest -m integration
"""

import os
import time
import uuid

import httpx
import pytest

pytestmark = pytest.mark.integration

BASE_URL = os.environ.get("GATEWAY_URL", "http://gateway:8000")
KEY = {"X-API-Key": os.environ.get("GATEWAY_API_KEY", "dev-local-key")}
COMPLAINT = (
    "My broadband drops every evening around 8 and I've already restarted the router twice, "
    "I work from home and this is costing me"
)


@pytest.fixture(scope="module")
def http():
    with httpx.Client(base_url=BASE_URL, timeout=60) as client:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                if client.get("/ready").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(2)
        else:
            pytest.exit(
                f"The gateway is not ready at {BASE_URL}. Run 'docker compose up -d' "
                "and check 'docker compose logs gateway'.",
                returncode=1,
            )
        yield client


def test_every_dependency_is_reachable(http):
    checks = http.get("/ready").json()["checks"]
    assert checks == {"triage": True, "retrieval": True, "generation": True, "database": True}


def test_a_request_without_a_key_is_refused(http):
    assert http.post("/v1/resolve", json={"complaint": COMPLAINT}).status_code == 401
    wrong = http.post("/v1/resolve", json={"complaint": COMPLAINT}, headers={"X-API-Key": "wrong"})
    assert wrong.status_code == 401


def test_the_quick_path_returns_labels_and_sources(http):
    response = http.post("/v1/resolve", json={"complaint": COMPLAINT, "generate": False}, headers=KEY)
    body = response.json()
    assert response.status_code == 200, response.text
    assert body["triage"]["product"]["label"] == "broadband"
    assert body["triage"]["category"]["best_guess"].startswith("connectivity")
    assert {source["source_type"] for source in body["sources"]} == {"ticket", "kb"}
    assert body["meta"]["confident_match"] is True
    assert body["resolution"] is None  # drafting was skipped on purpose


def test_an_off_topic_question_is_escalated_and_feedback_is_saved(http):
    # Nothing in the knowledge base is close to this, so the model is never asked.
    question = {"complaint": "What is the best recipe for a chocolate cake?"}
    response = http.post("/v1/resolve", json=question, headers=KEY)
    body = response.json()
    assert response.status_code == 200, response.text
    assert body["resolution"] is None
    assert body["escalate"] is True
    assert body["meta"]["confident_match"] is False

    # The request was written to the audit log, so feedback can be attached to it.
    feedback = {"request_id": body["request_id"], "helpful": False, "comment": "integration test"}
    assert http.post("/v1/feedback", json=feedback, headers=KEY).json() == {"status": "saved"}


def test_feedback_for_an_unknown_request_is_refused(http):
    feedback = {"request_id": str(uuid.uuid4()), "helpful": True}
    assert http.post("/v1/feedback", json=feedback, headers=KEY).status_code == 404


def test_a_reply_can_be_drafted_for_an_escalated_complaint(http):
    question = {"complaint": "What is the best recipe for a chocolate cake?"}
    request_id = http.post("/v1/resolve", json=question, headers=KEY).json()["request_id"]

    # One short model call (or the template if no model answers), so allow a slow local model.
    response = http.post("/v1/reply", json={"request_id": request_id}, headers=KEY, timeout=180)
    body = response.json()
    assert response.status_code == 200, response.text
    assert body["mode"] in ("llm", "template")
    assert body["escalated"] is True and body["steps_used"] == 0  # no fix exists, so none is offered
    assert body["reply"].startswith("Hello") and "[Agent name]" in body["reply"]

    unknown = http.post("/v1/reply", json={"request_id": str(uuid.uuid4())}, headers=KEY)
    assert unknown.status_code == 404


def test_every_complaint_is_checked_for_a_possible_incident(http):
    # The same words sent twice are one customer, so the count must not grow.
    body = {
        "complaint": f"My set-top box is stuck on the start screen, reference {uuid.uuid4()}",
        "generate": False,
    }
    first = http.post("/v1/resolve", json=body, headers=KEY).json()["incident"]
    second = http.post("/v1/resolve", json=body, headers=KEY).json()["incident"]
    assert first is not None and {"detected", "similar_recent", "needed", "window_minutes"} <= set(first)
    assert first["similar_recent"] >= 1 and second["similar_recent"] == first["similar_recent"]

    opted_out = http.post("/v1/resolve", json={**body, "track_incident": False}, headers=KEY).json()
    assert opted_out["incident"] is None
