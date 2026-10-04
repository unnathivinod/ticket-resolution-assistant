"""Client for a large language model behind an OpenAI-compatible chat API.

Ollama exposes this API at http://<host>:11434/v1, and so do most hosted providers (Groq, OpenAI,
Gemini ...). Switching provider therefore only means changing the base URL, the model name and
the API key.
"""

from __future__ import annotations

import json
import time

import httpx


class LLMError(RuntimeError):
    """The model could not be used. See the two subclasses for the reason."""


class LLMUnavailableError(LLMError):
    """The model server could not be reached or took too long."""


class LLMOutputError(LLMError):
    """The model answered, but not with the JSON we asked for."""


def parse_json_object(text: str) -> dict:
    """Read a JSON object from the model's reply, tolerating extra text around it."""
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise LLMOutputError("The model did not return JSON") from None
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError as error:
            raise LLMOutputError("The model returned malformed JSON") from error
    if not isinstance(value, dict):
        raise LLMOutputError("The model returned JSON that is not an object")
    return value


class LLMClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "not-needed",
        timeout: float = 180.0,
        transport: httpx.BaseTransport | None = None,  # lets tests replace the network
        reasoning_effort: str = "",  # "low" | "medium" | "high" for models that think first; "" = not sent
        extra_tokens: int = 0,  # extra output room for such models: their thinking counts as output
        rate_limit_wait: float = 0.0,  # wait at most this long when the provider says "too many requests"
    ) -> None:
        self.model = model
        self._reasoning_effort = reasoning_effort
        self._extra_tokens = extra_tokens
        self._rate_limit_wait = rate_limit_wait
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
            transport=transport,
        )

    def ready(self) -> bool:
        """True when the server answers and has our model available."""
        try:
            response = self._http.get("/models", timeout=2)
            names = {item.get("id") for item in response.json().get("data", [])}
            return response.status_code == 200 and self.model in names
        except (httpx.HTTPError, ValueError):
            return False

    def chat_json(
        self, system: str, user: str, schema: dict, max_tokens: int = 500, temperature: float = 0.1
    ) -> tuple[dict, dict]:
        """Ask for a reply that follows a JSON schema. Returns (parsed reply, token usage)."""
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": temperature,
            "max_tokens": max_tokens + self._extra_tokens,
            # The server constrains the output to this schema, so the reply is always parseable.
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "reply", "schema": schema, "strict": True},
            },
        }
        if self._reasoning_effort:
            payload["reasoning_effort"] = self._reasoning_effort
        try:
            response = self._http.post("/chat/completions", json=payload)
            if response.status_code == 429 and (wait := self._short_wait(response)) is not None:
                # A hosted model's per-minute limit. A few seconds of patience is cheaper than giving up.
                time.sleep(wait)
                response = self._http.post("/chat/completions", json=payload)
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except httpx.TimeoutException as error:
            raise LLMUnavailableError("The model took too long to answer") from error
        except httpx.HTTPStatusError as error:
            detail = error.response.text[:300].replace("\n", " ")
            raise LLMUnavailableError(
                f"The model server answered {error.response.status_code}: {detail}"
            ) from error
        except httpx.HTTPError as error:
            raise LLMUnavailableError(f"The model server could not be used: {error}") from error
        except (KeyError, IndexError, ValueError) as error:
            raise LLMOutputError("The model server returned an unexpected response") from error
        if not content:
            # Seen with models that think first: the thinking used up all the output room.
            raise LLMOutputError("The model returned an empty reply")
        return parse_json_object(content), data.get("usage") or {}

    def _short_wait(self, response: httpx.Response) -> float | None:
        """Seconds to wait before one more try, or None when the provider asks for too long."""
        try:
            asked = float(response.headers.get("retry-after", ""))
        except ValueError:
            return None
        return asked + 0.5 if 0 <= asked <= self._rate_limit_wait else None

    def close(self) -> None:
        self._http.close()
