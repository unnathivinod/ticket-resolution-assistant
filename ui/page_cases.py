"""Cases: the complaints that were handled, and how each one ended.

An agent sees their own cases. An expert or an engineer sees the whole desk. That rule is applied
by the gateway (GET /v1/cases), which tells this page which of the two it sent ("scope").
"""

from __future__ import annotations

import streamlit as st

from ui import auth
from ui.api import GatewayError, read_gateway, record_decision
from ui.components import ago, case_counters_html, cases_table_html, page_title_html

STATUSES = ["All", "Open", "Resolved", "Escalated"]
PERIODS = {"Last 24 hours": 24, "Last 7 days": 24 * 7, "Last 30 days": 24 * 30}


def close_a_case(items: list[dict]) -> None:
    """A case left open on the Resolve page can still be closed here, after the customer answered."""
    waiting = {item["request_id"]: item for item in items if item["decision"] is None}
    if not waiting:
        return

    def label(request_id: str) -> str:
        item = waiting[request_id]
        text = item["complaint"]
        return f"{ago(int(item['minutes_ago']))}: {text[:70]}{'...' if len(text) > 70 else ''}"

    with st.container(key="caseclose", horizontal=True, vertical_alignment="center"):
        st.html('<span class="res-note">Record how an open case ended</span>')
        chosen = st.selectbox(
            "Open case",
            list(waiting),
            format_func=label,
            index=None,
            placeholder="Choose an open case",
            label_visibility="collapsed",
        )
        escalated = st.button("Escalated", icon=":material/arrow_forward:", disabled=chosen is None)
        resolved = st.button("Resolved", type="primary", icon=":material/check:", disabled=chosen is None)
    if chosen and (escalated or resolved):
        try:
            record_decision(chosen, "resolved" if resolved else "escalated")
        except GatewayError as error:
            st.error(str(error))
            return
        st.rerun()


def show() -> None:
    hint = (
        "Every complaint handled on the desk, and how each one ended"
        if auth.may("all_cases")
        else "The complaints you handled, and how each one ended"
    )
    st.html(page_title_html("Cases", hint))
    counters = st.container()  # filled in below, once the filters are known
    with st.container(key="casefilters", horizontal=True, vertical_alignment="center"):
        status = st.segmented_control("Show", STATUSES, default="All", label_visibility="collapsed")
        period = st.selectbox("Period", list(PERIODS), label_visibility="collapsed", width=190)
    status = (status or "All").lower()
    try:
        found = read_gateway(f"/v1/cases?status={status}&hours={PERIODS[period]}")
    except GatewayError as error:
        st.error(str(error))
        return
    counters.html(case_counters_html(found["totals"], found["scope"], period))
    st.html(cases_table_html(found["items"], show_handler=found["scope"] == "all"))
    close_a_case(found["items"])
