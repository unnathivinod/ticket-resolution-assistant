"""Fast tests for the triage rules and the triage API."""

import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient

from libs.common.service_client import ServiceError
from services.ingestion.indexer import ensure_collection, index_chunks, ticket_chunks
from services.retrieval.config import Settings as RetrievalSettings
from services.retrieval.search import Searcher
from services.triage.config import Settings
from services.triage.logic import (
    UNKNOWN,
    Evidence,
    Params,
    Signal,
    SignalMatch,
    baseline_severity,
    decide,
    load_signals,
    match_signals,
    split_segments,
    vote,
)
from services.triage.main import create_app
from services.triage.service import Triage
from tests.fakes import DIM, FakeEmbedder, InProcessRetrieval

SIGNALS = [
    Signal("vulnerable", "", ["relies on this for help"], severity_delta=2),
    Signal("business_impact", "", ["I work from home"], severity_delta=1),
    Signal("low_urgency", "", ["no rush"], severity_delta=-1),
    Signal("frustrated", "", ["I am fed up"], sentiment="negative"),
    Signal("appreciative", "", ["thank you"], sentiment="positive"),
]
PARAMS = Params(
    neighbours=5,
    similarity_power=4,
    min_similarity=0.6,
    min_confidence=0.4,
    severity_signal_threshold=0.75,
    sentiment_signal_threshold=0.75,
)


def neighbour(similarity, category="connectivity", product="broadband", severity="medium"):
    return {"similarity": similarity, "category": category, "product": product, "severity": severity}


def evidence(neighbours, **signal_similarities):
    signals = {name: SignalMatch(0.0, "", "") for name in (s.name for s in SIGNALS)}
    signals.update(
        {name: SignalMatch(value, "some sentence", "") for name, value in signal_similarities.items()}
    )
    return Evidence(neighbours=neighbours, signals=signals)


# ---- cutting a complaint into pieces ----------------------------------------------------------


def test_split_segments_uses_sentences():
    assert split_segments("Hi. My wifi is slow! Can you help?") == [
        "Hi.",
        "My wifi is slow!",
        "Can you help?",
    ]


def test_split_segments_falls_back_to_word_windows_without_punctuation():
    text = " ".join(f"word{i}" for i in range(30))
    segments = split_segments(text)
    assert len(segments) > 1
    assert all(len(segment.split()) <= 10 for segment in segments)
    assert segments[0].startswith("word0") and "word29" in segments[-1]


def test_split_segments_keeps_short_text_whole():
    assert split_segments("no internet") == ["no internet"]


# ---- voting -----------------------------------------------------------------------------------


def test_vote_gives_closer_tickets_a_bigger_say():
    neighbours = [neighbour(0.9, category="a"), neighbour(0.5, category="b"), neighbour(0.5, category="b")]
    assert vote(neighbours, "category", power=1)[0] == "b"  # two weak votes beat one strong vote
    assert vote(neighbours, "category", power=8)[0] == "a"  # a high power lets the closest one win


def test_vote_reports_the_winning_share_and_ignores_missing_labels():
    neighbours = [neighbour(1.0, category="a"), neighbour(1.0, category="b"), neighbour(1.0, category=None)]
    label, confidence = vote(neighbours, "category", power=1)
    assert label in {"a", "b"} and confidence == pytest.approx(0.5)
    assert vote([], "category", power=1) == (None, 0.0)


def test_baseline_severity_ignores_tickets_that_were_raised_by_urgency():
    neighbours = [neighbour(0.9, severity=level) for level in ["medium"] * 5 + ["high"] * 6 + ["critical"]]
    assert baseline_severity(neighbours, "connectivity") == "medium"
    assert baseline_severity([], None) == "medium"


# ---- the decision rules -----------------------------------------------------------------------


def test_decide_labels_from_neighbours():
    result = decide(evidence([neighbour(0.9), neighbour(0.8)]), SIGNALS, PARAMS)
    assert result["category"] == {"label": "connectivity", "confidence": 1.0, "best_guess": "connectivity"}
    assert result["product"]["label"] == "broadband"
    assert result["severity"] == {"label": "medium", "baseline": "medium", "reasons": []}
    assert result["sentiment"] == {"label": "neutral", "reasons": []}
    assert result["needs_review"] is False


def test_decide_says_unknown_when_nothing_is_similar_enough():
    result = decide(evidence([neighbour(0.3), neighbour(0.2)]), SIGNALS, PARAMS)
    assert result["category"]["label"] == UNKNOWN
    assert result["category"]["best_guess"] == "connectivity"  # still shown, to help the reviewer
    assert result["needs_review"] is True


def test_decide_says_unknown_when_the_vote_is_too_split():
    neighbours = [neighbour(0.8, category=name) for name in ("a", "b", "c", "d")]
    result = decide(evidence(neighbours), SIGNALS, PARAMS)
    assert result["category"]["label"] == UNKNOWN
    assert result["product"]["label"] == "broadband"  # the product vote was not split


def test_decide_says_unknown_without_neighbours():
    result = decide(evidence([]), SIGNALS, PARAMS)
    assert result["category"]["label"] == UNKNOWN and result["severity"]["label"] == "medium"


@pytest.mark.parametrize(
    ("signal_similarities", "expected", "reasons"),
    [
        ({"business_impact": 0.8}, "high", ["business_impact"]),
        ({"vulnerable": 0.9}, "critical", ["vulnerable"]),
        ({"low_urgency": 0.8}, "low", ["low_urgency"]),
        ({"business_impact": 0.8, "low_urgency": 0.9}, "high", ["business_impact"]),  # raising wins
        ({"business_impact": 0.8, "vulnerable": 0.8}, "critical", ["vulnerable"]),  # strongest wins
        ({"business_impact": 0.7}, "medium", []),  # below the threshold: ignored
    ],
)
def test_decide_moves_severity_with_urgency_signals(signal_similarities, expected, reasons):
    result = decide(evidence([neighbour(0.9)], **signal_similarities), SIGNALS, PARAMS)
    assert result["severity"]["label"] == expected
    assert result["severity"]["reasons"] == reasons


def test_decide_severity_never_leaves_the_scale():
    top = decide(evidence([neighbour(0.9, severity="high")], vulnerable=0.9), SIGNALS, PARAMS)
    bottom = decide(evidence([neighbour(0.9, severity="low")], low_urgency=0.9), SIGNALS, PARAMS)
    assert top["severity"]["label"] == "critical" and bottom["severity"]["label"] == "low"


def test_decide_picks_the_strongest_tone():
    result = decide(evidence([neighbour(0.9)], frustrated=0.8, appreciative=0.9), SIGNALS, PARAMS)
    assert result["sentiment"] == {"label": "positive", "reasons": ["appreciative"]}
    assert [hit["name"] for hit in result["signals"]] == ["appreciative", "frustrated"]


def test_severity_and_sentiment_thresholds_are_separate():
    params = Params(severity_signal_threshold=0.9, sentiment_signal_threshold=0.5)
    result = decide(evidence([neighbour(0.9)], business_impact=0.8, frustrated=0.6), SIGNALS, params)
    assert result["severity"]["reasons"] == [] and result["sentiment"]["label"] == "negative"


def test_match_signals_finds_the_closest_sentence():
    embedder = FakeEmbedder()
    segments = ["My wifi is slow.", "I work from home"]
    vectors = embedder.embed(segments)["dense"]
    examples = {s.name: embedder.embed(s.examples)["dense"] for s in SIGNALS}
    matches = match_signals(segments, vectors, SIGNALS, examples)
    assert matches["business_impact"].similarity == pytest.approx(1.0, abs=1e-3)
    assert matches["business_impact"].matched_text == "I work from home"
    assert matches["vulnerable"].similarity < 0.75


# ---- configuration files ----------------------------------------------------------------------


def test_the_real_signals_file_is_valid():
    signals = load_signals(Settings().signals_path)
    assert {s.name for s in signals} >= {"vulnerable", "business_impact", "repeat_contact", "frustrated"}
    for signal in signals:
        assert len(signal.examples) >= 3
        assert (signal.severity_delta is not None) != (signal.sentiment is not None)  # exactly one purpose


def test_the_real_params_file_loads(tmp_path):
    assert Params.from_file(Settings().params_path).neighbours >= 1
    custom = tmp_path / "params.json"
    custom.write_text(json.dumps({"_comment": "ignored", "neighbours": 7, "calibrated": True}))
    assert Params.from_file(custom).neighbours == 7


# ---- the API, on top of the real search code --------------------------------------------------


def ticket(number, text, category, product, severity="medium"):
    return {
        "id": f"T-{number:06d}",
        "subject": "s",
        "description": text,
        "resolution_steps": ["Do the fix."],
        "category": category,
        "product": product,
        "severity": severity,
        "sentiment": "neutral",
        "scenario_id": "S",
        "created_at": "2026-01-01T00:00:00+00:00",
    }


@pytest.fixture
def setup():
    qdrant, embedder = QdrantClient(":memory:"), FakeEmbedder()
    ensure_collection(qdrant, DIM)
    tickets = [
        ticket(1, "broadband drops every evening", "connectivity", "broadband"),
        ticket(2, "broadband drops every evening after dinner", "connectivity", "broadband"),
        ticket(3, "broadband drops every evening at peak time", "connectivity", "broadband", "high"),
        ticket(4, "charged twice on my mobile bill", "billing", "mobile", "low"),
    ]
    index_chunks(qdrant, embedder, [chunk for t in tickets for chunk in ticket_chunks(t)])
    retrieval = InProcessRetrieval(Searcher(qdrant, embedder, RetrievalSettings()))
    # The stand-in embedder only counts words, so a long complaint looks less similar to short
    # tickets than it would with the real model. A lower bar keeps this test about the plumbing.
    params = replace(PARAMS, min_similarity=0.3)
    triage = Triage(retrieval, embedder, load_signals(Settings().signals_path), params)
    with TestClient(create_app(triage, Settings(max_complaint_chars=300))) as client:
        yield client, retrieval, embedder


def test_classify_end_to_end(setup):
    client, _, _ = setup
    complaint = (
        "My broadband drops every evening. I work from home and this is costing me. "
        "I am extremely frustrated. Call me on 07700 900123."
    )
    response = client.post("/classify", json={"complaint": complaint})
    body = response.json()
    assert response.status_code == 200, response.text
    assert body["category"]["label"] == "connectivity"
    assert body["product"]["label"] == "broadband"
    assert body["severity"] == {"label": "high", "baseline": "medium", "reasons": ["business_impact"]}
    assert body["sentiment"] == {"label": "negative", "reasons": ["frustrated"]}
    assert {"neighbours", "signals", "total"} <= set(body["timings_ms"])
    assert "07700" not in json.dumps(body)  # personal details are masked before anything else


def test_classify_flags_an_unfamiliar_complaint_for_review(setup):
    client, _, _ = setup
    body = client.post("/classify", json={"complaint": "zebra giraffe elephant rhinoceros"}).json()
    assert body["category"]["label"] == UNKNOWN and body["needs_review"] is True


@pytest.mark.parametrize("complaint", ["", "   ", "x" * 301])
def test_classify_rejects_bad_input(setup, complaint):
    client, _, _ = setup
    assert client.post("/classify", json={"complaint": complaint}).status_code == 422


def test_ready_reflects_dependencies(setup):
    client, retrieval, _ = setup
    assert client.get("/ready").json()["params"]["neighbours"] == 5
    retrieval.is_ready = False
    assert client.get("/ready").status_code == 503


def test_dependency_outage_gives_a_clean_503(setup):
    client, retrieval, _ = setup

    def broken(*args, **kwargs):
        raise ServiceError("retrieval is down")

    retrieval.search = broken
    response = client.post("/classify", json={"complaint": "broadband drops"})
    assert response.status_code == 503


def test_metrics_count_labels_and_unknowns(setup):
    client, _, _ = setup
    client.post(
        "/classify", json={"complaint": "My broadband drops every evening. I am extremely frustrated."}
    )
    client.post("/classify", json={"complaint": "zebra giraffe elephant rhinoceros"})
    metrics = client.get("/metrics").text
    assert 'triage_labels_total{field="category",label="connectivity"}' in metrics
    assert 'triage_unknown_total{field="category"}' in metrics
    assert 'triage_signals_total{signal="frustrated"}' in metrics
