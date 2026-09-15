"""Sandbox protocol. No host-process fallback for untrusted code."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol


class SandboxUnavailable(RuntimeError):
    """Raised/returned when a qualified executor is not configured."""


@dataclass(frozen=True)
class ProgramResult:
    stdout: str = ""
    program_error: Optional[dict[str, Any]] = None
    tool_trace: list[dict[str, Any]] = field(default_factory=list)
    proposed_action: Optional[dict[str, Any]] = None
    expanded_calls: int = 0
    error_code: Optional[str] = None
    retryable: bool = False
    infrastructure: bool = False
    payload: dict[str, Any] = field(default_factory=dict)

    def to_tool_payload(self) -> dict[str, Any]:
        return {
            "stdout": self.stdout,
            "program_error": self.program_error,
            "tool_trace": list(self.tool_trace),
            "proposed_action": self.proposed_action,
            "expanded_calls": self.expanded_calls,
            "infrastructure": self.infrastructure,
            **dict(self.payload),
        }


class SandboxExecutor(Protocol):
    def available(self) -> bool:
        ...

    def run_program(
        self,
        code: str,
        snapshot: dict[str, Any],
        budget: dict[str, Any] | None = None,
    ) -> ProgramResult:
        ...
