"""Settings for the generation service. Override any of them with GENERATION_<NAME>."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GENERATION_")

    # The language model. Any OpenAI-compatible server works: local Ollama (default) or a hosted API.
    llm_enabled: bool = True  # False = never call a model, always answer from the sources directly
    llm_base_url: str = "http://host.docker.internal:11434/v1"
    llm_model: str = "llama3.2:3b"
    llm_api_key: str = "not-needed"
    llm_timeout_seconds: float = 180.0  # a hosted model answers in seconds: 30 is plenty there
    # Only for hosted models that "think" before they answer (for example gpt-oss on Groq).
    llm_reasoning_effort: str = ""  # low | medium | high. Empty = the setting is not sent
    llm_extra_tokens: int = 0  # extra output room, because the thinking counts as output
    llm_rate_limit_wait_seconds: float = 10.0  # on "too many requests": wait this long at most, then move on

    # A backup model, asked only when the first one fails (unreachable, rate limited, or two
    # unusable answers). Empty = no backup. Typical use: a fast hosted model first, local Ollama
    # as the backup. If both fail, the steps are quoted from the best source as before.
    fallback_llm_base_url: str = ""
    fallback_llm_model: str = ""
    fallback_llm_api_key: str = "not-needed"
    fallback_llm_timeout_seconds: float = 180.0
    temperature: float = 0.1  # low = consistent, factual answers
    max_output_tokens: int = 400

    # Ask the model to say whether the best source is about the SAME problem before it writes
    # steps, and withhold the answer if it says no (prompt v3). OFF by default: measured with
    # llama3.2:3b it refused every complaint, including the ones it answers correctly without it
    # (docs/DESIGN_DECISIONS.md). Worth trying again with a larger model.
    match_check: bool = False

    embedding_url: str = "http://embedding:8004"

    # Prompt size limits. A small local model on a CPU reads slowly, so the prompt is kept short.
    max_prompt_sources: int = 3  # distinct sources shown to the model
    max_source_chars: int = 1200
    max_steps: int = 4  # fewer steps = a shorter, faster answer
    max_complaint_chars: int = 4000

    # The reply to the customer (POST /reply). Short on purpose: an agent reads it before sending.
    max_reply_words: int = 130
    max_reply_tokens: int = 350
    max_reply_chars: int = 1800  # a longer reply is treated as unusable
    max_reply_steps: int = 6
    reply_temperature: float = 0.3  # a little freer than the answer, so the wording sounds natural

    # Checks on the model's answer.
    support_threshold: float = 0.65  # how close a step must be to its cited source to count as supported
    repeat_threshold: float = 0.8  # how close a step must be to an already-tried action to be flagged
    duplicate_overlap: float = 0.6  # sources sharing this share of their steps are treated as one
