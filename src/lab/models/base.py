"""Model adapter protocol."""

from __future__ import annotations

from typing import Any, Protocol

from src.lab.domain import ModelTurn, Usage


class ModelAdapter(Protocol):
    def generate(self, request: dict[str, Any]) -> tuple[ModelTurn, Usage]:
        ...


class ModelCallError(Exception):
    """Provider/transport failure. Infrastructure, not a model decision."""

    def __init__(self, code: str, *, retryable: bool = False, attempts: int = 1, detail: str = ""):
        self.code = code
        self.retryable = retryable
        self.attempts = attempts
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)
