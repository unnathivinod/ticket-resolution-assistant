"""Look for new kinds of problems among recent complaints and propose them as new ticket classes.

  docker compose run --rm tools python scripts/discover_classes.py             # save proposals
  docker compose run --rm tools python scripts/discover_classes.py --dry-run   # only print them

In production this would run on a schedule (for example every night). It reads the complaints
that an agent marked "none of these categories fits" or that triage labelled "unknown",
groups the similar ones, and saves each big enough group as a proposal. A person then
approves or rejects it through the gateway:

  GET  /v1/classes/proposals
  POST /v1/classes/proposals/{id}/approve   {"name": "esim_management"}
"""

from __future__ import annotations

import argparse
import os

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from libs.common.embedding_client import EmbeddingClient
from services.ingestion.discovery import DEFAULT_THRESHOLD, Proposal, build_proposals

CANDIDATES = """
    SELECT DISTINCT ON (r.complaint_masked) r.request_id::text AS id, r.complaint_masked AS text
    FROM resolve_requests r
    WHERE r.created_at > now() - make_interval(days => %(days)s)
      AND (
            EXISTS (SELECT 1 FROM feedback f
                    WHERE f.request_id = r.request_id AND f.correct_category = 'none_of_these')
            OR (r.triage ->> 'needs_review') = 'true'
          )
      -- complaints behind a proposal that was already approved or rejected are not proposed again
      AND NOT EXISTS (SELECT 1 FROM class_proposals p
                      WHERE p.status <> 'pending' AND r.request_id::text = ANY (p.request_ids))
    ORDER BY r.complaint_masked, r.created_at DESC
"""

# A sample of everything agents were asked recently. It shows which words are ordinary.
BACKGROUND = """
    SELECT DISTINCT complaint_masked AS text FROM resolve_requests
    WHERE created_at > now() - make_interval(days => %(days)s) LIMIT 2000
"""


def discover(
    conn: psycopg.Connection,
    embedder,
    days: int = 30,
    threshold: float = DEFAULT_THRESHOLD,
    min_size: int = 5,
    save: bool = True,
) -> list[Proposal]:
    """Find groups among the flagged complaints. With save=True the pending proposals are replaced."""
    with conn.cursor(row_factory=dict_row) as cur:
        items = cur.execute(CANDIDATES, {"days": days}).fetchall()
        background = [row["text"] for row in cur.execute(BACKGROUND, {"days": days}).fetchall()]
    if not items:
        return []
    vectors = embedder.embed([item["text"] for item in items], kind="document")["dense"]
    proposals = build_proposals(items, vectors, threshold=threshold, min_size=min_size, background=background)
    if save:
        with conn.cursor() as cur:
            # Each run gives the full current picture, so undecided proposals are rebuilt.
            cur.execute("DELETE FROM class_proposals WHERE status = 'pending'")
            for proposal in proposals:
                cur.execute(
                    """INSERT INTO class_proposals
                           (kind, suggested_name, keywords, cluster_size, request_ids, examples)
                       VALUES ('category', %s, %s, %s, %s, %s)""",
                    (
                        proposal.suggested_name,
                        proposal.keywords,
                        proposal.size,
                        proposal.member_ids,
                        Jsonb(proposal.examples),
                    ),
                )
        conn.commit()
    return proposals


def main() -> None:
    parser = argparse.ArgumentParser(description="Propose new ticket classes from flagged complaints.")
    parser.add_argument("--days", type=int, default=30, help="how far back to look")
    parser.add_argument(
        "--threshold", type=float, default=DEFAULT_THRESHOLD, help="how similar two complaints must be"
    )
    parser.add_argument("--min-size", type=int, default=5, help="smallest group worth proposing")
    parser.add_argument("--dry-run", action="store_true", help="print the proposals without saving them")
    args = parser.parse_args()

    embedder = EmbeddingClient(os.environ.get("EMBEDDING_URL", "http://embedding:8004"), timeout=120)
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=30) as conn:
        proposals = discover(conn, embedder, args.days, args.threshold, args.min_size, not args.dry_run)

    if not proposals:
        print("No group of similar flagged complaints is big enough to propose a new class.")
        return
    for number, proposal in enumerate(proposals, start=1):
        print(f"\nProposal {number}: {proposal.size} complaints, keywords: {', '.join(proposal.keywords)}")
        for example in proposal.examples[:3]:
            print(f"  - {example[:140]}")
    action = "Printed only (dry run)." if args.dry_run else "Saved. Review them at GET /v1/classes/proposals."
    print(f"\n{len(proposals)} proposal(s). {action}")


if __name__ == "__main__":
    main()
