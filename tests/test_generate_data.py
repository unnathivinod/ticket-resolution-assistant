"""Checks on the synthetic dataset. Run: docker compose run --rm tools pytest"""

import pytest

from scripts.generate_data import SENTIMENTS, SEVERITIES, build_dataset, load_inputs


@pytest.fixture(scope="module")
def inputs():
    return load_inputs()


@pytest.fixture(scope="module")
def dataset(inputs):
    return build_dataset(seed=42, inputs=inputs)


def test_expected_counts(dataset, inputs):
    regular = [s for s in inputs["scenarios"] if not s.get("holdout")]
    holdout = [s for s in inputs["scenarios"] if s.get("holdout")]
    assert len(dataset["tickets"]) == len(regular) * 40
    assert len(dataset["test_queries"]) == len(regular) * 10
    assert len(dataset["holdout_tickets"]) == len(holdout) * 40
    assert len(dataset["kb_articles"]) == len(regular) + len(inputs["kb_general"])


def test_same_seed_gives_same_data(inputs):
    assert build_dataset(seed=7, inputs=inputs) == build_dataset(seed=7, inputs=inputs)


def test_different_seed_gives_different_data(inputs):
    a = build_dataset(seed=1, inputs=inputs)["tickets"]
    b = build_dataset(seed=2, inputs=inputs)["tickets"]
    assert [t["description"] for t in a] != [t["description"] for t in b]


def test_ids_are_unique(dataset):
    for name, rows in dataset.items():
        ids = [row["id"] for row in rows]
        assert len(ids) == len(set(ids)), f"duplicate ids in {name}"


def test_no_complaint_text_is_repeated(dataset):
    texts = [t["description"] for t in dataset["tickets"] + dataset["holdout_tickets"]]
    texts += [q["complaint"] for q in dataset["test_queries"] + dataset["holdout_test_queries"]]
    assert len(texts) == len(set(texts))


def test_no_leak_between_index_and_test(dataset, inputs):
    """Held-out wordings must never appear inside an indexed ticket, or the evals would be cheating."""
    indexed = " ".join(t["description"].lower() for t in dataset["tickets"])
    for scenario in inputs["scenarios"]:
        for wording in scenario["test_symptoms"]:
            assert wording.lower().rstrip(".") not in indexed


def test_labels_are_valid(dataset, inputs):
    categories = {c["name"] for c in inputs["taxonomy"]["category"]}
    products = {p["name"] for p in inputs["taxonomy"]["product"]}
    for row in dataset["tickets"] + dataset["test_queries"]:
        assert row["category"] in categories
        assert row["product"] in products
        assert row["severity"] in SEVERITIES
        assert row["sentiment"] in SENTIMENTS


def test_holdout_classes_are_not_in_main_data(dataset, inputs):
    known = {c["name"] for c in inputs["taxonomy"]["category"]}
    assert all(t["category"] not in known for t in dataset["holdout_tickets"])
    assert all(t["scenario_id"].startswith("S") for t in dataset["tickets"])


def test_every_test_query_has_matching_indexed_tickets(dataset):
    indexed_scenarios = {t["scenario_id"] for t in dataset["tickets"]}
    kb_ids = {a["id"] for a in dataset["kb_articles"]}
    for query in dataset["test_queries"]:
        assert query["scenario_id"] in indexed_scenarios
        assert set(query["relevant_kb_ids"]) <= kb_ids


def test_test_queries_keep_their_core_problem_sentence(dataset, inputs):
    held_out = {w for s in inputs["scenarios"] for w in s["test_symptoms"]}
    assert all(q["core_problem"] in held_out for q in dataset["test_queries"])


def test_all_severity_and_sentiment_levels_appear(dataset):
    assert {t["severity"] for t in dataset["tickets"]} == set(SEVERITIES)
    assert {t["sentiment"] for t in dataset["tickets"]} == set(SENTIMENTS)


def test_tickets_have_resolution_and_kb_has_steps(dataset):
    assert all(len(t["resolution_steps"]) >= 4 for t in dataset["tickets"])
    assert all("## Resolution steps" in a["body"] for a in dataset["kb_articles"] if a["scenario_id"])
