"""Fast tests for the API gateway: orchestration, checkpoints, degradation, auth, limits, feedback."""

import uuid

import fakeredis
import pytest
import redis
from fastapi.testclient import TestClient

from libs.common.service_client import ServiceError
from services.gateway.config import Settings
from services.gateway.infra import IngestQueue, RedisCache, RedisRateLimiter
from services.gateway.main import create_app
from services.gateway.orchestrator import Orchestrator
from tests.fakes import InMemoryCache, InMemoryQueue, InMemoryStore

KEY = {"X-API-Key": "test-key"}
COMPLAINT = "My broadband drops every evening. Call me on 07700 900123."


def source(source_id, source_type, similarity):
    return {
        "id": source_id,
        "source_type": source_type,
        "title": "Evening broadband drops",
        "text": "My broadband drops every evening.",
        "content": "Problem: drops.\nResolution steps:\n1. Run a line test.",
        "similarity": similarity,
        "score": 0.5,
    }


class FakeService:
    def __init__(self):
        self.calls = []
        self.error = None
        self.is_ready = True

    def ready(self):
        return self.is_ready

    def _call(self, *args):
        self.calls.append(args)
        if self.error:
            raise self.error


class FakeTriage(FakeService):
    category = "connectivity_intermittent"

    def classify(self, complaint):
        self._call(complaint)
        return {
            "category": {
                "label": self.category,
                "confidence": 0.9,
                "best_guess": "connectivity_intermittent",
            },
            "product": {"label": "broadband", "confidence": 1.0, "best_guess": "broadband"},
            "severity": {"label": "high", "baseline": "medium", "reasons": ["business_impact"]},
            "sentiment": {"label": "negative", "reasons": ["frustrated"]},
            "needs_review": self.category == "unknown",
            "signals": [],
        }


class FakeRetrieval(FakeService):
    similarity = 0.9

    def search(self, query, top_k_tickets=3, top_k_kb=2, **options):
        self._call(query, top_k_tickets, top_k_kb)
        return {"results": [source("T-000001", "ticket", self.similarity), source("KB-001", "kb", 0.8)]}


class FakeGeneration(FakeService):
    def generate(self, complaint, sources, triage=None):
        self._call(complaint, sources, triage)
        step = {
            "n": 1,
            "text": "Run a line test.",
            "citations": ["T-000001"],
            "support": 0.95,
            "verified": True,
        }
        return {
            "mode": "llm",
            "summary": "Likely congestion.",
            "already_tried": [],
            "steps": [{**step, "repeats_already_tried": False}],
            "escalate": False,
            "escalation_reason": "",
            "grounded": True,
            "fallback_reason": None,
            "model": "fake-llm",
            "prompt_version": "v2",
        }


class Limiter:
    def __init__(self, allowed=True):
        self.allowed = allowed
        self.limits = []

    def allow(self, identity, limit=None):
        self.limits.append(limit)
        return self.allowed, 42


@pytest.fixture
def setup():
    parts = {
        "triage": FakeTriage(),
        "retrieval": FakeRetrieval(),
        "generation": FakeGeneration(),
        "cache": InMemoryCache(),
        "store": InMemoryStore(),
        "limiter": Limiter(),
        "queue": InMemoryQueue(),
    }
    settings = Settings(
        api_keys="test-key, other-key",
        admin_api_keys="admin-key",
        min_similarity=0.7,
        max_complaint_chars=300,
    )
    orchestrator = Orchestrator(
        parts["triage"], parts["retrieval"], parts["generation"], parts["cache"], parts["store"], settings
    )
    with TestClient(create_app(orchestrator, parts["limiter"], settings, parts["queue"])) as client:
        yield client, parts


def resolve(client, complaint=COMPLAINT, **extra):
    response = client.post("/v1/resolve", json={"complaint": complaint, **extra}, headers=KEY)
    assert response.status_code == 200, response.text
    return response.json()


# ---- the normal path --------------------------------------------------------------------------


def test_resolve_returns_labels_sources_and_a_cited_resolution(setup):
    client, parts = setup
    body = resolve(client)
    uuid.UUID(body["request_id"])
    assert body["triage"]["category"] == {
        "label": "connectivity_intermittent",
        "confidence": 0.9,
        "best_guess": "connectivity_intermittent",
    }
    assert body["triage"]["severity"] == {"label": "high", "reasons": ["business_impact"]}
    assert [s["id"] for s in body["sources"]] == ["T-000001", "KB-001"]
    assert "content" not in body["sources"][0]  # the long text is for the model, not the response
    assert body["resolution"]["steps"][0]["citations"] == ["T-000001"]
    assert body["resolution"]["grounded"] is True and body["escalate"] is False
    assert body["meta"]["cached"] is False and body["meta"]["degraded"] == []
    assert body["meta"]["model"] == "fake-llm" and body["meta"]["prompt_version"] == "v2"
    assert {"triage", "retrieval", "generation", "total"} <= set(body["meta"]["latency_ms"])


def test_personal_details_are_masked_before_any_service_sees_them(setup):
    client, parts = setup
    resolve(client)
    for service in ("triage", "retrieval", "generation"):
        assert "07700" not in parts[service].calls[0][0]
        assert "[PHONE]" in parts[service].calls[0][0]
    assert "07700" not in parts["store"].requests[0]["complaint_masked"]


def test_every_request_is_written_to_the_audit_log(setup):
    client, parts = setup
    body = resolve(client)
    (record,) = parts["store"].requests
    assert record["request_id"] == body["request_id"]
    assert record["source_ids"] == ["T-000001", "KB-001"]
    assert record["grounded"] is True and record["escalated"] is False
    assert record["llm_model"] == "fake-llm" and record["prompt_version"] == "v2"


def test_a_repeated_complaint_is_served_from_the_cache(setup):
    client, parts = setup
    first = resolve(client)
    second = resolve(client, complaint="  my BROADBAND drops every evening.   call me on 07700 900123. ")
    assert second["meta"]["cached"] is True
    assert second["resolution"] == first["resolution"]
    assert second["request_id"] != first["request_id"]  # still its own request, with its own audit row
    assert len(parts["generation"].calls) == 1 and len(parts["store"].requests) == 2


def test_a_change_to_the_search_index_makes_cached_answers_stale(setup):
    client, parts = setup
    first = resolve(client)
    assert first["meta"]["index_version"] == "0"
    parts["cache"].version = "1"  # the ingestion worker indexed something new
    second = resolve(client)
    assert second["meta"]["cached"] is False and second["meta"]["index_version"] == "1"
    assert len(parts["generation"].calls) == 2
    assert [record["index_version"] for record in parts["store"].requests] == ["0", "1"]


def test_analyze_only_skips_drafting_and_is_not_logged(setup):
    client, parts = setup
    body = resolve(client, generate=False)
    assert body["resolution"] is None and body["triage"] is not None and body["sources"]
    assert parts["generation"].calls == [] and parts["store"].requests == [] and parts["cache"].data == {}


def test_unknown_labels_are_not_passed_to_the_model_as_facts(setup):
    client, parts = setup
    parts["triage"].category = "unknown"
    body = resolve(client)
    assert body["triage"]["needs_review"] is True
    assert parts["generation"].calls[0][2]["category"] is None
    assert parts["generation"].calls[0][2]["product"] == "broadband"


# ---- checkpoints and degradation --------------------------------------------------------------


def test_no_confident_match_means_no_answer_is_drafted(setup):
    client, parts = setup
    parts["retrieval"].similarity = 0.5  # below the 0.7 bar (the article at 0.8 is removed below)
    parts["retrieval"].search = lambda *a, **k: {"results": [source("T-000001", "ticket", 0.5)]}
    body = resolve(client)
    assert body["resolution"] is None and body["escalate"] is True
    assert "similar enough" in body["escalation_reason"]
    assert body["meta"]["confident_match"] is False and body["sources"]  # sources are still shown
    assert parts["generation"].calls == []
    assert parts["store"].requests[0]["escalated"] is True
    assert parts["cache"].data == {}


def test_the_gateway_still_answers_when_triage_is_down(setup):
    client, parts = setup
    parts["triage"].error = ServiceError("triage is down")
    body = resolve(client)
    assert body["triage"] is None and body["resolution"] is not None
    assert body["meta"]["degraded"] == ["triage"]
    assert parts["generation"].calls[0][2] is None
    assert parts["cache"].data == {}  # incomplete answers are not cached


def test_the_gateway_returns_sources_when_generation_is_down(setup):
    client, parts = setup
    parts["generation"].error = ServiceError("generation is down")
    body = resolve(client)
    assert body["resolution"] is None and body["escalate"] is True and body["sources"]
    assert body["meta"]["degraded"] == ["generation"]


def test_without_search_the_request_fails_clearly(setup):
    client, parts = setup
    parts["retrieval"].error = ServiceError("retrieval is down")
    response = client.post("/v1/resolve", json={"complaint": COMPLAINT}, headers=KEY)
    assert response.status_code == 503 and "Search is unavailable" in response.json()["detail"]


# ---- auth, limits, validation -----------------------------------------------------------------


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}, {"X-API-Key": ""}])
def test_requests_without_a_valid_key_are_refused(setup, headers):
    client, parts = setup
    response = client.post("/v1/resolve", json={"complaint": COMPLAINT}, headers=headers)
    assert response.status_code == 401
    assert parts["triage"].calls == []


def test_any_configured_key_works_and_health_needs_none(setup):
    client, _ = setup
    assert (
        client.post("/v1/resolve", json={"complaint": "x"}, headers={"X-API-Key": "other-key"}).status_code
        == 200
    )
    assert client.get("/health").status_code == 200 and client.get("/metrics").status_code == 200


def test_rate_limited_requests_get_429_with_retry_after(setup):
    client, parts = setup
    parts["limiter"].allowed = False
    response = client.post("/v1/resolve", json={"complaint": COMPLAINT}, headers=KEY)
    assert response.status_code == 429 and response.headers["Retry-After"] == "42"
    assert parts["triage"].calls == []


@pytest.mark.parametrize("complaint", ["", "   ", "x" * 301])
def test_bad_complaints_are_rejected(setup, complaint):
    client, _ = setup
    assert client.post("/v1/resolve", json={"complaint": complaint}, headers=KEY).status_code == 422


# ---- feedback ---------------------------------------------------------------------------------


def test_feedback_is_stored_against_the_request(setup):
    client, parts = setup
    request_id = resolve(client)["request_id"]
    body = {"request_id": request_id, "helpful": True, "comment": "worked first time"}
    assert client.post("/v1/feedback", json=body, headers=KEY).json() == {"status": "saved"}
    assert parts["store"].feedback == [(request_id, True, "worked first time", None, None)]


def test_an_agent_can_correct_the_category(setup):
    client, parts = setup
    request_id = resolve(client)["request_id"]
    body = {"request_id": request_id, "helpful": False, "correct_category": "billing_dispute"}
    assert client.post("/v1/feedback", json=body, headers=KEY).status_code == 200
    body["correct_category"] = "none_of_these"  # "no existing category fits": feeds new-class discovery
    assert client.post("/v1/feedback", json=body, headers=KEY).status_code == 200
    assert [row[4] for row in parts["store"].feedback] == ["billing_dispute", "none_of_these"]

    body["correct_category"] = "made_up_category"
    assert client.post("/v1/feedback", json=body, headers=KEY).status_code == 422
    metrics = client.get("/metrics").text
    assert 'gateway_category_corrections_total{kind="relabelled"}' in metrics
    assert 'gateway_category_corrections_total{kind="none_of_these"}' in metrics


def test_feedback_for_an_unknown_or_invalid_request_is_refused(setup):
    client, _ = setup
    unknown = {"request_id": str(uuid.uuid4()), "helpful": False}
    assert client.post("/v1/feedback", json=unknown, headers=KEY).status_code == 404
    assert (
        client.post(
            "/v1/feedback", json={"request_id": "not-an-id", "helpful": True}, headers=KEY
        ).status_code
        == 422
    )
    assert client.post("/v1/feedback", json=unknown).status_code == 401


def test_feedback_during_a_database_outage_fails_clearly(setup):
    client, parts = setup
    request_id = resolve(client)["request_id"]

    def broken(*_):
        raise RuntimeError("database is down")

    parts["store"].save_feedback = broken
    response = client.post("/v1/feedback", json={"request_id": request_id, "helpful": True}, headers=KEY)
    assert response.status_code == 503
    assert "could not be saved" in response.json()["detail"]


# ---- readiness and metrics --------------------------------------------------------------------


def test_ready_lists_each_dependency(setup):
    client, parts = setup
    assert client.get("/ready").json()["checks"] == {
        "triage": True,
        "retrieval": True,
        "generation": True,
        "database": True,
    }
    parts["generation"].is_ready = False  # optional: still ready
    assert client.get("/ready").status_code == 200
    parts["retrieval"].is_ready = False  # required
    assert client.get("/ready").status_code == 503


def test_metrics_cover_outcomes_cache_and_rejections(setup):
    client, parts = setup
    resolve(client)
    resolve(client)
    client.post("/v1/resolve", json={"complaint": COMPLAINT})
    metrics = client.get("/metrics").text
    assert 'gateway_resolve_total{outcome="answered"}' in metrics
    assert 'gateway_resolve_total{outcome="cached"}' in metrics
    assert 'gateway_cache_total{result="hit"}' in metrics
    assert 'gateway_rejected_total{reason="bad_api_key"}' in metrics
    assert 'gateway_stage_seconds_count{stage="generation"}' in metrics


# ---- Redis cache and rate limiter -------------------------------------------------------------


class BrokenRedis:
    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise redis.ConnectionError("redis is down")

        return fail


def test_cache_round_trip_and_expiry():
    client = fakeredis.FakeRedis()
    cache = RedisCache(client, ttl_seconds=60)
    assert cache.get("k") is None
    cache.set("k", {"answer": 1})
    assert cache.get("k") == {"answer": 1}
    assert 0 < client.ttl("k") <= 60
    assert cache.index_version() == "0"
    client.incr("index:version")
    assert cache.index_version() == "1"


def test_rate_limiter_allows_up_to_the_limit_per_caller():
    limiter = RedisRateLimiter(fakeredis.FakeRedis(), limit_per_minute=3)
    assert [limiter.allow("alice")[0] for _ in range(4)] == [True, True, True, False]
    assert limiter.allow("bob")[0] is True  # each caller has their own allowance
    assert 1 <= limiter.allow("bob")[1] <= 60


def test_a_redis_outage_never_blocks_requests():
    assert RedisCache(BrokenRedis(), 60).get("k") is None
    RedisCache(BrokenRedis(), 60).set("k", {"a": 1})  # must not raise
    assert RedisRateLimiter(BrokenRedis(), 1).allow("alice")[0] is True  # fail open
    assert RedisCache(BrokenRedis(), 60).index_version() == "0"
    assert IngestQueue(BrokenRedis()).publish("ticket", "T-1") is False  # reported, not raised
