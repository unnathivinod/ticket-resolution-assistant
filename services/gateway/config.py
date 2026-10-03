"""Settings for the API gateway. Override any of them with GATEWAY_<NAME>."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GATEWAY_")

    triage_url: str = "http://triage:8001"
    retrieval_url: str = "http://retrieval:8002"
    generation_url: str = "http://generation:8003"
    redis_url: str = "redis://redis:6379/0"
    database_url: str = "postgresql://app:local-dev-password@postgres:5432/tickets"

    # Comma-separated keys that may call the API. In production these come from a secret manager.
    api_keys: str = "dev-local-key"
    rate_limit_per_minute: int = 30
    cache_ttl_seconds: int = 3600

    top_k_tickets: int = 3
    top_k_kb: int = 2
    # Below this similarity nothing we found is close enough to the complaint, so we do not ask the
    # model to write an answer and recommend escalation instead. From evals/results/triage.md:
    # three quarters of off-topic questions have a closest match below 0.70, and three quarters of
    # real complaints are above 0.80. The end-to-end eval will measure this value directly.
    min_similarity: float = 0.72
    max_complaint_chars: int = 4000
    generation_timeout_seconds: float = 240.0

    def key_set(self) -> set[str]:
        return {key.strip() for key in self.api_keys.split(",") if key.strip()}
