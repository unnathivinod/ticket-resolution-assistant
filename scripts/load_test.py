"""Load test: many agents press Resolve at the same time. How fast does it stay, and where does it slow first?

  docker compose run --rm tools python scripts/load_test.py
  docker compose run --rm tools python scripts/load_test.py --users 1 5 10 20 40 --seconds 30

The test runs in steps. In each step a fixed number of "agents" send complaints one after another,
as fast as the system answers, for a fixed time. For every step it reports:

  requests per second    how much work got done
  typical time (p50)     half of the requests were faster than this
  slow time (p95)        19 of 20 requests were faster than this
  errors                 anything that was not a normal answer

It uses the quick path (labels and sources, no drafting), because the drafting speed belongs to
the language model provider, not to this system. Every request does real work: the test complaints
are all different, and nothing on this path is cached.

Before a run, lift the rate limit, or the limiter will (correctly) refuse most of the test:

  PowerShell:  $env:RATE_LIMIT_PER_MINUTE="1000000"; docker compose up -d gateway
  afterwards:  Remove-Item Env:RATE_LIMIT_PER_MINUTE; docker compose up -d gateway

The test complaints are not counted for incident detection, so agents see no false notice.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import sys
import threading
import time
from pathlib import Path

import httpx

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://gateway:8000")
API_KEY = os.environ.get("GATEWAY_API_KEY", "dev-local-key")
ROOT = Path(__file__).resolve().parents[1]
COMPLAINTS = ROOT / "data" / "generated" / "test_queries.jsonl"
RESULTS = ROOT / "evals" / "results" / "load_test.json"


def percentile(values: list[float], share: float) -> float:
    """The value below which `share` of the values lie (0.5 = the middle one). 0 for an empty list."""
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered), max(1, math.ceil(share * len(ordered)))) - 1]


def summarise(users: int, seconds: float, samples: list[dict]) -> dict:
    """Turn the raw results of one step into the numbers of one table row.

    samples: one dict per request: {"status": 200, "seconds": 0.21, "triage_ms": 40, "retrieval_ms": 90}
    """
    ok = [sample for sample in samples if sample["status"] == 200]
    times = [sample["seconds"] for sample in ok]
    return {
        "users": users,
        "requests": len(samples),
        "per_second": round(len(ok) / seconds, 1) if seconds else 0.0,
        "p50_ms": round(percentile(times, 0.5) * 1000),
        "p95_ms": round(percentile(times, 0.95) * 1000),
        "slowest_ms": round(max(times, default=0.0) * 1000),
        "triage_p50_ms": round(
            percentile([s["triage_ms"] for s in ok if s.get("triage_ms") is not None], 0.5)
        ),
        "retrieval_p50_ms": round(
            percentile([s["retrieval_ms"] for s in ok if s.get("retrieval_ms") is not None], 0.5)
        ),
        "errors": len(samples) - len(ok),
        "rate_limited": sum(sample["status"] == 429 for sample in samples),
    }


def reading(rows: list[dict]) -> str:
    """One plain sentence about where the system stopped getting faster."""
    clean = [row for row in rows if row["requests"] and not row["errors"]]
    if len(clean) < 2:
        return "Not enough clean steps to say where the limit is."
    best = max(row["per_second"] for row in clean)
    # The first step that already gets within 15% of the best: from there on, more users add little.
    knee = next(index for index, row in enumerate(clean) if row["per_second"] >= 0.85 * best)
    last = clean[-1]
    if knee == len(clean) - 1 and last["per_second"] > 1.15 * clean[-2]["per_second"]:
        return (
            f"Throughput was still growing at {last['users']} users "
            f"({last['per_second']} requests per second). "
            "The limit of this machine was not reached: run again with more users."
        )
    lowest = min(row["per_second"] for row in clean[knee:])
    return (
        f"Throughput levelled off between {lowest} and {best} requests per second, "
        f"from {clean[knee]['users']} users on. More users after that only wait longer: "
        "that is the capacity of one copy of each service on this machine."
    )


def load_complaints() -> list[str]:
    with COMPLAINTS.open(encoding="utf-8") as lines:
        complaints = [json.loads(line)["complaint"] for line in lines if line.strip()]
    if not complaints:
        sys.exit(f"No test complaints in {COMPLAINTS}. Run scripts/generate_data.py first.")
    return complaints


def run_step(users: int, seconds: float, complaints: list[str], offset: int) -> list[dict]:
    """`users` threads send complaints one after another until the time is up."""
    samples: list[dict] = []
    lock = threading.Lock()
    stop_at = time.monotonic() + seconds

    def agent(number: int) -> None:
        position = offset + number * 7919  # every agent walks the list from a different place
        with httpx.Client(base_url=GATEWAY_URL, headers={"X-API-Key": API_KEY}, timeout=60) as http:
            while time.monotonic() < stop_at:
                complaint = complaints[position % len(complaints)]
                position += 1
                body = {"complaint": complaint, "generate": False, "track_incident": False}
                started = time.monotonic()
                try:
                    response = http.post("/v1/resolve", json=body)
                    sample = {"status": response.status_code, "seconds": time.monotonic() - started}
                    if response.status_code == 200:
                        stages = response.json()["meta"]["latency_ms"]
                        sample["triage_ms"] = stages.get("triage")
                        sample["retrieval_ms"] = stages.get("retrieval")
                except httpx.HTTPError:
                    sample = {"status": 0, "seconds": time.monotonic() - started}  # no answer at all
                with lock:
                    samples.append(sample)
                if sample["status"] == 429:
                    time.sleep(0.5)  # refused by the limiter: do not hammer it

    threads = [threading.Thread(target=agent, args=(number,), daemon=True) for number in range(users)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return samples


def main() -> None:
    parser = argparse.ArgumentParser(description="Send many complaints at once and measure speed.")
    parser.add_argument("--users", type=int, nargs="+", default=[1, 5, 10, 20], help="agents per step")
    parser.add_argument("--seconds", type=float, default=20, help="how long each step lasts")
    args = parser.parse_args()

    try:
        ready = httpx.get(f"{GATEWAY_URL}/ready", timeout=10).status_code == 200
    except httpx.HTTPError:
        ready = False
    if not ready:
        sys.exit(f"The gateway is not ready at {GATEWAY_URL}. Run: docker compose up -d")
    complaints = load_complaints()

    print(f"Load test: {len(args.users)} steps of {args.seconds:.0f} s, quick path (labels and sources).\n")
    header = (
        f"{'users':>6} {'requests':>9} {'per second':>11} {'typical':>9} "
        f"{'slow (p95)':>11} {'slowest':>9} {'errors':>7}"
    )
    print(header)
    rows, offset = [], 0
    for users in args.users:
        started = time.monotonic()
        samples = run_step(users, args.seconds, complaints, offset)
        row = summarise(users, time.monotonic() - started, samples)
        rows.append(row)
        offset += len(samples)
        print(
            f"{row['users']:>6} {row['requests']:>9} {row['per_second']:>11} {row['p50_ms']:>6} ms "
            f"{row['p95_ms']:>8} ms {row['slowest_ms']:>6} ms {row['errors']:>7}"
        )
        if row["rate_limited"] > row["requests"] / 10:
            print(
                "\nMost requests were refused by the rate limit, so these numbers measure the limiter.\n"
                "Lift it for the test (PowerShell):\n"
                '  $env:RATE_LIMIT_PER_MINUTE="1000000"; docker compose up -d gateway\n'
                "and afterwards:\n"
                "  Remove-Item Env:RATE_LIMIT_PER_MINUTE; docker compose up -d gateway"
            )
            sys.exit(1)

    widest = rows[-1]
    print(
        f"\nWhere the time goes at {widest['users']} users (typical): "
        f"triage {widest['triage_p50_ms']} ms, search {widest['retrieval_p50_ms']} ms."
    )
    print(reading(rows))

    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    saved = {
        "path": "quick (labels and sources, no drafting)",
        "seconds_per_step": args.seconds,
        "machine": f"{platform.machine()}, {os.cpu_count()} CPUs visible to the container",
        "steps": rows,
        "reading": reading(rows),
    }
    RESULTS.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")
    print(f"\nSaved to {RESULTS.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
