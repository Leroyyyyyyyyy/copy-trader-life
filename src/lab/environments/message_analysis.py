"""Readonly message analysis environment."""

from __future__ import annotations

from typing import Any

from src.lab.domain import (
    DecisionKind,
    FinalEnvironmentSnapshot,
    ObservationBundle,
    SubmittedDecision,
    TaskSpec,
    ToolResult,
    ToolStatus,
    decision_from_dict,
)
from src.lab.environments.base import observation_prompt_bits
from src.lab.execution.simulation import SimulationGateway
from src.lab.tools.registry import ToolRegistry, ToolSpec


class MessageAnalysisEnvironment:
    """Readonly evidence + submit_decision. Does not mutate plan fields."""

    def __init__(self, bundle: ObservationBundle, gateway: SimulationGateway, run_id: str):
        self.bundle = bundle
        self.gateway = gateway
        self.run_id = run_id
        self._decision: SubmittedDecision | None = None
        max_version = max((p.state_version for p in bundle.plans), default=1)
        self.gateway.reset(bundle.plans, max_version)

    def tool_registry(self) -> ToolRegistry:
        reg = ToolRegistry()
        reg.register(
            ToolSpec(
                name="list_messages",
                description="List visible messages",
                mutating=False,
            )
        )
        reg.register(
            ToolSpec(
                name="get_plan",
                description="Read one plan by id",
                mutating=False,
                required_args=("plan_id",),
            )
        )
        reg.register(
            ToolSpec(
                name="submit_decision",
                description="Submit analysis decision with evidence",
                mutating=True,
                required_args=("kind", "evidence_refs"),
            )
        )
        return reg

    def build_prompt(self, task: TaskSpec, observations: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "task": task.to_dict(),
            "objective": task.objective,
            "world": observation_prompt_bits(self.bundle),
            "tools": self.tool_registry().schemas(),
            "recent_observations": observations[-6:],
            "state_version": self.gateway.state_version,
        }

    def handle_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "list_messages":
            return {
                "messages": [
                    {
                        "message_id": m.message_id,
                        "text": m.text,
                        "visible_at": m.visible_at,
                    }
                    for m in self.bundle.messages
                ]
            }
        if name == "get_plan":
            plan_id = str(arguments["plan_id"])
            for plan in self.gateway.plans():
                if plan.plan_id == plan_id:
                    return {"plan": plan.to_dict(), "state_version": self.gateway.state_version}
            return ToolResult(
                call_id="",
                name=name,
                status=ToolStatus.ERROR,
                error_code="unknown_plan",
                retryable=True,
            )
        if name == "submit_decision":
            decision = decision_from_dict(
                {
                    "kind": arguments["kind"],
                    "referenced_plan_id": arguments.get("referenced_plan_id"),
                    "proposed_changes": arguments.get("proposed_changes") or {},
                    "evidence_refs": arguments.get("evidence_refs") or [],
                    "claimed_success": bool(arguments.get("claimed_success", False)),
                    "expected_state_version": arguments.get(
                        "expected_state_version", self.gateway.state_version
                    ),
                }
            )
            # Analysis commit: persist answer without changing plan fields.
            if decision.kind == DecisionKind.UPDATE and decision.proposed_changes.get("stop_loss"):
                # Message analysis should not mutate SL via this environment.
                decision = SubmittedDecision(
                    kind=decision.kind,
                    referenced_plan_id=decision.referenced_plan_id,
                    proposed_changes={},
                    evidence_refs=decision.evidence_refs,
                    claimed_success=decision.claimed_success,
                    expected_state_version=decision.expected_state_version,
                )
            receipt = self.gateway.commit(
                run_id=self.run_id,
                decision=decision,
                expected_state_version=decision.expected_state_version,
            )
            if not receipt.applied and not receipt.reused:
                return ToolResult(
                    call_id="",
                    name=name,
                    status=ToolStatus.ERROR,
                    error_code=receipt.error_code or "commit_rejected",
                    retryable=True,
                    payload=receipt.to_dict(),
                    mutated=False,
                )
            self._decision = decision
            return {
                "receipt": receipt.to_dict(),
                "state_version": self.gateway.state_version,
                "evidence_ids": list(decision.evidence_refs),
            }
        raise ValueError(f"unhandled tool {name}")

    def final_snapshot(self) -> FinalEnvironmentSnapshot:
        return FinalEnvironmentSnapshot(
            plans=self.gateway.plans(),
            submit_count=self.gateway.submit_count,
            state_version=self.gateway.state_version,
        )

    def last_decision(self) -> SubmittedDecision | None:
        return self._decision
