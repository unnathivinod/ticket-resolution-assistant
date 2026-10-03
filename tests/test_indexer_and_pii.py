"""Fast tests for PII masking and for turning documents into searchable chunks."""

from qdrant_client import QdrantClient

from libs.common.pii import mask_pii
from services.ingestion.indexer import (
    COLLECTION_ALIAS,
    collection_dim,
    ensure_collection,
    index_chunks,
    kb_chunks,
    point_id,
    remove_document,
    resolve_alias,
    split_by_heading,
    ticket_chunks,
)
from tests.fakes import DIM, FakeEmbedder

TICKET = {
    "id": "T-000001",
    "subject": "Evening broadband drops",
    "description": "My broadband drops every evening. Call me on 07700 900123, account 48213377.",
    "resolution_steps": ["Run a line test.", "Change the Wi-Fi channel."],
    "category": "connectivity_intermittent",
    "product": "broadband",
    "severity": "high",
    "sentiment": "negative",
    "scenario_id": "S01",
    "created_at": "2026-01-01T00:00:00+00:00",
}
ARTICLE = {
    "id": "KB-001",
    "title": "Evening broadband drops",
    "body": "# Evening broadband drops\n\n## Symptoms\nDrops at night.\n\n## Resolution steps\n1. Test.\n",
    "category": "connectivity_intermittent",
    "product": "broadband",
    "scenario_id": "S01",
    "version": 1,
    "updated_at": "2026-01-01T00:00:00+00:00",
}


def test_mask_pii_hides_personal_details():
    text = "Email sam.patel@example.com or call 07700 900123. Account 48213377."
    assert mask_pii(text) == "Email [EMAIL] or call [PHONE]. Account [NUMBER]."


def test_mask_pii_keeps_useful_technical_details():
    text = "Error E-102 at 8pm, paying for 900 Mbps, restarted 2 times in 24 hours."
    assert mask_pii(text) == text


def test_mask_pii_is_safe_to_repeat():
    once = mask_pii("call 07700 900123")
    assert mask_pii(once) == once


def test_point_id_is_stable_and_unique_per_chunk():
    assert point_id("KB-001", 0) == point_id("KB-001", 0)
    assert point_id("KB-001", 0) != point_id("KB-001", 1)
    assert point_id("KB-001", 0) != point_id("KB-002", 0)


def test_ticket_chunk_embeds_the_problem_and_masks_pii():
    (chunk,) = ticket_chunks(TICKET)
    assert "07700" not in chunk.text and "48213377" not in chunk.text
    assert "[PHONE]" in chunk.text
    assert "Change the Wi-Fi channel" not in chunk.text  # the resolution is not what we search on
    assert "2. Change the Wi-Fi channel." in chunk.payload["content"]  # but it is kept for the LLM
    assert chunk.payload["source_type"] == "ticket"


def test_kb_article_is_split_by_heading_and_keeps_full_content():
    chunks = kb_chunks(ARTICLE)
    assert [c.payload["chunk_index"] for c in chunks] == [0, 1]
    assert chunks[0].text.startswith("Evening broadband drops. Symptoms:")
    assert all(c.payload["content"] == ARTICLE["body"] for c in chunks)
    assert len({c.point_id for c in chunks}) == 2


def test_split_by_heading_handles_articles_without_headings():
    assert split_by_heading("# Title\n\nJust one block of text.") == [("", "Just one block of text.")]


def test_long_sections_are_cut_into_smaller_chunks():
    body = "## Steps\n" + "\n\n".join("word " * 120 for _ in range(6))
    sections = split_by_heading(body)
    assert len(sections) > 1
    assert all(len(text) <= 1500 for _, text in sections)


def test_indexing_twice_does_not_create_duplicates():
    client, embedder = QdrantClient(":memory:"), FakeEmbedder()
    name = ensure_collection(client, DIM)
    chunks = ticket_chunks(TICKET) + kb_chunks(ARTICLE)
    index_chunks(client, embedder, chunks)
    index_chunks(client, embedder, chunks)
    assert client.count(COLLECTION_ALIAS, exact=True).count == 3
    assert resolve_alias(client) == name


def test_ensure_collection_reuses_or_recreates():
    client = QdrantClient(":memory:")
    first = ensure_collection(client, DIM)
    index_chunks(client, FakeEmbedder(), ticket_chunks(TICKET))
    assert ensure_collection(client, DIM) == first  # second call keeps the data
    assert client.count(COLLECTION_ALIAS, exact=True).count == 1
    ensure_collection(client, DIM, recreate=True)  # recreate wipes it
    assert client.count(COLLECTION_ALIAS, exact=True).count == 0


def test_collection_dim_reports_the_vector_size():
    client = QdrantClient(":memory:")
    assert collection_dim(client) is None
    ensure_collection(client, DIM)
    assert collection_dim(client) == DIM
    ensure_collection(client, 8, recreate=True)  # switching to a model with a different vector size
    assert collection_dim(client) == 8


def test_remove_document_deletes_all_its_chunks():
    client = QdrantClient(":memory:")
    ensure_collection(client, DIM)
    index_chunks(client, FakeEmbedder(), ticket_chunks(TICKET) + kb_chunks(ARTICLE))
    remove_document(client, "KB-001")
    assert client.count(COLLECTION_ALIAS, exact=True).count == 1
