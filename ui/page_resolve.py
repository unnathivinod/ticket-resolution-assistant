"""The main page: paste a complaint, get the labels, the drafted fix, a reply and the sources."""

from __future__ import annotations

import streamlit as st

from ui import nav
from ui.api import GatewayError, call_gateway
from ui.components import (
    cited_ids,
    drafting_html,
    page_title_html,
    reply_head_html,
    reply_notes_html,
    resolution_body_html,
    resolution_head_html,
    sources_html,
    tiles_html,
)

EXAMPLE = (
    "My broadband drops every evening around 8 and I've already restarted the router twice, "
    "I work from home and this is costing me"
)

# One line of a card: text on the left, a control on the right, both centred on the line.
ROW = {"horizontal": True, "horizontal_alignment": "distribute", "vertical_alignment": "center"}


def feedback_link(request_id: str) -> None:
    """Feedback is not needed for every answer, so it lives on its own page, one click away."""
    if st.session_state.get("feedback_sent") == request_id:
        st.html('<span class="badge ok">Thank you, your feedback was saved</span>')
    else:
        st.page_link(nav.PAGES["feedback"], label="Give feedback", icon=":material/rate_review:")


def resolution_card(result: dict) -> None:
    with st.container(key="res"):
        with st.container(key="res_head", horizontal=True, horizontal_alignment="distribute"):
            st.html(resolution_head_html(result))
            feedback_link(result["request_id"])
        st.html(resolution_body_html(result))
        with st.container(key="res_foot", horizontal=True, horizontal_alignment="distribute"):
            drafted = bool(result["resolution"] and result["resolution"]["steps"])
            note = (
                "A draft for you to review before replying to the customer."
                if drafted
                else "Nothing was drafted. The closest sources are listed below."
            )
            st.html(f'<span class="res-note">{note}</span>')
            st.page_link(
                nav.PAGES["fixes"],
                label="Solved it another way? Record the real fix" if drafted else "Record the real fix",
                icon=":material/chevron_right:",
            )


def write_reply(request_id: str) -> None:
    """Ask the gateway for the customer reply of this answer and keep it for the page."""
    try:
        with st.spinner("Writing the reply ..."):
            st.session_state["reply"] = call_gateway("/v1/reply", {"request_id": request_id})
    except GatewayError as error:
        st.error(str(error))
        return
    st.rerun()


def reply_card(result: dict) -> None:
    """The message for the customer. It is written only when the agent asks for it."""
    request_id = result["request_id"]
    draft = st.session_state.get("reply")
    if draft and draft.get("request_id") != request_id:
        draft = None  # a reply that belongs to an earlier complaint
    with st.container(key="reply"):
        if draft is None:
            with st.container(key="reply_ask", **ROW):
                st.html(
                    reply_head_html() + '<span class="res-note">A message you can edit and send, '
                    "built only from the checked steps.</span>"
                )
                if st.button("Draft reply to customer", type="primary", icon=":material/mail:"):
                    write_reply(request_id)
            return

        with st.container(key="reply_head", **ROW):
            st.html(reply_head_html(draft))
            editing = st.toggle("Edit", key=f"reply_edit_{request_id}")
        if notes := reply_notes_html(draft):
            st.html(notes)
        with st.container(key="reply_body"):
            if editing:
                # No key: the edited text is kept in the draft itself, so it survives a page change.
                draft["reply"] = st.text_area(
                    "Reply to the customer", value=draft["reply"], height=320, label_visibility="collapsed"
                )
            else:
                st.code(draft["reply"], language=None, wrap_lines=True)
        with st.container(key="reply_foot", **ROW):
            st.html(
                '<span class="res-note">Read it before sending. '
                "The copy button is at the top right of the message.</span>"
            )
            if st.button("Write it again", type="tertiary", icon=":material/refresh:"):
                write_reply(request_id)


def show() -> None:
    st.html(page_title_html("Resolve a complaint", "Paste what the customer wrote, then press Resolve"))
    with st.container(key="ask"):
        box, go = st.columns([7, 1], vertical_alignment="bottom")
        # The last complaint is kept when the agent visits another page and comes back.
        complaint = box.text_area(
            "Customer complaint",
            value=st.session_state.get("complaint", EXAMPLE),
            height=92,
            label_visibility="collapsed",
        )
        pressed = go.button(
            "Resolve", type="primary", icon=":material/arrow_forward:", icon_position="right", width="stretch"
        )

    if pressed:
        for name in ("result", "reply", "feedback_sent", "fix_saved"):
            st.session_state.pop(name, None)
        st.session_state["complaint"] = complaint
        if not complaint.strip():
            st.error("Please enter a complaint.")
            return
        try:
            with st.spinner("Reading the complaint and searching past cases ..."):
                quick = call_gateway("/v1/resolve", {"complaint": complaint, "generate": False}, timeout=60)
            # Labels and sources take under a second, so they are shown while the fix is drafted.
            st.html(tiles_html(quick["triage"]))
            st.html(drafting_html())
            st.html(sources_html(quick["sources"]))
            st.session_state["result"] = call_gateway("/v1/resolve", {"complaint": complaint})
            st.rerun()
        except GatewayError as error:
            st.error(str(error))
        return

    if "result" in st.session_state:
        result = st.session_state["result"]
        st.html(tiles_html(result["triage"]))
        resolution_card(result)
        reply_card(result)
        st.html(sources_html(result["sources"], cited_ids(result["resolution"])))
