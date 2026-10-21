"""Provider boundary and strict structured-output validation."""
import json
from typing import Any, Protocol

import httpx
from pydantic import ValidationError

from .schemas import TestCase


class LLMProvider(Protocol):
    name: str
    def complete(self, prompt: str) -> Any: ...


class OpenAICompatibleProvider:
    """Adapter for providers exposing OpenAI chat-completions semantics."""

    name = "openai_compatible"

    def __init__(self, api_key: str, model: str, base_url: str,
                 timeout_seconds: float = 30) -> None:
        if not api_key:
            raise ValueError("LLM_API_KEY is required for the real provider")
        if not model:
            raise ValueError("LLM_MODEL is required for the real provider")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds

    def complete(self, prompt: str) -> Any:
        try:
            response = httpx.post(
                self.base_url,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={"model": self.model, "messages": [{"role": "user", "content": prompt}],
                      "temperature": 0},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise TimeoutError("LLM provider request timed out") from exc
        except httpx.HTTPError as exc:
            raise ValueError(f"LLM provider request failed: {exc}") from exc
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ValueError("LLM provider response did not contain chat content") from exc
        if not isinstance(content, str):
            raise TypeError("LLM provider content must be a JSON string")
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError("LLM provider returned malformed JSON") from exc


def validate_cases(raw: Any, allowed_source_references: set[str] | None = None) -> list[TestCase]:
    if not isinstance(raw, list) or not 3 <= len(raw) <= 5:
        raise ValueError("LLM output must be a list containing 3 to 5 cases")
    cases = [TestCase.model_validate(item) for item in raw]
    selected = {ref for case in cases for ref in case.source_references}
    if not selected:
        raise ValueError("LLM output must contain source references")
    if allowed_source_references is not None:
        unsupported = sorted(selected - allowed_source_references)
        if unsupported:
            raise ValueError(f"LLM output contains unsupported source references: {unsupported}")
    return cases


def generate_with_retries(provider: LLMProvider, prompt: str, retries: int = 2,
                          allowed_source_references: set[str] | None = None) -> list[TestCase]:
    last: Exception | None = None
    for _ in range(retries + 1):
        try:
            return validate_cases(provider.complete(prompt), allowed_source_references)
        except (ValidationError, ValueError, TypeError, TimeoutError) as exc:
            last = exc
    raise ValueError(f"LLM output remained invalid after {retries + 1} attempts: {last}")

