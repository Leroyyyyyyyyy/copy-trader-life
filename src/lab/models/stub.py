"""Scripted stub model for offline harness protocol tests."""

from __future__ import annotations

from typing import Any

from src.lab.domain import ModelTurn, ToolCall, Usage


class StubModelAdapter:
    """Returns pre-scripted turns. Does not call a real LLM."""

    def __init__(self, script: list[dict[str, Any]]):
        self.script = list(script)
        self._idx = 0

    def generate(self, request: dict[str, Any]) -> tuple[ModelTurn, Usage]:
        del request  # stub ignores prompt content; protocol still receives it
        if self._idx >= len(self.script):
            turn = ModelTurn(
                raw={"error": "stub_exhausted"},
                parse_error="stub_exhausted",
            )
            return turn, Usage(model_calls=1)
        item = self.script[self._idx]
        self._idx += 1
        if "invalid_json" in item:
            return (
                ModelTurn(raw=item, parse_error="invalid_json"),
                Usage(model_calls=1, output_tokens=1),
            )
        tool_calls = tuple(
            ToolCall(
                call_id=str(tc.get("call_id", f"call_{self._idx}_{i}")),
                name=str(tc["name"]),
                arguments=dict(tc.get("arguments") or {}),
            )
            for i, tc in enumerate(item.get("tool_calls") or [])
        )
        final = item.get("final")
        return (
            ModelTurn(raw=item, tool_calls=tool_calls, final=final),
            Usage(model_calls=1, output_tokens=8),
        )
