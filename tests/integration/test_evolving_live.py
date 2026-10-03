"""Checks the path for new data against the REAL gateway, queue, worker and search index.

A test ticket about a made-up device is added, found by search, then retired again.
It always uses the same ID, so repeated runs do not pile up.

Run with:  docker compose run --rm tools pytest -m integration
"""

import os
import time

import httpx
import pytest

pytestmark = pytest.mark.integration

BASE_URL = os.environ.get("GATEWAY_URL", "http://gateway:8000")
AGENT = {"X-API-Key": os.environ.get("GATEWAY_API_KEY", "dev-local-key")}
ADMIN = {"X-API-Key": os.environ.get("GATEWAY_ADMIN_API_KEY", "dev-admin-key")}

TICKET_ID = "T-LIVE-TEST"
# Words that appear nowhere else in the data, so the keyword half of the search finds it for certain.
COMPLAINT = "My zorblatt quimby modem flashes turquoise and the broadband is dead."
TICKET = {
    "id": TICKET_ID,
    "subject": "Zorblatt quimby modem flashes turquoise",
    "description": COMPLAINT,
    "resolution_steps": ["Replace the zorblatt quimby modem.", "Confirm the turquoise light is gone."],
    "category": "device_hardware",
    "product": "broadband",
    "severity": "low",
}


@pytest.fixture(scope="module")
def http():
    with httpx.Client(base_url=BASE_URL, timeout=60) as client:
        try:
            client.get("/health").raise_for_status()
        except httpx.HTTPError:
            pytest.exit(f"The gateway is not reachable at {BASE_URL}. Run 'docker compose up -d'.", 1)
        yield client
        client.delete(f"/v1/documents/{TICKET_ID}", headers=ADMIN)  # leave nothing behind


def wait_until_indexed(http, seconds: int = 90) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        status = http.get(f"/v1/documents/{TICKET_ID}", headers=ADMIN).json()
        if status["index_up_to_date"]:
            return
        time.sleep(1)
    pytest.fail(
        "The worker did not index the ticket in time. Check 'docker compose ps' and "
        "'docker compose logs ingestion'."
    )


def found_ids(http) -> list[str]:
    body = {"complaint": COMPLAINT, "generate": False}
    response = http.post("/v1/resolve", json=body, headers=AGENT)
    assert response.status_code == 200, response.text
    return [source["id"] for source in response.json()["sources"]]


def test_an_agent_key_may_not_change_data(http):
    assert http.post("/v1/tickets", json=TICKET, headers=AGENT).status_code == 403
    assert http.post("/v1/tickets", json=TICKET).status_code == 401


def test_a_ticket_with_a_class_that_does_not_exist_is_refused(http):
    response = http.post("/v1/tickets", json={**TICKET, "category": "teleportation"}, headers=ADMIN)
    assert response.status_code == 422
    assert "unknown category 'teleportation'" in response.json()["detail"]


def test_a_new_ticket_becomes_searchable_and_a_retired_one_disappears(http):
    version_before = http.post(
        "/v1/resolve", json={"complaint": COMPLAINT, "generate": False}, headers=AGENT
    ).json()["meta"]["index_version"]

    added = http.post("/v1/tickets", json=TICKET, headers=ADMIN)
    assert added.status_code == 202, added.text
    wait_until_indexed(http)
    assert TICKET_ID in found_ids(http)

    version_after = http.post(
        "/v1/resolve", json={"complaint": COMPLAINT, "generate": False}, headers=AGENT
    ).json()["meta"]["index_version"]
    assert int(version_after) > int(version_before)  # answers cached before the change are now stale

    assert http.delete(f"/v1/documents/{TICKET_ID}", headers=ADMIN).status_code == 202
    wait_until_indexed(http)
    assert TICKET_ID not in found_ids(http)


def test_the_classes_can_be_read_by_agents(http):
    names = {item["name"] for item in http.get("/v1/taxonomy", headers=AGENT).json()["category"]}
    assert {"connectivity_intermittent", "billing_dispute"} <= names
