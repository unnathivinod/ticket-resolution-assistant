"""Checks against the REAL embedding service with the real models.

Needs the service running:   docker compose up -d embedding
Run with:                    docker compose run --rm tools pytest -m integration
"""

import math
import os
import time

import httpx
import pytest

pytestmark = pytest.mark.integration

BASE_URL = os.environ.get("EMBEDDING_URL", "http://embedding:8004")


def wait_until_ready(client: httpx.Client, seconds: int = 60) -> None:
    """Give the service time to load its models. Stop with one clear message if it never answers."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if client.get("/ready").status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(2)
    pytest.exit(
        f"The embedding service did not answer at {BASE_URL} within {seconds} seconds. "
        "Start it with 'docker compose up -d' and look at 'docker compose logs embedding'.",
        returncode=1,
    )


@pytest.fixture(scope="module")
def http():
    with httpx.Client(base_url=BASE_URL, timeout=60) as client:
        wait_until_ready(client)
        yield client


def cosine(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True))  # vectors are already length 1


def test_service_is_ready(http):
    body = http.get("/ready").json()
    assert body["status"] == "ready"
    assert body["dim"] == 384


def test_similar_meaning_scores_higher_than_different_meaning(http):
    """The whole point of semantic search: different words, same meaning, close vectors."""
    texts = [
        "My wifi keeps dying at night",
        "Broadband drops every evening",
        "I was charged twice this month",
    ]
    wifi, broadband, billing = http.post("/embed", json={"texts": texts}).json()["dense"]
    assert cosine(wifi, broadband) > cosine(wifi, billing) + 0.1


def test_dense_vectors_have_length_one(http):
    vector = http.post("/embed", json={"texts": ["hello there"]}).json()["dense"][0]
    assert len(vector) == 384
    assert math.isclose(math.sqrt(sum(x * x for x in vector)), 1.0, abs_tol=1e-3)


def test_sparse_vectors_match_on_shared_words(http):
    """Keyword vectors: a query and a document that share a word share a word id."""
    document = http.post("/embed", json={"texts": ["Error E-102 on the set-top box"]}).json()["sparse"][0]
    query = http.post("/embed", json={"texts": ["what is error E-102"], "kind": "query"}).json()["sparse"][0]
    unrelated = http.post("/embed", json={"texts": ["roaming abroad"], "kind": "query"}).json()["sparse"][0]
    assert set(document["indices"]) & set(query["indices"])
    assert not set(document["indices"]) & set(unrelated["indices"])


def test_reranker_prefers_the_relevant_document(http):
    payload = {
        "query": "internet keeps disconnecting in the evening",
        "documents": [
            "How to pay your bill by direct debit.",
            "Evening broadband drops are usually caused by peak-hour congestion at the exchange.",
        ],
    }
    scores = http.post("/rerank", json=payload).json()["scores"]
    assert scores[1] > scores[0]
