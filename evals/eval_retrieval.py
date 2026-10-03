"""Measure search quality on the held-out test complaints, for several search setups.

This is the evidence for the main design claim: semantic search finds the right past
ticket when the customer uses different words, and keyword search often does not.

Run (services up and data loaded):
  docker compose run --rm tools python evals/eval_retrieval.py                   # main table (about a minute)
  docker compose run --rm tools python evals/eval_retrieval.py --with-reranker   # adds the slow reranker row
  docker compose run --rm tools python evals/eval_retrieval.py --diagnose        # "why" experiments
  docker compose run --rm tools python evals/eval_retrieval.py --limit 60        # quicker sample

How a result is judged: every test complaint was generated from a known scenario. A returned
ticket or article is "relevant" when it comes from the same scenario.

Metrics (all between 0 and 1, higher is better):
  Hit@1         the top ticket is relevant
  Hit@3         at least one of the top 3 tickets is relevant
  Context hit   at least one relevant source among the 3 tickets + 2 articles the LLM receives.
                This is the one that matters most for the final answer.
  Category@1    the top ticket has the same category as the complaint (what triage relies on)
  MRR@10        1 / position of the first relevant ticket (1.0 = always first)
  KB Hit@1      the top knowledge-base article is the right one
  KB Recall@3   the right knowledge-base article is in the top 3
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = ROOT / "evals" / "results"

# Each setup: label, search mode, use the reranker?, which text to search with, dense weight
SETUPS = [
    ("Keyword only (BM25) - today's baseline", "sparse", False, "complaint", 1.0),
    ("Dense only (meaning)", "dense", False, "complaint", 1.0),
    ("Hybrid, equal weights", "hybrid", False, "complaint", 1.0),
    ("Hybrid, dense counts 3x (our default)", "hybrid", False, "complaint", 3.0),
]
RERANKER_SETUP = ("Our default + reranker", "hybrid", True, "complaint", 3.0)

# Experiments that explain WHY the numbers are what they are.
# "core_problem" is the one sentence that describes the fault, without greeting, impact or tone.
# Searching with it shows whether the extra sentences in a complaint are what limits accuracy.
DIAGNOSTIC_SETUPS = [
    ("Dense, full complaint", "dense", False, "complaint", 1.0),
    ("Hybrid, dense counts 3x, full complaint", "hybrid", False, "complaint", 3.0),
    ("Keyword, core problem only", "sparse", False, "core_problem", 1.0),
    ("Dense, core problem only", "dense", False, "core_problem", 1.0),
    ("Hybrid, core problem only", "hybrid", False, "core_problem", 1.0),
    ("Hybrid, dense counts 3x, core problem only", "hybrid", False, "core_problem", 3.0),
]
METRICS = ["hit@1", "hit@3", "context_hit", "category@1", "precision@5", "mrr@10", "kb_hit@1", "kb_recall@3"]
# Columns of the printed table: (title, key in the summary, number format)
TABLE_COLUMNS = [
    ("Hit@1", "hit@1", ".3f"),
    ("Hit@3", "hit@3", ".3f"),
    ("Context hit", "context_hit", ".3f"),
    ("Category@1", "category@1", ".3f"),
    ("MRR@10", "mrr@10", ".3f"),
    ("KB Hit@1", "kb_hit@1", ".3f"),
    ("KB Recall@3", "kb_recall@3", ".3f"),
    ("p50 ms", "latency_p50_ms", ".0f"),
    ("p95 ms", "latency_p95_ms", ".0f"),
]


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def score_query(query: dict, results: list[dict]) -> dict[str, float]:
    """Compare one search response with the answer key for that test complaint."""
    tickets = [r for r in results if r["source_type"] == "ticket"]
    articles = [r for r in results if r["source_type"] == "kb"]
    relevant = [t["scenario_id"] == query["scenario_id"] for t in tickets]
    first_hit = next((position for position, ok in enumerate(relevant[:10], start=1) if ok), None)
    right_articles = set(query["relevant_kb_ids"])
    return {
        "hit@1": float(bool(relevant[:1] and relevant[0])),
        "hit@3": float(any(relevant[:3])),
        "context_hit": float(any(relevant[:3]) or any(a["id"] in right_articles for a in articles[:2])),
        "category@1": float(bool(tickets and tickets[0]["category"] == query["category"])),
        "precision@5": sum(relevant[:5]) / 5,
        "mrr@10": 1 / first_hit if first_hit else 0.0,
        "kb_hit@1": float(bool(articles and articles[0]["id"] in right_articles)),
        "kb_recall@3": float(any(a["id"] in right_articles for a in articles[:3])),
    }


def percentile(values: list[float], percent: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(percent / 100 * (len(ordered) - 1)))]


def run_setup(
    http: httpx.Client, queries: list[dict], mode: str, rerank: bool, text_field: str, dense_weight: float
) -> dict:
    per_query: list[dict[str, float]] = []
    latencies: list[float] = []
    by_scenario: dict[str, list[float]] = defaultdict(list)
    for query in queries:
        body = {
            "query": query[text_field],
            "top_k_tickets": 10,
            "top_k_kb": 3,
            "mode": mode,
            "rerank": rerank,
            "dense_weight": dense_weight,
        }
        response = http.post("/search", json=body)
        response.raise_for_status()
        data = response.json()
        scores = score_query(query, data["results"])
        per_query.append(scores)
        latencies.append(data["timings_ms"]["total"])
        by_scenario[query["scenario_id"]].append(scores["hit@1"])
    summary = {metric: round(statistics.mean(q[metric] for q in per_query), 3) for metric in METRICS}
    summary["latency_p50_ms"] = round(percentile(latencies, 50), 1)
    summary["latency_p95_ms"] = round(percentile(latencies, 95), 1)
    summary["hit@1_by_scenario"] = {s: round(statistics.mean(v), 2) for s, v in sorted(by_scenario.items())}
    return summary


def to_markdown(rows: list[tuple[str, dict]], query_count: int, embedding_model: str) -> str:
    lines = [
        f"Retrieval quality on {query_count} held-out test complaints (wording never seen by the index).",
        f"Embedding model: {embedding_model}",
        "",
        "| Setup | " + " | ".join(title for title, _, _ in TABLE_COLUMNS) + " |",
        "|---" * (len(TABLE_COLUMNS) + 1) + "|",
    ]
    for label, summary in rows:
        cells = [format(summary[key], spec) for _, key, spec in TABLE_COLUMNS]
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def embedding_model_name() -> str:
    """Ask the embedding service which model it runs, so every results table says what produced it."""
    try:
        url = os.environ.get("EMBEDDING_URL", "http://embedding:8004")
        return httpx.get(f"{url}/ready", timeout=5).json()["dense_model"]
    except (httpx.HTTPError, KeyError, ValueError):
        return "unknown"


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate retrieval quality for several search setups.")
    parser.add_argument("--url", default=os.environ.get("RETRIEVAL_URL", "http://retrieval:8002"))
    parser.add_argument("--queries", type=Path, default=ROOT / "data" / "generated" / "test_queries.jsonl")
    parser.add_argument("--limit", type=int, default=0, help="only use the first N test complaints")
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="run the fast diagnostic experiments instead of the main table",
    )
    parser.add_argument(
        "--with-reranker", action="store_true", help="also measure the reranker (slow: about 1 s per search)"
    )
    args = parser.parse_args()
    setups, name = (
        (DIAGNOSTIC_SETUPS, "retrieval_diagnosis") if args.diagnose else (list(SETUPS), "retrieval")
    )
    if args.with_reranker and not args.diagnose:
        setups.append(RERANKER_SETUP)

    queries = read_jsonl(args.queries)
    if args.limit:
        queries = queries[: args.limit]

    with httpx.Client(base_url=args.url, timeout=120) as http:
        try:
            http.get("/ready").raise_for_status()
        except httpx.HTTPError as error:
            sys.exit(
                f"The retrieval service is not ready at {args.url} ({error}). "
                "Start it and load the data first."
            )

        rows: list[tuple[str, dict]] = []
        for label, mode, rerank, text_field, dense_weight in setups:
            started = time.monotonic()
            summary = run_setup(http, queries, mode, rerank, text_field, dense_weight)
            rows.append((label, summary))
            print(f"  finished: {label} ({time.monotonic() - started:.0f}s)")

    model = embedding_model_name()
    table = to_markdown(rows, len(queries), model)
    print("\n" + table)

    best_label, best = max(rows, key=lambda row: row[1]["hit@1"])
    hardest = sorted(best["hit@1_by_scenario"].items(), key=lambda item: item[1])[:5]
    print(f"\nHardest scenarios for '{best_label}' (Hit@1): " + ", ".join(f"{s}={v}" for s, v in hardest))

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / f"{name}.md").write_text(table + "\n", encoding="utf-8", newline="\n")
    (RESULTS_DIR / f"{name}.json").write_text(
        json.dumps({"queries": len(queries), "embedding_model": model, "setups": dict(rows)}, indent=2)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"\nSaved to {RESULTS_DIR.relative_to(ROOT)}/{name}.md and {name}.json")


if __name__ == "__main__":
    main()
