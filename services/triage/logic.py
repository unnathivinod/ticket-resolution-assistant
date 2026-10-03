"""The triage decision rules, as plain functions with no network calls.

Keeping the rules separate from the service has two benefits: they are easy to test,
and the eval script can try many settings on the same evidence in a fraction of a second.

How a complaint is labelled:
  category, product  the most similar past tickets vote; closer tickets get a bigger vote
  severity           a baseline from similar past tickets, moved up or down by urgency signals
  sentiment          a tone signal if one is found, otherwise neutral
  unknown            when nothing similar enough exists, or the vote is too split, we say so
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

SEVERITIES = ["low", "medium", "high", "critical"]
UNKNOWN = "unknown"
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class Params:
    """Tunable settings. The values in services/triage/params.json are fitted by evals/eval_triage.py."""

    neighbours: int = 15  # how many similar tickets get a vote
    similarity_power: float = 4  # higher = the closest tickets dominate the vote
    min_similarity: float = 0.6  # below this, nothing similar exists -> unknown
    min_confidence: float = 0.4  # below this share of the vote, the result is too split -> unknown
    severity_signal_threshold: float = 0.75  # how close a sentence must be to an urgency example
    sentiment_signal_threshold: float = 0.75  # how close a sentence must be to a tone example

    @classmethod
    def from_file(cls, path: Path) -> Params:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(**{name: data[name] for name in cls.__dataclass_fields__ if name in data})

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Signal:
    name: str
    description: str
    examples: list[str]
    severity_delta: int | None = None
    sentiment: str | None = None


@dataclass(frozen=True)
class SignalMatch:
    """How strongly one signal showed up in the complaint."""

    similarity: float
    matched_text: str
    example: str


@dataclass
class Evidence:
    """Everything gathered about one complaint. decide() turns it into labels."""

    neighbours: list[dict]  # similar past tickets, best first, with similarity and labels
    signals: dict[str, SignalMatch] = field(default_factory=dict)


def load_signals(path: Path) -> list[Signal]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return [Signal(name=name, **spec) for name, spec in data["signals"].items()]


def split_segments(text: str, window: int = 10, stride: int = 5, limit: int = 24) -> list[str]:
    """Cut a complaint into sentences. If it has no punctuation, use overlapping word windows instead."""
    sentences = [part.strip() for part in _SENTENCE_END.split(text.strip()) if part.strip()]
    words = text.split()
    if len(sentences) <= 1 and len(words) > window:
        sentences = [" ".join(words[i : i + window]) for i in range(0, len(words) - stride, stride)]
    return sentences[:limit] or [text.strip()]


def dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def match_signals(
    segments: list[str],
    segment_vectors: list[list[float]],
    signals: list[Signal],
    example_vectors: dict[str, list[list[float]]],
) -> dict[str, SignalMatch]:
    """For each signal, find the complaint sentence that is closest in meaning to one of its examples."""
    matches: dict[str, SignalMatch] = {}
    for signal in signals:
        best = SignalMatch(-1.0, "", "")
        for segment, segment_vector in zip(segments, segment_vectors, strict=True):
            for example, example_vector in zip(signal.examples, example_vectors[signal.name], strict=True):
                similarity = dot(segment_vector, example_vector)
                if similarity > best.similarity:
                    best = SignalMatch(round(similarity, 4), segment, example)
        matches[signal.name] = best
    return matches


def vote(neighbours: list[dict], label_field: str, power: float) -> tuple[str | None, float]:
    """Weighted vote. Returns (winning label, its share of the total vote)."""
    totals: dict[str, float] = {}
    for neighbour in neighbours:
        label = neighbour.get(label_field)
        if label is not None:
            totals[label] = totals.get(label, 0.0) + max(neighbour["similarity"], 0.0) ** power
    if not totals:
        return None, 0.0
    winner = max(totals, key=totals.get)
    return winner, totals[winner] / sum(totals.values())


def baseline_severity(neighbours: list[dict], category: str | None) -> str:
    """How severe this kind of problem is when nothing special is going on.

    Past tickets for the same problem include some that were raised by urgency (a vulnerable
    customer, business impact). Taking the lower quarter of their severities filters those out.
    """
    same_category = [n for n in neighbours if n.get("category") == category] or neighbours
    levels = sorted(SEVERITIES.index(n["severity"]) for n in same_category if n.get("severity") in SEVERITIES)
    if not levels:
        return "medium"
    return SEVERITIES[levels[(len(levels) - 1) // 4]]


def decide(evidence: Evidence, signals: list[Signal], params: Params) -> dict:
    """Turn the evidence into the final labels, with confidence and reasons."""
    neighbours = evidence.neighbours[: params.neighbours]
    top_similarity = neighbours[0]["similarity"] if neighbours else 0.0
    nothing_similar = top_similarity < params.min_similarity

    labels = {}
    for label_field in ("category", "product"):
        label, confidence = vote(neighbours, label_field, params.similarity_power)
        is_unknown = label is None or nothing_similar or confidence < params.min_confidence
        labels[label_field] = {
            "label": UNKNOWN if is_unknown else label,
            "confidence": round(confidence, 3),
            "best_guess": label,
        }

    fired = {
        signal.name: evidence.signals[signal.name]
        for signal in signals
        if signal.name in evidence.signals
        and evidence.signals[signal.name].similarity
        >= (
            params.severity_signal_threshold
            if signal.severity_delta is not None
            else params.sentiment_signal_threshold
        )
    }

    # Severity: baseline from similar tickets, then the strongest urgency signal moves it.
    baseline = baseline_severity(neighbours, labels["category"]["best_guess"])
    deltas = {s.name: s.severity_delta for s in signals if s.severity_delta is not None and s.name in fired}
    raising = {name: delta for name, delta in deltas.items() if delta > 0}
    if raising:
        delta = max(raising.values())
        reasons = [name for name, value in raising.items() if value == delta]
    elif deltas:
        delta = min(deltas.values())
        reasons = list(deltas)
    else:
        delta, reasons = 0, []
    level = max(0, min(SEVERITIES.index(baseline) + delta, len(SEVERITIES) - 1))

    # Sentiment: the strongest tone signal, or neutral when none is found.
    tones = [(fired[s.name].similarity, s) for s in signals if s.sentiment is not None and s.name in fired]
    tone = max(tones, key=lambda item: item[0])[1] if tones else None

    return {
        "category": labels["category"],
        "product": labels["product"],
        "severity": {"label": SEVERITIES[level], "baseline": baseline, "reasons": reasons},
        "sentiment": {"label": tone.sentiment if tone else "neutral", "reasons": [tone.name] if tone else []},
        "signals": [
            {"name": name, "similarity": match.similarity, "matched_text": match.matched_text}
            for name, match in sorted(fired.items(), key=lambda item: -item[1].similarity)
        ],
        "neighbours_used": len(neighbours),
        "top_similarity": round(top_similarity, 4),
        "needs_review": labels["category"]["label"] == UNKNOWN,
    }
