"""Agent UI: paste a customer complaint, get labels, sources and a cited resolution.

A thin page on top of the gateway API. It holds no business logic of its own.
"""

from __future__ import annotations

import os

import httpx
import streamlit as st

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://gateway:8000")
API_KEY = os.environ.get("GATEWAY_API_KEY", "dev-local-key")
EXAMPLE = (
    "My broadband drops every evening around 8 and I've already restarted the router twice, "
    "I work from home and this is costing me"
)
SEVERITY_COLOURS = {"low": "green", "medium": "orange", "high": "red", "critical": "red"}


class GatewayError(RuntimeError):
    pass


def call_gateway(path: str, payload: dict, timeout: float = 300) -> dict:
    try:
        response = httpx.post(
            f"{GATEWAY_URL}{path}", json=payload, headers={"X-API-Key": API_KEY}, timeout=timeout
        )
    except httpx.HTTPError as error:
        raise GatewayError(f"The assistant could not be reached ({error.__class__.__name__}).") from error
    if response.status_code == 429:
        raise GatewayError("Too many requests. Please wait a moment and try again.")
    if response.status_code != 200:
        detail = response.json().get("detail", response.text) if response.content else response.reason_phrase
        raise GatewayError(f"The assistant returned an error: {detail}")
    return response.json()


@st.cache_data(ttl=300)
def known_categories() -> list[str]:
    """The ticket categories in use, read from the gateway so new ones appear without a code change."""
    try:
        response = httpx.get(f"{GATEWAY_URL}/v1/taxonomy", headers={"X-API-Key": API_KEY}, timeout=10)
        return [item["name"] for item in response.json()["category"]]
    except (httpx.HTTPError, KeyError, ValueError):
        return []


def show_triage(triage: dict | None) -> None:
    if triage is None:
        st.warning("Labels are unavailable right now. The search and the answer still work.")
        return
    category, product, severity, sentiment = st.columns(4)
    category.metric("Category", triage["category"]["label"].replace("_", " "))
    category.caption(f"confidence {triage['category']['confidence']:.0%}")
    product.metric("Product", triage["product"]["label"].replace("_", " "))
    product.caption(f"confidence {triage['product']['confidence']:.0%}")
    level = triage["severity"]["label"]
    severity.metric("Severity", level)
    reasons = ", ".join(reason.replace("_", " ") for reason in triage["severity"]["reasons"])
    severity.caption(f":{SEVERITY_COLOURS.get(level, 'gray')}[{reasons or 'no urgency signals'}]")
    sentiment.metric("Sentiment", triage["sentiment"]["label"])
    if triage["needs_review"]:
        guess = (triage["category"].get("best_guess") or "none").replace("_", " ")
        st.warning(
            f"This complaint does not clearly match a known category (closest: {guess}). "
            "Please label it manually."
        )


def show_sources(sources: list[dict], expanded: bool = False) -> None:
    with st.expander(f"Sources found ({len(sources)})", expanded=expanded):
        for source in sources:
            kind = "Knowledge base" if source["source_type"] == "kb" else "Past ticket"
            st.markdown(f"**{source['id']}** · {kind} · similarity {source['similarity']:.2f}")
            text, title = source["text"], source["title"]
            st.caption(text if text.startswith(title) else f"{title}: {text}")


def show_resolution(result: dict) -> None:
    resolution = result["resolution"]
    if resolution is None:
        st.error(result["escalation_reason"] or "No resolution could be drafted. Please escalate.")
        return
    if resolution["mode"] == "extractive":
        st.info("The language model was not used. These steps are quoted from the best matching source.")
    st.markdown(f"**Likely cause:** {resolution['summary']}")
    if resolution["already_tried"]:
        st.markdown("**Customer already tried:** " + "; ".join(resolution["already_tried"]))
    for step in resolution["steps"]:
        notes = []
        if not step["verified"]:
            notes.append(":orange[not verified against the source]")
        if step["repeats_already_tried"]:
            notes.append(":orange[customer already tried this]")
        st.markdown(f"{step['n']}. {step['text']}")
        st.caption(
            "Sources: " + ", ".join(step["citations"]) + ("  ·  " + "  ·  ".join(notes) if notes else "")
        )
    if result["escalate"]:
        st.warning(f"Escalation recommended: {result['escalation_reason']}")
    if not resolution["grounded"]:
        st.warning("Some steps are not backed by the cited sources. Check them before using this answer.")


def show_footer(meta: dict) -> None:
    seconds = meta["latency_ms"].get("total", 0) / 1000
    parts = [f"{seconds:.1f}s", "from cache" if meta["cached"] else "fresh answer"]
    if meta.get("model"):
        parts.append(f"model {meta['model']}")
    if meta.get("prompt_version"):
        parts.append(f"prompt {meta['prompt_version']}")
    if meta["degraded"]:
        parts.append("degraded: " + ", ".join(meta["degraded"]))
    st.caption(" · ".join(parts))


def show_feedback(request_id: str) -> None:
    if st.session_state.get("feedback_sent") == request_id:
        st.success("Thank you, your feedback was saved.")
        return
    st.markdown("**Was this helpful?**")
    right, none_fits = "The category is right", "None of the categories fits"
    picked = st.selectbox(
        "Was the category right? If not, pick the right one.",
        [right, *known_categories(), none_fits],
        format_func=lambda name: name.replace("_", " "),
        key="feedback_category",
    )
    correct_category = None if picked == right else "none_of_these" if picked == none_fits else picked
    comment = st.text_input("Comment (optional)", key="feedback_comment")
    helpful, not_helpful, _ = st.columns([1, 1, 4])
    choice = True if helpful.button("Helpful") else False if not_helpful.button("Not helpful") else None
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


st.set_page_config(page_title="Support Ticket Resolution Assistant", layout="wide")
st.title("Support Ticket Resolution Assistant")
st.caption("Paste a customer complaint. The assistant labels it, finds similar past cases and drafts a fix.")

complaint = st.text_area("Customer complaint", value=EXAMPLE, height=130)
if st.button("Resolve", type="primary"):
    st.session_state.pop("result", None)
    st.session_state.pop("feedback_sent", None)
    if not complaint.strip():
        st.error("Please enter a complaint.")
    else:
        try:
            with st.spinner("Reading the complaint and searching past cases ..."):
                quick = call_gateway("/v1/resolve", {"complaint": complaint, "generate": False}, timeout=60)
            preview = st.empty()
            with preview.container():
                show_triage(quick["triage"])
                show_sources(quick["sources"])
            with st.spinner("Drafting the resolution. A local model on a CPU can take up to a minute ..."):
                st.session_state["result"] = call_gateway("/v1/resolve", {"complaint": complaint})
            preview.empty()
        except GatewayError as error:
            st.error(str(error))

if "result" in st.session_state:
    result = st.session_state["result"]
    show_triage(result["triage"])
    st.subheader("Suggested resolution")
    show_resolution(result)
    # Without a drafted answer the sources are all the agent has, so show them opened.
    show_sources(result["sources"], expanded=result["resolution"] is None)
    show_footer(result["meta"])
    st.divider()
    show_feedback(result["request_id"])
