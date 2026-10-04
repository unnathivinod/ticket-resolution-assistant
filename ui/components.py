"""The parts of the agent page that are plain HTML: header, labels, sources and the resolution.

Each function takes the gateway's answer and returns a string of HTML. They do not call
Streamlit or the network, so they can be tested on their own. Every piece of text that came
from a customer, a ticket or the model goes through esc() before it is put into the page.
"""

from __future__ import annotations

from html import escape as esc

SEVERITY_LEVELS = ["low", "medium", "high", "critical"]
# Icons are drawn by ui/style.css: Streamlit removes inline <svg> from the HTML it is given.
ICON_WARN = '<i class="ico warn-ico" aria-hidden="true"></i>'


def pretty(label: str | None) -> str:
    """'billing_dispute' -> 'Billing dispute'."""
    words = (label or "").replace("_", " ").strip()
    return words[:1].upper() + words[1:]


def notice(text: str, kind: str = "warn") -> str:
    """A one-line message inside a card. kind: warn (amber), bad (red) or info (blue)."""
    return f'<div class="note {kind}">{ICON_WARN}<span>{esc(text)}</span></div>'


def header_html(status: tuple[str, str], model: str | None = None, prompt_version: str | None = None) -> str:
    """The bar at the top. status is (kind, text) where kind is ok, warn or bad."""
    kind, text = status
    labels = [model, f"prompt {prompt_version}" if prompt_version else None]
    chips = "".join(f'<span class="chip">{esc(label)}</span>' for label in labels if label)
    return (
        '<div class="topbar"><div class="brand">'
        '<div class="mark" aria-hidden="true"></div>'
        "<div><b>Support Ticket Resolution Assistant</b>"
        "<span>Telecom support desk · first-line agent view</span></div></div>"
        f'<div class="status"><span class="dot {kind}"></span><span>{esc(text)}</span>{chips}</div></div>'
    )


def _bar(share: float) -> str:
    width = max(0, min(100, round(share * 100)))
    return f'<div class="bar"><i style="width:{width}%"></i></div>'


def _confidence_fact(name: str, part: dict, note: str = "") -> str:
    note_html = f'<div class="fact-s">{esc(note)}</div>' if note else ""
    return (
        f'<div class="fact"><div class="fact-k">{name}</div>'
        f'<div class="fact-top"><div class="fact-v">{esc(pretty(part["label"]))}</div>'
        f'<div class="fact-n">{part["confidence"]:.0%}</div></div>'
        f"{_bar(part['confidence'])}{note_html}</div>"
    )


def _severity_fact(severity: dict) -> str:
    level = severity["label"]
    position = SEVERITY_LEVELS.index(level) if level in SEVERITY_LEVELS else -1
    serious = position >= 2
    segments = "".join(
        f'<i class="{"top" if i == position and serious else "on" if i <= position else ""}"></i>'
        for i in range(len(SEVERITY_LEVELS))
    )
    names = "".join(
        f"<b>{pretty(name)}</b>" if i == position else f"<span>{pretty(name)}</span>"
        for i, name in enumerate(SEVERITY_LEVELS)
    )
    reasons = ", ".join(pretty(reason).lower() for reason in severity.get("reasons", []))
    pill = (
        f'<span class="pill {"hot" if serious else "calm"}">{esc(pretty(reasons))}</span>'
        if reasons
        else '<div class="fact-n plain">No urgency signals</div>'
    )
    tone = "hot" if serious else "calm"
    return (
        '<div class="fact"><div class="fact-k">Severity</div>'
        f'<div class="fact-top"><div class="fact-v {tone}">{esc(pretty(level))}</div>{pill}</div>'
        f'<div class="scale {tone}">{segments}</div>'
        f'<div class="scale-l {tone}">{names}</div></div>'
    )


def facts_html(triage: dict | None) -> str:
    """The 'What this is' card: category, product, severity and sentiment."""
    if triage is None:
        body = notice("Labels are unavailable right now. The search and the answer still work.")
        return f'<section class="card pad"><div class="eyebrow">What this is</div>{body}</section>'
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
        '<section class="card pad"><div class="eyebrow">What this is</div>'
        + _confidence_fact("Category", triage["category"], "Agreement among the most similar past tickets")
        + _confidence_fact("Product", triage["product"])
        + _severity_fact(triage["severity"])
        + '<div class="fact"><div class="fact-k">Sentiment</div><div class="fact-top">'
        f'<div class="fact-v">{esc(pretty(sentiment["label"]))}</div>'
        f'<div class="fact-n plain">{esc(tone_note)}</div></div></div>' + review + "</section>"
    )


def cited_ids(resolution: dict | None) -> set[str] | None:
    """The sources the drafted steps refer to. None when no answer was drafted."""
    if not resolution or not resolution.get("steps"):
        return None
    return {source_id for step in resolution["steps"] for source_id in step["citations"]}


def _chip(source_id: str) -> str:
    kind = " kb" if source_id.upper().startswith("KB") else ""
    return f'<span class="chip{kind}">{esc(source_id)}</span>'


def sources_html(sources: list[dict], cited: set[str] | None = None) -> str:
    """The 'Sources found' card. Each source opens to show its full text."""
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
            f'<details class="src{dim}"><summary><div class="src-top">'
            f'<span class="chip{" kb" if is_article else ""}">{esc(source["id"])}</span>'
            f"<small>{'Article' if is_article else 'Past ticket'}</small>"
            f'<span class="src-sc">{_bar(source["similarity"])}'
            f'<span class="mono">{source["similarity"]:.2f}</span>{used}</span></div>'
            f'<div class="src-t">{esc(title)}</div></summary>'
            f'<div class="src-x">{esc(full)}</div></details>'
        )
    count = f"{len(sources)} · best match first" if sources else "none"
    body = "".join(rows) or '<div class="fact-s">No similar past case was found.</div>'
    return (
        '<section class="card pad"><div class="card-top"><div class="eyebrow">Sources found</div>'
        f'<div class="count">{count}</div></div><div class="srcs">{body}</div></section>'
    )


def _record(name: str, value: str) -> str:
    return f'<div class="rec"><div class="rec-k">{name}</div><div class="rec-v">{value}</div></div>'


def _step(step: dict) -> str:
    notes = []
    if not step["verified"]:
        notes.append("Not verified against the source")
    if step["repeats_already_tried"]:
        notes.append("The customer already tried this")
    flags = "".join(f'<span class="flag">{note}</span>' for note in notes)
    chips = "".join(_chip(source_id) for source_id in step["citations"])
    return (
        f'<li><div class="num">{int(step["n"])}</div><div><div class="step-t">{esc(step["text"])}</div>'
        f'<div class="step-s"><span>From</span>{chips}{flags}</div></div></li>'
    )


def resolution_html(result: dict) -> str:
    """The 'Suggested resolution' card: likely cause, what was already tried, and the steps."""
    head = (
        '<section class="card"><div class="res-head"><h2>Suggested resolution</h2></div>'
        '<div class="res-body">'
    )
    resolution = result["resolution"]
    degraded = result["meta"].get("degraded") or []
    extra = notice("Running with reduced service: " + ", ".join(degraded) + ".") if degraded else ""
    if resolution is None or not resolution["steps"]:
        # Nothing similar enough was found, or the model judged the sources to be about another problem.
        summary = resolution["summary"] if resolution else ""
        reason = result["escalation_reason"] or "No resolution could be drafted."
        text = f"{summary} {reason}".strip()
        if "escalate" not in text.lower():
            text += " Please escalate."
        return head + '<div class="notes">' + notice(text, "bad") + extra + "</div></div></section>"

    records = [_record("Likely cause", f'<p class="cause-t">{esc(resolution["summary"])}</p>')]
    if resolution["already_tried"]:
        tried = "".join(f'<div class="tried-i">{esc(item)}</div>' for item in resolution["already_tried"])
        records.append(_record("Already tried", f'{tried}<div class="tried-n">Not suggested again</div>'))
    steps = "".join(_step(step) for step in resolution["steps"])
    records.append(_record("Recommended steps", f'<ol class="steps">{steps}</ol>'))

    notes = []
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
    notes_html = f'<div class="notes">{"".join(notes)}{extra}</div>' if notes or extra else ""
    return head + notes_html + "".join(records) + "</div></section>"
