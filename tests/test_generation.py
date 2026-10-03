"""Fast tests for the generation service: prompt, source handling, answer checks and fallbacks."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from libs.common.llm_client import LLMClient, LLMOutputError, LLMUnavailableError, parse_json_object
from libs.common.service_client import ServiceError
from services.generation.config import Settings
from services.generation.main import create_app
from services.generation.prompt import answer_schema, build_user_prompt, clean_step_text
from services.generation.service import Generator
from services.generation.sources import group_sources, resolution_steps, shorten, statements
from tests.fakes import FakeEmbedder, FakeLLM

STEPS = ["Run a remote line test at peak time.", "Change the router Wi-Fi channel to a less crowded one."]


def ticket(source_id, steps, outcome="Customer confirmed the issue is resolved."):
    numbered = "\n".join(f"{n}. {step}" for n, step in enumerate([*steps, outcome], start=1))
    return {
        "id": source_id,
        "source_type": "ticket",
        "title": "Evening broadband drops",
        "content": f"Problem: My broadband drops every evening.\nResolution steps:\n{numbered}",
    }


def article(source_id, steps):
    numbered = "\n".join(f"{n}. {step}" for n, step in enumerate(steps, start=1))
    return {
        "id": source_id,
        "source_type": "kb",
        "title": "Evening broadband drops",
        "content": f"# Evening broadband drops\n\n## Likely cause\nPeak-hour congestion.\n\n"
        f"## Resolution steps\n{numbered}\n\n## When to escalate\nEscalate after three evenings.\n",
    }


TICKET_A = ticket("T-000001", STEPS)
TICKET_B = ticket("T-000002", STEPS, outcome="Monitored for 24 hours and no further issues were seen.")
KB = article("KB-001", STEPS)
OTHER = ticket("T-000099", ["Refund the duplicate payment.", "Confirm the refund date with the customer."])
SOURCES = [TICKET_A, TICKET_B, KB, OTHER]


def reply(steps, already_tried=(), escalate=False, summary="Likely evening congestion."):
    return {
        "summary": summary,
        "already_tried": list(already_tried),
        "steps": [{"text": text, "citations": list(citations)} for text, citations in steps],
        "escalate": escalate,
        "escalation_reason": "Not covered by the sources." if escalate else "",
    }


def generator(*replies, **overrides):
    llm = FakeLLM(*replies) if replies else None
    settings = Settings(**overrides)
    return Generator(llm, FakeEmbedder(), settings), llm


# ---- reading the sources ----------------------------------------------------------------------


def test_resolution_steps_are_read_from_tickets_and_articles():
    assert resolution_steps(TICKET_A["content"])[:2] == STEPS
    assert resolution_steps(KB["content"]) == STEPS  # stops at the next section
    assert resolution_steps("Problem: no steps here") == []


def test_statements_split_a_source_into_checkable_lines():
    lines = statements(KB["content"])
    assert "Run a remote line test at peak time." in lines
    assert "Peak-hour congestion." in lines
    assert all(not line.startswith(("#", "1.")) for line in lines)


def test_sources_that_say_the_same_thing_are_grouped():
    groups = group_sources(SOURCES, duplicate_overlap=0.6)
    assert [group.ids for group in groups] == [["T-000001", "T-000002", "KB-001"], ["T-000099"]]
    assert groups[0].primary["id"] == "T-000001"  # the best-ranked one represents the group


def test_shorten_cuts_at_a_line_break():
    text = "line one\n" + "x" * 50 + "\nline three"
    assert shorten(text, 1000) == text
    assert shorten(text, 62) == "line one\n" + "x" * 50 + "\n[...]"


# ---- the prompt -------------------------------------------------------------------------------


def test_prompt_shows_ids_and_keeps_data_in_marked_blocks():
    prompt = build_user_prompt(
        "My broadband drops.", {"product": "broadband", "severity": None}, [TICKET_A, KB]
    )
    assert "COMPLAINT:\n<<<\nMy broadband drops.\n>>>" in prompt
    assert "[T-000001] (past ticket)" in prompt and "[KB-001] (knowledge-base article)" in prompt
    assert "TRIAGE: product=broadband" in prompt and "severity" not in prompt


def test_schema_only_allows_citing_the_sources_shown():
    schema = answer_schema(["T-000001", "KB-001"])
    assert schema["properties"]["steps"]["items"]["properties"]["citations"]["items"]["enum"] == [
        "T-000001",
        "KB-001",
    ]


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    [
        ("Run a remote line test (KB-001)", "Run a remote line test."),
        ("Run a remote line test (KB-001).", "Run a remote line test."),
        ("Run a remote line test [T-000481, KB-001]", "Run a remote line test."),
        ("Check error E-102 on the box.", "Check error E-102 on the box."),  # codes in the text stay
        (
            "Move the customer to a less loaded port (if available).",
            "Move the customer to a less loaded port (if available).",
        ),
        ("  ", ""),
    ],
)
def test_source_ids_are_removed_from_the_end_of_a_step(raw, cleaned):
    assert clean_step_text(raw) == cleaned


def test_a_step_with_an_id_in_its_text_is_still_checked_against_the_source():
    gen, _ = generator(reply([(STEPS[0].rstrip(".") + " (T-000001)", ["T-000001"])]))
    result = gen.generate("My broadband drops every evening.", SOURCES)
    assert result["steps"][0]["text"] == STEPS[0] and result["steps"][0]["verified"] is True


# ---- the LLM client ---------------------------------------------------------------------------


def test_parse_json_object_tolerates_extra_text():
    assert parse_json_object('{"a": 1}') == {"a": 1}
    assert parse_json_object('Here you go:\n```json\n{"a": 1}\n```') == {"a": 1}
    for bad in ("no json at all", "{broken", "[1, 2]"):
        with pytest.raises(LLMOutputError):
            parse_json_object(bad)


def llm_client(handler):
    return LLMClient("http://llm/v1", "test-model", transport=httpx.MockTransport(handler))


def test_llm_client_sends_the_schema_and_reads_the_reply():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        body = {"choices": [{"message": {"content": '{"ok": true}'}}], "usage": {"prompt_tokens": 7}}
        return httpx.Response(200, json=body)

    parsed, usage = llm_client(handler).chat_json("system text", "user text", {"type": "object"})
    assert parsed == {"ok": True} and usage == {"prompt_tokens": 7}
    assert seen["model"] == "test-model"
    assert seen["messages"][0] == {"role": "system", "content": "system text"}
    assert seen["response_format"]["json_schema"]["schema"] == {"type": "object"}


def test_llm_client_reports_whether_the_model_is_available():
    available = llm_client(lambda request: httpx.Response(200, json={"data": [{"id": "test-model"}]}))
    missing = llm_client(lambda request: httpx.Response(200, json={"data": [{"id": "another-model"}]}))

    def offline(request):
        raise httpx.ConnectError("refused")

    assert available.ready() is True
    assert missing.ready() is False
    assert llm_client(offline).ready() is False


def test_llm_client_separates_unavailable_from_bad_output():
    def timeout(request):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(LLMUnavailableError):
        llm_client(timeout).chat_json("s", "u", {})
    with pytest.raises(LLMUnavailableError):
        llm_client(lambda request: httpx.Response(500)).chat_json("s", "u", {})
    not_json = {"choices": [{"message": {"content": "I cannot help with that."}}]}
    with pytest.raises(LLMOutputError):
        llm_client(lambda request: httpx.Response(200, json=not_json)).chat_json("s", "u", {})


# ---- checking the model's answer --------------------------------------------------------------


def test_a_good_answer_is_verified_and_credits_every_matching_source():
    gen, llm = generator(reply([(STEPS[0], ["T-000001"]), (STEPS[1], ["T-000001"])]))
    result = gen.generate("My broadband drops every evening.", SOURCES)
    assert result["mode"] == "llm" and result["grounded"] is True and result["fallback_reason"] is None
    assert [step["n"] for step in result["steps"]] == [1, 2]
    assert result["steps"][0]["citations"] == ["T-000001", "T-000002", "KB-001"]
    assert all(step["verified"] and step["support"] > 0.99 for step in result["steps"])
    assert result["model"] == "fake-llm" and result["usage"]["prompt_tokens"] == 120
    assert len(llm.calls) == 1


def test_only_one_copy_of_duplicate_sources_is_shown_to_the_model():
    gen, llm = generator(reply([(STEPS[0], ["T-000001"])]))
    gen.generate("My broadband drops every evening.", SOURCES)
    prompt = llm.calls[0]["user"]
    assert "[T-000001]" in prompt and "[T-000099]" in prompt
    assert "[T-000002]" not in prompt and "[KB-001]" not in prompt
    allowed = llm.calls[0]["schema"]["properties"]["steps"]["items"]["properties"]["citations"]["items"][
        "enum"
    ]
    assert allowed == ["T-000001", "T-000099"]


def test_an_invented_step_is_kept_but_marked_unverified():
    gen, _ = generator(reply([(STEPS[0], ["T-000001"]), ("Offer the customer a free tablet.", ["T-000001"])]))
    result = gen.generate("My broadband drops every evening.", SOURCES)
    assert [step["verified"] for step in result["steps"]] == [True, False]
    assert result["grounded"] is False


def test_a_step_citing_an_unknown_source_is_dropped():
    gen, _ = generator(reply([(STEPS[0], ["T-000001"]), ("Reboot the exchange.", ["KB-999"])]))
    result = gen.generate("My broadband drops every evening.", SOURCES)
    assert len(result["steps"]) == 1 and result["dropped_steps"] == 1
    assert result["grounded"] is True


def test_steps_the_customer_already_tried_are_flagged():
    answer = reply([(STEPS[0], ["T-000001"]), (STEPS[1], ["T-000001"])], already_tried=[STEPS[1]])
    gen, _ = generator(answer)
    result = gen.generate("I already changed the Wi-Fi channel.", SOURCES)
    assert [step["repeats_already_tried"] for step in result["steps"]] == [False, True]
    assert result["already_tried"] == [STEPS[1]]


def test_the_model_may_escalate_instead_of_answering():
    gen, _ = generator(reply([], escalate=True))
    result = gen.generate("Something the sources do not cover.", SOURCES)
    assert result["mode"] == "llm" and result["steps"] == [] and result["escalate"] is True
    assert result["grounded"] is True


def test_when_the_model_says_the_source_is_about_something_else_no_steps_are_shown():
    # The model ignores its own verdict and writes steps anyway. The code enforces the verdict.
    mismatch = {
        **reply([("Refund the duplicate payment.", ["T-000099"])]),
        "customer_problem": "eSIM QR code will not scan",
        "source_problem": "duplicate payment on the bill",
        "same_problem": False,
    }
    gen, llm = generator(mismatch, match_check=True)
    result = gen.generate("My eSIM QR code will not scan.", SOURCES)
    assert result["steps"] == [] and result["escalate"] is True and result["mode"] == "llm"
    assert "duplicate payment on the bill" in result["escalation_reason"]
    assert "eSIM QR code will not scan" in result["summary"]
    assert len(llm.calls) == 1  # a refusal is an answer, not a failure to retry
    assert result["prompt_version"] == "v3" and "same_problem" in llm.calls[0]["system"]


def test_the_match_check_is_off_by_default_because_the_small_model_refused_everything():
    # Measured in evals/eval_answers.py: with the check on, llama3.2:3b refused 20 of 20 known complaints.
    verdict = {**reply([(STEPS[0], ["T-000001"])]), "same_problem": False}
    gen, llm = generator(verdict)
    result = gen.generate("My broadband drops every evening.", SOURCES)
    assert len(result["steps"]) == 1 and result["prompt_version"] == "v2"
    assert "same_problem" not in llm.calls[0]["system"]
    assert "same_problem" not in llm.calls[0]["schema"]["properties"]


def test_the_model_must_judge_the_match_before_it_writes_steps():
    schema = answer_schema(["T-000001"], match_check=True)
    order = list(schema["properties"])
    assert order.index("same_problem") < order.index("steps")
    assert {"customer_problem", "source_problem", "same_problem"} <= set(schema["required"])


def test_an_answer_without_the_verdict_field_still_works():
    # Older replies (or a hosted model that drops the field) are treated as "same problem".
    gen, _ = generator(reply([(STEPS[0], ["T-000001"])]))
    assert len(gen.generate("My broadband drops every evening.", SOURCES)["steps"]) == 1


def test_personal_details_never_reach_the_model():
    gen, llm = generator(reply([(STEPS[0], ["T-000001"])]))
    gen.generate("Broadband drops. Call me on 07700 900123 or sam@example.com.", SOURCES)
    assert "07700" not in llm.calls[0]["user"] and "example.com" not in llm.calls[0]["user"]


# ---- fallbacks --------------------------------------------------------------------------------


def test_unusable_answers_are_retried_once_then_quoted_from_the_source():
    gen, llm = generator(reply([("Reboot the exchange.", ["KB-999"])]))  # never cites a real source
    result = gen.generate("My broadband drops every evening.", SOURCES)
    assert len(llm.calls) == 2
    assert result["mode"] == "extractive" and result["fallback_reason"] == "invalid_output"
    assert [step["text"] for step in result["steps"]] == STEPS  # the article's clean wording
    assert result["steps"][0]["citations"] == ["T-000001", "T-000002", "KB-001"]
    assert result["grounded"] is True and result["model"] is None


def test_a_second_try_can_succeed():
    gen, llm = generator(LLMOutputError("not json"), reply([(STEPS[0], ["T-000001"])]))
    result = gen.generate("My broadband drops every evening.", SOURCES)
    assert len(llm.calls) == 2 and result["mode"] == "llm" and result["fallback_reason"] is None


def test_when_the_model_is_down_we_do_not_wait_twice():
    gen, llm = generator(LLMUnavailableError("timeout"))
    result = gen.generate("My broadband drops every evening.", SOURCES)
    assert len(llm.calls) == 1
    assert result["mode"] == "extractive" and result["fallback_reason"] == "llm_unavailable"
    assert result["steps"]


def test_the_service_works_with_the_model_switched_off():
    gen, _ = generator(llm_enabled=False)
    result = gen.generate("My broadband drops every evening.", [KB, TICKET_A])
    assert result["mode"] == "extractive" and result["fallback_reason"] == "llm_disabled"
    assert [step["text"] for step in result["steps"]] == STEPS


def test_the_fallback_quotes_the_best_ranked_source():
    gen, _ = generator(llm_enabled=False)
    result = gen.generate("Charged twice.", [OTHER, article("KB-019", ["Check the payment history."])])
    assert result["steps"][0]["citations"] == ["T-000099"]
    assert result["steps"][0]["text"] == "Refund the duplicate payment."


def test_the_fallback_uses_the_article_wording_inside_a_group():
    gen, _ = generator(llm_enabled=False)
    result = gen.generate("My broadband drops every evening.", [TICKET_A, KB])
    assert [step["text"] for step in result["steps"]] == STEPS  # no ticket closing note
    assert result["steps"][0]["citations"] == ["T-000001", "KB-001"]


def test_sources_without_steps_lead_to_escalation():
    gen, _ = generator(llm_enabled=False)
    no_steps = {"id": "KB-G03", "source_type": "kb", "title": "Router lights", "content": "Green means on."}
    result = gen.generate("What do the lights mean?", [no_steps])
    assert result["steps"] == [] and result["escalate"] is True and result["mode"] == "extractive"


# ---- the API ----------------------------------------------------------------------------------


@pytest.fixture
def api():
    gen, llm = generator(reply([(STEPS[0], ["T-000001"])]))
    with TestClient(create_app(gen, Settings(max_complaint_chars=200))) as client:
        yield client, gen, llm


def test_generate_endpoint(api):
    client, _, _ = api
    body = {"complaint": "My broadband drops.", "sources": SOURCES, "triage": {"product": "broadband"}}
    response = client.post("/generate", json=body)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["steps"][0]["text"] == STEPS[0] and data["prompt_version"] == "v2"
    assert {"llm", "check", "total"} <= set(data["timings_ms"])


@pytest.mark.parametrize(
    "body",
    [
        {"complaint": "", "sources": [TICKET_A]},
        {"complaint": "   ", "sources": [TICKET_A]},
        {"complaint": "x" * 201, "sources": [TICKET_A]},
        {"complaint": "ok", "sources": []},
        {"complaint": "ok", "sources": [{**TICKET_A, "source_type": "blog"}]},
    ],
)
def test_generate_rejects_bad_input(api, body):
    client, _, _ = api
    assert client.post("/generate", json=body).status_code == 422


def test_ready_reports_whether_the_model_is_available(api):
    client, _, llm = api
    assert client.get("/ready").json()["llm_available"] is True
    llm.is_ready = False
    response = client.get("/ready")
    assert response.status_code == 200 and response.json()["llm_available"] is False  # still usable


def test_embedding_outage_gives_a_clean_503(api):
    client, gen, _ = api

    def broken(*args, **kwargs):
        raise ServiceError("embedding is down")

    gen._embedder.embed = broken
    response = client.post("/generate", json={"complaint": "My broadband drops.", "sources": SOURCES})
    assert response.status_code == 503


def test_metrics_record_how_answers_were_made(api):
    client, _, _ = api
    client.post("/generate", json={"complaint": "My broadband drops.", "sources": SOURCES})
    metrics = client.get("/metrics").text
    assert 'generation_answers_total{mode="llm"}' in metrics
    assert 'generation_tokens_total{kind="prompt"}' in metrics
