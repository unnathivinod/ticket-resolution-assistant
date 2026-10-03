"""Fast tests for the scoring helpers used by the eval scripts."""

import pytest

from evals.eval_answers import checkpoint_table, judge, spread, summarise
from evals.eval_evolving import summarise_groups, ticket_body
from evals.eval_retrieval import score_query
from evals.eval_triage import load_items, macro_f1, share
from services.ingestion.discovery import Proposal


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


# ---- eval_evolving -----------------------------------------------------------------------------


def test_groups_are_scored_against_the_real_classes():
    labels = {"a": "esim", "b": "esim", "c": "fraud", "d": "fraud", "x": None, "y": None}
    clean = Proposal("esim", ["esim"], ["a", "b"])
    mixed = Proposal("mixed", ["sim"], ["c", "x"])
    summary = summarise_groups([clean, mixed], labels)
    assert summary["groups"] == 2 and summary["pure_groups"] == 1
    assert summary["new_complaints_grouped"] == 0.75  # a, b, c of the four real ones
    assert summary["off_topic_in_a_group"] == 1  # x
    assert summary["classes_found"] == 2

    assert summarise_groups([], labels) == {
        "groups": 0,
        "pure_groups": 0,
        "new_complaints_grouped": 0.0,
        "off_topic_in_a_group": 0,
        "classes_found": 0,
    }


def test_ticket_body_sends_only_fields_the_gateway_accepts():
    ticket = {
        "id": "T-900001",
        "subject": "s",
        "description": "d",
        "resolution_steps": ["one"],
        "category": "esim_management",
        "product": "mobile",
        "severity": "medium",
        "sentiment": None,
        "scenario_id": "H01",
        "created_at": "2026-01-01",
        "contains_pii": False,
    }
    body = ticket_body(ticket)
    assert "created_at" not in body and "contains_pii" not in body and "sentiment" not in body
    assert body["scenario_id"] == "H01" and body["id"] == "T-900001"


# ---- eval_answers ------------------------------------------------------------------------------

SCENARIO_OF = {"T-1": "S01", "KB-001": "S01", "T-2": "S02", "KB-G01": None}


def answer(citations_per_step, verified=True, already_tried=(), sources=("T-1", "T-2"), model="llama"):
    steps = [
        {"citations": list(cited), "verified": verified, "repeats_already_tried": False}
        for cited in citations_per_step
    ]
    resolution = {"mode": "llm", "steps": steps, "already_tried": list(already_tried), "grounded": verified}
    return {
        "resolution": resolution if steps else None,
        "sources": [{"id": source} for source in sources],
        "meta": {"top_similarity": 0.9, "model": model if steps else None},
    }


def test_an_answer_is_right_only_when_it_cites_the_complaints_own_problem():
    assert judge(answer([["T-1"], ["KB-001", "T-1"]]), "S01", SCENARIO_OF)["outcome"] == "right"
    assert judge(answer([["T-1"], ["T-2"]]), "S01", SCENARIO_OF)["outcome"] == "partly"
    assert judge(answer([["T-2"]]), "S01", SCENARIO_OF)["outcome"] == "wrong"
    # A general article belongs to no problem: citing only that is not the fix either.
    assert judge(answer([["KB-G01"]]), "S01", SCENARIO_OF)["outcome"] == "wrong"
    assert judge(answer([]), "S01", SCENARIO_OF)["outcome"] == "escalated"


def test_judge_separates_search_misses_from_model_mistakes():
    model_mistake = judge(answer([["T-2"]], sources=("T-1", "T-2")), "S01", SCENARIO_OF)
    search_miss = judge(answer([["T-2"]], sources=("T-2",)), "S01", SCENARIO_OF)
    assert model_mistake["right_source_found"] is True
    assert search_miss["right_source_found"] is False


def test_judge_counts_unverified_steps_and_whether_the_model_was_asked():
    result = judge(answer([["T-1"], ["T-1"]], verified=False, already_tried=["restart"]), "S01", SCENARIO_OF)
    assert result["unverified_steps"] == 2 and result["grounded"] is False
    assert result["noticed_already_tried"] is True and result["model_used"] is True
    assert judge(answer([]), None, SCENARIO_OF)["model_used"] is False  # stopped before the model


def test_checkpoint_table_shows_who_would_be_stopped_at_each_cut_off():
    table = checkpoint_table({"known": [0.9, 0.8, 0.7], "off_topic": [0.4, 0.6]}, [0.5, 0.75])
    assert table[0] == {"cut_off": 0.5, "known": 0.0, "off_topic": 0.5}
    assert table[1] == {"cut_off": 0.75, "known": 0.333, "off_topic": 1.0}


def test_summarise_counts_outcomes_and_step_quality():
    rows = [
        judge(answer([["T-1"]]), "S01", SCENARIO_OF),
        judge(answer([["T-2"], ["T-2"]], verified=False), "S01", SCENARIO_OF),
        judge(answer([]), "S01", SCENARIO_OF),
    ]
    summary = summarise(rows)
    assert (summary["right"], summary["wrong"], summary["escalated"]) == (1, 1, 1)
    assert summary["answer_drafted"] == 0.667 and summary["every_step_backed"] == 0.5
    assert summary["unverified_steps"] == 0.667  # 2 of the 3 steps
    assert summarise([])["complaints"] == 0


def test_spread_picks_evenly_across_the_list():
    items = [{"id": n} for n in range(100)]
    assert [item["id"] for item in spread(items, 4)] == [0, 25, 50, 75]
    assert spread(items, 0) == [] and len(spread(items[:3], 10)) == 3
