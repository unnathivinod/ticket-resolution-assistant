"""Checks against the REAL triage service. Label quality is measured by evals/eval_triage.py.

Needs:     docker compose up -d      and the data loaded with scripts/seed.py
Run with:  docker compose run --rm tools pytest -m integration
"""

import json
import os
import time

import httpx
import pytest

pytestmark = pytest.mark.integration

BASE_URL = os.environ.get("TRIAGE_URL", "http://triage:8001")
SEVERITIES = {"low", "medium", "high", "critical"}
SENTIMENTS = {"negative", "neutral", "positive"}


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
                f"The triage service is not ready at {BASE_URL}. Run 'docker compose up -d' "
                "and check 'docker compose logs triage'.",
                returncode=1,
            )
        yield client


def test_the_example_complaint_from_the_brief(http):
    complaint = (
        "My broadband drops every evening around 8 and I've already restarted the router twice, "
        "I work from home and this is costing me. Call me on 07700 900123."
    )
    response = http.post("/classify", json={"complaint": complaint})
    body = response.json()
    assert response.status_code == 200, response.text
    assert body["product"]["best_guess"] == "broadband"
    assert body["category"]["best_guess"].startswith("connectivity")
    assert body["severity"]["label"] in SEVERITIES and body["sentiment"]["label"] in SENTIMENTS
    assert 0 <= body["category"]["confidence"] <= 1
    assert body["neighbours_used"] > 0
    assert "07700" not in json.dumps(body)


def test_an_off_topic_question_is_less_similar_than_a_real_complaint(http):
    real = http.post("/classify", json={"complaint": "my internet connection keeps dropping"}).json()
    off_topic = http.post("/classify", json={"complaint": "how long should I boil an egg"}).json()
    assert off_topic["top_similarity"] < real["top_similarity"]
