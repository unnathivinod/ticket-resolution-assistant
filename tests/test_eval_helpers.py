"""Fast tests for the scoring helpers used by the eval scripts."""

import pytest

from evals.eval_retrieval import score_query
from evals.eval_triage import load_items, macro_f1, share


def result(source_type, doc_id, scenario, category="cat"):
    return {"source_type": source_type, "id": doc_id, "scenario_id": scenario, "category": category}


QUERY = {"scenario_id": "S01", "category": "cat", "relevant_kb_ids": ["KB-001"]}


def test_score_query_when_everything_is_right():
    results = [result("ticket", "T-1", "S01"), result("kb", "KB-001", "S01")]
    scores = score_query(QUERY, results)
    assert scores["hit@1"] == scores["hit@3"] == scores["context_hit"] == scores["kb_hit@1"] == 1.0
    assert scores["mrr@10"] == 1.0 and scores["category@1"] == 1.0


def test_score_query_rewards_a_late_hit_less():
    tickets = [result("ticket", f"T-{n}", "S99", "other") for n in range(3)] + [
        result("ticket", "T-9", "S01")
    ]
    scores = score_query(QUERY, tickets)
    assert scores["hit@1"] == 0.0 and scores["hit@3"] == 0.0
    assert scores["mrr@10"] == pytest.approx(0.25)
    assert scores["category@1"] == 0.0 and scores["context_hit"] == 0.0


def test_context_hit_counts_the_right_article_too():
    results = [result("ticket", "T-1", "S99"), result("kb", "KB-777", "S99"), result("kb", "KB-001", "S01")]
    scores = score_query(QUERY, results)
    assert scores["hit@3"] == 0.0 and scores["kb_hit@1"] == 0.0
    assert scores["context_hit"] == 1.0  # the right article is among the 2 articles given to the LLM


def test_score_query_with_no_results():
    assert set(score_query(QUERY, []).values()) == {0.0}


def test_macro_f1_treats_every_class_equally():
    perfect = [("a", "a"), ("b", "b")]
    assert macro_f1(perfect) == 1.0
    # "a" is always right (F1 = 0.8 because one "b" was also called "a"); "b" is found half the time.
    pairs = [("a", "a"), ("a", "a"), ("b", "b"), ("b", "a")]
    assert macro_f1(pairs) == pytest.approx((0.8 + 2 / 3) / 2)
    assert macro_f1([]) == 0.0


def test_share():
    assert share([True, False, True, True]) == 0.75
    assert share([]) == 0.0


def test_eval_complaints_are_split_evenly_into_dev_and_test():
    items = load_items()
    assert len(items) == 430
    for group, size in {"known": 360, "new_class": 40, "off_topic": 30}.items():
        dev = [i for i in items if i["group"] == group and i["split"] == "dev"]
        test = [i for i in items if i["group"] == group and i["split"] == "test"]
        assert len(dev) + len(test) == size and abs(len(dev) - len(test)) <= 1
    assert not {i["id"] for i in items if i["split"] == "dev"} & {
        i["id"] for i in items if i["split"] == "test"
    }
    assert all(i["base_severity"] in {"low", "medium", "high"} for i in items if i["group"] == "known")
