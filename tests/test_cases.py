"""Fast tests for signing in, for who may see which cases, and for recording how a case ended."""

import re
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from services.gateway.auth import hash_password, issue_token, read_token, verify_password
from services.gateway.cases_api import DECISIONS, LOGINS
from services.gateway.config import Settings
from services.gateway.main import REJECTED, create_app
from services.gateway.orchestrator import Orchestrator
from tests.fakes import InMemoryCache, InMemoryQueue, InMemoryStore
from tests.test_gateway import FakeGeneration, FakeRetrieval, FakeTriage, Limiter, source
from ui.components import (
    ago,
    case_counters_html,
    cases_table_html,
    decided_html,
    decision_html,
    who_html,
)
from ui.roles import MAY, ROLE_NAMES, role_may

KEY = {"X-API-Key": "test-key"}
SECRET = "test-secret"
DEMO_PASSWORD = "demo1234"  # the three accounts the database file creates
COMPLAINT = "My broadband drops every evening. Call me on 07700 900123."
BILLING = "I was charged twice on my bill this month."
PEOPLE = [
    ("priya", "Priya S", "agent", "agent-pass"),
    ("kavya", "Kavya M", "agent", "other-pass"),
    ("arun", "Arun K", "expert", "expert-pass"),
    ("meera", "Meera R", "engineer", "engineer-pass"),
]
PRIYA = {"username": "priya", "display_name": "Priya S", "role": "agent"}


# ---- passwords and tokens: plain functions -----------------------------------------------------------


def test_a_password_is_stored_as_a_salted_hash_and_never_as_itself():
    first, second = hash_password("agent-pass", rounds=1000), hash_password("agent-pass", rounds=1000)
    assert "agent-pass" not in first
    assert first != second  # a new random salt each time, so equal passwords do not look equal
    assert first.split("$")[:2] == ["pbkdf2_sha256", "1000"]


def test_only_the_right_password_matches():
    stored = hash_password("agent-pass", rounds=1000)
    assert verify_password("agent-pass", stored)
    assert not verify_password("agent-pasS", stored)
    assert not verify_password("", stored)


def test_an_unknown_user_or_a_damaged_hash_never_matches_and_never_crashes():
    assert not verify_password("no-such-user", None)  # even the password of the stand-in hash
    assert not verify_password("anything", "not-a-hash")
    assert not verify_password("anything", "md5$1$00$00")
    assert not verify_password("anything", "pbkdf2_sha256$many$zz$00")


def test_a_token_says_who_it_belongs_to():
    token = issue_token(PRIYA, SECRET, ttl_seconds=60, now=1000)
    assert read_token(token, SECRET, now=1030) == {"username": "priya", "name": "Priya S", "role": "agent"}


def test_a_token_stops_working_when_its_time_is_over():
    token = issue_token(PRIYA, SECRET, ttl_seconds=60, now=1000)
    assert read_token(token, SECRET, now=1061) is None


def test_a_token_cannot_be_changed_or_made_without_the_secret():
    token = issue_token(PRIYA, SECRET, ttl_seconds=60, now=1000)
    payload, signature = token.split(".")
    as_engineer = issue_token({**PRIYA, "role": "engineer"}, "a-guessed-secret", 60, now=1000)
    assert read_token(as_engineer, SECRET, now=1001) is None  # signed with another secret
    assert read_token(f"{as_engineer.split('.')[0]}.{signature}", SECRET, now=1001) is None  # edited
    assert read_token(payload, SECRET, now=1001) is None  # signature cut off
    for nonsense in (None, "", "abc", "a.b.c", "....."):
        assert read_token(nonsense, SECRET) is None


def test_a_token_with_a_role_that_does_not_exist_is_refused():
    token = issue_token({**PRIYA, "role": "owner"}, SECRET, 60, now=1000)
    assert read_token(token, SECRET, now=1001) is None


def test_the_demo_accounts_in_the_database_file_open_with_the_password_in_the_readme():
    sql = Path("infra/postgres/migrations/003_users_and_cases.sql").read_text(encoding="utf-8")
    stored = dict(re.findall(r"\('(\w+)', '[^']+', '\w+',\s+'(pbkdf2_sha256\$[^']+)'\)", sql))
    assert sorted(stored) == ["arun", "meera", "priya"]
    for username, stored_hash in stored.items():
        assert verify_password(DEMO_PASSWORD, stored_hash), username
    # One password, but a salt per account: the stored hashes must not give away that they match.
    assert len(set(stored.values())) == 3
    readme = Path("README.md").read_text(encoding="utf-8")
    assert f"`{DEMO_PASSWORD}`" in readme


# ---- the endpoints -----------------------------------------------------------------------------------


@pytest.fixture
def setup():
    parts = {
        "triage": FakeTriage(),
        "retrieval": FakeRetrieval(),
        "generation": FakeGeneration(),
        "cache": InMemoryCache(),
        "store": InMemoryStore(),
    }
    for username, name, role, password in PEOPLE:
        parts["store"].add_user(username, name, role, hash_password(password, rounds=1000))
    settings = Settings(
        api_keys="test-key",
        admin_api_keys="admin-key",
        min_similarity=0.7,
        token_secret=SECRET,
        incident_min_similar=3,
        incident_min_similarity=0.875,
        incident_window_minutes=30,
    )
    orchestrator = Orchestrator(
        parts["triage"], parts["retrieval"], parts["generation"], parts["cache"], parts["store"], settings
    )
    with TestClient(create_app(orchestrator, Limiter(), settings, InMemoryQueue())) as client:
        yield client, parts


def sign_in(client, username):
    password = next(password for name, _display, _role, password in PEOPLE if name == username)
    response = client.post("/v1/login", json={"username": username, "password": password}, headers=KEY)
    assert response.status_code == 200, response.text
    return {**KEY, "X-User-Token": response.json()["token"]}


def resolve(client, headers, complaint=COMPLAINT):
    response = client.post("/v1/resolve", json={"complaint": complaint}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["request_id"]


def decide(client, headers, request_id, decision):
    return client.post(f"/v1/cases/{request_id}/decision", json={"decision": decision}, headers=headers)


def cases(client, headers, **query):
    response = client.get("/v1/cases", params=query, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_signing_in_returns_a_token_and_who_the_person_is(setup):
    client, _ = setup
    before = LOGINS.labels("ok")._value.get()
    response = client.post("/v1/login", json={"username": "priya", "password": "agent-pass"}, headers=KEY)
    body = response.json()
    assert response.status_code == 200
    assert body["user"] == {"username": "priya", "name": "Priya S", "role": "agent"}
    assert body["expires_in"] == 8 * 60 * 60
    assert read_token(body["token"], SECRET)["username"] == "priya"
    assert "password" not in response.text
    assert LOGINS.labels("ok")._value.get() == before + 1


def test_the_username_is_not_case_sensitive(setup):
    client, _ = setup
    response = client.post("/v1/login", json={"username": " Priya ", "password": "agent-pass"}, headers=KEY)
    assert response.status_code == 200


def test_a_wrong_password_and_an_unknown_user_get_the_same_answer(setup):
    client, _ = setup
    before = LOGINS.labels("failed")._value.get()
    wrong = client.post("/v1/login", json={"username": "priya", "password": "nope"}, headers=KEY)
    unknown = client.post("/v1/login", json={"username": "nobody", "password": "nope"}, headers=KEY)
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() == {"detail": "Wrong username or password."}
    assert LOGINS.labels("failed")._value.get() == before + 2


def test_an_account_that_was_switched_off_cannot_sign_in(setup):
    client, parts = setup
    parts["store"].users["priya"]["is_active"] = False
    response = client.post("/v1/login", json={"username": "priya", "password": "agent-pass"}, headers=KEY)
    assert response.status_code == 401


def test_signing_in_still_needs_the_api_key_and_both_fields(setup):
    client, _ = setup
    assert client.post("/v1/login", json={"username": "priya", "password": "agent-pass"}).status_code == 401
    assert client.post("/v1/login", json={"username": "priya"}, headers=KEY).status_code == 422
    assert client.post("/v1/login", json={"username": "", "password": "x"}, headers=KEY).status_code == 422


def test_a_database_failure_at_sign_in_is_said_clearly(setup):
    client, parts = setup

    def broken(_username):
        raise RuntimeError("connection refused")

    parts["store"].get_user = broken
    response = client.post("/v1/login", json={"username": "priya", "password": "agent-pass"}, headers=KEY)
    assert response.status_code == 503 and "database" in response.json()["detail"].lower()


def test_a_resolved_complaint_is_stored_with_the_person_who_handled_it(setup):
    client, parts = setup
    resolve(client, sign_in(client, "priya"))
    assert parts["store"].requests[-1]["handled_by"] == "priya"


def test_scripts_without_a_token_still_work_and_their_requests_belong_to_nobody(setup):
    client, parts = setup
    resolve(client, KEY)
    assert parts["store"].requests[-1]["handled_by"] is None
    assert cases(client, sign_in(client, "arun"))["items"] == []  # not a case, even for an expert


def test_a_wrong_or_expired_token_is_refused_instead_of_being_ignored(setup):
    client, _ = setup
    before = REJECTED.labels("bad_session")._value.get()
    expired = issue_token(PRIYA, SECRET, ttl_seconds=60, now=1000)
    for token in ("made-up", expired):
        response = client.post(
            "/v1/resolve", json={"complaint": COMPLAINT}, headers={**KEY, "X-User-Token": token}
        )
        assert response.status_code == 401 and "Sign in again" in response.json()["detail"]
    assert REJECTED.labels("bad_session")._value.get() == before + 2


def test_the_cases_list_needs_a_signed_in_person(setup):
    client, _ = setup
    response = client.get("/v1/cases", headers=KEY)
    assert response.status_code == 401 and response.json()["detail"] == "Sign in first."
    assert client.get("/v1/cases").status_code == 401  # and the API key as well


def test_an_agent_sees_only_their_own_cases(setup):
    client, _ = setup
    priya, kavya = sign_in(client, "priya"), sign_in(client, "kavya")
    mine = resolve(client, priya)
    hers = resolve(client, kavya, BILLING)
    found = cases(client, priya)
    assert found["scope"] == "mine"
    assert [item["request_id"] for item in found["items"]] == [mine]
    assert found["totals"]["handled"] == 1
    assert hers not in str(found)
    assert [item["request_id"] for item in cases(client, kavya)["items"]] == [hers]


@pytest.mark.parametrize("username", ["arun", "meera"])
def test_an_expert_and_an_engineer_see_every_case_with_the_name_of_who_handled_it(setup, username):
    client, _ = setup
    first = resolve(client, sign_in(client, "priya"))
    second = resolve(client, sign_in(client, "kavya"), BILLING)
    found = cases(client, sign_in(client, username))
    assert found["scope"] == "all"
    assert [item["request_id"] for item in found["items"]] == [second, first]  # newest first
    assert [item["handled_by_name"] for item in found["items"]] == ["Kavya M", "Priya S"]


def test_a_case_shows_the_masked_complaint_the_labels_and_what_was_suggested(setup):
    client, _ = setup
    priya = sign_in(client, "priya")
    resolve(client, priya)
    item = cases(client, priya)["items"][0]
    assert "07700 900123" not in item["complaint"] and "[PHONE]" in item["complaint"]
    assert item["category"] == "connectivity_intermittent" and item["severity"] == "high"
    assert item["fix_suggested"] is True and item["decision"] is None and item["incident"] is False


def test_an_agent_records_how_a_case_ended_and_can_change_it(setup):
    client, parts = setup
    priya = sign_in(client, "priya")
    request_id = resolve(client, priya)
    saved = decide(client, priya, request_id, "resolved")
    assert saved.status_code == 200
    assert saved.json() == {
        "request_id": request_id,
        "decision": "resolved",
        "decided_by": "priya",
        "followed": True,
        "decided_by_name": "Priya S",
    }
    assert cases(client, priya)["items"][0]["decision"] == "resolved"
    changed = decide(client, priya, request_id, "escalated").json()
    assert changed["decision"] == "escalated" and changed["followed"] is False
    assert parts["store"].requests[-1]["decision"] == "escalated"


def test_an_agent_cannot_decide_someone_elses_case_and_is_not_told_it_exists(setup):
    client, parts = setup
    hers = resolve(client, sign_in(client, "kavya"))
    response = decide(client, sign_in(client, "priya"), hers, "resolved")
    missing = decide(client, sign_in(client, "priya"), str(uuid.uuid4()), "resolved")
    assert response.status_code == missing.status_code == 404
    assert response.json() == missing.json() == {"detail": "No such case."}
    assert parts["store"].requests[-1].get("decision") is None


def test_an_expert_can_decide_any_case_but_not_a_request_made_by_a_script(setup):
    client, parts = setup
    hers = resolve(client, sign_in(client, "kavya"))
    arun = sign_in(client, "arun")
    assert decide(client, arun, hers, "escalated").json()["decided_by"] == "arun"
    by_script = resolve(client, KEY, BILLING)
    assert decide(client, arun, by_script, "resolved").status_code == 404


def test_a_decision_needs_a_person_a_real_id_and_one_of_the_two_answers(setup):
    client, _ = setup
    priya = sign_in(client, "priya")
    request_id = resolve(client, priya)
    assert decide(client, KEY, request_id, "resolved").status_code == 401
    assert decide(client, priya, "not-an-id", "resolved").status_code == 422
    assert decide(client, priya, request_id, "closed").status_code == 422


def test_the_filter_and_the_totals_cover_the_same_cases(setup):
    client, _ = setup
    priya = sign_in(client, "priya")
    first, second, _third = (resolve(client, priya, text) for text in (COMPLAINT, BILLING, "No signal."))
    decide(client, priya, first, "resolved")
    decide(client, priya, second, "escalated")
    everything = cases(client, priya)
    assert everything["totals"] == {
        "handled": 3,
        "resolved": 1,
        "escalated": 1,
        "open": 1,
        "followed_rate": 0.5,  # a fix was drafted for both: one followed, one escalated instead
    }
    assert [item["request_id"] for item in cases(client, priya, status="resolved")["items"]] == [first]
    assert [item["request_id"] for item in cases(client, priya, status="escalated")["items"]] == [second]
    assert len(cases(client, priya, status="open")["items"]) == 1
    assert cases(client, priya, status="open")["totals"]["handled"] == 3  # totals ignore the filter
    assert client.get("/v1/cases", params={"status": "lost"}, headers=priya).status_code == 422


def test_no_rate_is_shown_before_any_case_was_decided(setup):
    client, _ = setup
    priya = sign_in(client, "priya")
    assert cases(client, priya)["totals"]["followed_rate"] is None
    resolve(client, priya)
    assert cases(client, priya)["totals"]["followed_rate"] is None


def test_escalating_when_the_assistant_had_no_fix_counts_as_following_it(setup):
    client, parts = setup
    # nothing similar enough: the assistant itself says "escalate"
    parts["retrieval"].search = lambda *a, **k: {"results": [source("T-000001", "ticket", 0.2)]}
    priya = sign_in(client, "priya")
    request_id = resolve(client, priya)
    assert cases(client, priya)["items"][0]["fix_suggested"] is False
    before = DECISIONS.labels("escalated", "true")._value.get()
    assert decide(client, priya, request_id, "escalated").json()["followed"] is True
    assert DECISIONS.labels("escalated", "true")._value.get() == before + 1


def test_decisions_are_counted_for_the_dashboard(setup):
    client, _ = setup
    priya = sign_in(client, "priya")
    request_id = resolve(client, priya)
    followed = DECISIONS.labels("resolved", "true")._value.get()
    against = DECISIONS.labels("escalated", "false")._value.get()
    decide(client, priya, request_id, "resolved")
    decide(client, priya, request_id, "escalated")
    assert DECISIONS.labels("resolved", "true")._value.get() == followed + 1
    assert DECISIONS.labels("escalated", "false")._value.get() == against + 1
    metrics = client.get("/metrics").text
    assert "gateway_case_decisions_total" in metrics and "gateway_logins_total" in metrics


# ---- the page ----------------------------------------------------------------------------------------

CASE = {
    "request_id": "1",
    "complaint": "No internet in Adyar since this morning.",
    "category": "connectivity_outage",
    "severity": "high",
    "fix_suggested": True,
    "incident": False,
    "decision": None,
    "handled_by": "priya",
    "handled_by_name": "Priya S",
    "minutes_ago": 12,
}
TOTALS = {"handled": 4, "resolved": 2, "escalated": 1, "open": 1, "followed_rate": 0.667}


def test_each_role_gets_the_right_parts_of_the_menu():
    assert MAY == {
        "fixes": ("expert", "engineer"),
        "monitoring": ("engineer",),
        "all_cases": ("expert", "engineer"),
    }
    assert set(ROLE_NAMES) == {"agent", "expert", "engineer"}
    assert not role_may("agent", "fixes") and not role_may("agent", "all_cases")
    assert role_may("expert", "all_cases") and not role_may("expert", "monitoring")
    assert role_may("engineer", "monitoring") and not role_may(None, "fixes")


def test_time_is_shown_the_way_people_say_it():
    assert [ago(m) for m in (0, 12, 59, 60, 150, 60 * 24, 60 * 24 * 3)] == [
        "just now",
        "12 min ago",
        "59 min ago",
        "1 h ago",
        "2 h ago",
        "1 d ago",
        "3 d ago",
    ]


def test_a_decision_is_shown_as_open_resolved_or_escalated():
    assert "Open" in decision_html(None) and "state open" in decision_html(None)
    assert "Resolved" in decision_html("resolved") and "state res" in decision_html("resolved")
    assert "Escalated" in decision_html("escalated") and "state esc" in decision_html("escalated")
    assert "state open" in decision_html("<b>odd</b>") and "<b>" not in decision_html("<b>odd</b>")
    assert "Recorded for this case" in decided_html("resolved")


def test_the_counters_say_whose_cases_they_count():
    mine = case_counters_html(TOTALS, "mine", "Last 24 hours")
    assert "Handled by you" in mine and "last 24 hours" in mine
    assert "<strong>67%</strong>" in mine and "of 3 decided cases" in mine
    assert "50% of handled" in mine and "25% of handled" in mine
    assert "Handled on the desk" in case_counters_html(TOTALS, "all", "Last 7 days")


def test_the_counters_work_before_anything_was_handled_or_decided():
    empty = {"handled": 0, "resolved": 0, "escalated": 0, "open": 0, "followed_rate": None}
    html = case_counters_html(empty, "mine", "Last 24 hours")
    assert "none yet" in html and "no case has been decided yet" in html and "%" not in html
    one = case_counters_html({**TOTALS, "resolved": 1, "escalated": 0}, "mine", "Last 24 hours")
    assert "of 1 decided case<" in one


def test_only_people_who_see_every_case_get_the_handled_by_column():
    mine = cases_table_html([CASE], show_handler=False)
    everyone = cases_table_html([CASE], show_handler=True)
    assert "Handled by" not in mine and "Priya S" not in mine and "cases mine" in mine
    assert "Handled by" in everyone and "Priya S" in everyone and "cases all" in everyone
    assert "Connectivity outage" in mine and "12 min ago" in mine and "Fix drafted" in mine
    assert "1 case." in mine and "2 cases." in cases_table_html([CASE, CASE], show_handler=False)


def test_a_case_that_ended_differently_from_the_suggestion_is_marked():
    def row(**changes):
        return cases_table_html([{**CASE, **changes}], show_handler=False)

    assert "differs" not in row()  # still open: nothing to compare
    assert "differs" not in row(decision="resolved")
    assert "differs" in row(decision="escalated")
    assert "differs" in row(fix_suggested=False, decision="resolved")
    assert "differs" not in row(fix_suggested=False, decision="escalated")
    assert "Escalate" in row(fix_suggested=False) and "Fix drafted" not in row(fix_suggested=False)


def test_a_case_that_was_part_of_an_incident_says_so():
    assert "Part of a possible incident" in cases_table_html([{**CASE, "incident": True}], False)
    assert "Part of a possible incident" not in cases_table_html([CASE], False)


def test_an_empty_list_and_missing_labels_are_shown_plainly():
    assert "No cases here yet" in cases_table_html([], show_handler=True)
    bare = cases_table_html([{**CASE, "category": None, "severity": None}], show_handler=False)
    assert "Unlabelled" in bare


def test_text_in_a_case_cannot_inject_html():
    nasty = {**CASE, "complaint": "<script>alert(1)</script>", "handled_by_name": "<b>x</b>"}
    html = cases_table_html([nasty], show_handler=True)
    assert "<script>" not in html and "<b>x" not in html and "&lt;script&gt;" in html


def test_the_person_in_the_menu_is_shown_with_initials_and_role():
    html = who_html({"name": "Priya S"}, "Agent")
    assert ">PS<" in html and "Priya S" in html and "Agent" in html
    assert ">M<" in who_html({"name": "Meera"}, "Engineer")
    assert "<i>" not in who_html({"name": "<i>x</i>"}, "Agent")
