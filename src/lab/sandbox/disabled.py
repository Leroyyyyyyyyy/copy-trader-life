"""Disabled sandbox: tool stays off. Never execs guest code on the host."""

from __future__ import annotations

from typing import Any

from src.lab.sandbox.base import ProgramResult, SandboxExecutor


class DisabledSandbox:
    def available(self) -> bool:
        return False

    def run_program(
        self,
        code: str,
        snapshot: dict[str, Any],
        budget: dict[str, Any] | None = None,
    ) -> ProgramResult:
        del code, snapshot, budget
        return ProgramResult(
            error_code="sandbox_unavailable",
            retryable=False,
            infrastructure=True,
            payload={"message": "qualified SandboxExecutor is not configured"},
        )


def is_enabled(sandbox: SandboxExecutor | None) -> bool:
    return sandbox is not None and sandbox.available()
