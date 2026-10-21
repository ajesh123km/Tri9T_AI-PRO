"""Provider boundary and strict structured-output validation."""
from collections.abc import Callable
from typing import Any, Protocol

from pydantic import ValidationError

from .schemas import TestCase


class LLMProvider(Protocol):
    name: str
    def complete(self, prompt: str) -> Any: ...


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


def generate_with_retries(provider: LLMProvider, prompt: str, retries: int = 2) -> list[TestCase]:
    last: Exception | None = None
    for _ in range(retries + 1):
        try:
            return validate_cases(provider.complete(prompt))
        except (ValidationError, ValueError, TypeError, TimeoutError) as exc:
            last = exc
    raise ValueError(f"LLM output remained invalid after {retries + 1} attempts: {last}")

