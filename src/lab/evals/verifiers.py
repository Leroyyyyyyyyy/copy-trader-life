"""Deterministic F0 verifiers. No LLM, no venue calls."""

from __future__ import annotations

from typing import Optional

from src.lab.domain import (
    AcceptableOutcome,
    CaseScore,
    DecisionKind,
    EvalSpec,
    FinalEnvironmentSnapshot,
    PlanView,
    SubmittedDecision,
    TraceSummary,
    plan_map,
)

VERIFIER_VERSION = "f0.plan_sl.v1"


def _plans_equal(a: PlanView, b: PlanView) -> bool:
    return (
        a.plan_id == b.plan_id
        and a.symbol == b.symbol
        and a.status == b.status
        and a.side == b.side
        and a.cost_basis == b.cost_basis
        and a.stop_loss == b.stop_loss
        and a.take_profit == b.take_profit
    )


def _sl_matches(actual: Optional[float], expected: Optional[float]) -> bool:
    if expected is None:
        return True
    if actual is None:
        return False
    return abs(actual - expected) < 1e-9


def _outcome_label(outcome: AcceptableOutcome) -> str:
    parts = [outcome.kind.value]
    if outcome.referenced_plan_id:
        parts.append(outcome.referenced_plan_id)
    if outcome.stop_loss is not None:
        parts.append(f"sl={outcome.stop_loss}")
    return "|".join(parts)


def verify_plan_decision(
    *,
    task_id: str,
    run_id: str,
    eval_spec: EvalSpec,
    decision: SubmittedDecision,
    final_snapshot: FinalEnvironmentSnapshot,
    initial_plans: tuple[PlanView, ...] | None = None,
    trace: TraceSummary | None = None,
) -> CaseScore:
    """Score one offline case against hidden EvalSpec.

    Hard fails include: format-looking update on the wrong plan, verbal success
    with no state change, missing required evidence, and multi-submit when one
    commit is required.
    """
    protocol_errors: list[str] = []
    semantic_errors: list[str] = []
    trace = trace or TraceSummary()
    baseline = plan_map(initial_plans or eval_spec.initial_plans)
    final = plan_map(final_snapshot.plans)
    constraints = eval_spec.evidence_constraints
    min_evidence = int(constraints.get("min_evidence_refs", 0))
    require_submit = bool(constraints.get("require_submit", True))
    max_submits = constraints.get("max_submits", 1)

    if require_submit and final_snapshot.submit_count < 1:
        if decision.claimed_success or trace.claimed_done_without_submit:
            semantic_errors.append("claimed_success_without_submit")
        else:
            protocol_errors.append("missing_submit")

    if max_submits is not None and final_snapshot.submit_count > int(max_submits):
        protocol_errors.append(
            f"too_many_submits:{final_snapshot.submit_count}>{max_submits}"
        )

    if min_evidence and len(decision.evidence_refs) < min_evidence:
        protocol_errors.append(
            f"insufficient_evidence:{len(decision.evidence_refs)}<{min_evidence}"
        )

    # Non-target plans must not change unless an acceptable outcome says otherwise.
    for plan_id, before in baseline.items():
        after = final.get(plan_id)
        if after is None:
            semantic_errors.append(f"missing_final_plan:{plan_id}")
            continue
        target_ids = {
            o.referenced_plan_id
            for o in eval_spec.acceptable_outcomes
            if o.referenced_plan_id
        }
        if plan_id not in target_ids and not _plans_equal(before, after):
            semantic_errors.append(f"non_target_plan_changed:{plan_id}")

    matched: Optional[str] = None
    outcome_errors: list[str] = []

    for outcome in eval_spec.acceptable_outcomes:
        local: list[str] = []
        if decision.kind != outcome.kind:
            local.append(f"kind:{decision.kind.value}!={outcome.kind.value}")
        if outcome.referenced_plan_id is not None:
            if decision.referenced_plan_id != outcome.referenced_plan_id:
                local.append(
                    "referenced_plan:"
                    f"{decision.referenced_plan_id}!={outcome.referenced_plan_id}"
                )
        if outcome.kind == DecisionKind.UPDATE and outcome.referenced_plan_id:
            before = baseline.get(outcome.referenced_plan_id)
            after = final.get(outcome.referenced_plan_id)
            if before is None or after is None:
                local.append("target_plan_missing")
            else:
                if outcome.require_state_change and _plans_equal(before, after):
                    local.append("expected_state_change_missing")
                if outcome.stop_loss is not None and not _sl_matches(
                    after.stop_loss, outcome.stop_loss
                ):
                    local.append(
                        f"stop_loss:{after.stop_loss}!={outcome.stop_loss}"
                    )
                proposed_sl = decision.proposed_changes.get("stop_loss")
                if outcome.stop_loss is not None and proposed_sl is not None:
                    if not _sl_matches(float(proposed_sl), outcome.stop_loss):
                        local.append(
                            f"proposed_stop_loss:{proposed_sl}!={outcome.stop_loss}"
                        )
        if outcome.kind in {DecisionKind.REVIEW, DecisionKind.IGNORE}:
            if outcome.allow_unchanged:
                for plan_id, before in baseline.items():
                    after = final.get(plan_id)
                    if after is not None and not _plans_equal(before, after):
                        local.append(f"unexpected_change_on_{outcome.kind.value}:{plan_id}")
        if not local:
            matched = _outcome_label(outcome)
            break
        outcome_errors.extend(local)

    if matched is None:
        # Prefer a compact semantic reason over dumping every alternative.
        if decision.kind == DecisionKind.UPDATE and any(
            o.kind == DecisionKind.UPDATE for o in eval_spec.acceptable_outcomes
        ):
            wanted = next(
                o for o in eval_spec.acceptable_outcomes if o.kind == DecisionKind.UPDATE
            )
            if (
                wanted.referenced_plan_id
                and decision.referenced_plan_id != wanted.referenced_plan_id
            ):
                semantic_errors.append(
                    f"wrong_plan:{decision.referenced_plan_id}"
                    f"!={wanted.referenced_plan_id}"
                )
            elif "expected_state_change_missing" in outcome_errors:
                semantic_errors.append("format_ok_but_no_state_change")
            elif any(e.startswith("stop_loss:") for e in outcome_errors):
                semantic_errors.append("wrong_stop_loss")
            else:
                semantic_errors.append("no_acceptable_outcome_matched")
        else:
            semantic_errors.append("no_acceptable_outcome_matched")

    # Verbal success with unchanged target is always a hard fail for update tasks.
    update_outcomes = [
        o for o in eval_spec.acceptable_outcomes if o.kind == DecisionKind.UPDATE
    ]
    if decision.claimed_success and update_outcomes:
        target_id = update_outcomes[0].referenced_plan_id
        if target_id and target_id in baseline and target_id in final:
            if _plans_equal(baseline[target_id], final[target_id]):
                if "claimed_success_without_submit" not in semantic_errors:
                    semantic_errors.append("claimed_success_without_state_change")

    hard_pass = not protocol_errors and not semantic_errors and matched is not None
    if hard_pass:
        terminal_reason = "passed"
    elif protocol_errors:
        terminal_reason = "protocol_fail"
    else:
        terminal_reason = "semantic_fail"

    evidence_score = None
    if min_evidence:
        evidence_score = min(1.0, len(decision.evidence_refs) / float(min_evidence))

    return CaseScore(
        task_id=task_id,
        run_id=run_id,
        hard_pass=hard_pass,
        protocol_errors=protocol_errors,
        semantic_errors=semantic_errors,
        evidence_score=evidence_score,
        usage={"tool_calls": trace.tool_calls, "submits": final_snapshot.submit_count},
        latency_ms=None,
        terminal_reason=terminal_reason,
        verifier_version=eval_spec.verifier_version or VERIFIER_VERSION,
        matched_outcome=matched,
    )
