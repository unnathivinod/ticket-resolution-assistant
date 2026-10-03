"""Settings for the ingestion worker. Override any of them with INGESTION_<NAME>."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INGESTION_")

    redis_url: str = "redis://redis:6379/0"
    database_url: str = "postgresql://app:local-dev-password@postgres:5432/tickets"
    qdrant_url: str = "http://qdrant:6333"
    embedding_url: str = "http://embedding:8004"

    stream: str = "ingest:events"  # the queue the gateway writes to
    dead_letter_stream: str = "ingest:dead"  # events that kept failing, kept for a human to look at
    group: str = "indexers"  # workers in the same group share the work
    batch_size: int = 32
    block_ms: int = 5000  # how long to wait for new events before doing housekeeping
    max_attempts: int = 5
    retry_after_ms: int = 30_000  # an event not finished after this long is tried again

    sweep_every_seconds: int = 60
    # The sweep only picks up documents that have been waiting this long, so it does not
    # race with events that are still on their way through the queue.
    sweep_grace_seconds: int = 120

    metrics_port: int = 8005
