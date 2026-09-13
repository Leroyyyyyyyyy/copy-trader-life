"""Tool schema registry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    mutating: bool
    required_args: tuple[str, ...] = ()
    handler: Callable[..., Any] | None = None


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "mutating": t.mutating,
                "required_args": list(t.required_args),
            }
            for t in self._tools.values()
        ]

    def names(self) -> set[str]:
        return set(self._tools)
