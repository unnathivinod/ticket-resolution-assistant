"""Settings for the triage service. Override any of them with TRIAGE_<NAME>."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

HERE = Path(__file__).resolve().parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TRIAGE_")

    retrieval_url: str = "http://retrieval:8002"
    embedding_url: str = "http://embedding:8004"

    signals_path: Path = HERE / "signals.yaml"  # urgency and tone examples
    params_path: Path = HERE / "params.json"  # tuned thresholds, written by the eval script

    max_neighbours: int = 25  # fetched from retrieval; the tuned setting decides how many get a vote
    max_complaint_chars: int = 4000
