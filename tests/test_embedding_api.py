"""Fast tests for the embedding service API, using tiny stand-in models (no downloads needed).

They check the service's own logic: input limits, response shapes, request IDs and metrics.
The real models are checked separately in tests/integration/.
"""

import pytest
from fastapi.testclient import TestClient

from services.embedding.config import Settings
from services.embedding.main import create_app


class FakeModels:
    """Stands in for the real models. Returns predictable numbers instantly."""

    dense_name = "fake-dense"
    reranker_name = "fake-reranker"
    dense_dim = 3

    def __init__(self):
        self.sparse_kinds = []

    def embed_dense(self, texts):
        return [[float(len(t)), 0.0, 1.0] for t in texts]

    def embed_sparse(self, texts, kind):
        self.sparse_kinds.append(kind)
        return [{"indices": [1, 2], "values": [1.0, 0.5]} for _ in texts]

    def rerank(self, query, documents):
        return [1.0 if query.lower() in d.lower() else -1.0 for d in documents]


@pytest.fixture
def fake_models():
    return FakeModels()


@pytest.fixture
def client(fake_models):
    settings = Settings(max_texts_per_request=3, max_chars_per_text=50, max_rerank_documents=2)
    with TestClient(create_app(models=fake_models, settings=settings)) as test_client:
        yield test_client


def test_health_and_ready(client):
    assert client.get("/health").json() == {"status": "ok"}
    ready = client.get("/ready")
    assert ready.status_code == 200
    assert ready.json()["dim"] == 3


def test_ready_is_503_before_models_load():
    app = create_app(models=None)
    # No "with" block, so startup (which loads models) does not run: this mimics a starting service.
    response = TestClient(app).get("/ready")
    assert response.status_code == 503


def test_embed_returns_one_vector_per_text(client):
    response = client.post("/embed", json={"texts": ["router is down", "bill too high"]})
    body = response.json()
    assert response.status_code == 200
    assert body["dim"] == 3
    assert len(body["dense"]) == 2
    assert len(body["sparse"]) == 2
    assert body["sparse"][0] == {"indices": [1, 2], "values": [1.0, 0.5]}


def test_embed_can_skip_sparse(client):
    body = client.post("/embed", json={"texts": ["hello"], "sparse": False}).json()
    assert body["sparse"] is None


def test_embed_passes_kind_to_the_sparse_model(client, fake_models):
    client.post("/embed", json={"texts": ["hello"], "kind": "query"})
    client.post("/embed", json={"texts": ["hello"]})
    assert fake_models.sparse_kinds == ["query", "document"]


@pytest.mark.parametrize(
    "payload",
    [
        {"texts": []},  # nothing to embed
        {"texts": ["a", "b", "c", "d"]},  # more than the limit of 3
        {"texts": ["x" * 51]},  # longer than the limit of 50 characters
        {"texts": ["   "]},  # blank
        {"texts": ["ok"], "kind": "something-else"},  # unknown kind
    ],
)
def test_embed_rejects_bad_input(client, payload):
    assert client.post("/embed", json=payload).status_code == 422


def test_rerank_returns_one_score_per_document(client):
    response = client.post("/rerank", json={"query": "router", "documents": ["Router reboot steps", "Bill"]})
    assert response.status_code == 200
    assert response.json()["scores"] == [1.0, -1.0]


def test_rerank_rejects_too_many_documents(client):
    response = client.post("/rerank", json={"query": "router", "documents": ["a", "b", "c"]})
    assert response.status_code == 422


def test_request_id_is_passed_through(client):
    response = client.post("/embed", json={"texts": ["hello"]}, headers={"X-Request-ID": "abc-123"})
    assert response.headers["x-request-id"] == "abc-123"


def test_unsafe_request_id_is_replaced(client):
    response = client.get("/health", headers={"X-Request-ID": "bad id with spaces\n"})
    assert response.headers["x-request-id"] != "bad id with spaces\n"
    assert len(response.headers["x-request-id"]) == 32


def test_metrics_page_counts_requests(client):
    client.post("/embed", json={"texts": ["hello"]})
    metrics = client.get("/metrics").text
    assert 'http_requests_total{method="POST",path="/embed",service="embedding",status="200"}' in metrics
    assert "embedding_model_seconds" in metrics
