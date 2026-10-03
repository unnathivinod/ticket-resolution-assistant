"""Stand-ins used by the fast tests, so they need no models and no running services."""

from __future__ import annotations

import math
import re
import zlib

DIM = 64


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _word_id(word: str) -> int:
    return zlib.crc32(word.encode())


class FakeEmbedder:
    """Behaves like EmbeddingClient, but uses simple word counting instead of real models.

    Good enough to test the plumbing (indexing, filtering, ranking, limits). The quality of
    real semantic search is measured separately by the evals against the real models.
    """

    model = "fake-embedder"

    def __init__(self) -> None:
        self.rerank_calls = 0
        self.is_ready = True

    def ready(self) -> bool:
        return self.is_ready

    def embed(self, texts: list[str], kind: str = "document") -> dict:
        dense, sparse = [], []
        for text in texts:
            vector = [0.0] * DIM
            counts: dict[int, float] = {}
            for word in _words(text):
                vector[_word_id(word) % DIM] += 1.0
                counts[_word_id(word)] = 1.0 if kind == "query" else counts.get(_word_id(word), 0.0) + 1.0
            length = math.sqrt(sum(v * v for v in vector)) or 1.0
            dense.append([v / length for v in vector])
            sparse.append({"indices": list(counts), "values": list(counts.values())})
        return {"model": self.model, "dim": DIM, "dense": dense, "sparse": sparse}

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        self.rerank_calls += 1
        wanted = set(_words(query))
        # Share of query words found in the document, stretched to look like a raw model score.
        return [10.0 * len(wanted & set(_words(doc))) / max(len(wanted), 1) - 5.0 for doc in documents]


class InProcessRetrieval:
    """Behaves like RetrievalClient, but calls the real search code directly instead of over HTTP."""

    def __init__(self, searcher) -> None:
        self._searcher = searcher
        self.is_ready = True

    def ready(self) -> bool:
        return self.is_ready

    def search(self, query: str, top_k_tickets: int = 3, top_k_kb: int = 2, **options) -> dict:
        from services.retrieval.search import SearchRequest

        request = SearchRequest(query=query, top_k_tickets=top_k_tickets, top_k_kb=top_k_kb, **options)
        return self._searcher.search(request).model_dump()
