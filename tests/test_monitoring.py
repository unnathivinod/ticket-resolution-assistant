"""Fast checks that the monitoring files agree with the code.

A dashboard or alert that asks for a number nobody publishes fails silently: the panel is
just empty, and the alert never fires. These tests catch that when a metric is renamed.
"""

import json
import re
from pathlib import Path

import yaml

from infra.grafana.build_dashboard import OUTPUT, all_queries, build

ROOT = Path(__file__).resolve().parents[1]
PROMETHEUS = ROOT / "infra" / "prometheus"

_DEFINITION = re.compile(r'\b(Counter|Histogram|Gauge)\(\s*"([a-z_]+)"')
_USED = re.compile(r"\b((?:gateway|generation|retrieval|triage|embedding|ingestion|http)_[a-z0-9_]+)")


def published_metrics() -> set[str]:
    """Every metric name the services can publish, read from the source code."""
    names = {"up"}  # added by Prometheus itself for every service it scrapes
    for path in [*ROOT.glob("services/**/*.py"), *ROOT.glob("libs/**/*.py")]:
        for kind, name in _DEFINITION.findall(path.read_text(encoding="utf-8")):
            if kind == "Histogram":
                names |= {f"{name}_bucket", f"{name}_count", f"{name}_sum"}
            else:
                names.add(name)
    return names


def alert_rules() -> list[dict]:
    groups = yaml.safe_load((PROMETHEUS / "alerts.yml").read_text(encoding="utf-8"))["groups"]
    return [rule for group in groups for rule in group["rules"]]


def test_the_dashboard_file_matches_its_build_script():
    saved = json.loads(OUTPUT.read_text(encoding="utf-8"))
    assert saved == build(), "Run: python infra/grafana/build_dashboard.py"


def test_every_dashboard_query_uses_metrics_that_exist():
    published = published_metrics()
    for query in all_queries():
        unknown = set(_USED.findall(query)) - published
        assert not unknown, f"{unknown} in dashboard query: {query}"


def test_every_alert_uses_metrics_that_exist():
    published = published_metrics()
    for rule in alert_rules():
        unknown = set(_USED.findall(rule["expr"])) - published
        assert not unknown, f"{unknown} in alert {rule['alert']}"


def test_every_alert_says_how_bad_it_is_and_what_to_do():
    rules = alert_rules()
    assert len(rules) >= 10
    for rule in rules:
        assert rule["labels"]["severity"] in {"critical", "warning", "info"}, rule["alert"]
        assert rule["annotations"]["summary"] and rule["annotations"]["action"], rule["alert"]


def test_prometheus_collects_from_every_service_in_compose():
    config = yaml.safe_load((PROMETHEUS / "prometheus.yml").read_text(encoding="utf-8"))
    targets = {job["job_name"]: job["static_configs"][0]["targets"][0] for job in config["scrape_configs"]}
    assert targets == {
        "gateway": "gateway:8000",
        "triage": "triage:8001",
        "retrieval": "retrieval:8002",
        "generation": "generation:8003",
        "embedding": "embedding:8004",
        "ingestion": "ingestion:8005",
    }
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert set(targets) <= set(compose["services"])


def test_dashboard_panels_have_unique_ids_and_fit_the_grid():
    panels = build()["panels"]
    ids = [panel["id"] for panel in panels]
    assert len(ids) == len(set(ids))
    for panel in panels:
        assert panel["gridPos"]["x"] + panel["gridPos"]["w"] <= 24, panel["title"]
        if panel["type"] != "row":
            assert panel["description"] and panel["targets"], panel["title"]
