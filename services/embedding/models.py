"""Loads the three models once and keeps them in memory."""

from __future__ import annotations

from pathlib import Path

from services.embedding.config import Settings


def stored_model_folder(model_class, model_name: str, cache_dir: str, must_contain: str) -> str:
    """Find the folder where the image build saved a model's files, by looking on disk (no internet).

    Downloads are stored as <cache_dir>/models--<owner>--<name>/snapshots/<version>/<files>.
    """
    repo = next(
        m["sources"]["hf"]
        for m in model_class.list_supported_models()
        if m["model"].lower() == model_name.lower()
    )
    snapshots = Path(cache_dir) / f"models--{repo.replace('/', '--')}" / "snapshots"
    matches = sorted(snapshots.glob(f"*/{must_contain}"))
    if not matches:
        raise FileNotFoundError(f"No saved files for {model_name} under {snapshots}. Rebuild the image.")
    return str(matches[-1].parent)


def load_sparse_model(settings: Settings):
    """Load the BM25 keyword model."""
    from fastembed import SparseTextEmbedding

    options = {"cache_dir": settings.cache_dir, "local_files_only": settings.offline}
    if settings.offline:
        # fastembed 0.8.1 cannot find the BM25 files by itself in offline mode: it checks for a
        # placeholder file that is never downloaded. So we hand it the stored folder directly.
        options["specific_model_path"] = stored_model_folder(
            SparseTextEmbedding, settings.sparse_model, settings.cache_dir, must_contain="english.txt"
        )
    return SparseTextEmbedding(settings.sparse_model, **options)


class EmbeddingModels:
    def __init__(self, settings: Settings) -> None:
        # Imported here (not at the top) so the API code can be tested without these heavy libraries.
        from fastembed import TextEmbedding
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        options = {"cache_dir": settings.cache_dir, "local_files_only": settings.offline}
        self.dense_name = settings.dense_model
        self.reranker_name = settings.reranker_model
        self._dense = TextEmbedding(settings.dense_model, **options)
        self._sparse = load_sparse_model(settings)
        self._reranker = TextCrossEncoder(settings.reranker_model, **options)

        # Run each model once now, so the first real request is not slow.
        self.dense_dim = len(self.embed_dense(["warm up"])[0])
        self.embed_sparse(["warm up"], kind="document")
        self.rerank("warm up", ["warm up"])

    def embed_dense(self, texts: list[str]) -> list[list[float]]:
        """Meaning vectors. Similar sentences get similar numbers. Already normalised to length 1."""
        return [vector.tolist() for vector in self._dense.embed(texts)]

    def embed_sparse(self, texts: list[str], kind: str) -> list[dict]:
        """Keyword vectors for BM25: which words appear, as (word id, weight) pairs.

        Stored documents and search queries are weighted differently, which is why `kind` matters.
        """
        results = self._sparse.query_embed(texts) if kind == "query" else self._sparse.embed(texts)
        return [
            {"indices": [int(i) for i in item.indices], "values": [float(v) for v in item.values]}
            for item in results
        ]

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        """Score how well each document answers the query. Higher is better."""
        return [float(score) for score in self._reranker.rerank(query, documents)]
