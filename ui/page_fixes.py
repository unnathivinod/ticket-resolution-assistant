"""Second-line support: record how a case was really solved.

Closing the loop: once an expert has solved a new problem, saving it teaches the system. The
next agent who gets the same kind of complaint is shown this fix a few seconds after it is
saved. Nothing is retrained. In production this page would sit behind an expert login.
"""

from __future__ import annotations

import streamlit as st

from ui.api import ADMIN_KEY, GatewayError, call_gateway, taxonomy
from ui.components import notice, page_title_html


def record_form(classes: dict[str, list[str]]) -> None:
    triage = (st.session_state.get("result") or {}).get("triage") or {}

    def position(options: list[str], field: str) -> int:
        guess = triage.get(field, {}).get("label")
        return options.index(guess) if guess in options else 0

    with st.container(key="fixform"):
        if saved := st.session_state.pop("fix_saved", None):
            st.success(
                f"Saved as {saved}. It is searchable in a few seconds: "
                "resolve the complaint again to see it used."
            )
        with st.form("record_fix", clear_on_submit=True, border=False):
            complaint = st.text_area(
                "What the customer said", value=st.session_state.get("complaint", ""), height=92
            )
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
            submitted = st.form_submit_button("Save as a resolved ticket", type="primary")
        if not submitted:
            return
        lines = [line.strip(" -\t") for line in steps.splitlines() if line.strip(" -\t")]
        if len(complaint.strip()) < 10 or len(subject.strip()) < 3 or not lines:
            st.error("Please give the customer's complaint, a title and at least one step.")
            return
        ticket = {
            "subject": subject.strip(),
            "description": complaint.strip(),
            "resolution_steps": lines,
            "category": category,
            "product": product,
        }
        try:
            st.session_state["fix_saved"] = call_gateway("/v1/tickets", ticket, timeout=30, key=ADMIN_KEY)[
                "id"
            ]
            st.rerun()
        except GatewayError as error:
            st.error(str(error))


def show() -> None:
    st.html(
        page_title_html(
            "Record a fix",
            "Record how a case was really solved. The next agent gets it as a suggestion.",
        )
    )
    classes = taxonomy()
    if not ADMIN_KEY:
        st.html(notice("This page needs a second-line key. Set UI_ADMIN_API_KEY in .env.", "info"))
        return
    if not classes["category"] or not classes["product"]:
        st.html(notice("The ticket classes could not be read. Is the gateway running?"))
        return
    record_form(classes)
