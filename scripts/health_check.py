"""One command that answers: is the whole system healthy right now?

  docker compose run --rm tools python scripts/health_check.py

It checks that every service is up, sends two test complaints through the gateway (one normal,
one off-topic), checks that new data is not stuck, and asks Prometheus whether any alert is firing.
It ends with exit code 1 if something is wrong, so it can also be run on a schedule.
"""

from __future__ import annotations

import os
import sys
import time

import httpx

GATEWAY = os.environ.get("GATEWAY_URL", "http://gateway:8000")
EMBEDDING = os.environ.get("EMBEDDING_URL", "http://embedding:8004")
GENERATION = os.environ.get("GENERATION_URL", "http://generation:8003")
INGESTION = os.environ.get("INGESTION_URL", "http://ingestion:8005")
PROMETHEUS = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
AGENT = {"X-API-Key": os.environ.get("GATEWAY_API_KEY", "dev-local-key")}
ADMIN = {"X-API-Key": os.environ.get("GATEWAY_ADMIN_API_KEY", "dev-admin-key")}

NORMAL = (
    "My broadband drops every evening around 8 and I've already restarted the router twice, "
    "I work from home and this is costing me"
)
OFF_TOPIC = "What is the best recipe for a chocolate cake?"

OK, WARN, FAIL = "OK", "WARN", "FAIL"


def check_services(http: httpx.Client) -> list[tuple[str, str, str]]:
    rows = []
    try:
        response = http.get(f"{GATEWAY}/ready")
        checks = response.json().get("checks") or response.json().get("detail", {}).get("checks", {})
    except (httpx.HTTPError, ValueError) as error:
        return [("gateway", FAIL, f"not reachable: {error.__class__.__name__}")]
    rows.append(("gateway", OK if response.status_code == 200 else FAIL, f"HTTP {response.status_code}"))
    for name in ("triage", "retrieval", "generation", "database"):
        rows.append((name, OK if checks.get(name) else FAIL, "ready" if checks.get(name) else "not ready"))
    for name, url in (("embedding", f"{EMBEDDING}/ready"), ("ingestion worker", f"{INGESTION}/metrics")):
        try:
            status = http.get(url).status_code
            rows.append((name, OK if status == 200 else FAIL, f"HTTP {status}"))
        except httpx.HTTPError as error:
            rows.append((name, FAIL, f"not reachable: {error.__class__.__name__}"))
    return rows


def check_model(http: httpx.Client) -> tuple[str, str, str]:
    try:
        body = http.get(f"{GENERATION}/ready").json()
    except (httpx.HTTPError, ValueError):
        return ("language model", FAIL, "generation service not reachable")
    if body.get("llm_available"):
        return ("language model", OK, f"{body.get('llm_model')} is available")
    # Not a failure: answers are then quoted from the best source instead of drafted.
    return ("language model", WARN, f"{body.get('llm_model')} not available, answers are quoted from sources")


def check_normal_complaint(http: httpx.Client) -> tuple[str, str, str]:
    started = time.monotonic()
    try:
        response = http.post(
            f"{GATEWAY}/v1/resolve", json={"complaint": NORMAL, "generate": False}, headers=AGENT
        )
        body = response.json()
    except (httpx.HTTPError, ValueError) as error:
        return ("test complaint", FAIL, f"{error.__class__.__name__}")
    seconds = time.monotonic() - started
    if response.status_code != 200:
        return ("test complaint", FAIL, f"HTTP {response.status_code}: {str(body)[:80]}")
    problems = []
    if not body["triage"]:
        problems.append("no labels")
    if not body["sources"]:
        problems.append("no sources")
    if not body["meta"]["confident_match"]:
        problems.append(f"closest match only {body['meta']['top_similarity']:.2f}")
    if problems:
        return ("test complaint", FAIL, ", ".join(problems))
    label = body["triage"]["category"]["best_guess"]
    detail = f"{label}, {len(body['sources'])} sources, similarity {body['meta']['top_similarity']:.2f}"
    return ("test complaint", OK, f"{detail}, {seconds:.1f}s")


def check_off_topic(http: httpx.Client) -> tuple[str, str, str]:
    # The quick path is enough: it shows whether the "nothing similar enough" checkpoint would
    # stop this question, without asking the model and without writing to the audit log.
    try:
        response = http.post(
            f"{GATEWAY}/v1/resolve", json={"complaint": OFF_TOPIC, "generate": False}, headers=AGENT
        )
        body = response.json()
    except (httpx.HTTPError, ValueError) as error:
        return ("off-topic question", FAIL, f"{error.__class__.__name__}")
    if response.status_code != 200:
        return ("off-topic question", FAIL, f"HTTP {response.status_code}")
    similarity = body["meta"]["top_similarity"]
    if not body["meta"]["confident_match"]:
        return ("off-topic question", OK, f"would be escalated, closest match only {similarity:.2f}")
    return ("off-topic question", FAIL, f"treated as a telecom complaint (similarity {similarity:.2f})")


def check_ingestion(http: httpx.Client) -> tuple[str, str, str]:
    try:
        response = http.get(f"{GATEWAY}/v1/ingest/status", headers=ADMIN)
        waiting = response.json()["documents_waiting"]
    except (httpx.HTTPError, ValueError, KeyError) as error:
        return ("new data", FAIL, f"could not read the status: {error.__class__.__name__}")
    if waiting == 0:
        return ("new data", OK, "the search index is up to date")
    # A few documents on their way is normal. Many means the worker is behind or stuck.
    return ("new data", WARN if waiting < 100 else FAIL, f"{waiting} documents waiting to be indexed")


def check_alerts(http: httpx.Client) -> tuple[str, str, str]:
    try:
        alerts = http.get(f"{PROMETHEUS}/api/v1/alerts").json()["data"]["alerts"]
    except (httpx.HTTPError, ValueError, KeyError):
        return ("alerts", WARN, "Prometheus is not reachable, so alerts are unknown")
    firing = sorted({alert["labels"]["alertname"] for alert in alerts if alert["state"] == "firing"})
    if not firing:
        return ("alerts", OK, "none firing")
    critical = any(a["labels"].get("severity") == "critical" for a in alerts if a["state"] == "firing")
    return ("alerts", FAIL if critical else WARN, "firing: " + ", ".join(firing))


def run_checks() -> list[tuple[str, str, str]]:
    with httpx.Client(timeout=30) as http:
        rows = check_services(http)
        if rows[0][1] == FAIL and len(rows) == 1:
            return rows  # without the gateway nothing else can be checked
        rows.append(check_model(http))
        rows.append(check_normal_complaint(http))
        rows.append(check_off_topic(http))
        rows.append(check_ingestion(http))
        rows.append(check_alerts(http))
    return rows


def main() -> None:
    rows = run_checks()
    width = max(len(name) for name, _, _ in rows)
    for name, result, detail in rows:
        print(f"  {name:<{width}}  {result:<4}  {detail}")
    failed = [name for name, result, _ in rows if result == FAIL]
    warned = [name for name, result, _ in rows if result == WARN]
    if failed:
        print(f"\nNOT HEALTHY: {', '.join(failed)}")
        sys.exit(1)
    print("\nHealthy." + (f" Worth a look: {', '.join(warned)}." if warned else ""))


if __name__ == "__main__":
    main()
