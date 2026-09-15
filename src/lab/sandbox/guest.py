"""In-container guest. The host agent process never execs this user code."""

from __future__ import annotations

import io
import json
import sys
import traceback
from contextlib import redirect_stdout

from snapshot_sim import simulate_action as _simulate


def main() -> int:
    job = json.loads(sys.stdin.read())
    snapshot = job["snapshot"]
    budget = job.get("budget") or {}
    max_sims = int(budget.get("max_simulations", 16))
    code = str(job.get("code") or "")
    trace: list[dict] = []
    proposed = None
    program_error = None

    def get_snapshot():
        return json.loads(json.dumps(snapshot))

    def simulate_action(action):
        if sum(1 for t in trace if t.get("name") == "simulate_action") >= max_sims:
            raise RuntimeError("simulation_budget_exceeded")
        result = _simulate(snapshot, action)
        trace.append({"name": "simulate_action", "arguments": dict(action or {}), "result": result})
        return result

    def propose_action(action):
        nonlocal proposed
        proposed = dict(action or {})
        trace.append({"name": "propose_action", "arguments": proposed, "result": {"accepted": True}})
        return {"ok": True}

    captured = io.StringIO()
    ns = {
        "get_snapshot": get_snapshot,
        "simulate_action": simulate_action,
        "propose_action": propose_action,
    }
    try:
        with redirect_stdout(captured):
            exec(compile(code, "<guest>", "exec"), ns, ns)  # noqa: S102 - guest process only
    except Exception as exc:  # noqa: BLE001
        program_error = {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc()[-2000:],
        }

    out = {
        "stdout": captured.getvalue()[-8000:],
        "program_error": program_error,
        "tool_trace": trace,
        "proposed_action": proposed,
        "expanded_calls": sum(1 for t in trace if t.get("name") == "simulate_action"),
    }
    json.dump(out, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
