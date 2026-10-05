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

    # Recent complaints (POST /recent), kept apart from the knowledge so they never show up in a search.
    recent_collection: str = "recent_complaints"
    recent_retention_minutes: int = 1440  # complaints older than a day are deleted
    recent_examples: int = 3  # how many of the similar complaints are returned for the agent to read
    recent_text_chars: int = 240  # only the start of each complaint is kept
