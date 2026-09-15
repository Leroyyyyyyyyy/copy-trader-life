"""Pure snapshot forks. Stdlib only so the guest container can import it."""

from __future__ import annotations

import copy
from typing import Any


def simulate_action(snapshot: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
    """Branch from an immutable snapshot. Never updates the caller's shared account."""
    snap = copy.deepcopy(snapshot)
    raw = dict(action or {})
    errors: list[str] = []
    kind = str(raw.get("kind") or "")
    plans = list(snap.get("plans") or [])
    by_id = {str(p.get("plan_id")): p for p in plans}
    evidence = list(raw.get("evidence_refs") or [])

    if kind not in {"update", "ignore", "review", "analyze"}:
        errors.append(f"unsupported_decision_kind:{kind or 'missing'}")
    if not evidence:
        errors.append("missing_evidence")

    if kind == "update":
        plan_id = raw.get("plan_id") or raw.get("referenced_plan_id")
        if not plan_id:
            errors.append("missing_referenced_plan")
        else:
            plan = by_id.get(str(plan_id))
            if plan is None:
                errors.append("unknown_plan")
            elif plan.get("status") != "filled":
                errors.append(f"plan_not_filled:{plan.get('status')}")
        if raw.get("stop_loss") is None:
            errors.append("missing_stop_loss")
        elif not errors:
            plan = by_id[str(plan_id)]
            plan["stop_loss"] = float(raw["stop_loss"])
            snap["state_version"] = int(snap.get("state_version") or 1) + 1
            plan["state_version"] = snap["state_version"]
            snap["plans"] = list(by_id.values())

    legal = not errors
    return {
        "simulated_state": snap if legal else copy.deepcopy(snapshot),
        "legal": legal,
        "constraint_results": errors,
        "action": raw,
        "shared_mutated": False,
    }
