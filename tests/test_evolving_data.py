"""Fast tests for new data: the gateway's data endpoints and the ingestion worker.

Everything runs in memory: fakeredis for the queue, an in-memory Qdrant for the index,
and tests/fakes.py for PostgreSQL and the embedding model.
"""

import fakeredis
import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient

from services.gateway.config import Settings as GatewaySettings
from services.gateway.infra import IngestQueue
from services.gateway.main import create_app
from services.gateway.orchestrator import Orchestrator
from services.ingestion.config import Settings as WorkerSettings
from services.ingestion.indexer import COLLECTION_ALIAS
from services.ingestion.worker import INDEX_VERSION_KEY, Worker
from services.retrieval.config import Settings as RetrievalSettings
from services.retrieval.search import Searcher, SearchRequest
from tests.fakes import FakeEmbedder, InMemoryCache, InMemoryStore

AGENT = {"X-API-Key": "agent-key"}
ADMIN = {"X-API-Key": "admin-key"}

TICKET = {
    "subject": "eSIM QR code will not scan",
    "description": "My phone refuses to scan the eSIM QR code and says the profile cannot be added.",
    "resolution_steps": ["Check the eSIM profile status.", "Issue a new QR code."],
    "category": "connectivity_intermittent",
    "product": "mobile",
    "severity": "medium",
}
ARTICLE = {
    "title": "Voicemail greeting cannot be recorded",
    "body": "# Voicemail greeting\n\n## Symptoms\nThe voicemail greeting recording fails.\n\n"
    "## Resolution steps\n1. Reset the voicemail mailbox.\n2. Ask the customer to record again.\n",
    "product": "mobile",
}


class AllowAll:
    def __init__(self):
        self.limits = []

    def allow(self, identity, limit=None):
        self.limits.append(limit)
        return True, 1


class FlakyEmbedder(FakeEmbedder):
    """Fails a set number of times, then works. Stands in for an embedding service that is down."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    def embed(self, texts, kind="document"):
        if kind == "document" and self.failures > 0:
            self.failures -= 1
            raise RuntimeError("embedding service is down")
        return super().embed(texts, kind)


@pytest.fixture
def system():
    """The gateway, the queue, the worker and the search index, all wired together in memory."""
    redis_client = fakeredis.FakeRedis(decode_responses=True)
    store, embedder, qdrant = InMemoryStore(), FakeEmbedder(), QdrantClient(":memory:")
    limiter = AllowAll()
    gateway_settings = GatewaySettings(api_keys="agent-key", admin_api_keys="admin-key")
    orchestrator = Orchestrator(None, None, None, InMemoryCache(), store, gateway_settings)
    app = create_app(orchestrator, limiter, gateway_settings, IngestQueue(redis_client))

    worker_settings = WorkerSettings(retry_after_ms=0, max_attempts=3)
    worker = Worker(redis_client, store, qdrant, embedder, worker_settings, consumer="test-worker")
    worker.setup()
    searcher = Searcher(qdrant, embedder, RetrievalSettings())

    def search(query: str) -> list[str]:
        request = SearchRequest(query=query, top_k_tickets=5, top_k_kb=5)
        return [result.id for result in searcher.search(request).results]

    with TestClient(app) as client:
        yield {
            "client": client,
            "store": store,
            "redis": redis_client,
            "qdrant": qdrant,
            "worker": worker,
            "search": search,
            "limiter": limiter,
            "settings": worker_settings,
        }


def add_ticket(system, **changes) -> str:
    response = system["client"].post("/v1/tickets", json={**TICKET, **changes}, headers=ADMIN)
    assert response.status_code == 202, response.text
    return response.json()["id"]


def waiting(system) -> int:
    settings = system["settings"]
    return system["redis"].xpending(settings.stream, settings.group)["pending"]


# ---- who may change data ----------------------------------------------------------------------


def test_only_admin_keys_may_change_data(system):
    client = system["client"]
    assert client.post("/v1/tickets", json=TICKET).status_code == 401
    refused = client.post("/v1/tickets", json=TICKET, headers=AGENT)
    assert refused.status_code == 403
    assert client.post("/v1/tickets", json=TICKET, headers=ADMIN).status_code == 202
    assert system["store"].documents["ticket"], "the ticket should be saved"


def test_admin_keys_get_a_higher_rate_limit_for_bulk_loads(system):
    system["client"].get("/v1/taxonomy", headers=AGENT)
    system["client"].get("/v1/taxonomy", headers=ADMIN)
    assert system["limiter"].limits == [30, 600]


def test_anyone_with_a_key_can_read_the_classes(system):
    body = system["client"].get("/v1/taxonomy", headers=AGENT).json()
    assert {"name": "broadband", "description": "Home broadband"} in body["product"]


# ---- a new ticket becomes searchable ------------------------------------------------------------


def test_a_new_ticket_is_searchable_after_the_worker_runs(system):
    ticket_id = add_ticket(system)
    assert ticket_id.startswith("T-")
    assert system["search"]("eSIM QR code will not scan") == []  # saved, but not indexed yet
    status = system["client"].get(f"/v1/documents/{ticket_id}", headers=ADMIN).json()
    assert status["index_up_to_date"] is False
    assert system["client"].get("/v1/ingest/status", headers=ADMIN).json() == {"documents_waiting": 1}

    assert system["worker"].run_once(block=False) == 1

    assert system["search"]("eSIM QR code will not scan") == [ticket_id]
    status = system["client"].get(f"/v1/documents/{ticket_id}", headers=ADMIN).json()
    assert status["index_up_to_date"] is True
    assert waiting(system) == 0  # the event was acknowledged
    assert system["redis"].get(INDEX_VERSION_KEY) == "1"  # cached answers are now stale


def test_recorded_fixes_can_be_listed_with_their_search_status(system):
    client = system["client"]
    assert client.get("/v1/tickets/recorded", headers=ADMIN).json() == {"items": []}
    recorded = add_ticket(system)
    add_ticket(system, id="T-000999", scenario_id="S01")  # part of a dataset: has an answer key
    listed = client.get("/v1/tickets/recorded", headers=ADMIN).json()["items"]
    assert [row["id"] for row in listed] == [recorded]
    assert listed[0]["subject"] == TICKET["subject"] and listed[0]["steps"] == 2
    assert listed[0]["searchable"] is False and listed[0]["is_active"] is True

    system["worker"].run_once(block=False)
    assert client.get("/v1/tickets/recorded", headers=ADMIN).json()["items"][0]["searchable"] is True
    client.delete(f"/v1/documents/{recorded}", headers=ADMIN)
    assert client.get("/v1/tickets/recorded", headers=ADMIN).json()["items"][0]["is_active"] is False
    assert client.get("/v1/tickets/recorded", headers=AGENT).status_code == 403  # agents cannot see it


def test_saving_the_same_ticket_again_updates_it_instead_of_duplicating(system):
    ticket_id = add_ticket(system, id="T-EXT-42")
    add_ticket(system, id="T-EXT-42", subject="eSIM QR code expired")
    system["worker"].run_once(block=False)
    assert ticket_id == "T-EXT-42"
    assert system["qdrant"].count(COLLECTION_ALIAS, exact=True).count == 1
    point = system["qdrant"].scroll(COLLECTION_ALIAS, limit=1)[0][0]
    assert point.payload["title"] == "eSIM QR code expired"


def test_personal_details_never_reach_the_index(system):
    add_ticket(system, description="My eSIM will not activate, call me on 07700 900123 please.")
    system["worker"].run_once(block=False)
    point = system["qdrant"].scroll(COLLECTION_ALIAS, limit=1)[0][0]
    assert "07700" not in point.payload["text"] and "[PHONE]" in point.payload["text"]


# ---- ticket classes -----------------------------------------------------------------------------


def test_a_ticket_with_an_unknown_class_is_refused_until_the_class_is_added(system):
    client = system["client"]
    new_class = {**TICKET, "category": "esim_management"}
    refused = client.post("/v1/tickets", json=new_class, headers=ADMIN)
    assert refused.status_code == 422
    assert "unknown category 'esim_management'" in refused.json()["detail"]

    added = client.post(
        "/v1/taxonomy",
        json={"kind": "category", "name": "esim_management", "description": "eSIM set-up and transfer"},
        headers=ADMIN,
    )
    assert added.status_code == 201
    assert client.post("/v1/tickets", json=new_class, headers=ADMIN).status_code == 202

    assert client.delete("/v1/taxonomy/category/esim_management", headers=ADMIN).status_code == 200
    assert client.post("/v1/tickets", json=new_class, headers=ADMIN).status_code == 422
    assert client.delete("/v1/taxonomy/category/esim_management", headers=ADMIN).status_code == 404


def test_class_names_must_be_tidy(system):
    bad = {"kind": "category", "name": "eSIM Management!"}
    assert system["client"].post("/v1/taxonomy", json=bad, headers=ADMIN).status_code == 422


def test_proposals_can_be_listed_approved_and_rejected(system):
    client, store = system["client"], system["store"]
    store.proposal_rows = [
        {"id": 1, "kind": "category", "suggested_name": "esim_qr_code", "status": "pending"},
        {"id": 2, "kind": "category", "suggested_name": "weather_tomorrow", "status": "pending"},
    ]
    listed = client.get("/v1/classes/proposals", headers=ADMIN).json()["proposals"]
    assert [row["id"] for row in listed] == [1, 2]
    assert client.get("/v1/classes/proposals", headers=AGENT).status_code == 403

    approved = client.post("/v1/classes/proposals/1/approve", json={"name": "esim_management"}, headers=ADMIN)
    assert approved.status_code == 200 and approved.json()["approved_name"] == "esim_management"
    assert "esim_management" in store.classes["category"]  # approving adds the class

    assert client.post("/v1/classes/proposals/2/reject", headers=ADMIN).status_code == 200
    assert client.post("/v1/classes/proposals/2/reject", headers=ADMIN).status_code == 404  # decided
    assert client.get("/v1/classes/proposals", headers=ADMIN).json()["proposals"] == []


# ---- knowledge-base articles --------------------------------------------------------------------


def test_an_edited_article_replaces_the_old_one_completely(system):
    client = system["client"]
    first = client.put("/v1/kb/KB-900", json=ARTICLE, headers=ADMIN)
    assert first.status_code == 202 and first.json()["version"] == 1
    system["worker"].run_once(block=False)
    assert system["qdrant"].count(COLLECTION_ALIAS, exact=True).count == 2  # two sections
    assert system["search"]("voicemail greeting recording fails") == ["KB-900"]

    shorter = {**ARTICLE, "body": "# Voicemail\n\n## Resolution steps\n1. Reset the voicemail mailbox PIN.\n"}
    assert client.put("/v1/kb/KB-900", json=shorter, headers=ADMIN).json()["version"] == 2
    system["worker"].run_once(block=False)
    points = system["qdrant"].scroll(COLLECTION_ALIAS, limit=10)[0]
    assert len(points) == 1  # the leftover section of the old version is gone
    assert "PIN" in points[0].payload["content"] and points[0].payload["version"] == 2


def test_an_article_needs_a_kb_id_and_known_classes(system):
    client = system["client"]
    assert client.put("/v1/kb/T-123", json=ARTICLE, headers=ADMIN).status_code == 422
    assert client.put("/v1/kb/XYZ", json=ARTICLE, headers=ADMIN).status_code == 404
    unknown = {**ARTICLE, "product": "satellite"}
    assert client.put("/v1/kb/KB-901", json=unknown, headers=ADMIN).status_code == 422


def test_a_retired_document_disappears_from_search_but_not_from_the_database(system):
    client = system["client"]
    ticket_id = add_ticket(system)
    system["worker"].run_once(block=False)
    assert system["search"]("eSIM QR code") == [ticket_id]

    assert client.delete(f"/v1/documents/{ticket_id}", headers=ADMIN).status_code == 202
    system["worker"].run_once(block=False)
    assert system["search"]("eSIM QR code") == []
    assert system["store"].documents["ticket"][ticket_id]["is_active"] is False
    assert client.delete("/v1/documents/T-does-not-exist", headers=ADMIN).status_code == 404


# ---- when things go wrong -----------------------------------------------------------------------


def test_a_failed_event_is_retried_until_it_works(system):
    ticket_id = add_ticket(system)
    system["worker"]._embedder = FlakyEmbedder(failures=1)

    system["worker"].run_once(block=False)  # fails: the event stays on the queue
    assert waiting(system) == 1
    assert system["store"].documents["ticket"][ticket_id]["indexed_at"] is None

    system["worker"].run_once(block=False)  # picked up again, works this time
    assert waiting(system) == 0
    assert system["store"].documents["ticket"][ticket_id]["indexed_at"] is not None


def test_an_event_that_keeps_failing_goes_to_the_dead_letter_list(system):
    add_ticket(system)
    system["worker"]._embedder = FlakyEmbedder(failures=99)
    for _ in range(3):  # max_attempts
        system["worker"].run_once(block=False)

    settings = system["settings"]
    assert waiting(system) == 0  # no longer blocking the queue
    dead = system["redis"].xrange(settings.dead_letter_stream)
    assert len(dead) == 1 and "embedding service is down" in dead[0][1]["error"]
    # PostgreSQL still says the document is behind, so the sweep retries it once the fault is fixed.
    assert system["store"].documents_waiting() == 1


def test_the_sweep_indexes_documents_whose_event_was_lost(system):
    system["client"].app.state.queue = type("DownQueue", (), {"publish": lambda *_: False})()
    response = system["client"].post("/v1/tickets", json=TICKET, headers=ADMIN)
    assert response.status_code == 202 and response.json()["queued"] is False  # saved, not queued

    assert system["worker"].run_once(block=False) == 0  # nothing on the queue
    assert system["worker"].sweep() == 1  # ... but PostgreSQL knows the index is behind
    assert system["search"]("eSIM QR code") == [response.json()["id"]]
    assert system["worker"].sweep() == 0


def test_events_for_missing_or_unreadable_documents_do_not_block_the_queue(system):
    settings = system["settings"]
    system["redis"].xadd(settings.stream, {"doc_type": "ticket", "doc_id": "T-deleted-meanwhile"})
    system["redis"].xadd(settings.stream, {"something": "else"})
    system["worker"].run_once(block=False)
    assert waiting(system) == 0


def test_a_database_outage_gives_a_clear_error(system):
    def broken(*_):
        raise RuntimeError("database is down")

    system["store"].taxonomy = broken
    response = system["client"].post("/v1/tickets", json=TICKET, headers=ADMIN)
    assert response.status_code == 503 and "database is unavailable" in response.json()["detail"]
