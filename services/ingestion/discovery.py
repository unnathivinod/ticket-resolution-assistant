"""Finds groups of similar complaints that no existing ticket class covers.

Why this exists: a single complaint about a brand-new kind of problem looks just as "familiar"
to the search as any other complaint (measured in docs/DESIGN_DECISIONS.md), so it cannot be
spotted one request at a time. But several of them together stand out: they are close to
each other, and an agent has said "none of the categories fits" or triage could not decide.

The steps:
  1. take the complaints that were flagged (by an agent, or by triage as "unknown")
  2. link every two complaints whose meaning is close enough
  3. every set of linked complaints is one group
  4. a group that is big enough becomes a proposal, with a few examples and keywords
A person then approves the proposal (giving the class its real name) or rejects it.

Plain functions with no database or network, so they are easy to test and to evaluate.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

import numpy as np

# Words that say nothing about the problem itself.
STOPWORDS = frozenset(
    """a about after again all already also am an and any are as at be because been before being but by
    can cannot could did do does doing done down each even ever every for from get getting got had has
    have having he hello help her here hey hi him his how however i if in into is it its just know let
    like me more most my need no nor not nothing now of off on once only or other our out over own
    please really regards same she should since so some still such team thank thanks that the their
    them then there these they this those through to today too tried try trying under until up very
    was we were what when where which while who why will with without would yesterday you your yours
    account customer day days week weeks month months morning evening night time times since started
    began happening happened issue problem working work works still keeps keep one two three first
    last new look looking sort fix fixed soon possible already number email phone""".split()
)
_WORD = re.compile(r"[a-z][a-z0-9]+")

# How similar two complaints must be to count as "the same kind of problem".
# Measured by evals/eval_evolving.py with bge-small-en-v1.5: at 0.80 the two new classes merged
# into one group, at 0.85 they came out as two clean groups, at 0.90 nothing grouped at all.
# The usable range is narrow, so re-run the eval after changing the embedding model.
DEFAULT_THRESHOLD = 0.85


@dataclass
class Proposal:
    suggested_name: str
    keywords: list[str]
    member_ids: list[str]
    examples: list[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.member_ids)


def group_similar(vectors: list[list[float]], threshold: float) -> list[list[int]]:
    """Link every pair with similarity >= threshold and return the linked groups (largest first).

    The vectors are unit length, so the dot product is the cosine similarity.
    Comparing every pair is fine for the few thousand complaints flagged in a month. At a much
    larger scale the pairs would come from the vector database's nearest-neighbour search instead.
    """
    if not vectors:
        return []
    matrix = np.asarray(vectors, dtype=np.float32)
    close = np.triu(matrix @ matrix.T >= threshold, k=1)  # each pair once, no self-pairs

    parent = list(range(len(vectors)))

    def root(item: int) -> int:
        while parent[item] != item:
            parent[item] = parent[parent[item]]  # shortcut for next time
            item = parent[item]
        return item

    for first, second in np.argwhere(close):
        parent[root(int(first))] = root(int(second))

    groups: dict[int, list[int]] = {}
    for item in range(len(vectors)):
        groups.setdefault(root(item), []).append(item)
    return sorted(groups.values(), key=lambda group: (-len(group), group[0]))


def _words(text: str) -> set[str]:
    return {word for word in _WORD.findall(text.lower()) if len(word) > 2 and word not in STOPWORDS}


def top_keywords(texts: list[str], limit: int = 4, background: list[str] | None = None) -> list[str]:
    """The words that set a group apart: common inside the group, less common in the other complaints.

    `background` is every flagged complaint. Words that are just as frequent there (for example
    "manager" or "urgent", which turn up in complaints of any kind) are pushed down the list.
    """
    inside = Counter(word for text in texts for word in _words(text))
    overall = Counter(word for text in background or [] for word in _words(text))
    total = len(background) if background else 1

    def score(word: str) -> float:
        return inside[word] / len(texts) - (overall[word] / total if background else 0.0)

    ranked = sorted(inside, key=lambda word: (-score(word), -inside[word], word))
    return ranked[:limit]


def build_proposals(
    items: list[dict],
    vectors: list[list[float]],
    threshold: float = DEFAULT_THRESHOLD,
    min_size: int = 5,
    max_examples: int = 5,
    background: list[str] | None = None,
) -> list[Proposal]:
    """Turn flagged complaints ({"id", "text"}) and their vectors into proposals for new classes.

    `background` is a sample of ordinary complaints, used only to pick keywords that are
    special to a group. Without it, the flagged complaints themselves are used.
    """
    background = background or [item["text"] for item in items]
    proposals = []
    for group in group_similar(vectors, threshold):
        if len(group) < min_size:
            continue  # a handful of odd complaints is not a new class
        texts = [items[index]["text"] for index in group]
        keywords = top_keywords(texts, background=background)
        proposals.append(
            Proposal(
                suggested_name="_".join(keywords[:3]) or "unnamed",
                keywords=keywords,
                member_ids=[str(items[index]["id"]) for index in group],
                examples=texts[:max_examples],
            )
        )
    return proposals
