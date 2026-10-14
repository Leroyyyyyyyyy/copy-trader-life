"""Score a persisted run (agent decision or human-reviewed decision) against its label."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Optional

from src.lab.domain import (
    DecisionKind,
    FinalEnvironmentSnapshot,
    SubmittedDecision,
    TraceSummary,
    decision_from_dict,
)
from src.lab.evals.loader import load_eval_spec
from src.lab.evals.verifiers import verify_plan_decision
from src.lab.execution.simulation import SimulationGateway
from src.lab.runtime.store import RunStore


def score_run(store: RunStore, run_id: str, account_id: Optional[str] = None) -> dict[str, Any]:
    run = store.get_run(run_id)
    if run is None:
        return {"error": "unknown_run", "run_id": run_id}
    card = store.get_review(f"card_{run_id}")
    account_id = account_id or (card or {}).get("account_id") or f"acct_{run_id}"
    gw = SimulationGateway(store, account_id=account_id)
    if not gw.load():
        return {"error": "unknown_account", "account_id": account_id}

    resolution = (card or {}).get("resolution")
    if resolution:
        source = f"review:{card['status']}"
        decision = decision_from_dict(resolution["decision"])
    else:
        raw = (run["payload"] or {}).get("decision")
        source = "agent"
        decision = (
            decision_from_dict(raw)
            if raw
            else SubmittedDecision(kind=DecisionKind.IGNORE)
        )
    snapshot = FinalEnvironmentSnapshot(
        plans=gw.plans(), submit_count=gw.submit_count, state_version=gw.state_version
    )
    spec = load_eval_spec(run["task_id"])
    # Changes made outside the run (another operator) are part of the baseline,
    # not something the run did to a non-target plan.
    baseline = {p.plan_id: p for p in spec.initial_plans}
    for change in gw.external_changes:
        if change["plan_id"] in baseline:
            baseline[change["plan_id"]] = replace(baseline[change["plan_id"]], stop_loss=change["stop_loss"])
    score = verify_plan_decision(
        task_id=run["task_id"],
        run_id=run_id,
        eval_spec=spec,
        initial_plans=tuple(baseline.values()),
        decision=decision,
        final_snapshot=snapshot,
        trace=TraceSummary(),
    )
    return {
        "run_id": run_id,
        "task_id": run["task_id"],
        "run_status": run["status"],
        "decision_source": source,
        "card_status": None if card is None else card["status"],
        "hard_pass": score.hard_pass,
        "matched_outcome": score.matched_outcome,
        "protocol_errors": score.protocol_errors,
        "semantic_errors": score.semantic_errors,
        "submit_count": gw.submit_count,
        "state_version": gw.state_version,
        "external_changes": gw.external_changes,
    }
