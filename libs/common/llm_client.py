"""Client for a large language model behind an OpenAI-compatible chat API.

Ollama exposes this API at http://<host>:11434/v1, and so do most hosted providers.
Switching provider therefore only means changing the base URL, the model name and the API key.
"""

from __future__ import annotations

import json

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
    ) -> None:
        self.model = model
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
            transport=transport,
        )

    def ready(self) -> bool:
        """True when the server answers and has our model available."""
        try:
            response = self._http.get("/models", timeout=5)
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
            "max_tokens": max_tokens,
            # The server constrains the output to this schema, so the reply is always parseable.
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "reply", "schema": schema, "strict": True},
            },
        }
        try:
            response = self._http.post("/chat/completions", json=payload)
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except httpx.TimeoutException as error:
            raise LLMUnavailableError("The model took too long to answer") from error
        except httpx.HTTPError as error:
            raise LLMUnavailableError(f"The model server could not be used: {error}") from error
        except (KeyError, IndexError, ValueError) as error:
            raise LLMOutputError("The model server returned an unexpected response") from error
        return parse_json_object(content), data.get("usage") or {}

    def close(self) -> None:
        self._http.close()
