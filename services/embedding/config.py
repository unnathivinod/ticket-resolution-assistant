"""Settings for the embedding service. Each one can be overridden with an environment
variable that starts with EMBEDDING_, for example EMBEDDING_DENSE_MODEL."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EMBEDDING_")

    # The three small models this service runs (all on CPU, no GPU needed).
    dense_model: str = "BAAI/bge-small-en-v1.5"  # text -> 384 numbers (meaning)
    sparse_model: str = "Qdrant/bm25"  # text -> word weights (keywords)
    reranker_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"  # careful second reader

    cache_dir: str = "/models"  # where the model files are stored inside the container
    offline: bool = True  # True = never download at runtime, use the files baked into the image

    # Limits that protect the service from oversized requests.
    max_texts_per_request: int = 256
    max_chars_per_text: int = 8000
    max_rerank_documents: int = 100
