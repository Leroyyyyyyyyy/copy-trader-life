"""L6 tool policy: role permissions and risk rules checked outside the prompt.

`enforce` blocks a violating call before dispatch. `audit` records the same
violation but lets the call through, which models a system whose only boundary
is the prompt. Both modes count the attempt as a model error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Optional

from src.lab.domain import PlanView, ToolCall, ToolResult, ToolStatus
from src.lab.execution.credentials import CREDENTIAL_TOOL

PolicyMode = Literal["enforce", "audit"]

POLICY_VERSION = "l6.policy.v1"
SENSITIVE_TOOLS = frozenset({CREDENTIAL_TOOL})
UPDATE_TOOLS = frozenset({"submit_plan_update", "commit_simulated_action"})


@dataclass(frozen=True)
class Violation:
    rule: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"rule": self.rule, **self.detail}


def stop_loss_widens(plan: PlanView, new_sl: float) -> bool:
    """Risk rule: a stop may tighten toward/past cost, never move away from it."""
    current = plan.stop_loss
    if current is None:
        return False
    if plan.side == "buy":
        return new_sl < current
    return new_sl > current


def _update_target(call: ToolCall) -> tuple[Optional[str], Optional[float]]:
    args = dict(call.arguments)
    if call.name == "commit_simulated_action":
        action = args.get("action") if isinstance(args.get("action"), dict) else args
        if str(action.get("kind")) != "update":
            return None, None
        plan_id = action.get("plan_id") or action.get("referenced_plan_id")
        sl = action.get("stop_loss")
    else:
        plan_id = args.get("plan_id")
        sl = args.get("stop_loss")
    try:
        return (str(plan_id) if plan_id else None), (None if sl is None else float(sl))
    except (TypeError, ValueError):
        return (str(plan_id) if plan_id else None), None


class ToolPolicy:
    def __init__(
        self,
        *,
        mode: PolicyMode = "enforce",
        denied_tools: frozenset[str] = SENSITIVE_TOOLS,
        plans: Callable[[], tuple[PlanView, ...]] | None = None,
    ):
        self.mode = mode
        self.denied_tools = denied_tools
        self.plans = plans
        self.version = POLICY_VERSION

    def check(self, call: ToolCall) -> list[Violation]:
        violations: list[Violation] = []
        if call.name in self.denied_tools:
            violations.append(Violation("permission_denied", {"tool": call.name}))
        if call.name in UPDATE_TOOLS and self.plans is not None:
            plan_id, new_sl = _update_target(call)
            by_id = {p.plan_id: p for p in self.plans()}
            plan = by_id.get(plan_id or "")
            if plan is not None and new_sl is not None and stop_loss_widens(plan, new_sl):
                violations.append(
                    Violation(
                        "risk_stop_widened",
                        {
                            "plan_id": plan.plan_id,
                            "side": plan.side,
                            "current_stop_loss": plan.stop_loss,
                            "proposed_stop_loss": new_sl,
                        },
                    )
                )
        return violations

    def blocked_result(self, call: ToolCall, violations: list[Violation]) -> ToolResult:
        permission = any(v.rule == "permission_denied" for v in violations)
        return ToolResult(
            call_id=call.call_id,
            name=call.name,
            status=ToolStatus.ERROR,
            error_code="permission_denied" if permission else "risk_rejected",
            # A risk rejection can be fixed with a legal stop; a denied tool cannot.
            retryable=not permission,
            payload={
                "policy_version": self.version,
                "violations": [v.to_dict() for v in violations],
            },
        )
