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
    # Keys that may also change data: add tickets and articles, manage ticket classes.
    admin_api_keys: str = "dev-admin-key"
    admin_rate_limit_per_minute: int = 600  # bulk loads send many requests
    # Signing in. The secret signs the session tokens: anyone who knows it can pretend to be any
    # user, so in production it comes from a secret manager, like the API keys.
    token_secret: str = "dev-token-secret"
    session_minutes: int = 480  # a working day, then the person signs in again
    ingest_stream: str = "ingest:events"
    cache_ttl_seconds: int = 3600

    top_k_tickets: int = 3
    top_k_kb: int = 2
    # Below this similarity nothing we found is close enough to the complaint, so we do not ask the
    # model to write an answer and recommend escalation instead. From evals/results/triage.md:
    # three quarters of off-topic questions have a closest match below 0.70, and three quarters of
    # real complaints are above 0.80. The end-to-end eval will measure this value directly.
    min_similarity: float = 0.72
    # A second check on the same question, from a different model: the cross-encoder's relevance
    # score for the best source. 0 switches it off. Set it from evals/eval_relevance_gate.py.
    min_relevance: float = 0.0
    # Incident detection. One complaint is one customer's problem. Several complaints that mean
    # the same, arriving close together, are probably one fault that affects many customers.
    # The two numbers at the end are measured by evals/eval_incidents.py: on test complaints the
    # tuning never saw they flag 71% of incidents and 2% of quiet half hours. The first guess
    # (0.8 and 5 complaints) flagged 38% and 16%, so it was replaced.
    incident_enabled: bool = True
    incident_window_minutes: int = 30  # how far back "close together" goes
    incident_min_similar: int = 3  # this many similar complaints (this one included) raise the flag
    incident_min_similarity: float = 0.875  # how close in meaning two complaints must be
    max_complaint_chars: int = 4000
    generation_timeout_seconds: float = 240.0

    def admin_key_set(self) -> set[str]:
        return {key.strip() for key in self.admin_api_keys.split(",") if key.strip()}

    def key_set(self) -> set[str]:
        """Every key that may call the API. An admin key can do everything an agent key can."""
        agent_keys = {key.strip() for key in self.api_keys.split(",") if key.strip()}
        return agent_keys | self.admin_key_set()
