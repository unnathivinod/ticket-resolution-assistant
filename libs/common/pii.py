"""Hide personal details (PII) before text is embedded, sent to the LLM, or written to logs.

This is a simple pattern-based masker. It catches the common cases: emails, phone numbers
and long account-style numbers. A production system would add a dedicated PII detection tool.
"""

from __future__ import annotations

import re

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# 10 to 15 digits, optionally separated by spaces or dashes, with an optional leading +
_PHONE = re.compile(r"(?<![\w-])\+?\d(?:[\s-]?\d){9,14}(?![\w-])")
# Any remaining run of 6 or more digits (account numbers, card fragments, order numbers)
_LONG_NUMBER = re.compile(r"(?<![\w-])\d{6,}(?![\w-])")


def mask_pii(text: str) -> str:
    """Replace personal details with placeholders. Safe to call more than once."""
    text = _EMAIL.sub("[EMAIL]", text)
    text = _PHONE.sub("[PHONE]", text)
    text = _LONG_NUMBER.sub("[NUMBER]", text)
    return text
