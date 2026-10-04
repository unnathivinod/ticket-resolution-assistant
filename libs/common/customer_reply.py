"""The message an agent sends back to the customer, written without a language model.

Used in two places, so the "Draft reply" button always returns something the agent can edit:
  - the generation service, when no model could write the reply
  - the gateway, when the generation service itself cannot be reached
"""

from __future__ import annotations

GREETING = "Hello,"
SIGN_OFF = "Kind regards,\n[Agent name]"
ESCALATED_LINE = (
    "I have passed your case to our specialist team, and they will contact you with the next steps."
)
FOLLOW_UP_LINE = (
    "If the problem continues after this, please reply to this message and we will look into it further."
)


def usable_steps(resolution: dict | None) -> tuple[list[str], int]:
    """The steps that may be told to a customer, and how many were left out.

    A step is left out when its cited source does not back it, or when it repeats something
    the customer already tried. A customer should only ever be told checked steps.
    """
    steps = (resolution or {}).get("steps") or []
    kept = [step["text"] for step in steps if step.get("verified") and not step.get("repeats_already_tried")]
    return kept, len(steps) - len(kept)


def template_reply(steps: list[str], sentiment: str | None = None, escalated: bool = False) -> str:
    """A plain, polite reply filled with the steps. No model is involved."""
    opening = "Thank you for contacting us."
    if sentiment == "negative":
        opening += " I am sorry for the trouble this has caused you."
    lines = [GREETING, "", opening, ""]
    if steps:
        lines.append("Here is what we will do to resolve this:")
        lines += [f"{number}. {text}" for number, text in enumerate(steps, start=1)]
        lines.append("")
    lines += [ESCALATED_LINE if escalated or not steps else FOLLOW_UP_LINE, "", SIGN_OFF]
    return "\n".join(lines)
