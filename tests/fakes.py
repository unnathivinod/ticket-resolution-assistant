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


class FakeLLM:
    """Behaves like LLMClient, but returns prepared replies (or raises prepared errors) instantly."""

    def __init__(self, *replies, model: str = "fake-llm") -> None:
        self.model = model
        self._replies = list(replies)
        self.calls: list[dict] = []
        self.is_ready = True

    def ready(self) -> bool:
        return self.is_ready

    def chat_json(
        self, system: str, user: str, schema: dict, max_tokens: int = 500, temperature: float = 0.1
    ):
        self.calls.append({"system": system, "user": user, "schema": schema})
        reply = self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply, {"prompt_tokens": 120, "completion_tokens": 40}


class InMemoryCache:
    """Behaves like the gateway's RedisCache."""

    def __init__(self) -> None:
        self.data: dict[str, dict] = {}
        self.version = "0"

    def get(self, key: str):
        return self.data.get(key)

    def set(self, key: str, value: dict) -> None:
        self.data[key] = value

    def index_version(self) -> str:
        return self.version


class InMemoryQueue:
    """Behaves like the gateway's IngestQueue, and remembers what was published."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.working = True

    def publish(self, doc_type: str, doc_id: str) -> bool:
        if self.working:
            self.events.append((doc_type, doc_id))
        return self.working


class InMemoryStore:
    """Behaves like PostgreSQL for both the gateway (PostgresStore) and the worker (DocumentStore).

    Time is a simple counter that goes up with every write, which is all the
    "is the index behind?" comparison needs.
    """

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.feedback: list[tuple] = []
        self.is_ready = True
        self.classes = {
            "category": {"connectivity_intermittent": "Keeps dropping", "billing_dispute": "Disputed charge"},
            "product": {"broadband": "Home broadband", "mobile": "Mobile"},
        }
        self.documents: dict[str, dict[str, dict]] = {"ticket": {}, "kb": {}}
        self.proposal_rows: list[dict] = []
        self._clock = 0

    def _tick(self) -> int:
        self._clock += 1
        return self._clock

    def ready(self) -> bool:
        return self.is_ready

    # audit log and feedback
    def save_request(self, record: dict) -> bool:
        self.requests.append(record)
        return True

    def save_feedback(self, request_id, helpful, comment, edited, correct_category=None) -> bool:
        if request_id not in {record["request_id"] for record in self.requests}:
            return False
        self.feedback.append((request_id, helpful, comment, edited, correct_category))
        return True

    # ticket classes
    def taxonomy(self) -> dict:
        return {
            kind: [{"name": name, "description": text} for name, text in sorted(items.items())]
            for kind, items in self.classes.items()
        }

    def add_class(self, kind, name, description) -> None:
        self.classes[kind][name] = description

    def retire_class(self, kind, name) -> bool:
        return self.classes[kind].pop(name, None) is not None

    # documents (gateway side)
    def _save(self, doc_type: str, document: dict) -> dict:
        previous = self.documents[doc_type].get(document["id"], {})
        row = {**document, "is_active": True, "updated_at": self._tick()}
        row["indexed_at"] = previous.get("indexed_at")
        row["version"] = previous.get("version", 0) + 1
        self.documents[doc_type][document["id"]] = row
        return row

    def save_ticket(self, ticket: dict) -> None:
        self._save("ticket", ticket)

    def save_article(self, article: dict) -> int:
        return self._save("kb", article)["version"]

    def retire_document(self, doc_type, doc_id) -> bool:
        row = self.documents[doc_type].get(doc_id)
        if row is None:
            return False
        row["is_active"], row["updated_at"] = False, self._tick()
        return True

    @staticmethod
    def _behind(row: dict) -> bool:
        return row["indexed_at"] is None or row["indexed_at"] < row["updated_at"]

    def document_status(self, doc_type, doc_id):
        row = self.documents[doc_type].get(doc_id)
        if row is None:
            return None
        return {
            "type": doc_type,
            "id": doc_id,
            "is_active": row["is_active"],
            "updated_at": str(row["updated_at"]),
            "indexed_at": str(row["indexed_at"]) if row["indexed_at"] else None,
            "index_up_to_date": not self._behind(row),
        }

    def documents_waiting(self) -> int:
        return sum(self._behind(row) for rows in self.documents.values() for row in rows.values())

    # documents (worker side)
    def load(self, doc_type, doc_id):
        row = self.documents[doc_type].get(doc_id)
        if row is None:
            return None, None
        document = {key: value for key, value in row.items() if key != "indexed_at"}
        document["updated_at"] = document["created_at"] = None
        return document, self._tick()

    def mark_indexed(self, doc_type, doc_id, read_at) -> None:
        self.documents[doc_type][doc_id]["indexed_at"] = read_at

    def pending(self, grace_seconds=0, limit=200):
        return [
            (doc_type, doc_id)
            for doc_type, rows in self.documents.items()
            for doc_id, row in rows.items()
            if self._behind(row)
        ][:limit]

    # proposals for new classes
    def proposals(self, status="pending") -> list[dict]:
        return [row for row in self.proposal_rows if row["status"] == status]

    def decide_proposal(self, proposal_id, approve, name=None, description=None):
        for row in self.proposal_rows:
            if row["id"] == proposal_id and row["status"] == "pending":
                row["status"] = "approved" if approve else "rejected"
                row["approved_name"] = name if approve else None
                if approve:
                    self.add_class(row["kind"], name, description)
                return row
        return None
