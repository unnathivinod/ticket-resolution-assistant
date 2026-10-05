"""Agent UI: the side menu and the pages behind it.

  Sign in               the only page until the gateway has accepted a password
  Resolve a complaint   paste a complaint, get labels, a cited fix and the sources
  Cases                 what was handled and how it ended (an agent sees only their own)
  Feedback              rate the last drafted answer
  Record a fix          second-line support records how a case was really solved   (expert, engineer)
  Monitoring            opens the Grafana dashboard                                (engineer)

A thin layer on top of the gateway API. It holds no business logic of its own. The menu below only
decides what is worth showing: what a person may really get is decided by the gateway.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from ui import auth, nav, page_cases, page_feedback, page_fixes, page_login, page_resolve
from ui.api import GRAFANA_URL, service_status
from ui.components import brand_html, menu_label_html, status_html, who_html

STYLE = (Path(__file__).parent / "style.css").read_text(encoding="utf-8")

st.set_page_config(
    page_title="Support Ticket Resolution Assistant", layout="wide", initial_sidebar_state="expanded"
)
st.html(f"<style>{STYLE}</style>")

nav.PAGES["resolve"] = st.Page(
    page_resolve.show, title="Resolve a complaint", url_path="resolve", default=True
)
nav.PAGES["cases"] = st.Page(page_cases.show, title="Cases", url_path="cases")
nav.PAGES["feedback"] = st.Page(page_feedback.show, title="Feedback", url_path="feedback")
nav.PAGES["fixes"] = st.Page(page_fixes.show, title="Record a fix", url_path="fixes")
current = st.navigation(list(nav.PAGES.values()), position="hidden")

person = auth.user()
if person is None:
    page_login.show()
    st.stop()

with st.sidebar:
    st.html(brand_html())
    st.html(menu_label_html("Agent"))
    st.page_link(nav.PAGES["resolve"], label="Resolve a complaint", icon=":material/bolt:")
    st.page_link(nav.PAGES["cases"], label="Cases", icon=":material/assignment:")
    st.page_link(nav.PAGES["feedback"], label="Feedback", icon=":material/forum:")
    if auth.may("fixes"):
        st.html(menu_label_html("Second-line support"))
        st.page_link(nav.PAGES["fixes"], label="Record a fix", icon=":material/menu_book:")
    if auth.may("monitoring"):
        st.html(menu_label_html("Operations"))
        st.page_link(GRAFANA_URL, label="Monitoring", icon=":material/monitoring:")
    meta = st.session_state.get("result", {}).get("meta", {})
    total_ms = (meta.get("latency_ms") or {}).get("total")
    st.html(
        status_html(
            service_status(),
            meta.get("model"),
            total_ms / 1000 if total_ms is not None and meta.get("model") else None,
        )
    )
    with st.container(
        key="who", horizontal=True, horizontal_alignment="distribute", vertical_alignment="center"
    ):
        st.html(who_html(person, auth.ROLE_NAMES[person["role"]]))
        if st.button("Sign out", type="tertiary"):
            auth.sign_out()
            st.rerun()

current.run()
