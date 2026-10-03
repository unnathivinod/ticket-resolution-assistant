"""Measure how the system copes with NEW data and NEW ticket classes.

Run (all services up and data loaded):
  docker compose run --rm tools python evals/eval_evolving.py          # about 5 minutes
  docker compose run --rm tools python evals/eval_evolving.py --keep   # leave the new classes in

The dataset holds back two classes the system has never seen (eSIM problems and fraud,
4 problem types, 160 resolved tickets, 4 articles, 40 test complaints). This script plays
out what happens when they turn up in real life:

  Part 1  Before: how does the system handle complaints about the new problem types?
  Part 2  Learning curve: add 1, 3, 5, 10, 20, 40 resolved tickets per problem type through
          the gateway API (the same way production data would arrive) and measure again each time.
          Nothing is retrained and nothing is restarted.
  Part 3  Add the knowledge-base articles and measure again.
  Part 4  Check that complaints about the OLD classes are still handled as well as before.
  Part 5  Discovery: could the system have suggested the new classes itself? The 40 new
          complaints and 30 off-topic questions are grouped by similarity.
  Finally everything is removed again, so the other evals keep measuring the same thing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import httpx

from libs.common.embedding_client import EmbeddingClient
from libs.common.pii import mask_pii
from libs.common.retrieval_client import RetrievalClient
from libs.common.triage_client import TriageClient
from services.ingestion.discovery import build_proposals

ROOT = Path(__file__).resolve().parents[1]
GENERATED = ROOT / "data" / "generated"
RESULTS_DIR = ROOT / "evals" / "results"

STEPS = [1, 3, 5, 10, 20, 40]  # resolved tickets per new problem type
THRESHOLDS = [0.70, 0.75, 0.80, 0.825, 0.85, 0.875, 0.90]
MIN_GROUP = 5
NEW_CLASSES = {
    "esim_management": "Setting up, activating or moving an eSIM.",
    "security_fraud": "Scam messages, SIM-swap fraud and other account security problems.",
}


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def share(flags: list[bool]) -> float:
    return round(sum(flags) / len(flags), 3) if flags else 0.0


def ticket_body(ticket: dict) -> dict:
    """The fields the gateway accepts for a resolved ticket."""
    names = ("id", "subject", "description", "resolution_steps", "category", "product", "severity")
    return {**{name: ticket[name] for name in names}, **optional(ticket, "sentiment", "scenario_id")}


def optional(row: dict, *names: str) -> dict:
    return {name: row[name] for name in names if row.get(name) is not None}


class Gateway:
    """The admin side of the gateway API, with patience for the rate limit."""

    def __init__(self, base_url: str, admin_key: str) -> None:
        self._http = httpx.Client(base_url=base_url, headers={"X-API-Key": admin_key}, timeout=60)

    def call(self, method: str, path: str, expect: tuple[int, ...] = (200, 201, 202), **kwargs):
        for _ in range(5):
            response = self._http.request(method, path, **kwargs)
            if response.status_code == 429:
                time.sleep(int(response.headers.get("Retry-After", "5")) + 1)
                continue
            if response.status_code not in expect:
                sys.exit(f"{method} {path} answered {response.status_code}: {response.text}")
            return response
        sys.exit(f"{method} {path} was rate limited five times in a row")

    def wait_until_indexed(self, timeout: float = 600) -> float:
        """Wait until the search index has caught up. Returns how long that took."""
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            if self.call("GET", "/v1/ingest/status").json()["documents_waiting"] == 0:
                return time.monotonic() - started
            time.sleep(0.5)
        sys.exit(
            "The search index did not catch up in time. Is the ingestion worker running? "
            "Check: docker compose logs ingestion"
        )


def measure(triage, retrieval, complaints: list[dict]) -> dict[str, float]:
    """Run complaints through triage and search, and score them against the answer key."""
    category, best_guess, ticket_hit, article_hit, unknown = [], [], [], [], []
    given: Counter = Counter()
    for item in complaints:
        labels = triage.classify(item["complaint"])
        given[f"{item['category']} -> {labels['category']['label']}"] += 1
        category.append(labels["category"]["label"] == item["category"])
        best_guess.append(labels["category"]["best_guess"] == item["category"])
        unknown.append(labels["needs_review"])
        found = retrieval.search(item["complaint"], top_k_tickets=3, top_k_kb=2)["results"]
        ticket_hit.append(
            any(r["source_type"] == "ticket" and r["scenario_id"] == item["scenario_id"] for r in found)
        )
        article_hit.append(
            any(r["source_type"] == "kb" and r["id"] in item["relevant_kb_ids"] for r in found)
        )
    return {
        "category_correct": share(category),
        "category_best_guess": share(best_guess),
        "flagged_for_review": share(unknown),
        "right_ticket_in_top_3": share(ticket_hit),
        "right_article_in_top_2": share(article_hit),
        # "real class -> label given", most frequent first. Shows WHERE the wrong labels go.
        "labels_given": dict(given.most_common()),
    }


def summarise_groups(proposals, labels: dict[str, str | None]) -> dict:
    """Score proposed groups against the truth. `labels` maps a complaint ID to its real class
    (None for an off-topic question)."""
    new_ids = {item for item, label in labels.items() if label is not None}
    grouped = {member for proposal in proposals for member in proposal.member_ids}
    pure = 0
    for proposal in proposals:
        classes = {labels[member] for member in proposal.member_ids}
        pure += len(classes) == 1 and None not in classes
    return {
        "groups": len(proposals),
        "pure_groups": pure,
        "new_complaints_grouped": share([item in grouped for item in new_ids]),
        "off_topic_in_a_group": len(grouped - new_ids),
        "classes_found": len({labels[m] for p in proposals for m in p.member_ids} - {None}),
    }


def reset(gateway: Gateway, tickets: list[dict], articles: list[dict], announce: bool = True) -> None:
    """Take the hold-out documents and classes out again."""
    for document in [*tickets, *articles]:
        gateway.call("DELETE", f"/v1/documents/{document['id']}", expect=(202, 404))
    for name in NEW_CLASSES:
        gateway.call("DELETE", f"/v1/taxonomy/category/{name}", expect=(200, 404))
    seconds = gateway.wait_until_indexed()
    if announce:
        print(f"  removed {len(tickets)} tickets, {len(articles)} articles and 2 classes ({seconds:.0f}s)")


def discovery_table(
    embedder, new_complaints: list[dict], off_topic: list[dict], ordinary: list[dict]
) -> tuple[list[dict], list]:
    items = [{"id": c["id"], "text": mask_pii(c["complaint"])} for c in [*new_complaints, *off_topic]]
    # Ordinary complaints show which words are common everywhere, so keywords can avoid them.
    background = [item["text"] for item in items] + [mask_pii(c["complaint"]) for c in ordinary]
    labels: dict[str, str | None] = {c["id"]: c["category"] for c in new_complaints}
    labels |= {c["id"]: None for c in off_topic}
    vectors = embedder.embed([item["text"] for item in items], kind="document")["dense"]
    rows, best = [], (None, [])
    for threshold in THRESHOLDS:
        proposals = build_proposals(
            items, vectors, threshold=threshold, min_size=MIN_GROUP, background=background
        )
        summary = {"threshold": threshold, **summarise_groups(proposals, labels)}
        summary["keywords"] = [", ".join(p.keywords) for p in proposals[:4]]
        rows.append(summary)
        # Best = every group is clean, both classes found, and as many complaints grouped as possible.
        score = (
            summary["pure_groups"] == summary["groups"] > 0,
            summary["classes_found"],
            summary["new_complaints_grouped"],
        )
        if best[0] is None or score > best[0]:
            best = (score, proposals)
    return rows, best[1]


def to_markdown(curve: list[dict], known: dict, discovery: list[dict], model: str) -> str:
    lines = [
        "# New data and new ticket classes",
        "",
        f"Embedding model: `{model}`. 40 complaints about 4 problem types the system had never seen.",
        "",
        "## Learning curve",
        "",
        "| Resolved tickets added per new problem type | Category correct | Category, best guess "
        "| Sent for human review | Right past ticket in top 3 | Right article in top 2 "
        "| Seconds until searchable |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in curve:
        seconds = "" if row["seconds"] is None else f"{row['seconds']:.1f}"
        lines.append(
            f"| {row['label']} | {row['category_correct']:.3f} | {row['category_best_guess']:.3f} "
            f"| {row['flagged_for_review']:.3f} | {row['right_ticket_in_top_3']:.3f} "
            f"| {row['right_article_in_top_2']:.3f} | {seconds} |"
        )
    lines += ["", "Labels given to the new complaints at the end (real class -> label given):", ""]
    lines += [f"- {pair}: {count}" for pair, count in curve[-1]["labels_given"].items()]
    lines += [
        "",
        "## Old classes, before and after the new ones were added",
        "",
        "| | Category correct | Right past ticket in top 3 |",
        "|---|---|---|",
        f"| Before | {known['before']['category_correct']:.3f} "
        f"| {known['before']['right_ticket_in_top_3']:.3f} |",
        f"| After | {known['after']['category_correct']:.3f} "
        f"| {known['after']['right_ticket_in_top_3']:.3f} |",
        "",
        "## Discovery: grouping 40 new-class complaints and 30 off-topic questions",
        "",
        f"A group needs at least {MIN_GROUP} complaints. A 'clean' group holds one real class only.",
        "",
        "| Similarity needed to link two complaints | Groups proposed | Clean groups "
        "| New classes found (of 2) | New complaints placed in a group | Off-topic questions in a group |",
        "|---|---|---|---|---|---|",
    ]
    for row in discovery:
        lines.append(
            f"| {row['threshold']:.3f} | {row['groups']} | {row['pure_groups']} | {row['classes_found']} "
            f"| {row['new_complaints_grouped']:.3f} | {row['off_topic_in_a_group']} |"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure how new data and new classes are handled.")
    parser.add_argument("--keep", action="store_true", help="leave the new tickets and classes in place")
    args = parser.parse_args()

    gateway = Gateway(
        os.environ.get("GATEWAY_URL", "http://gateway:8000"),
        os.environ.get("GATEWAY_ADMIN_API_KEY", "dev-admin-key"),
    )
    triage = TriageClient(os.environ.get("TRIAGE_URL", "http://triage:8001"), timeout=60)
    retrieval = RetrievalClient(os.environ.get("RETRIEVAL_URL", "http://retrieval:8002"), timeout=60)
    embedder = EmbeddingClient(os.environ.get("EMBEDDING_URL", "http://embedding:8004"), timeout=120)

    tickets = read_jsonl(GENERATED / "holdout_tickets.jsonl")
    articles = read_jsonl(GENERATED / "holdout_kb_articles.jsonl")
    new_complaints = read_jsonl(GENERATED / "holdout_test_queries.jsonl")
    off_topic = read_jsonl(GENERATED / "out_of_scope.jsonl")
    known_sample = read_jsonl(GENERATED / "test_queries.jsonl")[::3]  # every third one: 120 complaints
    by_type: dict[str, list[dict]] = defaultdict(list)
    for ticket in sorted(tickets, key=lambda t: t["id"]):
        by_type[ticket["scenario_id"]].append(ticket)

    try:
        gateway.call("GET", "/v1/ingest/status")
    except httpx.HTTPError as error:
        sys.exit(f"The gateway is not reachable ({error}). Run: docker compose up -d")

    # Start clean: a run that was stopped half-way (or used --keep) must not help this one.
    leftovers = [by_type[name][0]["id"] for name in by_type] + [article["id"] for article in articles]
    statuses = [gateway.call("GET", f"/v1/documents/{doc}", expect=(200, 404)) for doc in leftovers]
    if any(s.status_code == 200 and s.json()["is_active"] for s in statuses):
        print("Found hold-out documents from an earlier run. Removing them first ...")
        reset(gateway, tickets, articles)

    started = time.monotonic()
    print("Part 1: before anything is added")
    known = {"before": measure(triage, retrieval, known_sample)}
    curve = [{"label": "0 (before)", "seconds": None, **measure(triage, retrieval, new_complaints)}]
    print(
        f"  new complaints: category correct {curve[0]['category_correct']:.3f}, "
        f"sent for review {curve[0]['flagged_for_review']:.3f}, "
        f"right ticket in top 3 {curve[0]['right_ticket_in_top_3']:.3f}"
    )

    refused = gateway.call("POST", "/v1/tickets", expect=(422,), json=ticket_body(tickets[0]))
    print(f"  a ticket with a class that does not exist is refused: {refused.json()['detail']}")

    print("\nPart 2: add the two classes, then resolved tickets, a few at a time")
    for name, description in NEW_CLASSES.items():
        gateway.call(
            "POST", "/v1/taxonomy", json={"kind": "category", "name": name, "description": description}
        )
    added = 0
    for step in STEPS:
        batch = [ticket for rows in by_type.values() for ticket in rows[added:step]]
        sent = time.monotonic()
        for ticket in batch:
            gateway.call("POST", "/v1/tickets", json=ticket_body(ticket))
        gateway.wait_until_indexed()
        seconds = time.monotonic() - sent
        added = step
        row = {"label": str(step), "seconds": seconds, **measure(triage, retrieval, new_complaints)}
        curve.append(row)
        print(
            f"  {step:>2} per type ({len(batch)} tickets searchable after {seconds:.1f}s): "
            f"category {row['category_correct']:.3f}, right ticket top 3 {row['right_ticket_in_top_3']:.3f}"
        )

    print("\nPart 3: add the 4 knowledge-base articles")
    sent = time.monotonic()
    for article in articles:
        body = {key: article[key] for key in ("title", "body", "category", "product", "scenario_id")}
        gateway.call("PUT", f"/v1/kb/{article['id']}", json=body)
    gateway.wait_until_indexed()
    row = {"label": f"{STEPS[-1]} + articles", "seconds": time.monotonic() - sent}
    curve.append({**row, **measure(triage, retrieval, new_complaints)})
    print(f"  right article in top 2: {curve[-1]['right_article_in_top_2']:.3f}")

    print("  labels given to the new complaints now (real class -> label given):")
    for pair, count in curve[-1]["labels_given"].items():
        print(f"    {count:>2}  {pair}")

    print("\nPart 4: are the old classes still handled as well?")
    known["after"] = measure(triage, retrieval, known_sample)
    for moment in ("before", "after"):
        print(
            f"  {moment:<6}: category {known[moment]['category_correct']:.3f}, "
            f"right ticket in top 3 {known[moment]['right_ticket_in_top_3']:.3f}"
        )

    print("\nPart 5: discovery (grouping the new complaints and the off-topic questions)")
    discovery, proposals = discovery_table(embedder, new_complaints, off_topic, known_sample)
    real = {c["id"]: c["category"] for c in new_complaints}
    for proposal in proposals:
        inside = Counter(real.get(member, "off-topic") for member in proposal.member_ids)
        print(f"  group of {proposal.size}: keywords [{', '.join(proposal.keywords)}], really {dict(inside)}")

    if args.keep:
        print("\nThe new tickets, articles and classes were LEFT IN (--keep).")
        print("Run this script again without --keep before running the other evals.")
    else:
        print("\nCleaning up")
        reset(gateway, tickets, articles)
        leftover = retrieval.search(new_complaints[0]["complaint"], top_k_tickets=10, top_k_kb=3)["results"]
        still_there = [r["id"] for r in leftover if r["scenario_id"] in by_type]
        print(f"  hold-out documents still found by search: {len(still_there)} (should be 0)")

    model = embedder.embed(["model check"]).get("model") or "unknown"
    table = to_markdown(curve, known, discovery, model)
    print("\n" + table)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "evolving.md").write_text(table + "\n", encoding="utf-8", newline="\n")
    (RESULTS_DIR / "evolving.json").write_text(
        json.dumps({"curve": curve, "known": known, "discovery": discovery}, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    minutes = (time.monotonic() - started) / 60
    print(f"\nSaved to evals/results/evolving.md and evolving.json ({minutes:.1f} minutes)")


if __name__ == "__main__":
    main()
