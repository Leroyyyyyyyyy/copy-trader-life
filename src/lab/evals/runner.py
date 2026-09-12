"""Single-case offline scoring entry for F0."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from src.lab.domain import (
    DecisionKind,
    FinalEnvironmentSnapshot,
    PlanStatus,
    PlanView,
    SubmittedDecision,
    TraceSummary,
)
from src.lab.evals.loader import load_for_eval
from src.lab.evals.verifiers import verify_plan_decision


def _parse_plan(raw: dict[str, Any]) -> PlanView:
    return PlanView(
        plan_id=str(raw["plan_id"]),
        symbol=str(raw["symbol"]),
        status=PlanStatus(raw["status"]),
        side=str(raw["side"]),
        cost_basis=raw.get("cost_basis"),
        stop_loss=raw.get("stop_loss"),
        take_profit=raw.get("take_profit"),
        state_version=int(raw.get("state_version", 1)),
        note=str(raw.get("note", "")),
    )


def score_payload(payload: dict[str, Any]) -> dict[str, Any]:
    task_id = str(payload["task_id"])
    run_id = str(payload.get("run_id", "manual"))
    _, _, eval_spec = load_for_eval(task_id)

    decision_raw = payload["decision"]
    decision = SubmittedDecision(
        kind=DecisionKind(decision_raw["kind"]),
        referenced_plan_id=decision_raw.get("referenced_plan_id"),
        proposed_changes=dict(decision_raw.get("proposed_changes") or {}),
        evidence_refs=tuple(decision_raw.get("evidence_refs") or ()),
        claimed_success=bool(decision_raw.get("claimed_success", False)),
        expected_state_version=decision_raw.get("expected_state_version"),
    )
    snap_raw = payload["final_snapshot"]
    final_snapshot = FinalEnvironmentSnapshot(
        plans=tuple(_parse_plan(p) for p in snap_raw["plans"]),
        submit_count=int(snap_raw.get("submit_count", 0)),
        state_version=int(snap_raw.get("state_version", 1)),
    )
    trace_raw = payload.get("trace") or {}
    trace = TraceSummary(
        events=tuple(trace_raw.get("events") or ()),
        tool_calls=int(trace_raw.get("tool_calls", 0)),
        claimed_done_without_submit=bool(
            trace_raw.get("claimed_done_without_submit", False)
        ),
    )
    score = verify_plan_decision(
        task_id=task_id,
        run_id=run_id,
        eval_spec=eval_spec,
        decision=decision,
        final_snapshot=final_snapshot,
        trace=trace,
    )
    return score.to_dict()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score one F0 offline case JSON")
    parser.add_argument("path", type=Path, help="Path to hand-written result JSON")
    args = parser.parse_args(argv)
    payload = json.loads(args.path.read_text(encoding="utf-8"))
    result = score_payload(payload)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if result["hard_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
