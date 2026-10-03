"""Generate the synthetic telecom support dataset.

What it makes (all in data/generated/):
  tickets.jsonl              past resolved tickets that get indexed and searched
  test_queries.jsonl         held-out complaints, worded differently, used only for evals
  kb_articles.jsonl          knowledge-base articles (one per scenario + general ones)
  out_of_scope.jsonl         non-telecom questions the system should refuse
  holdout_tickets.jsonl      tickets from brand-new classes (for the "new class" demo)
  holdout_test_queries.jsonl test complaints for those new classes
  holdout_kb_articles.jsonl  KB articles for those new classes
  summary.json               counts, so you can see what was generated

The output is deterministic: the same seed always gives exactly the same files.

Run:
  docker compose run --rm tools python scripts/generate_data.py
"""

from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DEFAULT_OUT_DIR = DATA_DIR / "generated"

SEVERITIES = ["low", "medium", "high", "critical"]
SENTIMENTS = ["negative", "neutral", "positive"]

# Angrier customers are more likely when the problem is more severe.
# Each row is the chance of (negative, neutral, positive).
SENTIMENT_WEIGHTS = {
    "low": (30, 55, 15),
    "medium": (50, 40, 10),
    "high": (70, 25, 5),
    "critical": (80, 18, 2),
}

# All dates are counted back from this fixed day so the output never changes.
REFERENCE_DATE = datetime(2026, 10, 1, tzinfo=UTC)

REQUIRED_SCENARIO_FIELDS = (
    "id",
    "category",
    "product",
    "title",
    "base_severity",
    "cause",
    "symptoms",
    "test_symptoms",
    "tried",
    "resolution",
    "escalate",
)


# --------------------------------------------------------------------------- #
# Loading and checking the hand-written input files
# --------------------------------------------------------------------------- #


def _read_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_inputs(data_dir: Path = DATA_DIR) -> dict[str, Any]:
    """Read scenarios, phrase pools, taxonomy, general KB articles and out-of-scope questions."""
    scenarios: list[dict] = []
    for path in sorted((data_dir / "scenarios").glob("*.yaml")):
        scenarios.extend(_read_yaml(path))
    inputs = {
        "scenarios": scenarios,
        "phrases": _read_yaml(data_dir / "phrases.yaml"),
        "taxonomy": _read_yaml(data_dir / "taxonomy.yaml"),
        "kb_general": _read_yaml(data_dir / "kb_general.yaml"),
        "out_of_scope": _read_yaml(data_dir / "out_of_scope.yaml"),
    }
    validate_inputs(inputs)
    return inputs


def validate_inputs(inputs: dict[str, Any]) -> None:
    """Fail early with a clear message if a scenario file has a mistake."""
    categories = {c["name"] for c in inputs["taxonomy"]["category"]}
    products = {p["name"] for p in inputs["taxonomy"]["product"]}
    seen_ids: set[str] = set()

    for s in inputs["scenarios"]:
        sid = s.get("id", "<missing id>")
        missing = [f for f in REQUIRED_SCENARIO_FIELDS if f not in s]
        if missing:
            raise ValueError(f"Scenario {sid} is missing fields: {missing}")
        if sid in seen_ids:
            raise ValueError(f"Duplicate scenario id: {sid}")
        seen_ids.add(sid)
        if s["base_severity"] not in SEVERITIES[:3]:
            raise ValueError(f"Scenario {sid}: base_severity must be low, medium or high")
        if s["product"] not in products:
            raise ValueError(f"Scenario {sid}: unknown product {s['product']!r}")
        # Hold-out scenarios are *supposed* to use categories that are not in the taxonomy yet.
        if s.get("holdout"):
            if s["category"] in categories:
                raise ValueError(f"Hold-out scenario {sid} must use a category outside the taxonomy")
        elif s["category"] not in categories:
            raise ValueError(f"Scenario {sid}: unknown category {s['category']!r}")
        if len(s["symptoms"]) < 4 or len(s["test_symptoms"]) < 2:
            raise ValueError(f"Scenario {sid}: needs at least 4 symptoms and 2 test_symptoms")
        if len(s["tried"]) < 2 or len(s["resolution"]) < 3:
            raise ValueError(f"Scenario {sid}: needs at least 2 'tried' items and 3 resolution steps")
        overlap = set(s["symptoms"]) & set(s["test_symptoms"])
        if overlap:
            raise ValueError(f"Scenario {sid}: wording appears in both symptoms and test_symptoms: {overlap}")


# --------------------------------------------------------------------------- #
# Building one complaint
# --------------------------------------------------------------------------- #


def _pick_impact(rng: random.Random, phrases: dict, category: str) -> str:
    """Choose an impact type (e.g. business_impact) that makes sense for this category."""
    allowed = {
        name: spec
        for name, spec in phrases["impact"].items()
        if "only_for" not in spec or category in spec["only_for"]
    }
    names = list(allowed)
    weights = [allowed[n]["weight"] for n in names]
    return rng.choices(names, weights=weights, k=1)[0]


def _add_typos(rng: random.Random, text: str) -> str:
    """Swap two neighbouring letters in a few longer words, like a fast typist would."""
    words = text.split(" ")
    for i, word in enumerate(words):
        if len(word) > 4 and word.isalpha() and rng.random() < 0.12:
            j = rng.randint(1, len(word) - 3)
            words[i] = word[:j] + word[j + 1] + word[j] + word[j + 2 :]
    return " ".join(words)


def _apply_style(rng: random.Random, text: str) -> str:
    """Real customers are messy: some never use capitals or full stops, some make typos."""
    roll = rng.random()
    if roll < 0.70:
        return text
    if roll < 0.90:
        return text.lower().replace(". ", " ").rstrip(".")
    return _add_typos(rng, text)


def make_complaint(rng: random.Random, scenario: dict, symptom: str, phrases: dict, split: str) -> dict:
    """Wrap one symptom sentence with an opener, duration, tried steps, impact and closer.

    `split` is "train" or "test" and decides which phrase lists are used, so test
    complaints never reuse the surrounding wording of indexed tickets either.
    """
    # 1. Severity: start from the scenario's base level, then move it by the impact.
    impact_name = _pick_impact(rng, phrases, scenario["category"])
    impact = phrases["impact"][impact_name]
    level = SEVERITIES.index(scenario["base_severity"]) + impact["delta"]
    severity = SEVERITIES[max(0, min(level, len(SEVERITIES) - 1))]

    # 2. Sentiment: more severe problems make negative wording more likely.
    sentiment = rng.choices(SENTIMENTS, weights=SENTIMENT_WEIGHTS[severity], k=1)[0]

    # 3. Things the customer already tried (present in a bit over half of complaints).
    already_tried: list[str] = []
    tried_sentence = ""
    if rng.random() < 0.55:
        count = rng.choice([1, 1, 2])
        already_tried = rng.sample(scenario["tried"], k=count)
        if count == 1:
            tried_sentence = rng.choice(phrases["tried_one"][split]).format(a=already_tried[0])
        else:
            template = rng.choice(phrases["tried_two"][split])
            tried_sentence = template.format(a=already_tried[0], b=already_tried[1])

    # 4. Fake personal details in some complaints (to test PII masking later).
    pii_sentence = ""
    if rng.random() < 0.15:
        email = f"{rng.choice(phrases['first_names'])}.{rng.choice(phrases['last_names'])}@example.com"
        pii_sentence = rng.choice(phrases["pii"]).format(
            account=str(rng.randint(10_000_000, 99_999_999)),
            phone=f"07700 900{rng.randint(0, 999):03d}",
            email=email,
        )

    duration = rng.choice(phrases["durations"][split]) if rng.random() < 0.6 else ""
    middle = [tried_sentence, rng.choice(impact[split])]
    rng.shuffle(middle)

    parts = [
        rng.choice(phrases["openers"][split]),
        symptom,
        duration,
        *middle,
        pii_sentence,
        rng.choice(phrases["closers"][sentiment][split]),
    ]
    text = " ".join(p for p in parts if p)

    return {
        "text": _apply_style(rng, text),
        "severity": severity,
        "sentiment": sentiment,
        "already_tried": already_tried,
        "impact": impact_name,
        "contains_pii": bool(pii_sentence),
    }


def _unique_complaints(
    rng: random.Random,
    scenario: dict,
    symptoms: list[str],
    phrases: dict,
    split: str,
    count: int,
    seen: set[str],
) -> list[dict]:
    """Make `count` complaints for a scenario, cycling through its symptoms, with no exact duplicates."""
    complaints: list[dict] = []
    for i in range(count):
        symptom = symptoms[i % len(symptoms)]
        for _ in range(50):
            complaint = make_complaint(rng, scenario, symptom, phrases, split)
            if complaint["text"] not in seen:
                seen.add(complaint["text"])
                complaint["core_problem"] = symptom
                complaints.append(complaint)
                break
        else:
            raise RuntimeError(f"Could not make a unique complaint for {scenario['id']}; add more phrases")
    return complaints


# --------------------------------------------------------------------------- #
# Building tickets, test queries and KB articles
# --------------------------------------------------------------------------- #


def kb_id_for(scenario_id: str) -> str:
    """S01 -> KB-001, H01 -> KB-H01."""
    return f"KB-{int(scenario_id[1:]):03d}" if scenario_id.startswith("S") else f"KB-{scenario_id}"


def _random_past_date(rng: random.Random, max_days: int) -> str:
    moment = REFERENCE_DATE - timedelta(days=rng.randint(1, max_days), seconds=rng.randint(0, 86_399))
    return moment.isoformat()


def make_kb_article(rng: random.Random, scenario: dict) -> dict:
    steps = "\n".join(f"{n}. {step}" for n, step in enumerate(scenario["resolution"], start=1))
    examples = "\n".join(f"- {s}" for s in scenario["symptoms"][:3])
    body = (
        f"# {scenario['title']}\n\n"
        f"## Symptoms\nCustomers typically say things like:\n{examples}\n\n"
        f"## Likely cause\n{scenario['cause']}\n\n"
        f"## Resolution steps\n{steps}\n\n"
        f"## When to escalate\n{scenario['escalate']}\n"
    )
    return {
        "id": kb_id_for(scenario["id"]),
        "title": scenario["title"],
        "body": body,
        "category": scenario["category"],
        "product": scenario["product"],
        "scenario_id": scenario["id"],
        "version": 1,
        "updated_at": _random_past_date(rng, 365),
    }


def _make_tickets(
    rng: random.Random, scenarios: list[dict], phrases: dict, count: int, seen: set[str], first_number: int
) -> list[dict]:
    tickets: list[dict] = []
    number = first_number
    for scenario in scenarios:
        for c in _unique_complaints(rng, scenario, scenario["symptoms"], phrases, "train", count, seen):
            tickets.append(
                {
                    "id": f"T-{number:06d}",
                    "subject": scenario["title"],
                    "description": c["text"],
                    "resolution_steps": [*scenario["resolution"], rng.choice(phrases["outcomes"])],
                    "category": scenario["category"],
                    "product": scenario["product"],
                    "severity": c["severity"],
                    "sentiment": c["sentiment"],
                    "scenario_id": scenario["id"],
                    "already_tried": c["already_tried"],
                    "impact": c["impact"],
                    "contains_pii": c["contains_pii"],
                    "created_at": _random_past_date(rng, 540),
                }
            )
            number += 1
    rng.shuffle(tickets)
    return tickets


def _make_test_queries(
    rng: random.Random, scenarios: list[dict], phrases: dict, count: int, seen: set[str], prefix: str
) -> list[dict]:
    queries: list[dict] = []
    number = 1
    for scenario in scenarios:
        for c in _unique_complaints(rng, scenario, scenario["test_symptoms"], phrases, "test", count, seen):
            queries.append(
                {
                    "id": f"{prefix}-{number:04d}",
                    "complaint": c["text"],
                    # The scenario sentence alone, without greeting, impact or tone. Only used by
                    # diagnostic evals, to measure how much the extra sentences hurt search.
                    "core_problem": c["core_problem"],
                    "category": scenario["category"],
                    "product": scenario["product"],
                    "severity": c["severity"],
                    "sentiment": c["sentiment"],
                    "scenario_id": scenario["id"],  # the answer key for retrieval evals
                    "relevant_kb_ids": [kb_id_for(scenario["id"])],
                    "already_tried": c["already_tried"],
                    "impact": c["impact"],
                    "contains_pii": c["contains_pii"],
                }
            )
            number += 1
    rng.shuffle(queries)
    return queries


def build_dataset(
    seed: int = 42,
    index_per_scenario: int = 40,
    test_per_scenario: int = 10,
    inputs: dict[str, Any] | None = None,
) -> dict[str, list[dict]]:
    """Build every output file in memory. Same arguments always give the same result."""
    inputs = inputs or load_inputs()
    rng = random.Random(seed)
    phrases = inputs["phrases"]
    regular = [s for s in inputs["scenarios"] if not s.get("holdout")]
    holdout = [s for s in inputs["scenarios"] if s.get("holdout")]
    seen: set[str] = set()  # shared, so no complaint text is ever repeated across any file

    tickets = _make_tickets(rng, regular, phrases, index_per_scenario, seen, first_number=1)
    test_queries = _make_test_queries(rng, regular, phrases, test_per_scenario, seen, prefix="Q")
    holdout_tickets = _make_tickets(rng, holdout, phrases, index_per_scenario, seen, first_number=900_001)
    holdout_queries = _make_test_queries(rng, holdout, phrases, test_per_scenario, seen, prefix="HQ")

    kb_articles = [make_kb_article(rng, s) for s in regular]
    for article in inputs["kb_general"]:
        kb_articles.append(
            {
                "id": article["id"],
                "title": article["title"],
                "body": f"# {article['title']}\n\n{article['body'].strip()}\n",
                "category": None,
                "product": article["product"],
                "scenario_id": None,
                "version": 1,
                "updated_at": _random_past_date(rng, 365),
            }
        )

    return {
        "tickets": tickets,
        "test_queries": test_queries,
        "kb_articles": kb_articles,
        "out_of_scope": [
            {"id": f"OOS-{n:03d}", "complaint": text}
            for n, text in enumerate(inputs["out_of_scope"], start=1)
        ],
        "holdout_tickets": holdout_tickets,
        "holdout_test_queries": holdout_queries,
        "holdout_kb_articles": [make_kb_article(rng, s) for s in holdout],
    }


# --------------------------------------------------------------------------- #
# Writing files and printing a summary
# --------------------------------------------------------------------------- #


def summarise(dataset: dict[str, list[dict]]) -> dict[str, Any]:
    tickets = dataset["tickets"]
    return {
        "counts": {name: len(rows) for name, rows in dataset.items()},
        "tickets_by_category": dict(sorted(Counter(t["category"] for t in tickets).items())),
        "tickets_by_product": dict(sorted(Counter(t["product"] for t in tickets).items())),
        "tickets_by_severity": {s: sum(t["severity"] == s for t in tickets) for s in SEVERITIES},
        "tickets_by_sentiment": {s: sum(t["sentiment"] == s for t in tickets) for s in SENTIMENTS},
        "tickets_with_pii": sum(t["contains_pii"] for t in tickets),
    }


def write_dataset(dataset: dict[str, list[dict]], out_dir: Path, seed: int) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, rows in dataset.items():
        with (out_dir / f"{name}.jsonl").open("w", encoding="utf-8", newline="\n") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    summary = {"seed": seed, **summarise(dataset)}
    with (out_dir / "summary.json").open("w", encoding="utf-8", newline="\n") as f:
        json.dump(summary, f, indent=2)
        f.write("\n")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the synthetic telecom support dataset.")
    parser.add_argument("--seed", type=int, default=42, help="random seed (default: 42)")
    parser.add_argument("--index-per-scenario", type=int, default=40, help="indexed tickets per scenario")
    parser.add_argument("--test-per-scenario", type=int, default=10, help="test queries per scenario")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR, help="output folder")
    args = parser.parse_args()

    dataset = build_dataset(args.seed, args.index_per_scenario, args.test_per_scenario)
    summary = write_dataset(dataset, args.out, args.seed)

    print(f"Wrote dataset to {args.out} (seed {args.seed})")
    for name, count in summary["counts"].items():
        print(f"  {name:<22} {count:>5}")
    print("Severity :", summary["tickets_by_severity"])
    print("Sentiment:", summary["tickets_by_sentiment"])
    print("\nExample ticket:")
    example = dataset["tickets"][0]
    print(
        f"  [{example['id']}] {example['category']} / {example['product']} / "
        f"{example['severity']} / {example['sentiment']}"
    )
    print(f"  {example['description']}")


if __name__ == "__main__":
    main()
