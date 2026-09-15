"""Mutable plan simulation environment."""

from __future__ import annotations

from typing import Any

from src.lab.domain import (
    DecisionKind,
    FinalEnvironmentSnapshot,
    ObservationBundle,
    ObservationPolicy,
    SubmittedDecision,
    TaskSpec,
    ToolResult,
    ToolStatus,
    decision_from_dict,
)
from src.lab.environments.allowed_actions import allowed_actions_from_plans
from src.lab.environments.base import observation_prompt_bits
from src.lab.execution.simulation import SimulationGateway
from src.lab.sandbox.disabled import is_enabled
from src.lab.sandbox.snapshot_sim import simulate_action as fork_simulate
from src.lab.tools.registry import ToolRegistry, ToolSpec


class PlanSimulationEnvironment:
    """Teaching account that can apply a single stop-loss update commit."""

    def __init__(
        self,
        bundle: ObservationBundle,
        gateway: SimulationGateway,
        run_id: str,
        *,
        observation_policy: ObservationPolicy = ObservationPolicy.STATE_ONLY,
        sandbox: Any = None,
    ):
        self.bundle = bundle
        self.gateway = gateway
        self.run_id = run_id
        self.observation_policy = observation_policy
        self.sandbox = sandbox
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
        reg.register(
            ToolSpec(
                name="submit_decision",
                description="Commit ignore or review without changing plan fields",
                mutating=True,
                required_args=("kind", "evidence_refs"),
            )
        )
        reg.register(
            ToolSpec(
                name="simulate_action",
                description="Fork an immutable snapshot; does not commit",
                mutating=False,
                required_args=("kind",),
            )
        )
        reg.register(
            ToolSpec(
                name="commit_simulated_action",
                description="Host-side commit of a proposed simulated action",
                mutating=True,
                required_args=("kind", "evidence_refs", "expected_state_version"),
            )
        )
        if is_enabled(self.sandbox):
            reg.register(
                ToolSpec(
                    name="run_program",
                    description="Run Python in Docker sandbox. Simulate/read only; cannot commit.",
                    mutating=False,
                    required_args=("code",),
                )
            )
        return reg

    def build_prompt(self, task: TaskSpec, observations: list[dict[str, Any]]) -> dict[str, Any]:
        prompt = {
            "task": task.to_dict(),
            "objective": task.objective,
            "world": observation_prompt_bits(self.bundle),
            "tools": self.tool_registry().schemas(),
            "recent_observations": observations[-6:],
            "state_version": self.gateway.state_version,
            "observation_policy": self.observation_policy.value,
        }
        if self.observation_policy == ObservationPolicy.ALLOWED_ACTIONS:
            prompt["allowed_actions"] = allowed_actions_from_plans(self.gateway.plans())
        return prompt

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
        if name == "submit_decision":
            kind = DecisionKind(str(arguments["kind"]))
            if kind == DecisionKind.UPDATE:
                return ToolResult(
                    call_id="",
                    name=name,
                    status=ToolStatus.ERROR,
                    error_code="wrong_tool_for_update",
                    retryable=True,
                    payload={"use": "submit_plan_update"},
                )
            decision = decision_from_dict(
                {
                    "kind": kind.value,
                    "referenced_plan_id": arguments.get("referenced_plan_id"),
                    "proposed_changes": {},
                    "evidence_refs": arguments.get("evidence_refs") or [],
                    "claimed_success": bool(arguments.get("claimed_success", False)),
                    "expected_state_version": arguments.get(
                        "expected_state_version", self.gateway.state_version
                    ),
                }
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
        if name == "simulate_action":
            before = self.gateway.submit_count
            action = _tool_action(arguments)
            forked = fork_simulate(self.gateway.snapshot(), action)
            if self.gateway.submit_count != before:
                raise RuntimeError("simulate_action mutated shared account")
            return {
                **forked,
                "state_version": self.gateway.state_version,
            }
        if name == "run_program":
            if not is_enabled(self.sandbox):
                return ToolResult(
                    call_id="",
                    name=name,
                    status=ToolStatus.ERROR,
                    error_code="sandbox_unavailable",
                    retryable=False,
                    payload={"message": "run_program disabled without qualified sandbox"},
                )
            result = self.sandbox.run_program(
                str(arguments["code"]),
                self.gateway.snapshot(),
                arguments.get("budget") or {"max_simulations": 16},
            )
            payload = result.to_tool_payload()
            payload["state_version"] = self.gateway.state_version
            if result.error_code and result.infrastructure:
                return ToolResult(
                    call_id="",
                    name=name,
                    status=ToolStatus.ERROR,
                    error_code=result.error_code,
                    retryable=result.retryable,
                    payload=payload,
                    expanded_calls=result.expanded_calls,
                )
            if result.program_error:
                return ToolResult(
                    call_id="",
                    name=name,
                    status=ToolStatus.ERROR,
                    error_code="program_error",
                    retryable=True,
                    payload=payload,
                    expanded_calls=result.expanded_calls,
                )
            return payload
        if name == "commit_simulated_action":
            return self._commit_action(
                arguments,
                decision_id=str(arguments.get("idempotency_key") or "simulated"),
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

    def _commit_action(self, arguments: dict[str, Any], *, decision_id: str) -> Any:
        action = _tool_action(arguments)
        kind = DecisionKind(str(action["kind"]))
        if kind == DecisionKind.UPDATE:
            plan_id = action.get("plan_id") or action.get("referenced_plan_id")
            decision = SubmittedDecision(
                kind=kind,
                referenced_plan_id=str(plan_id) if plan_id else None,
                proposed_changes=(
                    {"stop_loss": float(action["stop_loss"])}
                    if action.get("stop_loss") is not None
                    else {}
                ),
                evidence_refs=tuple(action.get("evidence_refs") or ()),
                claimed_success=bool(action.get("claimed_success", False)),
                expected_state_version=action.get(
                    "expected_state_version", self.gateway.state_version
                ),
            )
        else:
            decision = decision_from_dict(
                {
                    "kind": kind.value,
                    "referenced_plan_id": action.get("referenced_plan_id")
                    or action.get("plan_id"),
                    "proposed_changes": {},
                    "evidence_refs": action.get("evidence_refs") or [],
                    "claimed_success": bool(action.get("claimed_success", False)),
                    "expected_state_version": action.get(
                        "expected_state_version", self.gateway.state_version
                    ),
                }
            )
        receipt = self.gateway.commit(
            run_id=self.run_id,
            decision=decision,
            decision_id=decision_id,
            expected_state_version=decision.expected_state_version,
        )
        if not receipt.applied and not receipt.reused:
            return ToolResult(
                call_id="",
                name="commit_simulated_action",
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

    def final_snapshot(self) -> FinalEnvironmentSnapshot:
        return FinalEnvironmentSnapshot(
            plans=self.gateway.plans(),
            submit_count=self.gateway.submit_count,
            state_version=self.gateway.state_version,
        )

    def last_decision(self) -> SubmittedDecision | None:
        return self._decision


def _tool_action(arguments: dict[str, Any]) -> dict[str, Any]:
    if isinstance(arguments.get("action"), dict):
        merged = dict(arguments["action"])
        for key in ("expected_state_version", "idempotency_key", "evidence_refs"):
            if key in arguments and key not in merged:
                merged[key] = arguments[key]
        return merged
    return dict(arguments)
