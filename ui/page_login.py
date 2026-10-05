"""The sign-in page. It is the only thing shown until the gateway has accepted a password."""

from __future__ import annotations

import streamlit as st

from ui import auth
from ui.components import login_pitch_html, login_title_html, notice


def show() -> None:
    pitch, form = st.columns([11, 9], gap="large", vertical_alignment="center")
    pitch.html(login_pitch_html())
    with form, st.container(key="login"):
        st.html(login_title_html())
        if note := st.session_state.pop("signin_note", None):
            st.html(notice(note, "info"))
        with st.form("signin", border=False):
            username = st.text_input("Username", autocomplete="username")
            password = st.text_input("Password", type="password", autocomplete="current-password")
            submitted = st.form_submit_button(
                "Sign in",
                type="primary",
                icon=":material/arrow_forward:",
                icon_position="right",
                width="stretch",
            )
        if submitted:
            if not username.strip() or not password:
                st.error("Enter your username and your password.")
            elif problem := auth.sign_in(username, password):
                st.error(problem)
            else:
                st.rerun()
