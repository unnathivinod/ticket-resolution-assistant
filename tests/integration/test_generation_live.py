"""Checks against the REAL generation service.

It passes whether or not a language model is running: without one the service quotes
the sources, and that fallback is part of what is being checked.

Run with:  docker compose run --rm tools pytest -m integration
"""

import os
import time

import httpx
import pytest

pytestmark = pytest.mark.integration

BASE_URL = os.environ.get("GENERATION_URL", "http://generation:8003")
SOURCES = [
    {
        "id": "KB-001",
        "source_type": "kb",
        "title": "Evening broadband drops caused by peak-hour congestion",
        "content": "## Resolution steps\n1. Run a remote line test at peak time.\n"
        "2. Change the router Wi-Fi channel to a less crowded one.\n",
    }
]


@pytest.fixture(scope="module")
def http():
    with httpx.Client(base_url=BASE_URL, timeout=300) as client:
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
                f"The generation service is not ready at {BASE_URL}. Run 'docker compose up -d' "
                "and check 'docker compose logs generation'.",
                returncode=1,
            )
        yield client


def test_an_answer_always_comes_back_with_real_citations(http):
    complaint = "My broadband drops every evening and I already restarted the router."
    response = http.post("/generate", json={"complaint": complaint, "sources": SOURCES})
    body = response.json()
    assert response.status_code == 200, response.text
    assert body["mode"] in {"llm", "extractive"}
    assert body["steps"] or body["escalate"]
    for step in body["steps"]:
        assert step["citations"] and set(step["citations"]) <= {"KB-001"}
