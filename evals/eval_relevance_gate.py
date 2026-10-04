"""Can a second check tell "we have a fix for this" from "we do not"?

  docker compose run --rm tools python evals/eval_relevance_gate.py     # about 3 minutes, no language model

The problem (measured in eval_answers.py): a complaint about a NEW kind of telecom problem looks
just as similar to the indexed tickets as a known complaint does, so the similarity cut-off lets
it through and the model drafts a confident answer from the wrong source.

The idea tested here: ask a different model for a second opinion. The cross-encoder reads the
complaint and one source TOGETHER and scores how relevant the source is (0 to 1). If that score
is clearly lower for new-class complaints than for known ones, it can be used as a checkpoint.

For three groups (known complaints, new-class complaints, off-topic questions) the script prints
how the score is spread, and then answers the practical question: if we accept wrongly stopping
1%, 2%, 5% or 10% of real, answerable complaints, how many of the others do we stop?
"""

from __future__ import annotations

import json
import os
import statistics
import sys
from pathlib import Path

from libs.common.pii import mask_pii
from libs.common.retrieval_client import RetrievalClient
from libs.common.service_client import ServiceError
from services.gateway.config import Settings

ROOT = Path(__file__).resolve().parents[1]
GENERATED = ROOT / "data" / "generated"
RESULTS_DIR = ROOT / "evals" / "results"

BUDGETS = [0.01, 0.02, 0.05, 0.10, 0.20]  # share of known complaints we accept stopping by mistake
USEFUL_AT = 0.05  # the check is worth switching on if, at this cost, ...
USEFUL_CATCH = 0.5  # ... it stops at least this share of new-class complaints


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def share(flags: list[bool]) -> float:
    return round(sum(flags) / len(flags), 3) if flags else 0.0


def quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def threshold_for_budget(known_scores: list[float], budget: float) -> float:
    """The highest threshold that stops at most `budget` of the known complaints.

    A complaint is stopped when its score is below the threshold, so the threshold is the
    score of the first known complaint we are NOT willing to stop.
    """
    ordered = sorted(known_scores)
    allowed = int(budget * len(ordered))  # how many known complaints may be stopped
    return ordered[min(allowed, len(ordered) - 1)]


def gate_table(scores: dict[str, list[float]], budgets: list[float]) -> list[dict]:
    """For each budget: the threshold, and the share of each group it would stop."""
    rows = []
    for budget in budgets:
        threshold = threshold_for_budget(scores["known"], budget)
        row = {"budget": budget, "threshold": round(threshold, 4)}
        row |= {name: share([value < threshold for value in values]) for name, values in scores.items()}
        rows.append(row)
    return rows


def verdict(rows: list[dict]) -> tuple[bool, dict]:
    """Is the check worth using? Judged at the 5% budget."""
    row = next(row for row in rows if row["budget"] == USEFUL_AT)
    return row["new_class"] >= USEFUL_CATCH, row


def collect(retrieval: RetrievalClient, complaints: list[dict]) -> tuple[list[float], list[float]]:
    """For each complaint: the best similarity and the best relevance among its five sources."""
    similarity, relevance = [], []
    for item in complaints:
        found = retrieval.search(
            mask_pii(item["complaint"]), top_k_tickets=3, top_k_kb=2, score_relevance=True
        )["results"]
        similarity.append(max((r["similarity"] for r in found), default=0.0))
        relevance.append(max((r["relevance"] or 0.0 for r in found), default=0.0))
    return similarity, relevance


def to_markdown(spread: dict, rows: list[dict], combined: list[dict], useful: bool, cut_off: float) -> str:
    lines = [
        "# A second check: does the best source really fit?",
        "",
        "Cross-encoder relevance of the best source, per group (0 = unrelated, 1 = relevant).",
        "",
        "| Group | Lowest 10% | Lowest 25% | Middle | Top 25% |",
        "|---|---|---|---|---|",
    ]
    for name, values in spread.items():
        lines.append(
            f"| {name} | {values['p10']:.3f} | {values['p25']:.3f} "
            f"| {values['p50']:.3f} | {values['p75']:.3f} |"
        )
    lines += [
        "",
        "If we accept stopping this share of answerable complaints by mistake, how much else is stopped?",
        "",
        "| Known complaints stopped (the cost) | Relevance threshold | New-class stopped (want high) "
        "| Off-topic stopped (want high) |",
        "|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['known']:.3f} | {row['threshold']:.3f} "
            f"| {row['new_class']:.3f} | {row['off_topic']:.3f} |"
        )
    lines += [
        "",
        f"Together with the similarity cut-off already in use ({cut_off}): stopped if EITHER check fails.",
        "",
        "| Relevance threshold | Known stopped | New-class stopped | Off-topic stopped |",
        "|---|---|---|---|",
    ]
    for row in combined:
        lines.append(
            f"| {row['threshold']:.3f} | {row['known']:.3f} "
            f"| {row['new_class']:.3f} | {row['off_topic']:.3f} |"
        )
    lines += [
        "",
        "**Verdict: "
        + (
            "useful.** At a cost of 5% of answerable complaints it stops at least half of the new-class ones."
            if useful
            else "not useful.** It does not separate new-class complaints from known ones well "
            "enough to pay for the answerable complaints it would stop."
        ),
    ]
    return "\n".join(lines)


def main() -> None:
    retrieval = RetrievalClient(os.environ.get("RETRIEVAL_URL", "http://retrieval:8002"), timeout=120)
    groups = {
        "known": read_jsonl(GENERATED / "test_queries.jsonl"),
        "new_class": read_jsonl(GENERATED / "holdout_test_queries.jsonl"),
        "off_topic": read_jsonl(GENERATED / "out_of_scope.jsonl"),
    }
    similarity, relevance = {}, {}
    for name, complaints in groups.items():
        print(f"Scoring {len(complaints)} {name} complaints ...")
        try:
            similarity[name], relevance[name] = collect(retrieval, complaints)
        except ServiceError as error:
            sys.exit(f"The retrieval service could not be used ({error}). Run: docker compose up -d")

    spread = {
        name: {
            "p10": quantile(values, 0.10),
            "p25": quantile(values, 0.25),
            "p50": statistics.median(values),
            "p75": quantile(values, 0.75),
        }
        for name, values in relevance.items()
    }
    rows = gate_table(relevance, BUDGETS)
    useful, at_five = verdict(rows)

    # The check would run next to the similarity cut-off, so also show the two together.
    cut_off = Settings().min_similarity
    combined = []
    for row in rows:
        stopped = {
            name: share(
                [
                    s < cut_off or r < row["threshold"]
                    for s, r in zip(similarity[name], relevance[name], strict=True)
                ]
            )
            for name in groups
        }
        combined.append({"threshold": row["threshold"], **stopped})

    table = to_markdown(spread, rows, combined, useful, cut_off)
    print("\n" + table)
    if useful:
        print(
            f"\nTo switch it on: add  MIN_RELEVANCE={at_five['threshold']}  to .env, then run "
            "'docker compose up -d'."
        )
    else:
        print("\nLeave MIN_RELEVANCE=0 (off). The result is still worth keeping: it rules this idea out.")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "relevance_gate.md").write_text(table + "\n", encoding="utf-8", newline="\n")
    (RESULTS_DIR / "relevance_gate.json").write_text(
        json.dumps({"spread": spread, "alone": rows, "with_similarity": combined, "useful": useful}, indent=2)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print("\nSaved to evals/results/relevance_gate.md and relevance_gate.json")


if __name__ == "__main__":
    main()
