"""Preparing the retrieved sources before they are shown to the model."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.*\S)\s*$")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass
class SourceGroup:
    """One distinct piece of knowledge, possibly backed by several sources that say the same thing."""

    primary: dict  # the best-ranked source of the group; this one is shown to the model
    members: list[dict] = field(default_factory=list)  # all sources of the group, primary first

    @property
    def ids(self) -> list[str]:
        return [member["id"] for member in self.members]


def resolution_steps(content: str) -> list[str]:
    """The numbered resolution steps inside a source's content."""
    steps, in_steps = [], False
    for line in content.splitlines():
        if "resolution steps" in line.lower():
            in_steps = True
            continue
        match = _NUMBERED.match(line)
        if in_steps and match:
            steps.append(match.group(1))
        elif in_steps and steps and line.strip():
            break  # a new section started
    return steps


def statements(content: str) -> list[str]:
    """Every line or sentence of a source, used to check that a step is supported by it."""
    parts = []
    for line in content.splitlines():
        line = _NUMBERED.sub(r"\1", line).lstrip("#- ").strip()
        parts.extend(piece.strip() for piece in _SENTENCE_END.split(line) if len(piece.strip()) > 3)
    return parts


def group_sources(sources: list[dict], duplicate_overlap: float) -> list[SourceGroup]:
    """Merge sources that contain (nearly) the same resolution steps.

    Past tickets for the same problem often have identical resolutions, and the knowledge-base
    article repeats them too. Showing the model one copy keeps the prompt short; the others are
    still credited as citations afterwards.
    """
    groups: list[SourceGroup] = []
    step_sets: list[set[str]] = []
    for source in sources:
        steps = {step.lower() for step in resolution_steps(source["content"])}
        for group, existing in zip(groups, step_sets, strict=True):
            smaller = min(len(steps), len(existing))
            if smaller and len(steps & existing) / smaller >= duplicate_overlap:
                group.members.append(source)
                break
        else:
            groups.append(SourceGroup(primary=source, members=[source]))
            step_sets.append(steps)
    return groups


def shorten(content: str, limit: int) -> str:
    """Cut very long content at a line break so the prompt stays within its size budget."""
    if len(content) <= limit:
        return content
    cut = content.rfind("\n", 0, limit)
    return content[: cut if cut > limit // 2 else limit].rstrip() + "\n[...]"


_WORD = re.compile(r"[a-z0-9]+")
_FILLER = frozenset(
    "the and for with that this have has had was were are but not you your she her his its our "
    "they them from into onto out off then than too very also just still already tried try".split()
)


def _key_words(text: str) -> set[str]:
    """The meaningful words of a text, with common endings removed (restarted, restarting -> restart)."""
    words = set()
    for word in _WORD.findall(text.lower()):
        if len(word) < 3 or word in _FILLER:
            continue
        for ending in ("ing", "ed", "es", "s"):
            if word.endswith(ending) and len(word) - len(ending) >= 3:
                word = word[: -len(ending)]
                break
        words.add(word.rstrip("e"))  # change, changed -> chang
    return words


def said_by_customer(item: str, complaint: str) -> bool:
    """Is this "already tried" item really in the complaint?

    A small model sometimes copies an action from a past ticket ("reset the router to factory
    settings") and reports it as something THIS customer tried. An item is kept only when it
    shares at least two meaningful words with the complaint (one, for a one-word item), and
    those are at least a third of its words. Sharing only "router" is not enough.
    """
    wanted = _key_words(item)
    if not wanted:
        return False
    shared = len(wanted & _key_words(complaint))
    return shared >= min(2, len(wanted)) and shared / len(wanted) >= 0.3
