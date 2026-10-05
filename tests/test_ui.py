"""The HTML parts of the agent pages (ui/components.py). No browser and no Streamlit needed."""

from ui.components import (
    cited_ids,
    incident_html,
    pretty,
    reply_head_html,
    reply_notes_html,
    resolution_body_html,
    resolution_head_html,
    sources_html,
    status_html,
    tiles_html,
)

TRIAGE = {
    "category": {"label": "connectivity_intermittent", "confidence": 0.92, "best_guess": None},
    "product": {"label": "broadband", "confidence": 1.0},
    "severity": {"label": "high", "reasons": ["business_impact"]},
    "sentiment": {"label": "neutral", "reasons": []},
    "needs_review": False,
}
SOURCES = [
    {
        "id": "KB-001",
        "source_type": "kb",
        "similarity": 0.91,
        "title": "Evening drops",
        "text": "Evening drops: congestion",
    },
    {
        "id": "T-000481",
        "source_type": "ticket",
        "similarity": 0.89,
        "title": "Drops at 8pm",
        "text": "Restart did not help",
    },
]


def answer(**changes) -> dict:
    result = {
        "resolution": {
            "summary": "Peak-hour congestion.",
            "already_tried": ["Restarted the router twice"],
            "steps": [
                {
                    "n": 1,
                    "text": "Run a remote line test.",
                    "citations": ["KB-001"],
                    "verified": True,
                    "repeats_already_tried": False,
                }
            ],
            "mode": "generative",
            "grounded": True,
        },
        "escalate": False,
        "escalation_reason": None,
        "meta": {"degraded": []},
    }
    result.update(changes)
    return result


def test_labels_are_made_readable():
    assert pretty("billing_dispute") == "Billing dispute"
    assert pretty(None) == ""


# ---- what this is ---------------------------------------------------------------------------------


def test_tiles_show_labels_confidence_and_the_severity_scale():
    html = tiles_html(TRIAGE)
    assert "Connectivity intermittent" in html and "92% of similar tickets agree" in html
    assert "width:92%" in html and "Business impact" in html
    assert html.count('class="on"') == 2 and html.count('class="hot"') == 1  # low, medium, then high
    assert "t-sev hot" in html and "No strong tone found" in html
    assert "label it manually" not in html


def test_tiles_ask_for_a_manual_label_when_the_category_is_unclear():
    unclear = TRIAGE | {
        "needs_review": True,
        "category": {"label": "unknown", "confidence": 0.3, "best_guess": "billing_dispute"},
    }
    assert "closest: Billing dispute" in tiles_html(unclear)


def test_without_triage_the_page_says_that_labels_are_missing():
    assert "Labels are unavailable" in tiles_html(None)


def test_low_severity_is_not_shown_as_urgent():
    html = tiles_html(TRIAGE | {"severity": {"label": "low", "reasons": []}})
    assert "No urgency signals" in html and 'class="hot"' not in html and "t-sev calm" in html


def test_a_negative_tone_is_never_described_as_no_tone_found():
    upset = TRIAGE | {"sentiment": {"label": "negative"}}  # the gateway sends the label only
    html = tiles_html(upset)
    assert "Negative" in html and "No strong tone found" not in html


# ---- possible incident ------------------------------------------------------------------------------

INCIDENT = {
    "detected": True,
    "similar_recent": 6,
    "needed": 5,
    "window_minutes": 30,
    "examples": [
        {"text": "No internet in Anna Nagar since morning.", "minutes_ago": 4, "similarity": 0.91},
        {"text": "Broadband down in Anna Nagar <b>again</b>", "minutes_ago": 9, "similarity": 0.88},
    ],
}


def test_no_banner_without_an_incident():
    assert incident_html(None) == ""
    assert incident_html(INCIDENT | {"detected": False}) == ""


def test_the_incident_banner_says_how_many_and_shows_the_other_complaints():
    html = incident_html(INCIDENT)
    assert "Possible service incident." in html
    assert "6 similar complaints were received in the last 30 minutes." in html
    assert "View similar complaints" in html and "4 min ago" in html and "9 min ago" in html
    assert "No internet in Anna Nagar since morning." in html
    assert "<b>again" not in html and "&lt;b&gt;again" in html  # a customer's text cannot inject HTML


def test_the_incident_banner_works_without_examples():
    html = incident_html(INCIDENT | {"examples": []})
    assert "Possible service incident." in html
    assert "<details" not in html and "View similar complaints" not in html  # nothing to open
    moments_ago = [{"text": "No internet.", "minutes_ago": 0, "similarity": 0.9}]
    assert "just now" in incident_html(INCIDENT | {"examples": moments_ago})


# ---- sources ----------------------------------------------------------------------------------------


def test_sources_are_marked_used_or_not_used():
    html = sources_html(SOURCES, cited_ids(answer()["resolution"]))
    assert html.count(">Used<") == 1 and html.count(">Not used<") == 1
    assert "Article" in html and "Past ticket" in html and "0.91" in html
    assert "Drops at 8pm: Restart did not help" in html  # the full text opens under the title


def test_sources_carry_no_used_mark_before_an_answer_exists():
    assert cited_ids(None) is None
    html = sources_html(SOURCES, None)
    assert "Used" not in html and "Not used" not in html


def test_no_sources_is_said_plainly():
    assert "No similar past case was found" in sources_html([])


# ---- suggested resolution ---------------------------------------------------------------------------


def test_resolution_shows_cause_tried_and_steps_with_their_sources():
    html = resolution_body_html(answer())
    assert "Peak-hour congestion." in html and "Restarted the router twice" in html
    assert "Run a remote line test." in html and 'class="chip kb">KB-001<' in html
    assert 'class="note' not in html and 'class="flag"' not in html
    assert "1 of 1 steps backed by a source" in resolution_head_html(answer())


def test_resolution_flags_steps_that_need_a_second_look():
    result = answer()
    result["resolution"]["steps"][0] |= {"verified": False, "repeats_already_tried": True}
    result["resolution"]["grounded"] = False
    html = resolution_body_html(result)
    assert "Not verified against the source" in html and "The customer already tried this" in html
    assert "not backed by the cited sources" in html
    head = resolution_head_html(result)
    assert "0 of 1 steps backed by a source" in head and "badge warn" in head


def test_resolution_without_an_answer_asks_to_escalate():
    refused = answer(resolution=None, escalation_reason="Nothing similar was found.")
    html = resolution_body_html(refused)
    assert "Nothing similar was found. Please escalate." in html and 'class="steps"' not in html
    assert "badge" not in resolution_head_html(refused)
    said_once = resolution_body_html(
        answer(resolution=None, escalation_reason="Escalate to second-line support.")
    )
    assert said_once.lower().count("escalate") == 1


def test_resolution_says_when_the_model_was_not_used_or_a_service_is_down():
    result = answer(meta={"degraded": ["generation"]})
    result["resolution"]["mode"] = "extractive"
    html = resolution_body_html(result)
    assert "The language model was not used" in html and "reduced service: generation" in html


def test_resolution_says_when_the_backup_model_answered():
    html = resolution_body_html(answer(meta={"degraded": [], "failover_from": "openai/gpt-oss-20b"}))
    assert "first-choice model (openai/gpt-oss-20b) did not answer" in html
    assert "backup model" not in resolution_body_html(answer())


def test_text_from_customers_and_the_model_cannot_inject_html():
    result = answer()
    result["resolution"]["summary"] = "<script>alert(1)</script>"
    result["resolution"]["steps"][0]["text"] = "<img src=x onerror=alert(1)>"
    html = resolution_body_html(result)
    assert "<script>" not in html and "<img" not in html and "&lt;script&gt;" in html
    nasty = [SOURCES[0] | {"title": "<b>bold</b>", "text": "<b>bold</b> body"}]
    assert "<b>bold" not in sources_html(nasty)


# ---- reply to the customer ---------------------------------------------------------------------------


def test_the_reply_card_says_what_the_reply_was_built_from():
    assert "badge" not in reply_head_html()  # before the agent asks for a reply
    assert "Built from 2 checked steps" in reply_head_html({"steps_used": 2})
    assert "Built from 1 checked step<" in reply_head_html({"steps_used": 1})
    escalated = reply_head_html({"steps_used": 0})
    assert "No fix included" in escalated and "badge warn" in escalated


def test_the_reply_card_warns_about_left_out_steps_and_a_missing_model():
    assert reply_notes_html({"mode": "llm", "steps_left_out": 0}) == ""
    assert "1 step was left out" in reply_notes_html({"mode": "llm", "steps_left_out": 1})
    assert "2 steps were left out" in reply_notes_html({"mode": "llm", "steps_left_out": 2})
    assert "The language model was not used" in reply_notes_html({"mode": "template"})
    backup = reply_notes_html({"mode": "llm", "failover_from": "<b>hosted</b>"})
    assert "The backup model wrote this" in backup and "<b>hosted" not in backup


# ---- side menu ---------------------------------------------------------------------------------------


def test_the_menu_shows_status_and_the_model_that_answered():
    html = status_html(("ok", "All services connected"), "openai/gpt-oss-20b", 1.44)
    assert "All services connected" in html and "openai/gpt-oss-20b" in html and "answered in 1.4 s" in html
    bare = status_html(("bad", "The assistant cannot be reached"))
    assert "<code>" not in bare and "dot bad" in bare
