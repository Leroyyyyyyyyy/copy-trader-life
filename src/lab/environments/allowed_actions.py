"""Derive currently legal submits from visible plan state. Never reads labels."""

from __future__ import annotations

from typing import Any, Iterable

from src.lab.domain import PlanStatus, PlanView


def allowed_actions_from_plans(plans: Iterable[PlanView]) -> list[dict[str, Any]]:
    """List per-plan update legality plus always-legal ignore/review.

    Filled plans may be updated; closed/pending/cancelled may not. Multiple
    filled plans stay listed as legal — the list must not pick a gold target.
    """
    actions: list[dict[str, Any]] = []
    for plan in sorted(plans, key=lambda p: p.plan_id):
        legal = plan.status == PlanStatus.FILLED
        actions.append(
            {
                "tool": "submit_plan_update",
                "plan_id": plan.plan_id,
                "symbol": plan.symbol,
                "status": plan.status.value,
                "legal": legal,
                "reason": "filled" if legal else f"plan_not_filled:{plan.status.value}",
            }
        )
    actions.append(
        {
            "tool": "submit_decision",
            "kind": "ignore",
            "legal": True,
            "reason": "non_mutating_submit",
        }
    )
    actions.append(
        {
            "tool": "submit_decision",
            "kind": "review",
            "legal": True,
            "reason": "non_mutating_submit",
        }
    )
    return actions
