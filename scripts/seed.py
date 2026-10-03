"""Load the dataset into PostgreSQL (source of truth) and Qdrant (search index).

Run (the databases and the embedding service must be up):
  docker compose run --rm tools python scripts/seed.py
  docker compose run --rm tools python scripts/seed.py --recreate   # wipe the index and rebuild it

Safe to run more than once: rows and points are updated in place, never duplicated.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import psycopg
import yaml
from psycopg.types.json import Jsonb
from qdrant_client import QdrantClient

from libs.common.embedding_client import EmbeddingClient
from scripts.migrate import migrate
from services.ingestion.indexer import (
    COLLECTION_ALIAS,
    collection_dim,
    ensure_collection,
    index_chunks,
    kb_chunks,
    ticket_chunks,
)

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def wait_for(name: str, check, seconds: int = 60) -> None:
    """Wait until a dependency answers, or stop with one clear message."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except Exception:  # noqa: BLE001 - any failure just means "not ready yet"
            pass
        time.sleep(2)
    sys.exit(f"{name} did not become ready within {seconds} seconds. Is 'docker compose up -d' running?")


def load_postgres(
    conn: psycopg.Connection, taxonomy: dict, tickets: list[dict], articles: list[dict]
) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO taxonomy (kind, name, description) VALUES (%s, %s, %s)
               ON CONFLICT (kind, name) DO UPDATE SET description = EXCLUDED.description""",
            [
                (kind, item["name"], item["description"])
                for kind in ("category", "product")
                for item in taxonomy[kind]
            ],
        )
        cur.executemany(
            """INSERT INTO tickets (id, subject, description, resolution_steps, category, product,
                                    severity, sentiment, scenario_id, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (id) DO UPDATE SET
                   subject = EXCLUDED.subject, description = EXCLUDED.description,
                   resolution_steps = EXCLUDED.resolution_steps, category = EXCLUDED.category,
                   product = EXCLUDED.product, severity = EXCLUDED.severity,
                   sentiment = EXCLUDED.sentiment, scenario_id = EXCLUDED.scenario_id, updated_at = now()""",
            [
                (
                    t["id"],
                    t["subject"],
                    t["description"],
                    Jsonb(t["resolution_steps"]),
                    t["category"],
                    t["product"],
                    t["severity"],
                    t["sentiment"],
                    t["scenario_id"],
                    t["created_at"],
                )
                for t in tickets
            ],
        )
        cur.executemany(
            """INSERT INTO kb_articles (id, title, body, category, product, scenario_id, version, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (id) DO UPDATE SET
                   title = EXCLUDED.title, body = EXCLUDED.body, category = EXCLUDED.category,
                   product = EXCLUDED.product, scenario_id = EXCLUDED.scenario_id,
                   version = EXCLUDED.version, updated_at = EXCLUDED.updated_at""",
            [
                (
                    a["id"],
                    a["title"],
                    a["body"],
                    a["category"],
                    a["product"],
                    a["scenario_id"],
                    a["version"],
                    a["updated_at"],
                )
                for a in articles
            ],
        )
    conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Load the dataset into PostgreSQL and Qdrant.")
    parser.add_argument("--recreate", action="store_true", help="delete the search index and rebuild it")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR / "generated")
    args = parser.parse_args()

    database_url = os.environ["DATABASE_URL"]
    qdrant = QdrantClient(url=os.environ.get("QDRANT_URL", "http://qdrant:6333"), timeout=30)
    embedder = EmbeddingClient(os.environ.get("EMBEDDING_URL", "http://embedding:8004"), timeout=120)

    wait_for("Qdrant", lambda: qdrant.get_collections() is not None)
    wait_for("The embedding service", embedder.ready)

    taxonomy = yaml.safe_load((DATA_DIR / "taxonomy.yaml").read_text(encoding="utf-8"))
    tickets = read_jsonl(args.data_dir / "tickets.jsonl")
    articles = read_jsonl(args.data_dir / "kb_articles.jsonl")

    print(f"1/3 PostgreSQL: saving {len(tickets)} tickets and {len(articles)} articles ...")
    with psycopg.connect(database_url, connect_timeout=30) as conn:
        migrate(conn)  # makes sure an older database has the newest columns
        load_postgres(conn, taxonomy, tickets, articles)
        counts = {
            table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608 - fixed table names
            for table in ("taxonomy", "tickets", "kb_articles")
        }

    print("2/3 Preparing the search index ...")
    dim = embedder.embed(["dimension check"])["dim"]
    existing_dim = collection_dim(qdrant)
    if existing_dim not in (None, dim) and not args.recreate:
        sys.exit(
            f"The search index was built with a different embedding model ({existing_dim} numbers "
            f"per vector, the current model gives {dim}). Run again with --recreate to rebuild it."
        )
    collection = ensure_collection(qdrant, dim, recreate=args.recreate)
    chunks = [chunk for ticket in tickets for chunk in ticket_chunks(ticket)]
    chunks += [chunk for article in articles for chunk in kb_chunks(article)]

    print(f"3/3 Qdrant: embedding and saving {len(chunks)} chunks into '{collection}' ...")
    started = time.monotonic()

    def progress(done: int, total: int) -> None:
        if done == total or done % 320 == 0:
            print(f"      {done}/{total}")

    index_chunks(qdrant, embedder, chunks, on_progress=progress)
    points = qdrant.count(COLLECTION_ALIAS, exact=True).count

    # Tell the ingestion worker these documents are already in the index, so it does not redo them.
    with psycopg.connect(database_url, connect_timeout=30) as conn:
        conn.execute(
            "UPDATE tickets SET indexed_at = now() WHERE id = ANY(%s)", ([t["id"] for t in tickets],)
        )
        conn.execute(
            "UPDATE kb_articles SET indexed_at = now() WHERE id = ANY(%s)", ([a["id"] for a in articles],)
        )

    print(f"\nDone in {time.monotonic() - started:.0f}s.")
    print(f"  PostgreSQL rows : {counts}")
    print(f"  Qdrant points   : {points} in '{collection}' (searched through the alias '{COLLECTION_ALIAS}')")


if __name__ == "__main__":
    main()
