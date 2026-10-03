"""Measure the quality of the final answers, through the gateway, the way an agent would get them.

Run (all services up, data loaded, Ollama running):
  docker compose run --rm tools python evals/eval_answers.py               # about 30 minutes on a CPU
  docker compose run --rm tools python evals/eval_answers.py --answers 5   # a quick look, about 8 minutes

Part 1 (fast, no language model): the "nothing similar enough" checkpoint.
  Every test complaint goes through the quick path. For a range of cut-off values we count how
  many off-topic questions would be stopped, and how many real complaints would be stopped by mistake.

Part 2 (slow, with the language model): the drafted answers. Three groups of complaints:
  known       problems the knowledge base covers. A good answer cites the RIGHT problem's sources.
  new class   real telecom problems the knowledge base does NOT cover (eSIM, fraud).
              There is no right answer to give, so the honest outcome is to escalate.
  off topic   not telecom at all. These must be refused without asking the model.

The answer key comes from the dataset: every ticket and article belongs to a scenario, so a
citation can be checked against the scenario the complaint was written from. No human grading
and no second model are needed.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

import httpx

from services.gateway.config import Settings

ROOT = Path(__file__).resolve().parents[1]
GENERATED = ROOT / "data" / "generated"
RESULTS_DIR = ROOT / "evals" / "results"

CUT_OFFS = [0.55, 0.60, 0.65, 0.70, 0.72, 0.75, 0.80]
OUTCOMES = ["right", "partly", "wrong", "escalated"]


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def share(flags: list[bool]) -> float:
    return round(sum(flags) / len(flags), 3) if flags else 0.0


def judge(response: dict, scenario: str | None, scenario_of: dict[str, str | None]) -> dict:
    """Compare one gateway response with the answer key.

    outcome:
      right      every cited source is about the complaint's real problem
      partly     the right problem is cited, mixed with sources about another problem
      wrong      an answer was drafted, but from sources about a different problem
      escalated  no answer was drafted (nothing similar enough, or the model said so)
    """
    resolution = response["resolution"]
    steps = resolution["steps"] if resolution else []
    cited = {scenario_of.get(source) for step in steps for source in step["citations"]} - {None}
    if not steps:
        outcome = "escalated"
    elif scenario in cited:
        outcome = "right" if len(cited) == 1 else "partly"
    else:
        outcome = "wrong"
    return {
        "outcome": outcome,
        "mode": resolution["mode"] if resolution else None,
        "steps": len(steps),
        "unverified_steps": sum(not step["verified"] for step in steps),
        "repeated_steps": sum(step["repeats_already_tried"] for step in steps),
        "noticed_already_tried": bool(resolution and resolution["already_tried"]),
        "grounded": bool(resolution and resolution["grounded"]),
        # Was a source about the right problem among those the search handed to the model?
        "right_source_found": any(scenario_of.get(s["id"]) == scenario for s in response["sources"]),
        "top_similarity": response["meta"]["top_similarity"],
        "model_used": response["meta"]["model"] is not None,
        "prompt_version": response["meta"].get("prompt_version"),
    }


def checkpoint_table(similarities: dict[str, list[float]], cut_offs: list[float]) -> list[dict]:
    """For each cut-off: the share of each group that would be stopped (similarity below it)."""
    return [
        {
            "cut_off": cut_off,
            **{name: share([value < cut_off for value in values]) for name, values in similarities.items()},
        }
        for cut_off in cut_offs
    ]


def summarise(rows: list[dict]) -> dict:
    """Totals for one group of judged answers."""
    counts = Counter(row["outcome"] for row in rows)
    drafted = [row for row in rows if row["outcome"] != "escalated"]
    steps = sum(row["steps"] for row in drafted)
    return {
        "complaints": len(rows),
        **{outcome: counts.get(outcome, 0) for outcome in OUTCOMES},
        "answer_drafted": share([row["outcome"] != "escalated" for row in rows]),
        "written_by_model": share([row["mode"] == "llm" for row in drafted]),
        "every_step_backed": share([row["grounded"] for row in drafted]),
        "unverified_steps": round(sum(row["unverified_steps"] for row in drafted) / max(steps, 1), 3),
        "model_asked": share([row["model_used"] for row in rows]),
    }


class Gateway:
    def __init__(self, base_url: str, key: str) -> None:
        self._http = httpx.Client(base_url=base_url, headers={"X-API-Key": key}, timeout=300)

    def resolve(self, complaint: str, generate: bool) -> tuple[dict, float]:
        body = {"complaint": complaint, "generate": generate, "use_cache": False}
        for _ in range(5):
            started = time.monotonic()
            try:
                response = self._http.post("/v1/resolve", json=body)
            except httpx.HTTPError as error:
                sys.exit(f"The gateway is not reachable ({error}). Run: docker compose up -d")
            if response.status_code == 429:
                time.sleep(int(response.headers.get("Retry-After", "5")) + 1)
                continue
            if response.status_code != 200:
                sys.exit(f"The gateway answered {response.status_code}: {response.text}")
            return response.json(), time.monotonic() - started
        sys.exit("The gateway kept answering 'too many requests'")


def run_group(gateway: Gateway, name: str, items: list[dict], scenario_of: dict, done: int, total: int):
    rows = []
    for item in items:
        response, seconds = gateway.resolve(item["complaint"], generate=True)
        row = {"id": item["id"], "group": name, "seconds": round(seconds, 1)}
        row |= judge(response, item.get("scenario_id"), scenario_of)
        row["already_tried_in_complaint"] = bool(item.get("already_tried"))
        rows.append(row)
        done += 1
        print(
            f"  [{done:>2}/{total}] {item['id']:<8} {row['outcome']:<9} "
            f"{seconds:>5.0f}s  {row['mode'] or '-':<10} {row['steps']} steps"
        )
    return rows, done


def to_markdown(checkpoint: list[dict], current: float, groups: dict, notes: list[str], model: str) -> str:
    lines = [
        "# Answer quality, end to end",
        "",
        f"Language model: {model}. Every complaint went through the gateway, as an agent's would.",
        "",
    ]
    if checkpoint:
        lines += [
            "## The 'nothing similar enough' checkpoint",
            "",
            f"Share of each group that would be stopped at each cut-off. The gateway uses {current}.",
            "",
            "| Cut-off | Off-topic stopped (want high) | Known complaints stopped (want low) "
            "| New-class complaints stopped |",
            "|---|---|---|---|",
        ]
    for row in checkpoint:
        marker = " (in use)" if row["cut_off"] == current else ""
        lines.append(
            f"| {row['cut_off']:.2f}{marker} | {row['off_topic']:.3f} | {row['known']:.3f} "
            f"| {row['new_class']:.3f} |"
        )
    lines += [
        "" if checkpoint else "(checkpoint part skipped)",
        "## The drafted answers",
        "",
        "| Group | Complaints | Cites the right problem | Right, mixed with another "
        "| Cites a wrong problem | Escalated, no answer | Every step backed by its source "
        "| Written by the model |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name, summary in groups.items():
        lines.append(
            f"| {name} | {summary['complaints']} | {summary['right']} | {summary['partly']} "
            f"| {summary['wrong']} | {summary['escalated']} | {summary['every_step_backed']:.3f} "
            f"| {summary['written_by_model']:.3f} |"
        )
    lines += ["", "## Details", ""]
    lines += [f"- {text}" for text in notes]
    return "\n".join(lines)


def build_notes(by_group: dict[str, list[dict]], groups: dict[str, dict]) -> list[str]:
    """The findings that do not fit in the table, as plain sentences."""
    known = by_group["known"]
    drafted = [row for row in known if row["outcome"] != "escalated"]
    wrong = [row for row in known if row["outcome"] == "wrong"]
    tried = [row for row in drafted if row["already_tried_in_complaint"]]
    new_drafted = sum(row["outcome"] != "escalated" for row in by_group["new_class"])
    off = groups["Off topic"]
    notes = [
        f"Known problems: {share([row['outcome'] in ('right', 'partly') for row in known]):.0%} "
        "of the answers cite the right problem.",
        f"Of the {len(wrong)} wrong answers, the search had found a right source for "
        f"{sum(row['right_source_found'] for row in wrong)} (the model picked the wrong one) "
        f"and had not for {sum(not row['right_source_found'] for row in wrong)} (the search missed it).",
        f"The customer said what they already tried in {len(tried)} complaints: the answer listed it "
        f"in {sum(row['noticed_already_tried'] for row in tried)}, and repeated it as a step "
        f"in {sum(row['repeated_steps'] > 0 for row in tried)}.",
        f"Steps not backed by their cited source: {groups['Known problems']['unverified_steps']:.1%} "
        "of the steps in answers to known problems.",
        f"Off-topic questions: {off['escalated']} of {off['complaints']} refused. "
        f"The model was asked for {sum(row['model_used'] for row in by_group['off_topic'])} of them.",
        f"New-class complaints: an answer was drafted for {new_drafted} of {len(by_group['new_class'])}, "
        "although the knowledge base holds no fix for them.",
    ]
    refused = {
        name: sum(row["outcome"] == "escalated" and row["model_used"] for row in rows)
        for name, rows in by_group.items()
    }
    notes.append(
        "Answers the model itself withheld because the sources were about another problem: "
        f"{refused['known']} known, {refused['new_class']} new-class, {refused['off_topic']} off-topic."
    )
    seconds = [row["seconds"] for rows in by_group.values() for row in rows if row["mode"] == "llm"]
    if seconds:
        notes.append(
            f"Time per answer written by the model: typical {statistics.median(seconds):.0f}s, "
            f"slowest {max(seconds):.0f}s."
        )
    elif drafted:
        notes.append("NOTE: no answer was written by the model in this run. Is Ollama running?")
    return notes


def spread(items: list[dict], count: int) -> list[dict]:
    """Pick `count` items evenly from the list, so every problem type gets a turn."""
    if count <= 0:
        return []
    return items[:: max(1, len(items) // count)][:count]


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure the quality of the final answers.")
    parser.add_argument("--answers", type=int, default=20, help="known complaints to draft answers for")
    parser.add_argument("--new-class", type=int, default=6, help="new-class complaints to draft for")
    parser.add_argument("--skip-checkpoint", action="store_true", help="skip part 1")
    args = parser.parse_args()

    gateway = Gateway(
        os.environ.get("GATEWAY_URL", "http://gateway:8000"),
        # The admin key is used only because it has a higher request limit.
        os.environ.get("GATEWAY_ADMIN_API_KEY", "dev-admin-key"),
    )
    known = read_jsonl(GENERATED / "test_queries.jsonl")
    new_class = read_jsonl(GENERATED / "holdout_test_queries.jsonl")
    off_topic = read_jsonl(GENERATED / "out_of_scope.jsonl")
    documents = read_jsonl(GENERATED / "tickets.jsonl") + read_jsonl(GENERATED / "kb_articles.jsonl")
    scenario_of = {row["id"]: row.get("scenario_id") for row in documents}

    started = time.monotonic()
    checkpoint: list[dict] = []
    if not args.skip_checkpoint:
        count = len(known) + len(new_class) + len(off_topic)
        print(f"Part 1: the checkpoint ({count} quick requests, a few minutes)")
        similarities = {}
        for name, items in (("known", known), ("new_class", new_class), ("off_topic", off_topic)):
            values = [
                gateway.resolve(item["complaint"], generate=False)[0]["meta"]["top_similarity"]
                for item in items
            ]
            similarities[name] = values
            print(
                f"  {name:<10} closest match: lowest {min(values):.2f}, "
                f"middle {statistics.median(values):.2f}"
            )
        checkpoint = checkpoint_table(similarities, CUT_OFFS)
        for row in checkpoint:
            print(
                f"  cut-off {row['cut_off']:.2f}: off-topic stopped {row['off_topic']:.3f}, "
                f"known stopped {row['known']:.3f}, new-class stopped {row['new_class']:.3f}"
            )

    known_sample, new_sample = spread(known, args.answers), spread(new_class, args.new_class)
    slow = len(known_sample) + len(new_sample)
    total = slow + len(off_topic)
    print(f"\nPart 2: drafting {slow} answers (about {slow * 65 / 60:.0f} minutes on a CPU)")

    done, by_group = 0, {}
    for name, items in (("known", known_sample), ("new_class", new_sample), ("off_topic", off_topic)):
        by_group[name], done = run_group(gateway, name, items, scenario_of, done, total)

    groups = {
        "Known problems": summarise(by_group["known"]),
        "New class (no fix exists)": summarise(by_group["new_class"]),
        "Off topic": summarise(by_group["off_topic"]),
    }
    notes = build_notes(by_group, groups)

    model = "unknown"
    try:
        url = os.environ.get("GENERATION_URL", "http://generation:8003")
        ready = httpx.get(f"{url}/ready", timeout=10).json()
        model = ready.get("llm_model", model) + ("" if ready.get("llm_available") else " (NOT available)")
    except (httpx.HTTPError, ValueError):
        pass

    versions = [row["prompt_version"] for rows in by_group.values() for row in rows if row["prompt_version"]]
    prompt_version = versions[0] if versions else "none"
    table = to_markdown(
        checkpoint, Settings().min_similarity, groups, notes, f"{model}, prompt {prompt_version}"
    )
    print("\n" + table)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    # One result file per prompt version, so a prompt change can be compared with the one before.
    name = f"answers_{prompt_version}"
    (RESULTS_DIR / f"{name}.md").write_text(table + "\n", encoding="utf-8", newline="\n")
    answers = [row for rows in by_group.values() for row in rows]
    (RESULTS_DIR / f"{name}.json").write_text(
        json.dumps({"checkpoint": checkpoint, "groups": groups, "answers": answers}, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    minutes = (time.monotonic() - started) / 60
    print(f"\nSaved to evals/results/{name}.md and {name}.json ({minutes:.0f} minutes)")


if __name__ == "__main__":
    main()
