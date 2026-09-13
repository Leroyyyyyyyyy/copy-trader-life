"""Validate and execute tool calls against a registry + environment."""

from __future__ import annotations

from typing import Any

from src.lab.domain import ToolCall, ToolResult, ToolStatus
from src.lab.tools.registry import ToolRegistry


class ToolDispatcher:
    def __init__(self, registry: ToolRegistry, environment: Any):
        self.registry = registry
        self.environment = environment

    def execute(self, call: ToolCall, *, mutate_allowed: bool = True) -> ToolResult:
        spec = self.registry.get(call.name)
        if spec is None:
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                status=ToolStatus.ERROR,
                error_code="unknown_tool",
                retryable=True,
                payload={"available": sorted(self.registry.names())},
            )
        missing = [a for a in spec.required_args if a not in call.arguments]
        if missing:
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                status=ToolStatus.ERROR,
                error_code="missing_args",
                retryable=True,
                payload={"missing": missing},
            )
        if spec.mutating and not mutate_allowed:
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                status=ToolStatus.ERROR,
                error_code="mutate_not_allowed_this_turn",
                retryable=True,
            )
        try:
            result = self.environment.handle_tool(call.name, call.arguments)
        except Exception as exc:  # noqa: BLE001 - surface as tool observation
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                status=ToolStatus.ERROR,
                error_code="tool_exception",
                retryable=False,
                payload={"error": str(exc)},
            )
        if isinstance(result, ToolResult):
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                status=result.status,
                payload=result.payload,
                error_code=result.error_code,
                retryable=result.retryable,
                evidence_ids=result.evidence_ids,
                state_version=result.state_version,
                mutated=result.mutated or spec.mutating,
            )
        return ToolResult(
            call_id=call.call_id,
            name=call.name,
            status=ToolStatus.OK,
            payload=dict(result or {}),
            mutated=spec.mutating,
            state_version=(result or {}).get("state_version"),
            evidence_ids=tuple((result or {}).get("evidence_ids") or ()),
        )
