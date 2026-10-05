"""Which roles get which extra parts of the page. Plain data, so it can be tested without the page.

Everyone gets Resolve, Cases and Feedback. This only decides what is worth SHOWING: what a person
may really get is decided by the gateway, from the signed token (services/gateway/auth.py).
"""

from __future__ import annotations

MAY = {
    "fixes": ("expert", "engineer"),  # record how a case was really solved
    "monitoring": ("engineer",),  # the link to the dashboard
    "all_cases": ("expert", "engineer"),  # the whole desk's cases, not only their own
}
ROLE_NAMES = {"agent": "Agent", "expert": "Expert", "engineer": "Engineer"}


def role_may(role: str | None, what: str) -> bool:
    return role in MAY[what]
