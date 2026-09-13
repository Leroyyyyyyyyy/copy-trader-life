"""Shared environment helpers."""

from __future__ import annotations

from typing import Any, Protocol

from src.lab.domain import FinalEnvironmentSnapshot, ObservationBundle, SubmittedDecision, TaskSpec
from src.lab.tools.registry import ToolRegistry


class Environment(Protocol):
    def tool_registry(self) -> ToolRegistry:
        ...

    def build_prompt(self, task: TaskSpec, observations: list[dict[str, Any]]) -> dict[str, Any]:
        ...

    def handle_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        ...

    def final_snapshot(self) -> FinalEnvironmentSnapshot:
        ...

    def last_decision(self) -> SubmittedDecision | None:
        ...


def observation_prompt_bits(bundle: ObservationBundle) -> dict[str, Any]:
    return {
        "messages": [
            {
                "message_id": m.message_id,
                "channel": m.channel,
                "visible_at": m.visible_at,
                "text": m.text,
                "image_refs": list(m.image_refs),
            }
            for m in bundle.messages
        ],
        "plans": [p.to_dict() for p in bundle.plans],
        "chart_notes": list(bundle.chart_notes),
        "account_note": bundle.account_note,
    }
