"""Model adapter protocol."""

from __future__ import annotations

from typing import Any, Protocol

from src.lab.domain import ModelTurn, Usage


class ModelAdapter(Protocol):
    def generate(self, request: dict[str, Any]) -> tuple[ModelTurn, Usage]:
        ...
