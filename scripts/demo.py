"""Send one complaint to the gateway and print the result.

  docker compose run --rm tools python scripts/demo.py
  docker compose run --rm tools python scripts/demo.py "I was charged twice this month"

It makes the same two calls as the web page: a quick one (labels + sources),
then the full one (labels + sources + drafted resolution).
"""

from __future__ import annotations

import os
import sys
import time

import httpx

EXAMPLE = (
    "My broadband drops every evening around 8 and I've already restarted the router twice, "
    "I work from home and this is costing me"
)
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://gateway:8000")
API_KEY = os.environ.get("GATEWAY_API_KEY", "dev-local-key")


def resolve(http: httpx.Client, complaint: str, generate: bool) -> tuple[dict, float]:
    started = time.monotonic()
    try:
        response = http.post("/v1/resolve", json={"complaint": complaint, "generate": generate})
    except httpx.HTTPError as error:
        sys.exit(f"The gateway is not reachable at {GATEWAY_URL} ({error}). Run: docker compose up -d")
    if response.status_code != 200:
        sys.exit(f"The gateway answered {response.status_code}: {response.text}")
    return response.json(), time.monotonic() - started


def main() -> None:
    complaint = " ".join(sys.argv[1:]).strip() or EXAMPLE
    print(f"COMPLAINT\n  {complaint}\n")

    with httpx.Client(base_url=GATEWAY_URL, headers={"X-API-Key": API_KEY}, timeout=300) as http:
        quick, seconds = resolve(http, complaint, generate=False)

        triage = quick["triage"]
        print(f"TRIAGE  ({seconds:.1f}s)")
        if triage is None:
            print("  not available right now\n")
        else:
            category, product, severity = triage["category"], triage["product"], triage["severity"]
            print(f"  category : {category['label']} (confidence {category['confidence']})")
            print(f"  product  : {product['label']} (confidence {product['confidence']})")
            print(f"  severity : {severity['label']} {severity['reasons']}")
            print(f"  sentiment: {triage['sentiment']['label']}")
            if triage["needs_review"]:
                print("  NEEDS REVIEW: does not clearly match a known category")
            print()

        print(f"SOURCES FOUND  (closest similarity {quick['meta']['top_similarity']:.2f})")
        for source in quick["sources"]:
            kind, similarity = source["source_type"], source["similarity"]
            print(f"  [{source['id']}] {kind:<6} similarity {similarity:.2f}  {source['title']}")
        print()

        print("Writing the resolution (a local model on a CPU can take up to a minute) ...")
        full, seconds = resolve(http, complaint, generate=True)

    meta, answer = full["meta"], full["resolution"]
    origin = "from cache" if meta["cached"] else "fresh"
    print(f"\nRESOLUTION  ({seconds:.1f}s, {origin}, request {full['request_id']})")
    if answer is None:
        print(f"  No resolution was drafted. {full['escalation_reason']}")
        return
    print(f"  mode: {answer['mode']}   grounded: {answer['grounded']}   model: {meta['model']}")
    if answer["fallback_reason"]:
        print(f"  note: the model was not used ({answer['fallback_reason']})")
    print(f"  Likely cause: {answer['summary']}")
    if answer["already_tried"]:
        print(f"  Customer already tried: {'; '.join(answer['already_tried'])}")
    for step in answer["steps"]:
        flags = "" if step["verified"] else "  [NOT VERIFIED]"
        flags += "  [REPEATS WHAT THE CUSTOMER TRIED]" if step["repeats_already_tried"] else ""
        print(f"  {step['n']}. {step['text']}")
        print(f"       sources: {', '.join(step['citations'])}   support {step['support']:.2f}{flags}")
    if full["escalate"]:
        print(f"  ESCALATE: {full['escalation_reason']}")
    print(f"\n  timings (ms): {meta['latency_ms']}")


if __name__ == "__main__":
    main()
