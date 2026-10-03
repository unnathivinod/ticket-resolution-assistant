"""Settings for the retrieval service. Override any of them with RETRIEVAL_<NAME>."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RETRIEVAL_")

    qdrant_url: str = "http://qdrant:6333"
    embedding_url: str = "http://embedding:8004"
    collection: str = "support_knowledge"

    candidates: int = 20  # how many results each search fetches before reranking
    max_top_k: int = 50  # the most results one request may ask for, per source type
    product_boost: float = 0.05  # small bonus when a result's product matches the triage hint
