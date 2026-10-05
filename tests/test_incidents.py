"""Fast tests for incident detection: counting similar recent complaints, and the eval's arithmetic."""

import random

import numpy as np
import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient

from evals.eval_incidents import (
    MIN_COUNTS,
    choose,
    complaints_needed,
    pick,
    rates,
    run_half_hour,
    split_of,
    trigger_level,
)
from libs.common.embedding_client import EmbeddingServiceError
from services.retrieval.config import Settings
from services.retrieval.main import create_app
from services.retrieval.recent import RecentComplaints, RecentRequest, complaint_id
from tests.fakes import FakeEmbedder

OUTAGE = [
    "no internet since this morning in anna nagar",
    "internet is down since morning in anna nagar",
    "no internet in anna nagar since morning today",
    "since morning no internet at all in anna nagar",
]
BILLING = "i was charged twice on my bill this month"


class Clock:
    """A clock the test can move forward."""

    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now

    def minutes(self, count: float) -> None:
        self.now += count * 60


@pytest.fixture
def recent():
    clock = Clock()
    tracker = RecentComplaints(QdrantClient(":memory:"), FakeEmbedder(), Settings(), clock=clock)
    return tracker, clock


def record(tracker, text, **options):
    return tracker.record(RecentRequest(text=text, **{"min_similarity": 0.6, **options}))


# ---- counting similar recent complaints ---------------------------------------------------------


def test_similar_complaints_add_up_and_a_different_one_does_not(recent):
    tracker, clock = recent
    counts = []
    for text in OUTAGE:
        counts.append(record(tracker, text).count)
        clock.minutes(1)
    assert counts == [1, 2, 3, 4]
    assert record(tracker, BILLING).count == 1


def test_the_same_words_twice_are_one_complaint(recent):
    tracker, _ = recent
    record(tracker, OUTAGE[0])
    record(tracker, OUTAGE[1])
    again = record(
        tracker, "  NO Internet since this   morning in Anna Nagar "
    )  # a retry, typed slightly differently
    assert again.count == 2
    assert complaint_id("No  Internet") == complaint_id("no internet")


def test_only_complaints_inside_the_time_window_count(recent):
    tracker, clock = recent
    record(tracker, OUTAGE[0])
    clock.minutes(20)
    record(tracker, OUTAGE[1])
    clock.minutes(20)  # the first one is now 40 minutes old
    assert record(tracker, OUTAGE[2], window_minutes=30).count == 2
    assert record(tracker, OUTAGE[2], window_minutes=60).count == 3


def test_a_stricter_similarity_counts_fewer(recent):
    tracker, _ = recent
    for text in OUTAGE[:3]:
        record(tracker, text)
    assert record(tracker, OUTAGE[3], min_similarity=0.6).count == 4
    assert record(tracker, OUTAGE[3], min_similarity=0.99).count == 1


def test_the_agent_gets_a_few_of_the_other_complaints_to_read(recent):
    tracker, clock = recent
    for text in OUTAGE:
        record(tracker, text)
        clock.minutes(2)
    found = record(tracker, "no internet since this morning here in anna nagar")
    assert found.count == 5 and len(found.others) == 3  # recent_examples
    assert all(other.text in OUTAGE for other in found.others)  # never the complaint itself
    assert found.others[0].similarity >= found.others[-1].similarity
    assert {other.minutes_ago for other in found.others} <= {2, 4, 6, 8}


def test_personal_details_are_not_stored(recent):
    tracker, _ = recent
    record(tracker, "no internet in anna nagar, call me on 07700 900123")
    found = record(tracker, "no internet in anna nagar, please call me")
    assert found.count == 2 and "07700" not in found.others[0].text and "[PHONE]" in found.others[0].text


def test_old_complaints_are_deleted(recent):
    tracker, clock = recent
    for text in OUTAGE:
        record(tracker, text)
    clock.minutes(2 * 24 * 60)  # two days later: older than the one-day retention
    record(tracker, BILLING)
    assert tracker._qdrant.count(Settings().recent_collection).count == 1


# ---- the API ------------------------------------------------------------------------------------


@pytest.fixture
def api():
    embedder = FakeEmbedder()
    tracker = RecentComplaints(QdrantClient(":memory:"), embedder, Settings())
    with TestClient(create_app(searcher=None, settings=Settings(), recent=tracker)) as client:
        yield client, embedder


def test_recent_endpoint(api):
    client, _ = api
    client.post("/recent", json={"text": OUTAGE[0], "min_similarity": 0.6})
    response = client.post("/recent", json={"text": OUTAGE[1], "min_similarity": 0.6, "window_minutes": 15})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 2 and body["window_minutes"] == 15 and body["others"][0]["text"] == OUTAGE[0]


@pytest.mark.parametrize(
    "body",
    [{"text": ""}, {"text": "   "}, {"text": "ok", "window_minutes": 0}, {"text": "ok", "min_similarity": 2}],
)
def test_recent_rejects_bad_input(api, body):
    client, _ = api
    assert client.post("/recent", json=body).status_code == 422


def test_recent_gives_a_clean_503_when_something_it_needs_is_down(api):
    client, embedder = api

    def broken(*args, **kwargs):
        raise EmbeddingServiceError("embedding is down")

    embedder.embed = broken
    assert client.post("/recent", json={"text": OUTAGE[0]}).status_code == 503
    with TestClient(create_app(searcher=object(), settings=Settings(), recent=None)) as without_tracker:
        assert without_tracker.post("/recent", json={"text": OUTAGE[0]}).status_code == 503


# ---- the eval's arithmetic ----------------------------------------------------------------------


def test_problems_are_split_into_a_tuning_half_and_a_report_half():
    assert split_of("S01") == "dev" and split_of("S02") == "test" and split_of("S35") == "dev"


def test_trigger_level_is_set_by_the_weakest_complaint_that_is_still_needed():
    earlier = [0.9, 0.5, 0.8, 0.7]
    assert trigger_level(earlier, 3) == 0.8  # needs 2 earlier ones: the 2nd closest decides
    assert trigger_level(earlier, 5) == 0.5  # needs all 4
    assert trigger_level([0.9], 3) == -1.0  # too few complaints so far: no flag at any setting


def test_a_half_hour_is_played_in_arrival_order():
    # Complaints 0 to 3 are one problem (0.9 alike), 4 is something else (0.2 to everything).
    similarity = np.full((5, 5), 0.2)
    similarity[:4, :4] = 0.9
    levels = run_half_hour(similarity, order=[4, 0, 1, 2, 3], burst={0, 1, 2, 3})
    assert levels[3]["burst"] == 0.9 and levels[4]["burst"] == 0.9
    assert levels[5]["burst"] == 0.2  # a fifth similar complaint never came, only the unrelated one
    assert levels[3]["other"] == -1.0  # the unrelated complaint arrived first, with nothing before it
    assert set(levels) == set(MIN_COUNTS)


def test_rates_count_flagged_incidents_and_false_alarms():
    incidents = [{4: {"burst": 0.85, "other": 0.1}}, {4: {"burst": 0.70, "other": 0.1}}]
    quiet = [{4: {"burst": -1.0, "other": 0.82}}, {4: {"burst": -1.0, "other": 0.4}}]
    row = rates(incidents, quiet, threshold=0.8, min_count=4)
    assert row["incidents_flagged"] == 0.5 and row["quiet_flagged"] == 0.5
    assert rates(incidents, quiet, 0.6, 4)["incidents_flagged"] == 1.0


def test_the_best_setting_flags_the_most_incidents_without_false_alarms():
    rows = [
        {"min_similarity": 0.70, "min_similar": 4, "incidents_flagged": 0.95, "quiet_flagged": 0.30},
        {"min_similarity": 0.80, "min_similar": 4, "incidents_flagged": 0.80, "quiet_flagged": 0.01},
        {"min_similarity": 0.85, "min_similar": 4, "incidents_flagged": 0.80, "quiet_flagged": 0.01},
        {"min_similarity": 0.90, "min_similar": 4, "incidents_flagged": 0.40, "quiet_flagged": 0.00},
    ]
    assert choose(rows)["min_similarity"] == 0.85  # 0.70 has too many false alarms; ties go stricter
    assert choose(rows, max_false_alarms=0.5)["min_similarity"] == 0.70


def test_how_many_complaints_it_takes_to_raise_the_flag():
    similarity = np.full((5, 5), 0.2)
    similarity[:4, :4] = 0.9
    half_hour = {"order": [0, 4, 1, 2, 3], "burst": {0, 1, 2, 3}}
    assert complaints_needed(similarity, half_hour, threshold=0.8, min_count=3) == 3
    assert complaints_needed(similarity, half_hour, threshold=0.8, min_count=5) is None


def test_background_complaints_are_about_different_problems():
    pool = {name: list(range(start, start + 10)) for name, start in (("a", 0), ("b", 10), ("c", 20))}
    chosen = pick(pool, random.Random(1), how_many=6)
    assert len(chosen) == 6 and all(sum(index // 10 == group for index in chosen) <= 2 for group in range(3))
