"""Checks against the REAL retrieval service, with real models and the loaded dataset.

Needs:     docker compose up -d      and      docker compose run --rm tools python scripts/seed.py
Run with:  docker compose run --rm tools pytest -m integration
"""

import os
import re
import time

import httpx
import pytest

pytestmark = pytest.mark.integration

BASE_URL = os.environ.get("RETRIEVAL_URL", "http://retrieval:8002")


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
                f"The retrieval service is not ready at {BASE_URL}. Run 'docker compose up -d', then "
                "'docker compose run --rm tools python scripts/seed.py', "
                "and check 'docker compose logs retrieval'.",
                returncode=1,
            )
        yield client


def search(http, query, **options):
    response = http.post("/search", json={"query": query, **options})
    assert response.status_code == 200, response.text
    return response.json()["results"]


def test_the_example_complaint_from_the_brief(http):
    complaint = (
        "My broadband drops every evening around 8 and I've already restarted the router twice, "
        "I work from home and this is costing me"
    )
    results = search(http, complaint)
    tickets = [r for r in results if r["source_type"] == "ticket"]
    articles = [r for r in results if r["source_type"] == "kb"]
    assert tickets[0]["scenario_id"] == "S01"
    assert articles[0]["id"] == "KB-001"
    assert "Resolution steps" in tickets[0]["content"]


def test_results_report_a_similarity_for_the_no_match_check(http):
    """Search quality itself is measured by evals/eval_retrieval.py. Here we only check the behaviour."""
    on_topic = search(http, "my internet connection keeps dropping", top_k_tickets=1, top_k_kb=0)
    off_topic = search(http, "how long should I boil an egg", top_k_tickets=1, top_k_kb=0)
    assert on_topic[0]["similarity"] > off_topic[0]["similarity"]


def test_exact_codes_are_found(http):
    results = search(http, "what does E-102 mean on my tv box", top_k_tickets=3, top_k_kb=1)
    assert results[0]["scenario_id"] == "S31"


def test_no_personal_details_are_stored_in_the_index(http):
    results = search(
        http, "my account number is 48213377 call me on 07700 900123", top_k_tickets=10, top_k_kb=0
    )
    for result in results:
        assert not re.search(r"\d{6,}", result["text"]), result["text"]
        assert "@example.com" not in result["text"]
