"""Fast tests for the retrieval service: real search code, in-memory Qdrant, stand-in embedder."""

import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient, models

from libs.common.embedding_client import EmbeddingServiceError
from services.ingestion.indexer import (
    COLLECTION_ALIAS,
    ensure_collection,
    index_chunks,
    kb_chunks,
    ticket_chunks,
)
from services.retrieval.config import Settings
from services.retrieval.main import create_app
from services.retrieval.search import Searcher
from tests.fakes import DIM, FakeEmbedder


def ticket(number: int, text: str, product: str, scenario: str) -> dict:
    return {
        "id": f"T-{number:06d}",
        "subject": f"Ticket about {scenario}",
        "description": text,
        "resolution_steps": ["Do the fix."],
        "category": "cat",
        "product": product,
        "severity": "medium",
        "sentiment": "neutral",
        "scenario_id": scenario,
        "created_at": "2026-01-01T00:00:00+00:00",
    }


def article(number: int, title: str, symptoms: str, scenario: str) -> dict:
    return {
        "id": f"KB-{number:03d}",
        "title": title,
        "body": f"# {title}\n\n## Symptoms\n{symptoms}\n\n## Resolution steps\n1. Follow the guide.\n",
        "category": "cat",
        "product": "broadband",
        "scenario_id": scenario,
        "version": 1,
        "updated_at": "2026-01-01T00:00:00+00:00",
    }


TICKETS = [
    ticket(1, "broadband drops every evening around eight", "broadband", "S01"),
    ticket(2, "internet drops every evening after dinner", "broadband", "S01"),
    ticket(3, "charged twice on my mobile bill this month", "mobile", "S19"),
    ticket(4, "set-top box shows error E-102 no signal", "tv", "S31"),
    ticket(5, "roaming not working abroad on holiday", "mobile", "S17"),
]
ARTICLES = [
    article(1, "Evening broadband drops", "broadband drops every evening at peak time", "S01"),
    article(31, "Set-top box error E-102", "error E-102 and no signal on all channels", "S31"),
]


@pytest.fixture
def setup():
    qdrant, embedder = QdrantClient(":memory:"), FakeEmbedder()
    ensure_collection(qdrant, DIM)
    chunks = [c for t in TICKETS for c in ticket_chunks(t)] + [c for a in ARTICLES for c in kb_chunks(a)]
    index_chunks(qdrant, embedder, chunks)
    settings = Settings(candidates=10, max_top_k=5)
    with TestClient(create_app(Searcher(qdrant, embedder, settings), settings)) as client:
        yield client, qdrant, embedder


def search(client, **body):
    response = client.post("/search", json=body)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("mode", ["hybrid", "dense", "sparse"])
def test_every_mode_finds_the_matching_ticket_and_article(setup, mode):
    client, _, _ = setup
    body = search(client, query="my broadband drops every evening", mode=mode)
    tickets = [r for r in body["results"] if r["source_type"] == "ticket"]
    articles = [r for r in body["results"] if r["source_type"] == "kb"]
    assert tickets[0]["scenario_id"] == "S01"
    assert articles[0]["id"] == "KB-001"
    assert body["mode"] == mode


def test_limits_are_applied_per_source_type(setup):
    client, _, _ = setup
    body = search(client, query="broadband drops every evening", top_k_tickets=2, top_k_kb=1)
    kinds = [r["source_type"] for r in body["results"]]
    assert kinds.count("ticket") == 2 and kinds.count("kb") == 1


def test_only_tickets_when_kb_limit_is_zero(setup):
    client, _, _ = setup
    body = search(client, query="error E-102", top_k_tickets=3, top_k_kb=0)
    assert {r["source_type"] for r in body["results"]} == {"ticket"}


def test_one_result_per_document_even_with_several_matching_chunks(setup):
    client, _, _ = setup
    body = search(client, query="Evening broadband drops guide symptoms", top_k_tickets=0, top_k_kb=5)
    ids = [r["id"] for r in body["results"]]
    assert len(ids) == len(set(ids))


def test_results_are_sorted_best_first_and_keep_content_for_the_llm(setup):
    client, _, _ = setup
    body = search(client, query="error E-102 no signal")
    scores = [r["score"] for r in body["results"]]
    assert scores == sorted(scores, reverse=True)
    assert body["results"][0]["scenario_id"] == "S31"
    assert "Resolution steps" in body["results"][0]["content"]


def test_rerank_can_be_switched_off(setup):
    client, _, embedder = setup
    with_rerank = search(client, query="broadband drops", rerank=True)
    calls = embedder.rerank_calls
    without = search(client, query="broadband drops", rerank=False)
    assert with_rerank["reranked"] is True and with_rerank["score_type"].startswith("reranker")
    assert without["reranked"] is False and without["score_type"] == "rrf"
    assert embedder.rerank_calls == calls  # no extra rerank call was made


def test_product_hint_boosts_matching_results(setup):
    client, _, _ = setup
    options = {"query": "not working this month", "top_k_tickets": 5, "top_k_kb": 0, "rerank": True}
    plain = search(client, **options)
    hinted = search(client, **options, product_hint="mobile")
    plain_scores = {r["id"]: r["score"] for r in plain["results"]}
    for result in hinted["results"]:
        expected_bonus = 0.05 if result["product"] == "mobile" else 0.0
        assert result["score"] == pytest.approx(
            min(1.0, plain_scores[result["id"]] + expected_bonus), abs=1e-3
        )


def test_defaults_follow_the_eval_results(setup):
    """Hybrid search with the meaning side weighted 3x and no reranker (see evals/results/)."""
    client, _, embedder = setup
    calls = embedder.rerank_calls
    body = search(client, query="my broadband drops every evening")
    assert body["mode"] == "hybrid" and body["reranked"] is False
    assert embedder.rerank_calls == calls


def test_similarity_is_reported_and_comparable(setup):
    client, _, _ = setup
    close = search(client, query="broadband drops every evening around eight", top_k_tickets=1, top_k_kb=0)
    far = search(client, query="zebra giraffe elephant", top_k_tickets=1, top_k_kb=0)
    assert close["results"][0]["similarity"] > 0.9
    assert far["results"][0]["similarity"] < close["results"][0]["similarity"]
    assert all(-1.0 <= r["similarity"] <= 1.0001 for r in close["results"] + far["results"])


def test_dense_weight_is_accepted_for_hybrid_search(setup):
    client, _, _ = setup
    body = search(client, query="my broadband drops every evening", rerank=False, dense_weight=3.0)
    assert body["results"][0]["scenario_id"] == "S01"
    assert client.post("/search", json={"query": "ok", "dense_weight": 0}).status_code == 422


def test_inactive_documents_are_never_returned(setup):
    client, qdrant, _ = setup
    qdrant.set_payload(
        COLLECTION_ALIAS,
        payload={"is_active": False},
        points=models.Filter(
            must=[models.FieldCondition(key="doc_id", match=models.MatchValue(value="T-000004"))]
        ),
    )
    body = search(client, query="set-top box error E-102 no signal", top_k_tickets=5, top_k_kb=0)
    assert "T-000004" not in [r["id"] for r in body["results"]]


def test_timings_are_reported_for_each_stage(setup):
    client, _, _ = setup
    body = search(client, query="broadband drops", rerank=True)
    assert {"embed", "search", "rerank", "total"} <= set(body["timings_ms"])


@pytest.mark.parametrize(
    "payload",
    [
        {"query": ""},
        {"query": "   "},
        {"query": "ok", "top_k_tickets": 0, "top_k_kb": 0},
        {"query": "ok", "top_k_tickets": 6},  # above max_top_k=5
        {"query": "ok", "top_k_kb": -1},
        {"query": "ok", "mode": "magic"},
    ],
)
def test_bad_requests_are_rejected(setup, payload):
    client, _, _ = setup
    assert client.post("/search", json=payload).status_code == 422


def test_ready_reflects_dependencies(setup):
    client, _, embedder = setup
    assert client.get("/ready").status_code == 200
    embedder.is_ready = False
    assert client.get("/ready").status_code == 503


def test_embedding_outage_gives_a_clean_503(setup):
    client, _, embedder = setup

    def broken(*args, **kwargs):
        raise EmbeddingServiceError("down")

    embedder.embed = broken
    response = client.post("/search", json={"query": "broadband drops"})
    assert response.status_code == 503
    assert response.json()["detail"] == "Embedding service is unavailable"


def test_search_metrics_are_exposed(setup):
    client, _, _ = setup
    search(client, query="broadband drops", rerank=True)
    metrics = client.get("/metrics").text
    assert 'retrieval_stage_seconds_count{stage="rerank"}' in metrics
    assert "retrieval_top_similarity_bucket" in metrics
