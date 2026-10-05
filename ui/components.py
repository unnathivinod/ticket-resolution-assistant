"""The parts of the agent pages that are plain HTML: labels, sources, the resolution, the lists.

Each function takes data from the gateway and returns a string of HTML. They do not call
Streamlit or the network, so they can be tested on their own. Every piece of text that came
from a customer, a ticket or the model goes through esc() before it is put into the page.

Icons are drawn by ui/style.css (classes "ico ico-..."): Streamlit removes inline <svg> from
the HTML it is given.
"""

from __future__ import annotations

from html import escape as esc

SEVERITY_LEVELS = ["low", "medium", "high", "critical"]


def pretty(label: str | None) -> str:
    """'billing_dispute' -> 'Billing dispute'."""
    words = (label or "").replace("_", " ").strip()
    return words[:1].upper() + words[1:]


def icon(name: str) -> str:
    return f'<i class="ico ico-{name}" aria-hidden="true"></i>'


def notice(text: str, kind: str = "warn") -> str:
    """A one-line message inside a card. kind: warn (amber), bad (red) or info (blue)."""
    return f'<div class="note {kind}">{icon("warn")}<span>{esc(text)}</span></div>'


# ---- side menu and page heading --------------------------------------------------------------


def brand_html() -> str:
    return (
        f'<div class="brand"><div class="mark">{icon("chat")}</div>'
        "<div><b>Support Assistant</b><span>Telecom support desk</span></div></div>"
    )


def menu_label_html(text: str) -> str:
    return f'<div class="menu-label">{esc(text)}</div>'


def status_html(status: tuple[str, str], model: str | None = None, seconds: float | None = None) -> str:
    """The foot of the side menu. status is (kind, text) where kind is ok, warn or bad."""
    kind, text = status
    chips = [esc(model)] if model else []
    if seconds is not None:
        chips.append(f"answered in {seconds:.1f} s")
    chips_html = "".join(f"<code>{chip}</code>" for chip in chips)
    return (
        f'<div class="menu-foot"><div><span class="dot {kind}"></span>{esc(text)}</div>'
        f"<div>{chips_html}</div></div>"
    )


def page_title_html(title: str, hint: str = "") -> str:
    return f'<div class="page-h"><h1>{esc(title)}</h1><span>{esc(hint)}</span></div>'


# ---- possible incident --------------------------------------------------------------------------


def _ago(minutes: int) -> str:
    return "just now" if minutes < 1 else f"{minutes} min ago"


def incident_html(incident: dict | None) -> str:
    """One calm line for "several customers are reporting the same thing". Empty when there is none.

    The line opens to show a few of the other complaints, so the agent can judge for themselves.
    """
    if not incident or not incident.get("detected"):
        return ""
    count, minutes = int(incident["similar_recent"]), int(incident["window_minutes"])
    message = (
        f'{icon("alert")}<span class="incident-m"><b>Possible service incident.</b> '
        f"{count} similar complaints were received in the last {minutes} minutes.</span>"
    )
    examples = incident.get("examples") or []
    if not examples:
        return f'<div class="incident"><div class="incident-h">{message}</div></div>'
    rows = "".join(
        f"<li><span>{_ago(int(item['minutes_ago']))}</span><p>{esc(item['text'])}</p></li>"
        for item in examples
    )
    return (
        f'<details class="incident"><summary class="incident-h">{message}'
        '<span class="incident-a"><u class="closed">View similar complaints</u>'
        '<u class="open">Hide</u></span></summary>'
        f"<ul>{rows}</ul></details>"
    )


# ---- what this is ------------------------------------------------------------------------------


def _meter(share: float) -> str:
    width = max(0, min(100, round(share * 100)))
    return f'<div class="meter"><i style="width:{width}%"></i></div>'


def _tile(kind: str, name: str, icon_name: str, value: str, extra: str) -> str:
    return (
        f'<div class="tile {kind}"><div class="tile-top"><div class="tile-k">{name}</div>'
        f'<div class="tile-i">{icon(icon_name)}</div></div>'
        f'<div class="tile-v">{esc(value)}</div>{extra}</div>'
    )


def _confidence_tile(kind: str, name: str, icon_name: str, part: dict) -> str:
    extra = _meter(part["confidence"]) + (
        f'<div class="tile-s">{part["confidence"]:.0%} of similar tickets agree</div>'
    )
    return _tile(kind, name, icon_name, pretty(part["label"]), extra)


def _severity_tile(severity: dict) -> str:
    level = severity["label"]
    position = SEVERITY_LEVELS.index(level) if level in SEVERITY_LEVELS else -1
    serious = position >= 2
    segments = "".join(
        f'<i class="{"hot" if i == position and serious else "on" if i <= position else ""}"></i>'
        for i in range(len(SEVERITY_LEVELS))
    )
    reasons = ", ".join(pretty(reason).lower() for reason in severity.get("reasons", []))
    extra = (
        f'<div class="scale">{segments}</div>'
        f'<div class="tile-s">{esc(pretty(reasons)) if reasons else "No urgency signals"}</div>'
    )
    return _tile(f"t-sev {'hot' if serious else 'calm'}", "Severity", "alert", pretty(level), extra)


def tiles_html(triage: dict | None) -> str:
    """The four 'what this is' tiles: category, product, severity and sentiment."""
    if triage is None:
        return notice("Labels are unavailable right now. The search and the answer still work.")
    sentiment = triage["sentiment"]
    tone = ", ".join(pretty(reason).lower() for reason in sentiment.get("reasons", []))
    # Only a neutral label may say that no tone was found. A negative one with no reason shows no note.
    tone_note = pretty(tone) if tone else "No strong tone found" if sentiment["label"] == "neutral" else ""
    review = ""
    if triage.get("needs_review"):
        guess = pretty(triage["category"].get("best_guess")) or "none"
        review = notice(
            f"This complaint does not clearly match a known category (closest: {guess}). "
            "Please label it manually."
        )
    return (
        '<div class="tiles">'
        + _confidence_tile("t-cat", "Category", "tag", triage["category"])
        + _confidence_tile("t-prod", "Product", "wifi", triage["product"])
        + _severity_tile(triage["severity"])
        + _tile(
            "t-sent",
            "Sentiment",
            "smile",
            pretty(sentiment["label"]),
            f'<div class="tile-s tone">{esc(tone_note)}</div>',
        )
        + "</div>"
        + review
    )


# ---- sources -----------------------------------------------------------------------------------


def cited_ids(resolution: dict | None) -> set[str] | None:
    """The sources the drafted steps refer to. None when no answer was drafted."""
    if not resolution or not resolution.get("steps"):
        return None
    return {source_id for step in resolution["steps"] for source_id in step["citations"]}


def _chip(source_id: str) -> str:
    kind = " kb" if source_id.upper().startswith("KB") else ""
    return f'<span class="chip{kind}">{esc(source_id)}</span>'


def sources_html(sources: list[dict], cited: set[str] | None = None) -> str:
    """The 'Sources found' card. Each row opens to show the full text of the source."""
    rows = []
    for source in sources:
        is_article = source["source_type"] == "kb"
        used = ""
        if cited is not None:
            used = (
                '<span class="used">Used</span>'
                if source["id"] in cited
                else '<span class="unused">Not used</span>'
            )
        dim = " dim" if cited is not None and source["id"] not in cited else ""
        title, text = source["title"], source["text"]
        full = text if text.startswith(title) else f"{title}: {text}"
        rows.append(
            f'<details class="src{dim}"><summary>'
            f'<span class="chip{" kb" if is_article else ""}">{esc(source["id"])}</span>'
            f"<small>{'Article' if is_article else 'Past ticket'}</small>"
            f'<span class="src-t">{esc(title)}</span>'
            f'<span class="sim">{_meter(source["similarity"])}{source["similarity"]:.2f}</span>'
            f"{used}</summary>"
            f'<div class="src-x">{esc(full)}</div></details>'
        )
    count = f"{len(sources)} · best match first · click a row to read it" if sources else "none"
    body = "".join(rows) or '<div class="empty">No similar past case was found.</div>'
    return (
        '<section class="card srcs"><div class="card-h"><h2>Sources found</h2>'
        f"<span>{count}</span></div>{body}</section>"
    )


# ---- suggested resolution ----------------------------------------------------------------------


def resolution_head_html(result: dict | None) -> str:
    """The title of the resolution card, with a badge that says how well the steps are backed."""
    badge = ""
    resolution = result["resolution"] if result else None
    if resolution and resolution["steps"]:
        total = len(resolution["steps"])
        backed = sum(step["verified"] for step in resolution["steps"])
        tone = "ok" if backed == total else "warn"
        badge = (
            f'<span class="badge {tone}">{icon("check" if backed == total else "warn")}'
            f"{backed} of {total} steps backed by a source</span>"
        )
    return f'<div class="res-title"><h2>Suggested resolution</h2>{badge}</div>'


def _step(step: dict) -> str:
    notes = []
    if not step["verified"]:
        notes.append("Not verified against the source")
    if step["repeats_already_tried"]:
        notes.append("The customer already tried this")
    flags = "".join(f'<span class="flag">{note}</span>' for note in notes)
    chips = "".join(_chip(source_id) for source_id in step["citations"])
    return (
        f'<li><div class="num">{int(step["n"])}</div><div><p>{esc(step["text"])}</p>'
        f'<div class="from"><span>From</span>{chips}{flags}</div></div></li>'
    )


def resolution_body_html(result: dict) -> str:
    """Inside the resolution card: likely cause, what was already tried, and the steps."""
    resolution = result["resolution"]
    degraded = result["meta"].get("degraded") or []
    notes = []
    if degraded:
        notes.append(notice("Running with reduced service: " + ", ".join(degraded) + "."))
    if resolution is None or not resolution["steps"]:
        # Nothing similar enough was found, or the model judged the sources to be about another problem.
        summary = resolution["summary"] if resolution else ""
        reason = result["escalation_reason"] or "No resolution could be drafted."
        text = f"{summary} {reason}".strip()
        if "escalate" not in text.lower():
            text += " Please escalate."
        return '<div class="res-notes">' + notice(text, "bad") + "".join(notes) + "</div>"

    if first_choice := result["meta"].get("failover_from"):
        notes.append(
            notice(
                f"The first-choice model ({first_choice}) did not answer. The backup model wrote this.",
                "info",
            )
        )
    if resolution["mode"] == "extractive":
        notes.append(
            notice(
                "The language model was not used. These steps are quoted from the best matching source.",
                "info",
            )
        )
    if result["escalate"]:
        notes.append(notice(f"Escalation recommended: {result['escalation_reason']}"))
    if not resolution["grounded"]:
        notes.append(
            notice("Some steps are not backed by the cited sources. Check them before using this answer.")
        )
    notes_html = f'<div class="res-notes">{"".join(notes)}</div>' if notes else ""

    tried = ""
    if resolution["already_tried"]:
        pills = "".join(f'<span class="pill">{esc(item)}</span>' for item in resolution["already_tried"])
        tried = f"<small>Already tried, not suggested again</small><div>{pills}</div>"
    steps = "".join(_step(step) for step in resolution["steps"])
    return (
        f'{notes_html}<div class="res-body"><div class="cause"><div class="eyebrow">Likely cause</div>'
        f"<p>{esc(resolution['summary'])}</p>{tried}</div>"
        f'<ol class="steps">{steps}</ol></div>'
    )


# ---- reply to the customer ---------------------------------------------------------------------


def reply_head_html(draft: dict | None = None) -> str:
    """The title of the reply card. Once a reply exists, a badge says what it was built from."""
    badge = ""
    if draft:
        used = draft.get("steps_used", 0)
        if used:
            plural = "" if used == 1 else "s"
            badge = f'<span class="badge ok">{icon("check")}Built from {used} checked step{plural}</span>'
        else:
            badge = f'<span class="badge warn">{icon("warn")}No fix included: handed to specialists</span>'
    return f'<div class="res-title"><h2>Reply to the customer</h2>{badge}</div>'


def reply_notes_html(draft: dict) -> str:
    """What the agent should know before sending the reply. Empty when there is nothing to say."""
    notes = []
    left_out = draft.get("steps_left_out", 0)
    if left_out:
        what = "1 step was" if left_out == 1 else f"{left_out} steps were"
        notes.append(
            notice(
                f"{what} left out of this reply: not verified against the source, "
                "or the customer already tried it."
            )
        )
    if draft.get("mode") == "template":
        notes.append(
            notice(
                "The language model was not used. This is a standard reply with the steps filled in.", "info"
            )
        )
    elif first_choice := draft.get("failover_from"):
        notes.append(
            notice(
                f"The first-choice model ({first_choice}) did not answer. The backup model wrote this.",
                "info",
            )
        )
    return f'<div class="reply-notes">{"".join(notes)}</div>' if notes else ""


def drafting_html() -> str:
    """Shown in place of the resolution while the model is writing."""
    lines = "".join(f'<div class="bone" style="width:{width}%"></div>' for width in (92, 78, 86, 64))
    return (
        '<section class="card res-wait"><div class="res-title"><h2>Suggested resolution</h2>'
        '<span class="badge info">Drafting the fix ...</span></div>'
        f'<div class="bones">{lines}</div></section>'
    )


# ---- sign-in ---------------------------------------------------------------------------------


def login_pitch_html() -> str:
    """The left half of the sign-in page: what the tool is, in three lines."""
    points = "".join(
        f"<li><i>{icon('check')}</i>{text}</li>"
        for text in (
            "Checked citations on every step",
            "Escalates when it has no fix",
            "Spots a possible service incident",
        )
    )
    return (
        f'<section class="pitch"><div class="brand"><div class="mark">{icon("chat")}</div>'
        "<div><b>Support Assistant</b><span>Telecom support desk</span></div></div>"
        "<h1>Resolve every complaint with the evidence in front of you.</h1>"
        "<p>Labels, similar past cases and a drafted fix in one screen. "
        "Every step names the ticket or article it came from.</p>"
        f"<ul>{points}</ul>"
        "<small>Internal tool. Personal details are masked before anything is stored.</small></section>"
    )


def login_title_html() -> str:
    return '<div class="login-h"><h1>Sign in</h1><span>Use your support desk account.</span></div>'


def who_html(user: dict, role_name: str) -> str:
    """The signed-in person at the foot of the side menu."""
    initials = "".join(part[:1] for part in user["name"].split()[:2]).upper() or "?"
    return (
        f'<div class="who"><div class="who-a">{esc(initials)}</div>'
        f"<div><b>{esc(user['name'])}</b><span>{esc(role_name)}</span></div></div>"
    )


# ---- cases: how each complaint ended -----------------------------------------------------------

DECISIONS = {"resolved": ("Resolved", "res", "check"), "escalated": ("Escalated", "esc", "arrow")}


def ago(minutes: int) -> str:
    """'just now', '12 min ago', '3 h ago', '2 d ago'."""
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    return f"{minutes // 60} h ago" if minutes < 60 * 24 else f"{minutes // (60 * 24)} d ago"


def decision_html(decision: str | None) -> str:
    """The badge for how a case ended. No decision yet shows as 'Open'."""
    if decision not in DECISIONS:
        return '<span class="state open">Open</span>'
    label, kind, icon_name = DECISIONS[decision]
    return f'<span class="state {kind}">{icon(icon_name)}{label}</span>'


def decided_html(decision: str) -> str:
    """Under the fix, once the agent has recorded how the case ended."""
    return (
        f'<span class="res-note decided">{decision_html(decision)}<span>Recorded for this case.</span></span>'
    )


def case_counters_html(totals: dict, scope: str, period: str) -> str:
    """The five numbers on top of the Cases page."""
    handled, decided = totals["handled"], totals["resolved"] + totals["escalated"]

    def share(count: int) -> str:
        return f"{count / handled:.0%} of handled" if handled else "none yet"

    rate = totals.get("followed_rate")
    followed = (
        f"<strong>{rate:.0%}</strong><span>of {decided} decided case{'' if decided == 1 else 's'}</span>"
        if rate is not None
        else "<strong>&ndash;</strong><span>no case has been decided yet</span>"
    )
    whose = "Handled by you" if scope == "mine" else "Handled on the desk"

    def counter(name: str, number: int, note: str) -> str:
        return f'<div class="kpi"><small>{name}</small><strong>{number}</strong><span>{note}</span></div>'

    return (
        '<div class="kpis">'
        + counter(whose, handled, esc(period.lower()))
        + counter("Resolved", totals["resolved"], share(totals["resolved"]))
        + counter("Escalated", totals["escalated"], share(totals["escalated"]))
        + counter("Open", totals["open"], "no decision yet")
        + f'<div class="kpi accent"><small>Suggestion followed</small>{followed}</div></div>'
    )


def cases_table_html(items: list[dict], show_handler: bool) -> str:
    """The list of cases. `show_handler` adds the column that only experts and engineers get."""
    if not items:
        return (
            '<section class="card cases"><div class="empty">No cases here yet. Resolve a complaint, '
            "then record how it ended.</div></section>"
        )
    layout = "all" if show_handler else "mine"
    head = "".join(
        f"<span>{name}</span>"
        for name in ("When", "Complaint", "Category", "Severity", "Assistant suggested", "Decision")
    ) + ("<span>Handled by</span>" if show_handler else "")
    rows = []
    for item in items:
        fix = item["fix_suggested"]
        suggested = f"{icon('check')}Fix drafted" if fix else f"{icon('arrow')}Escalate"
        # "differs" marks a case in which the person did not do what the assistant suggested.
        differs = item["decision"] is not None and (item["decision"] == "resolved") != fix
        severity = item.get("severity") or ""
        mark = "<small><i></i>Part of a possible incident</small>" if item.get("incident") else ""
        handler = f'<span class="by">{esc(item.get("handled_by_name") or "")}</span>' if show_handler else ""
        rows.append(
            f'<div class="case"><time>{ago(int(item["minutes_ago"]))}</time>'
            f'<div class="case-c"><p>{esc(item["complaint"])}</p>{mark}</div>'
            f'<span class="chip">{esc(pretty(item.get("category")) or "Unlabelled")}</span>'
            f'<span class="sev {esc(severity)}">{esc(pretty(severity))}</span>'
            f'<span class="sug">{suggested}</span>'
            f'<span class="dec">{decision_html(item["decision"])}'
            f"{'<em>differs</em>' if differs else ''}</span>{handler}</div>"
        )
    count = f"{len(items)} case{'' if len(items) == 1 else 's'}. Personal details are masked."
    return (
        f'<section class="card cases {layout}"><div class="case head">{head}</div>'
        f'{"".join(rows)}<div class="cases-f">{count}</div></section>'
    )
