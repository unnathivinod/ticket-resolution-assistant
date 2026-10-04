"""Agent UI: paste a customer complaint, get labels, sources and a cited resolution.

A thin page on top of the gateway API. It holds no business logic of its own.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import streamlit as st

from ui.components import cited_ids, facts_html, header_html, resolution_html, sources_html

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://gateway:8000")
API_KEY = os.environ.get("GATEWAY_API_KEY", "dev-local-key")
# Only set for people who may add knowledge (second-line support). Without it the form is hidden.
ADMIN_KEY = os.environ.get("GATEWAY_ADMIN_API_KEY", "")
EXAMPLE = (
    "My broadband drops every evening around 8 and I've already restarted the router twice, "
    "I work from home and this is costing me"
)
STYLE = (Path(__file__).parent / "style.css").read_text(encoding="utf-8")


class GatewayError(RuntimeError):
    pass


def call_gateway(path: str, payload: dict, timeout: float = 300, key: str = API_KEY) -> dict:
    try:
        response = httpx.post(
            f"{GATEWAY_URL}{path}", json=payload, headers={"X-API-Key": key}, timeout=timeout
        )
    except httpx.HTTPError as error:
        raise GatewayError(f"The assistant could not be reached ({error.__class__.__name__}).") from error
    if response.status_code == 429:
        raise GatewayError("Too many requests. Please wait a moment and try again.")
    if response.status_code not in (200, 202):
        detail = response.json().get("detail", response.text) if response.content else response.reason_phrase
        raise GatewayError(f"The assistant returned an error: {detail}")
    return response.json()


@st.cache_data(ttl=300)
def taxonomy() -> dict[str, list[str]]:
    """The ticket classes in use, read from the gateway so new ones appear without a code change."""
    try:
        response = httpx.get(f"{GATEWAY_URL}/v1/taxonomy", headers={"X-API-Key": API_KEY}, timeout=10)
        body = response.json()
        return {kind: [item["name"] for item in body[kind]] for kind in ("category", "product")}
    except (httpx.HTTPError, KeyError, ValueError):
        return {"category": [], "product": []}


def known_categories() -> list[str]:
    return taxonomy()["category"]


@st.cache_data(ttl=20)
def service_status() -> tuple[str, str]:
    """A short health line for the top bar: (ok | warn | bad, text)."""
    try:
        response = httpx.get(f"{GATEWAY_URL}/ready", timeout=5)
        body = response.json()
    except (httpx.HTTPError, ValueError):
        return "bad", "The assistant cannot be reached"
    checks = body.get("checks") or (body.get("detail") or {}).get("checks") or {}
    down = sorted(name for name, healthy in checks.items() if not healthy)
    if down:
        return "warn", "Not available: " + ", ".join(down)
    return ("ok", "All services connected") if response.status_code == 200 else ("warn", "Starting up")


def show_result(result: dict, complaint: str, drafting: bool = False) -> None:
    """Left: what the complaint is and the sources found. Right: the drafted resolution.

    With drafting=True the left side is shown at once (it takes under a second) and the right
    side waits for the language model.
    """
    left, right = st.columns([5, 11], gap="medium")
    with left:
        st.html(facts_html(result["triage"]))
        st.html(sources_html(result["sources"], None if drafting else cited_ids(result["resolution"])))
    with right:
        if drafting:
            with st.spinner("Drafting the resolution. A local model on a CPU can take up to a minute ..."):
                st.session_state["result"] = call_gateway("/v1/resolve", {"complaint": complaint})
            return
        st.html(resolution_html(result))
        show_feedback(result["request_id"])
        show_record_fix(complaint, result["triage"])


def show_feedback(request_id: str) -> None:
    with st.container(key="feedback"):
        st.html(
            '<div class="fb-title">Was this helpful?</div>'
            '<div class="fb-sub">Your answer is saved and used to measure and improve the assistant.</div>'
        )
        if st.session_state.get("feedback_sent") == request_id:
            st.success("Thank you, your feedback was saved.")
            return
        right, none_fits = "The category is right", "None of the categories fits"
        yes, no, category, note = st.columns([1.15, 1.45, 2.6, 2.6], vertical_alignment="bottom")
        picked = category.selectbox(
            "Was the category right?",
            [right, *known_categories(), none_fits],
            format_func=lambda name: name.replace("_", " "),
            key="feedback_category",
        )
        correct_category = None if picked == right else "none_of_these" if picked == none_fits else picked
        comment = note.text_input(
            "Comment (optional)", key="feedback_comment", placeholder="What was missing or wrong?"
        )
        helpful = yes.button("Helpful", icon=":material/thumb_up:", width="stretch")
        not_helpful = no.button("Not helpful", icon=":material/thumb_down:", width="stretch")
        choice = True if helpful else False if not_helpful else None
        if choice is not None:
            try:
                call_gateway(
                    "/v1/feedback",
                    {
                        "request_id": request_id,
                        "helpful": choice,
                        "comment": comment or None,
                        "correct_category": correct_category,
                    },
                    timeout=15,
                )
                st.session_state["feedback_sent"] = request_id
                st.rerun()
            except GatewayError as error:
                st.error(str(error))


def show_record_fix(complaint: str, triage: dict | None) -> None:
    """Closing the loop: once an expert has solved a new problem, saving it teaches the system.

    The next agent who gets the same kind of complaint is shown this fix, a few seconds after
    it is saved. Nothing is retrained. In production this form would sit behind an expert login.
    """
    classes = taxonomy()
    if not ADMIN_KEY or not classes["category"] or not classes["product"]:
        return
    if saved := st.session_state.get("fix_saved"):
        st.success(f"Saved as {saved}. Searchable in a few seconds. Press Resolve again to see it used.")
    with (
        st.container(key="learn"),
        st.expander("Second-line support: record the real fix", icon=":material/add:"),
    ):
        st.caption(
            "Use this when the answer above was wrong or missing and an expert has solved the case. "
            "It is stored as a resolved ticket and becomes searchable in a few seconds, "
            "so the next agent gets it as a suggestion."
        )

        def position(options: list[str], field: str) -> int:
            guess = (triage or {}).get(field, {}).get("label")
            return options.index(guess) if guess in options else 0

        with st.form("record_fix", clear_on_submit=True):
            subject = st.text_input("Short title of the problem", max_chars=200)
            steps = st.text_area("What fixed it, one step per line", height=120)
            left, right = st.columns(2)
            category = left.selectbox(
                "Category",
                classes["category"],
                index=position(classes["category"], "category"),
                format_func=lambda name: name.replace("_", " "),
            )
            product = right.selectbox(
                "Product",
                classes["product"],
                index=position(classes["product"], "product"),
                format_func=lambda name: name.replace("_", " "),
            )
            submitted = st.form_submit_button("Save as a resolved ticket")
        if submitted:
            lines = [line.strip(" -\t") for line in steps.splitlines() if line.strip(" -\t")]
            if len(subject.strip()) < 3 or not lines:
                st.error("Please give a title and at least one step.")
                return
            ticket = {
                "subject": subject.strip(),
                "description": complaint,
                "resolution_steps": lines,
                "category": category,
                "product": product,
            }
            try:
                saved = call_gateway("/v1/tickets", ticket, timeout=30, key=ADMIN_KEY)
                st.session_state["fix_saved"] = saved["id"]
                st.rerun()
            except GatewayError as error:
                st.error(str(error))


st.set_page_config(page_title="Support Ticket Resolution Assistant", layout="wide")
st.html(f"<style>{STYLE}</style>")
meta = st.session_state.get("result", {}).get("meta", {})
st.html(header_html(service_status(), meta.get("model"), meta.get("prompt_version")))

with st.container(key="ask"):
    st.html('<div class="eyebrow">Customer complaint</div>')
    box, go = st.columns([7, 1], vertical_alignment="bottom")
    complaint = box.text_area("Customer complaint", value=EXAMPLE, height=92, label_visibility="collapsed")
    pressed = go.button(
        "Resolve", type="primary", icon=":material/arrow_forward:", icon_position="right", width="stretch"
    )

if pressed:
    st.session_state.pop("result", None)
    st.session_state.pop("feedback_sent", None)
    st.session_state.pop("fix_saved", None)
    st.session_state["complaint"] = complaint
    if not complaint.strip():
        st.error("Please enter a complaint.")
    else:
        try:
            with st.spinner("Reading the complaint and searching past cases ..."):
                quick = call_gateway("/v1/resolve", {"complaint": complaint, "generate": False}, timeout=60)
            preview = st.empty()
            with preview.container():
                show_result(quick, complaint, drafting=True)
            preview.empty()
            st.rerun()  # so the top bar shows the model that answered
        except GatewayError as error:
            st.error(str(error))

if "result" in st.session_state:
    show_result(st.session_state["result"], st.session_state.get("complaint", complaint))
