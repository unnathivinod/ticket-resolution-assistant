"""Add a knowledge-base article or a resolved ticket to the running system, or retire one.

  docker compose run --rm tools python scripts/add_document.py data/examples/kb_broken_router.json
  docker compose run --rm tools python scripts/add_document.py --retire KB-900

The file is sent to the gateway, exactly as a ticketing system would send it. The script then
waits until the ingestion worker has put it in the search index and says how long that took.
Nothing is retrained and nothing is restarted.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

GATEWAY = os.environ.get("GATEWAY_URL", "http://gateway:8000")
ADMIN = {"X-API-Key": os.environ.get("GATEWAY_ADMIN_API_KEY", "dev-admin-key")}


def send(http: httpx.Client, method: str, path: str, **kwargs) -> dict:
    try:
        response = http.request(method, path, **kwargs)
    except httpx.HTTPError as error:
        sys.exit(f"The gateway is not reachable at {GATEWAY} ({error}). Run: docker compose up -d")
    if response.status_code >= 400:
        sys.exit(f"The gateway refused the request ({response.status_code}): {response.text}")
    return response.json()


def wait_until_indexed(http: httpx.Client, doc_id: str, seconds: int = 120) -> float:
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        if send(http, "GET", f"/v1/documents/{doc_id}")["index_up_to_date"]:
            return time.monotonic() - started
        time.sleep(0.5)
    sys.exit("The search index did not catch up. Check: docker compose logs ingestion")


def main() -> None:
    parser = argparse.ArgumentParser(description="Add or retire a ticket or knowledge-base article.")
    parser.add_argument("file", nargs="?", type=Path, help="a JSON file with the article or ticket")
    parser.add_argument("--retire", metavar="ID", help="hide this document from search instead")
    args = parser.parse_args()
    if bool(args.file) == bool(args.retire):
        parser.error("give either a file to add, or --retire with a document ID")

    with httpx.Client(base_url=GATEWAY, headers=ADMIN, timeout=30) as http:
        if args.retire:
            send(http, "DELETE", f"/v1/documents/{args.retire}")
            seconds = wait_until_indexed(http, args.retire)
            print(f"{args.retire} is no longer found by search ({seconds:.1f}s).")
            return

        document = json.loads(args.file.read_text(encoding="utf-8"))
        doc_id = document.get("id", "")
        if doc_id.startswith("KB-"):
            body = {key: value for key, value in document.items() if key != "id"}
            result = send(http, "PUT", f"/v1/kb/{doc_id}", json=body)
        else:
            result = send(http, "POST", "/v1/tickets", json=document)
        seconds = wait_until_indexed(http, result["id"])
        version = f", version {result['version']}" if "version" in result else ""
        print(f"{result['id']} saved{version} and searchable after {seconds:.1f}s.")


if __name__ == "__main__":
    main()
