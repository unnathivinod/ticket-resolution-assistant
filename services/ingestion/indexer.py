"""Turns tickets and knowledge-base articles into searchable points in Qdrant.

Used by scripts/seed.py for the first bulk load, and later by the ingestion worker
for new and updated documents. The steps are always the same:

    document -> chunks -> mask personal details -> embed -> upsert into Qdrant
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from qdrant_client import QdrantClient, models

from libs.common.pii import mask_pii

COLLECTION_ALIAS = "support_knowledge"  # the name services search; points at the real collection
DENSE_VECTOR = "dense"  # meaning vectors
SPARSE_VECTOR = "bm25"  # keyword vectors

_ID_NAMESPACE = uuid.UUID("6f1d3c1e-9a2b-4c57-8e21-0b7a54d1c9aa")
_MAX_CHUNK_CHARS = 1500


@dataclass
class Chunk:
    point_id: str
    text: str  # what gets embedded and searched
    payload: dict  # everything stored next to the vector


def point_id(doc_id: str, chunk_index: int = 0) -> str:
    """The same document always gets the same ID, so loading it twice never creates duplicates."""
    return str(uuid.uuid5(_ID_NAMESPACE, f"{doc_id}#{chunk_index}"))


def ticket_chunks(ticket: dict) -> list[Chunk]:
    """One chunk per ticket.

    We embed the customer's PROBLEM, not the resolution: a new complaint should match
    old complaints. The resolution steps travel along as stored data for the LLM to use.
    """
    problem = mask_pii(ticket["description"])
    steps = "\n".join(f"{n}. {step}" for n, step in enumerate(ticket["resolution_steps"], start=1))
    payload = {
        "doc_id": ticket["id"],
        "source_type": "ticket",
        "chunk_index": 0,
        "title": ticket["subject"],
        "text": problem,
        "content": f"Problem: {problem}\nResolution steps:\n{steps}",
        "category": ticket.get("category"),
        "product": ticket.get("product"),
        "severity": ticket.get("severity"),
        "sentiment": ticket.get("sentiment"),
        "scenario_id": ticket.get("scenario_id"),
        "is_active": ticket.get("is_active", True),
        "updated_at": ticket.get("created_at"),
    }
    return [Chunk(point_id(ticket["id"]), problem, payload)]


def split_by_heading(body: str) -> list[tuple[str, str]]:
    """Split a markdown article into (heading, text) sections at each '## ' heading."""
    sections: list[tuple[str, str]] = []
    heading, lines = "", []
    for line in body.splitlines():
        if line.startswith("## "):
            if "".join(lines).strip():
                sections.append((heading, "\n".join(lines).strip()))
            heading, lines = line[3:].strip(), []
        elif not line.startswith("# "):  # skip the article title line
            lines.append(line)
    if "".join(lines).strip():
        sections.append((heading, "\n".join(lines).strip()))

    # Very long sections are cut at paragraph breaks so each chunk stays focused.
    result: list[tuple[str, str]] = []
    for heading, text in sections:
        while len(text) > _MAX_CHUNK_CHARS:
            cut = text.rfind("\n\n", 0, _MAX_CHUNK_CHARS)
            cut = cut if cut > 0 else _MAX_CHUNK_CHARS
            result.append((heading, text[:cut].strip()))
            text = text[cut:].strip()
        result.append((heading, text))
    return result


def kb_chunks(article: dict) -> list[Chunk]:
    """Several small chunks per article, one per section ("small-to-big" retrieval).

    Small chunks match a complaint more precisely. Each chunk also stores the whole
    article in `content`, so the LLM still gets the full resolution steps.
    """
    chunks: list[Chunk] = []
    for index, (heading, text) in enumerate(split_by_heading(article["body"])):
        searchable = f"{article['title']}. {heading}: {text}" if heading else f"{article['title']}. {text}"
        payload = {
            "doc_id": article["id"],
            "source_type": "kb",
            "chunk_index": index,
            "title": article["title"],
            "text": searchable,
            "content": article["body"],
            "category": article.get("category"),
            "product": article.get("product"),
            "scenario_id": article.get("scenario_id"),
            "version": article.get("version", 1),
            "is_active": article.get("is_active", True),
            "updated_at": article.get("updated_at"),
        }
        chunks.append(Chunk(point_id(article["id"], index), searchable, payload))
    return chunks


def resolve_alias(client: QdrantClient, alias: str = COLLECTION_ALIAS) -> str | None:
    """Return the real collection behind the alias, or None if it does not exist yet."""
    for item in client.get_aliases().aliases:
        if item.alias_name == alias:
            return item.collection_name
    return None


def collection_dim(client: QdrantClient, alias: str = COLLECTION_ALIAS) -> int | None:
    """Size of the meaning vectors the existing collection was built for, or None if there is none."""
    name = resolve_alias(client, alias)
    if name is None:
        return None
    return client.get_collection(name).config.params.vectors[DENSE_VECTOR].size


def ensure_collection(
    client: QdrantClient, dim: int, alias: str = COLLECTION_ALIAS, recreate: bool = False
) -> str:
    """Create the collection (and the alias pointing at it) if needed. Returns the real collection name.

    Services always search the alias. To change the embedding model later, build a new
    collection in the background and switch the alias: no downtime, easy rollback.
    """
    existing = resolve_alias(client, alias)
    if existing and not recreate:
        return existing
    if existing:
        client.update_collection_aliases(
            change_aliases_operations=[
                models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=alias))
            ]
        )
        client.delete_collection(existing)

    name = f"{alias}_v1"
    if client.collection_exists(name):
        client.delete_collection(name)
    client.create_collection(
        name,
        vectors_config={DENSE_VECTOR: models.VectorParams(size=dim, distance=models.Distance.COSINE)},
        # IDF makes rare words (like an error code) count more than common words.
        sparse_vectors_config={SPARSE_VECTOR: models.SparseVectorParams(modifier=models.Modifier.IDF)},
    )
    # Indexes on the fields we filter by, so filtering stays fast as the data grows.
    for field, schema in [
        ("source_type", models.PayloadSchemaType.KEYWORD),
        ("product", models.PayloadSchemaType.KEYWORD),
        ("category", models.PayloadSchemaType.KEYWORD),
        ("doc_id", models.PayloadSchemaType.KEYWORD),
        ("is_active", models.PayloadSchemaType.BOOL),
    ]:
        client.create_payload_index(name, field_name=field, field_schema=schema)
    client.update_collection_aliases(
        change_aliases_operations=[
            models.CreateAliasOperation(
                create_alias=models.CreateAlias(collection_name=name, alias_name=alias)
            )
        ]
    )
    return name


def index_chunks(
    client: QdrantClient,
    embedder,
    chunks: list[Chunk],
    collection: str = COLLECTION_ALIAS,
    batch_size: int = 64,
    on_progress=None,
) -> int:
    """Embed the chunks and save them. Safe to run again: existing points are overwritten."""
    done = 0
    for start in range(0, len(chunks), batch_size):
        batch = chunks[start : start + batch_size]
        vectors = embedder.embed([chunk.text for chunk in batch], kind="document")
        points = [
            models.PointStruct(
                id=chunk.point_id,
                vector={
                    DENSE_VECTOR: dense,
                    SPARSE_VECTOR: models.SparseVector(indices=sparse["indices"], values=sparse["values"]),
                },
                payload={**chunk.payload, "embedding_model": vectors["model"]},
            )
            for chunk, dense, sparse in zip(batch, vectors["dense"], vectors["sparse"], strict=True)
        ]
        client.upsert(collection, points=points, wait=True)
        done += len(batch)
        if on_progress:
            on_progress(done, len(chunks))
    return done


def remove_document(client: QdrantClient, doc_id: str, collection: str = COLLECTION_ALIAS) -> None:
    """Delete every chunk of one document (used before re-indexing an edited article)."""
    client.delete(
        collection,
        points_selector=models.FilterSelector(
            filter=models.Filter(
                must=[models.FieldCondition(key="doc_id", match=models.MatchValue(value=doc_id))]
            )
        ),
        wait=True,
    )


def remove_stale_chunks(
    client: QdrantClient, doc_id: str, keep: int, collection: str = COLLECTION_ALIAS
) -> None:
    """Delete leftover chunks after an article became shorter (chunks numbered `keep` and above).

    The new chunks are written first and the leftovers removed afterwards, so the article
    never disappears from search while it is being updated.
    """
    client.delete(
        collection,
        points_selector=models.FilterSelector(
            filter=models.Filter(
                must=[
                    models.FieldCondition(key="doc_id", match=models.MatchValue(value=doc_id)),
                    models.FieldCondition(key="chunk_index", range=models.Range(gte=keep)),
                ]
            )
        ),
        wait=True,
    )
