"""Builds the Grafana dashboard file from the short description below.

  docker compose run --rm tools python infra/grafana/build_dashboard.py

A dashboard file is hundreds of lines of JSON. Keeping the panels as a short list here makes
them easy to read and review. A test checks that the JSON file matches this script.
"""

from __future__ import annotations

import json
from pathlib import Path

OUTPUT = Path(__file__).resolve().parent / "dashboards" / "support_assistant.json"
DATASOURCE = {"type": "prometheus", "uid": "prometheus"}

# Each row: (title, [panels]). Each panel: title, what it tells you, unit, and its queries
# as (PromQL, legend). Options: kind ("timeseries" or "stat"), stack, width (out of 24).
ROWS = [
    (
        "Is it up and fast enough?",
        [
            {
                "title": "Services",
                "help": "1 = the service answers, 0 = it is down.",
                "kind": "stat",
                "queries": [("up", "{{job}}")],
                "width": 8,
            },
            {
                "title": "Requests per minute",
                "help": "How busy the gateway is, per endpoint.",
                "queries": [
                    ('sum by (path) (rate(http_requests_total{service="gateway"}[5m])) * 60', "{{path}}")
                ],
                "width": 8,
            },
            {
                "title": "Failed requests",
                "help": "Share of requests that ended in a server error (5xx), per service.",
                "unit": "percentunit",
                "queries": [
                    (
                        # "or ... * 0" makes a service with no errors show 0% instead of no line
                        'sum by (service) (rate(http_requests_total{status=~"5.."}[5m])'
                        " or rate(http_requests_total[5m]) * 0)"
                        " / sum by (service) (rate(http_requests_total[5m]))",
                        "{{service}}",
                    )
                ],
                "width": 8,
            },
            {
                "title": "Time to answer",
                "help": "Whole request, as the agent experiences it. p95 = 19 out of 20 are faster.",
                "unit": "s",
                "queries": [
                    (
                        "histogram_quantile(0.5, sum by (le) "
                        '(rate(gateway_stage_seconds_bucket{stage="total"}[5m])))',
                        "typical (p50)",
                    ),
                    (
                        "histogram_quantile(0.95, sum by (le) "
                        '(rate(gateway_stage_seconds_bucket{stage="total"}[5m])))',
                        "slow (p95)",
                    ),
                ],
            },
            {
                "title": "Time per stage (p95)",
                "help": "Where the time goes: triage, retrieval or generation.",
                "unit": "s",
                "queries": [
                    (
                        "histogram_quantile(0.95, sum by (le, stage) "
                        '(rate(gateway_stage_seconds_bucket{stage!="total"}[5m])))',
                        "{{stage}}",
                    )
                ],
            },
        ],
    ),
    (
        "Are the answers good?",
        [
            {
                "title": "What happened to each complaint",
                "help": "answered, cached, escalated (nothing similar) or degraded (a service was down).",
                "stack": True,
                "queries": [("sum by (outcome) (rate(gateway_resolve_total[5m])) * 60", "{{outcome}}")],
                "width": 8,
            },
            {
                "title": "How answers were written",
                "help": "llm = drafted by the model. extractive = quoted from a source instead.",
                "stack": True,
                "queries": [("sum by (mode) (rate(generation_answers_total[5m])) * 60", "{{mode}}")],
                "width": 8,
            },
            {
                "title": "Why the model was not used",
                "help": "llm_unavailable, invalid_output or llm_disabled.",
                "queries": [("sum by (reason) (rate(generation_fallbacks_total[5m])) * 60", "{{reason}}")],
                "width": 8,
            },
            {
                "title": "Steps that failed the check, per answer",
                "help": "unsupported = the cited source does not say this. dropped = no real citation.",
                "queries": [
                    (
                        "sum(rate(generation_unsupported_steps_total[15m]))"
                        " / sum(rate(generation_answers_total[15m]))",
                        "not supported by the source",
                    ),
                    (
                        "sum(rate(generation_dropped_steps_total[15m]))"
                        " / sum(rate(generation_answers_total[15m]))",
                        "dropped (no citation)",
                    ),
                ],
                "width": 8,
            },
            {
                "title": "Agents who found the answer helpful",
                "help": "From the Helpful / Not helpful buttons, over the last 6 hours.",
                "unit": "percentunit",
                "queries": [
                    (
                        'sum(increase(gateway_feedback_total{helpful="true"}[6h]))'
                        " / sum(increase(gateway_feedback_total[6h]))",
                        "helpful",
                    )
                ],
                "width": 8,
            },
            {
                "title": "Category corrected by agents",
                "help": "Share of rated answers where the agent changed the category.",
                "unit": "percentunit",
                "queries": [
                    (
                        "sum by (kind) (increase(gateway_category_corrections_total[6h]))"
                        " / scalar(sum(increase(gateway_feedback_total[6h])))",
                        "{{kind}}",
                    )
                ],
                "width": 8,
            },
        ],
    ),
    (
        "Is the data still familiar? (drift)",
        [
            {
                "title": "Similarity of the closest match",
                "help": "Normal is about 0.83. A falling line means customers ask about new things.",
                "queries": [
                    (
                        "histogram_quantile(0.5, sum by (le) (rate(gateway_top_similarity_bucket[15m])))",
                        "typical (p50)",
                    ),
                    (
                        "histogram_quantile(0.1, sum by (le) (rate(gateway_top_similarity_bucket[15m])))",
                        "least familiar 10%",
                    ),
                ],
                "width": 8,
            },
            {
                "title": "Complaints with no confident match",
                "help": "Share that was escalated because nothing similar exists in the knowledge base.",
                "unit": "percentunit",
                "queries": [
                    (
                        'sum(rate(gateway_resolve_total{outcome="escalated_no_match"}[15m]))'
                        ' / sum(rate(gateway_resolve_total{outcome!="analyzed_only"}[15m]))',
                        "escalated",
                    )
                ],
                "width": 8,
            },
            {
                "title": "Triage could not decide",
                "help": "Share of complaints labelled 'unknown'. Normal is under 10% for category.",
                "unit": "percentunit",
                "queries": [
                    (
                        "sum by (field) (rate(triage_unknown_total[15m])) / scalar(sum("
                        'rate(http_requests_total{service="triage", path="/classify"}[15m])))',
                        "{{field}}",
                    )
                ],
                "width": 8,
            },
            {
                "title": "Categories seen",
                "help": "The mix of problem types. A category that suddenly grows is worth a look.",
                "stack": True,
                "queries": [
                    ('sum by (label) (rate(triage_labels_total{field="category"}[30m])) * 60', "{{label}}")
                ],
            },
            {
                "title": "Severity seen",
                "help": "A jump in high or critical complaints usually means an outage.",
                "stack": True,
                "queries": [
                    ('sum by (label) (rate(triage_labels_total{field="severity"}[30m])) * 60', "{{label}}")
                ],
            },
        ],
    ),
    (
        "Language model and cache",
        [
            {
                "title": "Model time per answer",
                "help": "About a minute on a laptop CPU. A GPU or a hosted model brings this to seconds.",
                "unit": "s",
                "queries": [
                    (
                        "histogram_quantile(0.5, sum by (le) (rate(generation_llm_seconds_bucket[15m])))",
                        "typical (p50)",
                    ),
                    (
                        "histogram_quantile(0.95, sum by (le) (rate(generation_llm_seconds_bucket[15m])))",
                        "slow (p95)",
                    ),
                ],
                "width": 8,
            },
            {
                "title": "Tokens per minute",
                "help": "What a hosted model would bill for. prompt = read, completion = written.",
                "queries": [("sum by (kind) (rate(generation_tokens_total[5m])) * 60", "{{kind}}")],
                "width": 8,
            },
            {
                "title": "Answers served from the cache",
                "help": "Repeated complaints answered instantly. It drops to zero after the index changes.",
                "unit": "percentunit",
                "queries": [
                    (
                        'sum(rate(gateway_cache_total{result="hit"}[15m]))'
                        " / sum(rate(gateway_cache_total[15m]))",
                        "cache hits",
                    )
                ],
                "width": 8,
            },
        ],
    ),
    (
        "New data and dependencies",
        [
            {
                "title": "Documents indexed per minute",
                "help": "indexed, removed, retry or dead_letter.",
                "stack": True,
                "queries": [("sum by (result) (rate(ingestion_events_total[5m])) * 60", "{{result}}")],
                "width": 8,
            },
            {
                "title": "Waiting to be indexed",
                "help": "Document changes on the queue. It should fall back to zero quickly.",
                "queries": [("ingestion_queue_waiting", "waiting")],
                "width": 8,
            },
            {
                "title": "Given up on or repaired (last hour)",
                "help": "dead letters = failed 5 times. repaired = found by the safety sweep, not the queue.",
                "queries": [
                    ("increase(ingestion_dead_letter_total[1h])", "dead letters"),
                    ("increase(ingestion_sweep_recovered_total[1h])", "repaired by the sweep"),
                ],
                "width": 8,
            },
            {
                "title": "Requests refused",
                "help": "bad_api_key, not_admin or rate_limited.",
                "queries": [("sum by (reason) (rate(gateway_rejected_total[5m])) * 60", "{{reason}}")],
                "width": 8,
            },
            {
                "title": "Answered without a service",
                "help": "The gateway carried on without triage or without generation.",
                "queries": [("sum by (service) (rate(gateway_degraded_total[5m])) * 60", "{{service}}")],
                "width": 8,
            },
            {
                "title": "Redis and PostgreSQL errors",
                "help": "cache, rate_limiter, queue or audit_log.",
                "queries": [
                    ("sum by (component) (rate(gateway_infra_errors_total[5m])) * 60", "{{component}}")
                ],
                "width": 8,
            },
        ],
    ),
]


def build_panel(panel_id: int, spec: dict, x: int, y: int) -> dict:
    kind = spec.get("kind", "timeseries")
    custom = {"fillOpacity": 20 if spec.get("stack") else 8, "lineWidth": 2, "showPoints": "never"}
    if spec.get("stack"):
        custom["stacking"] = {"mode": "normal", "group": "A"}
    defaults: dict = {"unit": spec.get("unit", "short"), "custom": custom, "min": 0}
    if spec.get("unit") == "percentunit":
        defaults["max"] = 1  # a share runs from 0% to 100%; without this an all-zero panel shows 10000%
    options: dict = {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True}}
    options["tooltip"] = {"mode": "multi", "sort": "desc"}
    if kind == "stat":
        defaults = {
            "mappings": [
                {
                    "type": "value",
                    "options": {
                        "0": {"text": "DOWN", "color": "red"},
                        "1": {"text": "UP", "color": "green"},
                    },
                }
            ],
            "thresholds": {
                "mode": "absolute",
                "steps": [{"color": "red", "value": None}, {"color": "green", "value": 1}],
            },
        }
        options = {
            "colorMode": "background",
            "graphMode": "none",
            "textMode": "value_and_name",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        }
    return {
        "id": panel_id,
        "type": kind,
        "title": spec["title"],
        "description": spec["help"],
        "datasource": DATASOURCE,
        "gridPos": {"h": 8, "w": spec.get("width", 12), "x": x, "y": y},
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": options,
        "targets": [
            {"datasource": DATASOURCE, "expr": expr, "legendFormat": legend, "refId": chr(ord("A") + index)}
            for index, (expr, legend) in enumerate(spec["queries"])
        ],
    }


def build() -> dict:
    panels: list[dict] = []
    panel_id, y = 1, 0
    for title, specs in ROWS:
        panels.append(
            {
                "id": panel_id,
                "type": "row",
                "title": title,
                "collapsed": False,
                "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
                "panels": [],
            }
        )
        panel_id, y, x = panel_id + 1, y + 1, 0
        for spec in specs:
            width = spec.get("width", 12)
            if x + width > 24:  # the row is full: start a new line
                x, y = 0, y + 8
            panels.append(build_panel(panel_id, spec, x, y))
            panel_id, x = panel_id + 1, x + width
        y += 8
    return {
        "uid": "support-assistant",
        "title": "Support Ticket Resolution Assistant",
        "description": "System health, answer quality, drift and ingestion. Built by build_dashboard.py.",
        "tags": ["support-assistant"],
        "timezone": "browser",
        "editable": True,
        "graphTooltip": 1,
        "refresh": "15s",
        "time": {"from": "now-1h", "to": "now"},
        "schemaVersion": 39,
        "version": 1,
        "panels": panels,
    }


def all_queries() -> list[str]:
    return [expr for _, specs in ROWS for spec in specs for expr, _ in spec["queries"]]


if __name__ == "__main__":
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(build(), indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"Wrote {OUTPUT.name} with {len(all_queries())} queries.")
