"""Mutable plan simulation environment."""

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
)
from src.lab.environments.base import observation_prompt_bits
from src.lab.execution.simulation import SimulationGateway
from src.lab.tools.registry import ToolRegistry, ToolSpec


class PlanSimulationEnvironment:
    """Teaching account that can apply a single stop-loss update commit."""

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
                name="get_account_snapshot",
                description="Read current simulated account plans",
                mutating=False,
            )
        )
        reg.register(
            ToolSpec(
                name="get_plan",
                description="Read one plan",
                mutating=False,
                required_args=("plan_id",),
            )
        )
        reg.register(
            ToolSpec(
                name="submit_plan_update",
                description="Commit a stop-loss update on one filled plan",
                mutating=True,
                required_args=("plan_id", "stop_loss", "evidence_refs"),
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
        if name == "get_account_snapshot":
            return self.gateway.snapshot()
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
        if name == "submit_plan_update":
            decision = SubmittedDecision(
                kind=DecisionKind.UPDATE,
                referenced_plan_id=str(arguments["plan_id"]),
                proposed_changes={"stop_loss": float(arguments["stop_loss"])},
                evidence_refs=tuple(arguments.get("evidence_refs") or ()),
                claimed_success=bool(arguments.get("claimed_success", False)),
                expected_state_version=arguments.get(
                    "expected_state_version", self.gateway.state_version
                ),
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
                    retryable="stale_state_version" in (receipt.error_code or ""),
                    payload=receipt.to_dict(),
                    mutated=False,
                )
            self._decision = decision
            return {
                "receipt": receipt.to_dict(),
                "state_version": self.gateway.state_version,
                "evidence_ids": list(decision.evidence_refs),
                "snapshot": self.gateway.snapshot(),
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
