"""Measure how well triage labels complaints, and tune its settings.

Run (services up and data loaded):
  docker compose run --rm tools python evals/eval_triage.py --calibrate   # tune, save, then report
  docker compose run --rm tools python evals/eval_triage.py               # report with the saved settings

Three groups of complaints are used:
  known       the 360 held-out test complaints, whose classes the system has seen
  new_class   40 complaints from two classes that are NOT in the index (eSIM, fraud)
  off_topic   30 questions that have nothing to do with telecom

Good triage labels the first group correctly and says "unknown" for the other two.

To keep the numbers honest, every group is split in half. Settings are tuned on one half (dev)
and the reported numbers come from the other half (test), which the tuning never saw.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from dataclasses import replace
from pathlib import Path

import httpx
import yaml

from libs.common.embedding_client import EmbeddingClient
from libs.common.retrieval_client import RetrievalClient
from services.triage.config import Settings
from services.triage.logic import SEVERITIES, UNKNOWN, Evidence, Params, decide, load_signals
from services.triage.service import Triage

ROOT = Path(__file__).resolve().parents[1]
GENERATED = ROOT / "data" / "generated"
RESULTS_DIR = ROOT / "evals" / "results"

NEIGHBOUR_GRID = [3, 5, 10, 15, 25]
POWER_GRID = [1, 2, 4, 8, 16, 32]
SIGNAL_GRID = [round(0.50 + 0.025 * step, 3) for step in range(19)]  # 0.50 ... 0.95
SIMILARITY_GRID = [round(0.30 + 0.025 * step, 3) for step in range(27)]  # 0.30 ... 0.95
CONFIDENCE_GRID = [0.0, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7]
BASELINE_NEIGHBOUR_GRID = [5, 10, 15, 25]
BASELINE_QUANTILE_GRID = [0.0, 0.25, 0.5]
NEVER_UNKNOWN = {"min_similarity": 0.0, "min_confidence": 0.0}
MAX_KNOWN_FLAGGED = 0.10  # at most 10% of normal complaints may be sent for human review


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_items() -> list[dict]:
    """All eval complaints, each tagged with its group and with 'dev' or 'test'."""
    groups = {
        "known": read_jsonl(GENERATED / "test_queries.jsonl"),
        "new_class": read_jsonl(GENERATED / "holdout_test_queries.jsonl"),
        "off_topic": read_jsonl(GENERATED / "out_of_scope.jsonl"),
    }
    # The severity each scenario starts from, before urgency moves it (used for diagnostics only).
    base_severity = {
        scenario["id"]: scenario["base_severity"]
        for path in sorted((ROOT / "data" / "scenarios").glob("*.yaml"))
        for scenario in yaml.safe_load(path.read_text(encoding="utf-8"))
    }
    items = []
    for group, rows in groups.items():
        for row in rows:
            number = int(row["id"].rsplit("-", 1)[1])
            item = {**row, "group": group, "split": "dev" if number % 2 else "test"}
            item["base_severity"] = base_severity.get(row.get("scenario_id"))
            items.append(item)
    return items


def macro_f1(pairs: list[tuple[str, str]]) -> float:
    """Average F1 over the true classes, so rare classes count as much as common ones."""
    scores = []
    for label in {truth for truth, _ in pairs}:
        hits = sum(1 for truth, guess in pairs if truth == label and guess == label)
        guessed = sum(1 for _, guess in pairs if guess == label)
        actual = sum(1 for truth, _ in pairs if truth == label)
        precision = hits / guessed if guessed else 0.0
        recall = hits / actual if actual else 0.0
        scores.append(2 * precision * recall / (precision + recall) if precision + recall else 0.0)
    return statistics.mean(scores) if scores else 0.0


def share(flags: list[bool]) -> float:
    return sum(flags) / len(flags) if flags else 0.0


def evaluate(items: list[dict], signals, params: Params) -> dict[str, float]:
    """Score one set of settings on a list of complaints (using evidence gathered earlier)."""
    results = [(item, decide(item["evidence"], signals, params)) for item in items]
    known = [(item, result) for item, result in results if item["group"] == "known"]

    def flagged(group: str) -> float:
        return share(
            [result["category"]["label"] == UNKNOWN for item, result in results if item["group"] == group]
        )

    def level(name: str) -> int:
        return SEVERITIES.index(name)

    return {
        "category_accuracy": share([r["category"]["label"] == i["category"] for i, r in known]),
        "category_best_guess_accuracy": share(
            [r["category"]["best_guess"] == i["category"] for i, r in known]
        ),
        "category_macro_f1": macro_f1([(i["category"], r["category"]["label"]) for i, r in known]),
        "product_accuracy": share([r["product"]["label"] == i["product"] for i, r in known]),
        "product_best_guess_accuracy": share([r["product"]["best_guess"] == i["product"] for i, r in known]),
        "severity_accuracy": share([r["severity"]["label"] == i["severity"] for i, r in known]),
        "severity_within_one_level": share(
            [abs(level(r["severity"]["label"]) - level(i["severity"])) <= 1 for i, r in known]
        ),
        # The two parts of severity, scored separately, to show which one needs work.
        "severity_baseline_accuracy": share(
            [r["severity"]["baseline"] == i["base_severity"] for i, r in known]
        ),
        "urgency_signal_accuracy": share(
            [
                (r["severity"]["reasons"][0] if r["severity"]["reasons"] else "none") == i["impact"]
                for i, r in known
            ]
        ),
        "sentiment_accuracy": share([r["sentiment"]["label"] == i["sentiment"] for i, r in known]),
        "sentiment_macro_f1": macro_f1([(i["sentiment"], r["sentiment"]["label"]) for i, r in known]),
        "known_flagged_unknown": flagged("known"),
        "new_class_flagged_unknown": flagged("new_class"),
        "off_topic_flagged_unknown": flagged("off_topic"),
    }


def calibrate(dev: list[dict], signals, start: Params) -> Params:
    """Pick each setting by trying a grid of values on the dev half."""
    params = replace(start, **NEVER_UNKNOWN)

    # 1. How many neighbours vote, and how strongly the closest ones dominate.
    best = max(
        ((n, p) for n in NEIGHBOUR_GRID for p in POWER_GRID),
        key=lambda pair: (
            evaluate(dev, signals, replace(params, neighbours=pair[0], similarity_power=pair[1]))[
                "category_best_guess_accuracy"
            ],
            evaluate(dev, signals, replace(params, neighbours=pair[0], similarity_power=pair[1]))[
                "product_best_guess_accuracy"
            ],
            -pair[0],
        ),
    )
    params = replace(params, neighbours=best[0], similarity_power=best[1])
    scores = evaluate(dev, signals, params)
    print(
        f"  vote: {best[0]} neighbours, power {best[1]} "
        f"-> category {scores['category_best_guess_accuracy']:.3f}, "
        f"product {scores['product_best_guess_accuracy']:.3f} (dev)"
    )

    # 2. Severity: how close a sentence must be to an urgency example, and how the baseline is
    #    read from similar tickets (how many of them, and which point of their severities).
    params = max(
        (
            replace(params, severity_signal_threshold=t, baseline_neighbours=n, baseline_quantile=q)
            for t in SIGNAL_GRID
            for n in BASELINE_NEIGHBOUR_GRID
            for q in BASELINE_QUANTILE_GRID
        ),
        key=lambda candidate: evaluate(dev, signals, candidate)["severity_accuracy"],
    )
    scores = evaluate(dev, signals, params)
    print(
        f"  severity: signal threshold {params.severity_signal_threshold}, baseline from "
        f"{params.baseline_neighbours} neighbours at quantile {params.baseline_quantile} "
        f"-> severity {scores['severity_accuracy']:.3f} "
        f"(baseline {scores['severity_baseline_accuracy']:.3f}, "
        f"urgency signal {scores['urgency_signal_accuracy']:.3f}) (dev)"
    )

    # 3. Sentiment: how close a sentence must be to a tone example.
    params = max(
        (replace(params, sentiment_signal_threshold=t) for t in SIGNAL_GRID),
        key=lambda candidate: evaluate(dev, signals, candidate)["sentiment_accuracy"],
    )
    print(
        f"  sentiment: signal threshold {params.sentiment_signal_threshold} "
        f"-> sentiment {evaluate(dev, signals, params)['sentiment_accuracy']:.3f} (dev)"
    )

    # 4. When to say "unknown". Flagging a normal complaint wastes a reviewer's time, so we allow
    #    at most MAX_KNOWN_FLAGGED of known complaints to be flagged, and within that limit catch
    #    as many new-class and off-topic complaints as possible.
    def caught(candidate: Params) -> tuple[bool, float]:
        scores = evaluate(dev, signals, candidate)
        within_limit = scores["known_flagged_unknown"] <= MAX_KNOWN_FLAGGED
        return within_limit, (scores["new_class_flagged_unknown"] + scores["off_topic_flagged_unknown"]) / 2

    candidates = [
        replace(params, min_similarity=s, min_confidence=c) for s in SIMILARITY_GRID for c in CONFIDENCE_GRID
    ]
    params = max(candidates, key=lambda c: (*caught(c), -c.min_similarity, -c.min_confidence))
    scores = evaluate(dev, signals, params)
    print(
        f"  unknown rule: min_similarity {params.min_similarity}, min_confidence {params.min_confidence} "
        f"-> known kept {1 - scores['known_flagged_unknown']:.3f}, "
        f"new classes flagged {scores['new_class_flagged_unknown']:.3f}, "
        f"off-topic flagged {scores['off_topic_flagged_unknown']:.3f} (dev)"
    )
    return params


def similarity_summary(items: list[dict]) -> str:
    """Closest-ticket similarity per group: shows how separable the three groups are."""
    lines = ["| Group | lowest | 25% | middle | 75% | highest |", "|---|---|---|---|---|---|"]
    for group in ("known", "new_class", "off_topic"):
        values = sorted(
            item["evidence"].neighbours[0]["similarity"] if item["evidence"].neighbours else 0.0
            for item in items
            if item["group"] == group
        )
        picks = [values[round(q * (len(values) - 1))] for q in (0, 0.25, 0.5, 0.75, 1)]
        lines.append(f"| {group} | " + " | ".join(f"{value:.3f}" for value in picks) + " |")
    return "\n".join(lines)


def to_markdown(scores: dict[str, float], count: int, params: Params, model: str) -> str:
    rows = [
        ("Category accuracy", "category_accuracy", "'unknown' counts as wrong"),
        ("Category accuracy, best guess", "category_best_guess_accuracy", "ignoring the 'unknown' rule"),
        ("Category macro-F1", "category_macro_f1", "rare classes count as much as common ones"),
        ("Product accuracy", "product_accuracy", ""),
        ("Severity accuracy", "severity_accuracy", "exact level"),
        ("Severity within one level", "severity_within_one_level", ""),
        ("  part 1: baseline severity", "severity_baseline_accuracy", "from similar tickets"),
        ("  part 2: urgency signal", "urgency_signal_accuracy", "which urgency signal, or none"),
        ("Sentiment accuracy", "sentiment_accuracy", ""),
        ("Sentiment macro-F1", "sentiment_macro_f1", ""),
        ("Known complaints flagged unknown", "known_flagged_unknown", "lower is better"),
        ("New-class complaints flagged unknown", "new_class_flagged_unknown", "higher is better"),
        ("Off-topic questions flagged unknown", "off_topic_flagged_unknown", "higher is better"),
    ]
    lines = [
        f"Triage quality on the test half ({count} complaints the tuning never saw).",
        f"Embedding model: {model}",
        f"Settings: {json.dumps(params.to_dict())}",
        "",
        "| Metric | Value | Note |",
        "|---|---|---|",
    ]
    lines += [f"| {title} | {scores[key]:.3f} | {note} |" for title, key, note in rows]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate and tune the triage service.")
    parser.add_argument(
        "--calibrate", action="store_true", help="tune the settings on the dev half and save them"
    )
    parser.add_argument("--retrieval-url", default=os.environ.get("RETRIEVAL_URL", "http://retrieval:8002"))
    parser.add_argument("--embedding-url", default=os.environ.get("EMBEDDING_URL", "http://embedding:8004"))
    args = parser.parse_args()

    settings = Settings()
    signals = load_signals(settings.signals_path)
    params = Params.from_file(settings.params_path)
    triage = Triage(
        RetrievalClient(args.retrieval_url, timeout=60),
        EmbeddingClient(args.embedding_url, timeout=60),
        signals,
        params,
        max(NEIGHBOUR_GRID),
    )
    if not triage.ready():
        sys.exit("The retrieval or embedding service is not ready. Start them and load the data first.")
    try:
        model = httpx.get(f"{args.embedding_url}/ready", timeout=5).json()["dense_model"]
    except (httpx.HTTPError, KeyError, ValueError):
        model = "unknown"

    items = load_items()
    print(f"Gathering evidence for {len(items)} complaints ...")
    started = time.monotonic()
    latencies = []
    for item in items:
        evidence, timings = triage.collect(item["complaint"])
        item["evidence"]: Evidence = evidence
        latencies.append(sum(timings.values()) * 1000)
    latencies.sort()
    print(
        f"  done in {time.monotonic() - started:.0f}s "
        f"(per complaint: middle {latencies[len(latencies) // 2]:.0f} ms, "
        f"95% under {latencies[round(0.95 * (len(latencies) - 1))]:.0f} ms)"
    )
    print("\nSimilarity of the closest past ticket, per group:\n" + similarity_summary(items))

    dev = [item for item in items if item["split"] == "dev"]
    test = [item for item in items if item["split"] == "test"]

    if args.calibrate:
        print(f"\nTuning on the dev half ({len(dev)} complaints):")
        params = calibrate(dev, signals, params)
        saved = {
            "_comment": "Tuned settings for the triage service. Written by: python evals/eval_triage.py "
            "--calibrate (fitted on the dev half of the test complaints, reported on the other half).",
            **params.to_dict(),
            "calibrated": True,
            "embedding_model": model,
        }
        settings.params_path.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8", newline="\n")
        print(f"  saved to {settings.params_path.relative_to(ROOT)}")

    scores = evaluate(test, signals, params)
    table = to_markdown(scores, len(test), params, model)
    print("\n" + table)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "triage.md").write_text(table + "\n", encoding="utf-8", newline="\n")
    (RESULTS_DIR / "triage.json").write_text(
        json.dumps(
            {"complaints": len(test), "embedding_model": model, "params": params.to_dict(), **scores},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"\nSaved to {RESULTS_DIR.relative_to(ROOT)}/triage.md and triage.json")
    if args.calibrate:
        print("To use the tuned settings in the running service: docker compose up -d --build triage")


if __name__ == "__main__":
    main()
