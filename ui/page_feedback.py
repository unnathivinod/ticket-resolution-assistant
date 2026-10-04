"""Rate the last drafted answer. Only the inputs: nothing else is shown on this page."""

from __future__ import annotations

import streamlit as st

from ui.api import GatewayError, call_gateway, taxonomy
from ui.components import notice, page_title_html

CATEGORY_RIGHT, NONE_FITS = "The category is right", "None of the categories fits"


def show() -> None:
    st.html(page_title_html("Feedback", "Rate the answer to the last complaint you resolved"))
    result = st.session_state.get("result")
    if result is None:
        st.html(notice("Resolve a complaint first. Then come back here to rate the answer.", "info"))
        return
    request_id = result["request_id"]
    with st.container(key="feedback"):
        st.html(
            '<div class="card-h"><h2>Was this helpful?</h2></div>'
            '<div class="card-sub">Your answer is saved and used to measure and improve the assistant.</div>'
        )
        if st.session_state.get("feedback_sent") == request_id:
            st.success("Thank you, your feedback was saved.")
            return
        yes, no, category, note = st.columns([1.15, 1.45, 2.6, 2.6], vertical_alignment="bottom")
        picked = category.selectbox(
            "Was the category right?",
            [CATEGORY_RIGHT, *taxonomy()["category"], NONE_FITS],
            format_func=lambda name: name.replace("_", " "),
            key="feedback_category",
        )
        correct_category = (
            None if picked == CATEGORY_RIGHT else "none_of_these" if picked == NONE_FITS else picked
        )
        comment = note.text_input(
            "Comment (optional)", key="feedback_comment", placeholder="What was missing or wrong?"
        )
        helpful = yes.button("Helpful", icon=":material/thumb_up:", width="stretch")
        not_helpful = no.button("Not helpful", icon=":material/thumb_down:", width="stretch")
        choice = True if helpful else False if not_helpful else None
        if choice is None:
            return
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
        except GatewayError as error:
            st.error(str(error))
            return
        st.session_state["feedback_sent"] = request_id
        st.rerun()
