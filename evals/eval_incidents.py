"""Measure incident detection: does a burst of similar complaints raise the flag, and only then?

  docker compose run --rm tools python evals/eval_incidents.py     # about a minute, no language model

The idea being tested (services/retrieval/recent.py): when a complaint arrives, count the recent
complaints that are close to it in meaning. Enough of them means "probably one fault that affects
many customers". Two settings decide how well this works:

  min_similarity   how close two complaints must be to count as "the same thing"
  min_similar      how many of them (the new one included) raise the flag

How it is measured. Every test complaint was written from a known problem (its scenario), so we
know which complaints are about the same fault. From them we build two kinds of half hour:

  incident   20 complaints about different problems, plus 7 customers reporting ONE problem
  quiet      27 complaints about different problems (never more than two about the same one)

The complaints arrive in random order. For every pair of settings we count how many incidents
are flagged (want high) and how many quiet half hours are flagged by mistake (want low).

To keep the numbers honest, the problems are split in two halves. The best settings are picked on
one half (dev) and every number in the report comes from the other half (test).

A caution about the data: these test complaints deliberately describe the same problem in very
different words, with different small talk around it. Real outage reports are usually far more
alike ("no internet since 9 in <area>"), so real detection should be easier than measured here.
"""

from __future__ import annotations

import json
import os
import random
import statistics
import sys
from pathlib import Path

import numpy as np

from libs.common.embedding_client import EmbeddingClient
from libs.common.pii import mask_pii
from libs.common.service_client import ServiceError
from services.gateway.config import Settings

ROOT = Path(__file__).resolve().parents[1]
GENERATED = ROOT / "data" / "generated"
RESULTS_DIR = ROOT / "evals" / "results"

THRESHOLDS = [round(0.60 + 0.025 * step, 3) for step in range(13)]  # 0.60 ... 0.90
MIN_COUNTS = [3, 4, 5]
BURST = 7  # customers reporting the same problem in an incident
BACKGROUND = 20  # other complaints in the same half hour
REPEATS = 20  # half hours built per problem
MAX_FALSE_ALARMS = 0.02  # a setting may flag at most this share of quiet half hours
SEED = 42


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def split_of(scenario_id: str) -> str:
    """Odd-numbered problems are used for tuning, even-numbered ones for the report."""
    return "dev" if int(scenario_id.lstrip("S")) % 2 else "test"


def trigger_level(similar_to_earlier: list[float], min_count: int) -> float:
    """The highest min_similarity at which this complaint would still raise the flag.

    The flag needs `min_count` complaints including this one, so `min_count - 1` earlier ones
    must be close enough. That is decided by the (min_count - 1)-th closest earlier complaint.
    -1 means: not enough earlier complaints yet, the flag cannot be raised at any setting.
    """
    needed = min_count - 1
    if len(similar_to_earlier) < needed:
        return -1.0
    return float(sorted(similar_to_earlier, reverse=True)[needed - 1])


def run_half_hour(similarity: np.ndarray, order: list[int], burst: set[int]) -> dict[int, dict[str, float]]:
    """Play the complaints in arrival order. For each min_count, return the best trigger level
    reached by a complaint that belongs to the incident ("burst") and by any other ("other")."""
    levels = {count: {"burst": -1.0, "other": -1.0} for count in MIN_COUNTS}
    for position, index in enumerate(order):
        earlier = [float(similarity[index, other]) for other in order[:position]]
        kind = "burst" if index in burst else "other"
        for count in MIN_COUNTS:
            levels[count][kind] = max(levels[count][kind], trigger_level(earlier, count))
    return levels


def pick(pool: dict[str, list[int]], rng: random.Random, how_many: int, per_problem: int = 2) -> list[int]:
    """Pick complaints about different problems: never more than `per_problem` about the same one."""
    candidates = [
        index for members in pool.values() for index in rng.sample(members, min(per_problem, len(members)))
    ]
    return rng.sample(candidates, min(how_many, len(candidates)))


def build_half_hours(by_problem: dict[str, list[int]], rng: random.Random) -> tuple[list[dict], list[dict]]:
    """Return (incident half hours, quiet half hours) as lists of {"order": [...], "burst": {...}}."""
    incidents, quiet = [], []
    for problem, members in sorted(by_problem.items()):
        others = {name: items for name, items in by_problem.items() if name != problem}
        for _ in range(REPEATS):
            burst = rng.sample(members, min(BURST, len(members)))
            order = burst + pick(others, rng, BACKGROUND)
            rng.shuffle(order)
            incidents.append({"order": order, "burst": set(burst)})
            calm = pick(by_problem, rng, BURST + BACKGROUND)
            quiet.append({"order": calm, "burst": set()})
    return incidents, quiet


def rates(incident_levels: list[dict], quiet_levels: list[dict], threshold: float, min_count: int) -> dict:
    """Share of incidents flagged and share of quiet half hours flagged, for one pair of settings."""
    detected = [levels[min_count]["burst"] >= threshold for levels in incident_levels]
    false_alarms = [levels[min_count]["other"] >= threshold for levels in quiet_levels]
    return {
        "min_similarity": threshold,
        "min_similar": min_count,
        "incidents_flagged": round(sum(detected) / max(len(detected), 1), 3),
        "quiet_flagged": round(sum(false_alarms) / max(len(false_alarms), 1), 3),
    }


def grid(incident_levels: list[dict], quiet_levels: list[dict]) -> list[dict]:
    return [
        rates(incident_levels, quiet_levels, threshold, count)
        for count in MIN_COUNTS
        for threshold in THRESHOLDS
    ]


def choose(rows: list[dict], max_false_alarms: float = MAX_FALSE_ALARMS) -> dict:
    """Most incidents flagged while quiet half hours stay quiet. Ties go to the stricter setting."""
    allowed = [row for row in rows if row["quiet_flagged"] <= max_false_alarms] or rows
    return max(
        allowed,
        key=lambda row: (row["incidents_flagged"], -row["quiet_flagged"], row["min_similarity"]),
    )


def complaints_needed(
    similarity: np.ndarray, half_hour: dict, threshold: float, min_count: int
) -> int | None:
    """How many of the incident's complaints had arrived when the flag was first raised."""
    seen = 0
    for position, index in enumerate(half_hour["order"]):
        if index not in half_hour["burst"]:
            continue
        seen += 1
        earlier = [float(similarity[index, other]) for other in half_hour["order"][:position]]
        if trigger_level(earlier, min_count) >= threshold:
            return seen
    return None


def spread(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)

    def at(q: float) -> float:
        return round(ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))], 3)

    return {"p10": at(0.10), "p50": at(0.50), "p90": at(0.90)}


def pair_spread(similarity: np.ndarray, problems: list[str]) -> dict[str, dict[str, float]]:
    """How similar are two complaints about the same problem, and two about different problems?"""
    same, different = [], []
    for a in range(len(problems)):
        for b in range(a + 1, len(problems)):
            (same if problems[a] == problems[b] else different).append(float(similarity[a, b]))
    return {"same_problem": spread(same), "different_problem": spread(different)}


def to_markdown(report: dict) -> str:
    best, now = report["best_on_dev"], report["configured"]
    lines = [
        "# Incident detection: a burst of similar complaints",
        "",
        f"An incident is {BURST} customers reporting the same problem among {BACKGROUND} other complaints "
        "in one half hour.",
        f"A quiet half hour is {BURST + BACKGROUND} complaints with at most two about the same problem.",
        f"Measured on the test half: {report['half_hours']} half hours of each kind, "
        f"{report['complaints']} complaints about {report['problems']} problems the tuning never saw.",
        "",
        "How similar two complaints are (1 = same meaning):",
        "",
        "| Pair | Lowest 10% | Middle | Top 10% |",
        "|---|---|---|---|",
    ]
    for name, label in (("same_problem", "Same problem"), ("different_problem", "Different problems")):
        values = report["pairs"][name]
        lines.append(f"| {label} | {values['p10']:.3f} | {values['p50']:.3f} | {values['p90']:.3f} |")
    lines += [
        "",
        "| Setting | Similarity needed | Complaints needed | Incidents flagged (want high) "
        "| Quiet half hours flagged (want low) |",
        "|---|---|---|---|---|",
        f"| In use now | {now['min_similarity']} | {now['min_similar']} "
        f"| {now['incidents_flagged']:.3f} | {now['quiet_flagged']:.3f} |",
        f"| Best on the dev half | {best['min_similarity']} | {best['min_similar']} "
        f"| {best['test']['incidents_flagged']:.3f} | {best['test']['quiet_flagged']:.3f} |",
        "",
        f"With the best setting, the flag is raised after {report['complaints_needed']} of the incident's "
        f"{BURST} complaints (the middle case).",
        "",
        "Every setting, on the test half (incidents flagged / quiet half hours flagged):",
        "",
        "| Similarity needed | " + " | ".join(f"{count} complaints" for count in MIN_COUNTS) + " |",
        "|---|" + "---|" * len(MIN_COUNTS),
    ]
    cells = {(row["min_similarity"], row["min_similar"]): row for row in report["grid"]}
    for threshold in THRESHOLDS:
        row = [
            f"{cells[(threshold, count)]['incidents_flagged']:.2f} / "
            f"{cells[(threshold, count)]['quiet_flagged']:.2f}"
            for count in MIN_COUNTS
        ]
        lines.append(f"| {threshold} | " + " | ".join(row) + " |")
    return "\n".join(lines) + "\n"


def main() -> None:
    path = GENERATED / "test_queries.jsonl"
    if not path.exists():
        sys.exit("No test complaints found. Run: docker compose run --rm tools python scripts/seed.py")
    items = read_jsonl(path)
    embedder = EmbeddingClient(os.environ.get("EMBEDDING_URL", "http://embedding:8004"), timeout=120)
    try:
        info = embedder.embed([mask_pii(item["complaint"]) for item in items], kind="document")
    except ServiceError as error:
        sys.exit(f"The embedding service is not reachable ({error}). Run: docker compose up -d")
    vectors = np.array(info["dense"])
    print(f"Embedded {len(items)} test complaints with {info['model']}.")

    halves: dict[str, dict] = {}
    for half in ("dev", "test"):
        positions = [i for i, item in enumerate(items) if split_of(item["scenario_id"]) == half]
        problems = [items[i]["scenario_id"] for i in positions]
        similarity = vectors[positions] @ vectors[positions].T
        by_problem: dict[str, list[int]] = {}
        for index, problem in enumerate(problems):
            by_problem.setdefault(problem, []).append(index)
        incidents, quiet = build_half_hours(by_problem, random.Random(SEED))
        halves[half] = {
            "problems": problems,
            "similarity": similarity,
            "incidents": incidents,
            "incident_levels": [run_half_hour(similarity, h["order"], h["burst"]) for h in incidents],
            "quiet_levels": [run_half_hour(similarity, h["order"], h["burst"]) for h in quiet],
        }

    dev, test = halves["dev"], halves["test"]
    best = choose(grid(dev["incident_levels"], dev["quiet_levels"]))
    best_on_test = rates(
        test["incident_levels"], test["quiet_levels"], best["min_similarity"], best["min_similar"]
    )
    settings = Settings()
    configured = rates(
        test["incident_levels"],
        test["quiet_levels"],
        settings.incident_min_similarity,
        settings.incident_min_similar if settings.incident_min_similar in MIN_COUNTS else MIN_COUNTS[-1],
    )
    needed = [
        complaints_needed(test["similarity"], half_hour, best["min_similarity"], best["min_similar"])
        for half_hour in test["incidents"]
    ]
    needed = [value for value in needed if value is not None]

    report = {
        "embedding_model": info["model"],
        "complaints": len(test["problems"]),
        "problems": len(set(test["problems"])),
        "half_hours": len(test["incident_levels"]),
        "pairs": pair_spread(test["similarity"], test["problems"]),
        "configured": configured,
        "best_on_dev": {**best, "test": best_on_test},
        "complaints_needed": int(statistics.median(needed)) if needed else None,
        "grid": grid(test["incident_levels"], test["quiet_levels"]),
    }

    print(f"\nTest half: {report['half_hours']} incident half hours and as many quiet ones.")
    print(
        f"  in use now      similarity {configured['min_similarity']}, {configured['min_similar']} complaints"
        f" -> incidents flagged {configured['incidents_flagged']:.3f},"
        f" quiet half hours flagged {configured['quiet_flagged']:.3f}"
    )
    print(
        f"  best on dev     similarity {best['min_similarity']}, {best['min_similar']} complaints"
        f" -> incidents flagged {best_on_test['incidents_flagged']:.3f},"
        f" quiet half hours flagged {best_on_test['quiet_flagged']:.3f}"
    )
    same, different = report["pairs"]["same_problem"], report["pairs"]["different_problem"]
    print(
        f"  two complaints about the same problem are typically {same['p50']:.2f} similar,"
        f" about different problems {different['p50']:.2f}"
    )
    if (best["min_similarity"], best["min_similar"]) != (
        configured["min_similarity"],
        configured["min_similar"],
    ):
        print(
            "\nTo use the best setting, put these two lines in .env and run: docker compose up -d gateway\n"
            f"  INCIDENT_MIN_SIMILARITY={best['min_similarity']}\n"
            f"  INCIDENT_MIN_SIMILAR={best['min_similar']}"
        )

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "incidents.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (RESULTS_DIR / "incidents.md").write_text(to_markdown(report), encoding="utf-8")
    print(f"\nSaved to {RESULTS_DIR / 'incidents.md'}")


if __name__ == "__main__":
    main()
