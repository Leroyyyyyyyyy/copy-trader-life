"""Deterministic checks before a simulated commit."""

from __future__ import annotations

from src.lab.domain import DecisionKind, PlanStatus, PlanView, SubmittedDecision, plan_map


class DecisionValidator:
    def validate(
        self,
        decision: SubmittedDecision,
        plans: tuple[PlanView, ...] | list[PlanView],
        *,
        expected_state_version: int | None,
        current_state_version: int,
    ) -> list[str]:
        errors: list[str] = []
        if (
            expected_state_version is not None
            and expected_state_version != current_state_version
        ):
            errors.append(
                f"stale_state_version:{expected_state_version}!={current_state_version}"
            )
        by_id = plan_map(plans)
        if decision.kind == DecisionKind.UPDATE:
            if not decision.referenced_plan_id:
                errors.append("missing_referenced_plan")
            else:
                plan = by_id.get(decision.referenced_plan_id)
                if plan is None:
                    errors.append("unknown_plan")
                elif plan.status != PlanStatus.FILLED:
                    errors.append(f"plan_not_filled:{plan.status.value}")
            if decision.proposed_changes and "stop_loss" not in decision.proposed_changes:
                errors.append("missing_stop_loss")
        if not decision.evidence_refs:
            errors.append("missing_evidence")
        return errors
