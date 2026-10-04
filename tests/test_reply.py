"""Fast tests for the customer reply: the template, the prompt, the checks and the fallbacks."""

import pytest
from fastapi.testclient import TestClient

from libs.common.customer_reply import ESCALATED_LINE, SIGN_OFF, template_reply, usable_steps
from libs.common.llm_client import LLMOutputError, LLMUnavailableError
from services.generation.config import Settings
from services.generation.main import create_app
from services.generation.reply import REPLY_SCHEMA, build_reply_prompt, clean_reply, tone_for
from services.generation.service import Generator
from tests.fakes import FakeEmbedder, FakeLLM

COMPLAINT = "My broadband drops every evening. Call me on 07700 900123."
STEPS = ["Run a remote line test at peak time.", "Change the router Wi-Fi channel to a less crowded one."]
GOOD = (
    "Hello,\n\nI am sorry for the trouble. We are running a line test from our side and will move your "
    "router to a quieter channel.\n\nKind regards,\n[Agent name]"
)


def step(text, verified=True, repeats=False):
    return {"text": text, "verified": verified, "repeats_already_tried": repeats}


def generator(*replies, **overrides):
    llm = FakeLLM(*replies) if replies else None
    return Generator(llm, FakeEmbedder(), Settings(**overrides)), llm


def with_backup(first_replies, backup_replies):
    first = FakeLLM(*first_replies, model="hosted-model")
    backup = FakeLLM(*backup_replies, model="local-model")
    return Generator(first, FakeEmbedder(), Settings(), backup), first, backup


# ---- which steps a customer may be told --------------------------------------------------------


def test_only_checked_steps_that_are_not_repeats_are_used():
    resolution = {
        "steps": [step("Run a line test."), step("Reset it.", verified=False), step("Restart.", repeats=True)]
    }
    assert usable_steps(resolution) == (["Run a line test."], 2)


def test_no_answer_means_no_steps():
    assert usable_steps(None) == ([], 0) and usable_steps({"steps": []}) == ([], 0)


# ---- the template (no model) -------------------------------------------------------------------


def test_the_template_lists_the_steps_and_ends_with_the_sign_off():
    text = template_reply(STEPS)
    assert text.startswith("Hello,") and text.endswith(SIGN_OFF)
    assert f"1. {STEPS[0]}" in text and f"2. {STEPS[1]}" in text
    assert "sorry" not in text and ESCALATED_LINE not in text


def test_the_template_apologises_to_an_upset_customer():
    assert "I am sorry for the trouble" in template_reply(STEPS, sentiment="negative")


def test_the_template_never_invents_a_fix():
    text = template_reply([])
    assert ESCALATED_LINE in text and "Here is what we will do" not in text
    both = template_reply(STEPS, escalated=True)  # steps exist, and a specialist also follows up
    assert f"1. {STEPS[0]}" in both and ESCALATED_LINE in both


# ---- the prompt ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sentiment", "severity", "expected"),
    [
        ("negative", "low", "one sincere apology"),
        ("positive", "low", "Friendly and warm"),
        ("neutral", "medium", "Polite, clear and brief"),
        (None, None, "Polite, clear and brief"),
        ("neutral", "critical", "urgent for the customer"),
    ],
)
def test_the_tone_follows_the_triage_labels(sentiment, severity, expected):
    assert expected in tone_for(sentiment, severity)
    assert ("urgent" in tone_for(sentiment, severity)) == (severity in ("high", "critical"))


def test_the_prompt_keeps_the_complaint_apart_from_the_steps():
    prompt = build_reply_prompt("My broadband drops.", STEPS, ["Restarted the router"], escalated=False)
    assert "COMPLAINT:\n<<<\nMy broadband drops.\n>>>" in prompt
    assert "ALREADY TRIED: Restarted the router" in prompt and "ESCALATED: no" in prompt
    assert f"STEPS:\n1. {STEPS[0]}\n2. {STEPS[1]}" in prompt


def test_the_prompt_says_so_when_there_is_nothing_to_offer():
    prompt = build_reply_prompt("My broadband drops.", [], [], escalated=True)
    assert "STEPS:\nnone" in prompt and "ESCALATED: yes" in prompt and "nothing mentioned" in prompt


# ---- tidying the model's reply ------------------------------------------------------------------


def test_internal_ids_are_taken_out_of_the_reply():
    text, removed = clean_reply(
        "Hello,\n\nWe ran the test (KB-014) as in T-000481.\n\nKind regards,\n[Agent name]"
    )
    assert removed == 2 and "KB-014" not in text and "T-000481" not in text
    assert "We ran the test as in." in text


def test_ordinary_words_are_left_alone():
    text, removed = clean_reply(
        "Hello,\n\nYour T-Mobile SIM and **Wi-Fi** are fine.\n\nKind regards,\n[Agent name]"
    )
    assert removed == 0 and "T-Mobile" in text and "**" not in text


def test_a_reply_always_shows_where_the_agent_signs():
    assert clean_reply("Hello,\n\nAll done.")[0].endswith(SIGN_OFF)
    assert clean_reply("Hello,\n\nAll done.\n\nKind regards,")[0].endswith(SIGN_OFF)
    assert clean_reply(GOOD)[0].count("Kind regards") == 1
    assert clean_reply("   ") == ("", 0)


# ---- writing the reply --------------------------------------------------------------------------


def test_the_model_writes_the_reply_from_the_checked_steps():
    gen, llm = generator({"reply": GOOD})
    result = gen.draft_reply(COMPLAINT, STEPS, ["Restarted the router"], "negative", "high")
    assert result["reply"] == GOOD and result["mode"] == "llm" and result["model"] == "fake-llm"
    assert result["escalated"] is False and result["prompt_version"] == "r1"
    call = llm.calls[0]
    assert call["schema"] == REPLY_SCHEMA and STEPS[0] in call["user"]
    assert "one sincere apology" in call["system"] and "urgent" in call["system"]


def test_personal_details_never_reach_the_model():
    gen, llm = generator({"reply": GOOD})
    gen.draft_reply(COMPLAINT, STEPS)
    assert "07700" not in llm.calls[0]["user"] and "[PHONE]" in llm.calls[0]["user"]


def test_without_steps_the_reply_hands_over_to_a_person():
    gen, llm = generator({"reply": GOOD})
    result = gen.draft_reply(COMPLAINT, [], escalated=False)
    assert result["escalated"] is True
    assert "STEPS:\nnone" in llm.calls[0]["user"] and "ESCALATED: yes" in llm.calls[0]["user"]


@pytest.mark.parametrize("bad", ["", "Hi.", "x" * 5000])
def test_an_unusable_reply_is_retried_once_then_replaced_by_the_template(bad):
    gen, llm = generator({"reply": bad}, max_reply_chars=600)
    result = gen.draft_reply(COMPLAINT, STEPS, sentiment="negative")
    assert len(llm.calls) == 2
    assert result["mode"] == "template" and result["fallback_reason"] == "invalid_output"
    assert result["model"] is None and f"1. {STEPS[0]}" in result["reply"] and "sorry" in result["reply"]


def test_a_second_try_can_succeed():
    gen, llm = generator(LLMOutputError("not json"), {"reply": GOOD})
    result = gen.draft_reply(COMPLAINT, STEPS)
    assert len(llm.calls) == 2 and result["mode"] == "llm" and result["fallback_reason"] is None


def test_when_the_model_is_down_we_do_not_wait_twice():
    gen, llm = generator(LLMUnavailableError("timeout"))
    result = gen.draft_reply(COMPLAINT, STEPS)
    assert len(llm.calls) == 1
    assert result["mode"] == "template" and result["fallback_reason"] == "llm_unavailable"


def test_the_backup_model_writes_the_reply_when_the_first_model_fails():
    gen, first, backup = with_backup([LLMUnavailableError("rate limited")], [{"reply": GOOD}])
    result = gen.draft_reply(COMPLAINT, STEPS)
    assert len(first.calls) == 1 and len(backup.calls) == 1
    assert result["model"] == "local-model" and result["failover_from"] == "hosted-model"
    assert backup.calls[0]["user"] == first.calls[0]["user"]  # the very same task


def test_the_backup_model_is_not_asked_when_the_first_model_answers():
    gen, first, backup = with_backup([{"reply": GOOD}], [{"reply": GOOD}])
    result = gen.draft_reply(COMPLAINT, STEPS)
    assert backup.calls == [] and result["model"] == "hosted-model" and result["failover_from"] is None


def test_when_both_models_fail_the_template_is_used():
    gen, _, _ = with_backup([LLMUnavailableError("down")], [LLMUnavailableError("down too")])
    result = gen.draft_reply(COMPLAINT, STEPS)
    assert result["mode"] == "template" and result["model"] is None and result["failover_from"] is None


def test_the_reply_works_with_the_model_switched_off():
    gen, _ = generator()
    result = gen.draft_reply(COMPLAINT, [])
    assert result["mode"] == "template" and result["fallback_reason"] == "llm_disabled"
    assert ESCALATED_LINE in result["reply"]


# ---- the API ------------------------------------------------------------------------------------


@pytest.fixture
def api():
    gen, llm = generator({"reply": GOOD})
    with TestClient(create_app(gen, Settings(max_complaint_chars=200))) as client:
        yield client, llm


def test_reply_endpoint(api):
    client, _ = api
    body = {"complaint": "My broadband drops.", "steps": STEPS, "sentiment": "negative", "severity": "high"}
    response = client.post("/reply", json=body)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["reply"] == GOOD and data["mode"] == "llm" and data["escalated"] is False
    assert {"llm", "total"} <= set(data["timings_ms"])
    assert 'generation_replies_total{mode="llm"}' in client.get("/metrics").text


@pytest.mark.parametrize("complaint", ["", "   ", "x" * 201])
def test_reply_rejects_bad_input(api, complaint):
    client, _ = api
    assert client.post("/reply", json={"complaint": complaint, "steps": STEPS}).status_code == 422
