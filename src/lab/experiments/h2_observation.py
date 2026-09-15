"""H2 2x2 observation ablation: two stub models x state_only/allowed_actions."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from src.lab.domain import (
    CompactionConfig,
    DecisionKind,
    ObservationPolicy,
    RunBudget,
    RunStatus,
    SubmittedDecision,
    TraceSummary,
)
from src.lab.environments.plan_simulation import PlanSimulationEnvironment
from src.lab.evals.loader import load_agent_view, load_for_eval
from src.lab.evals.verifiers import verify_plan_decision
from src.lab.execution.simulation import SimulationGateway
from src.lab.harness.loop import AgentHarness
from src.lab.models.policy_stub import h2_model
from src.lab.runtime.store import RunStore

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MANIFEST = (
    REPO_ROOT / "experiments" / "manifests" / "h2_observation_ablation_v1.json"
)

SCHEMA_ERROR_CODES = frozenset(
    {
        "unknown_tool",
        "missing_args",
        "invalid_json",
        "wrong_tool_for_update",
        "stub_exhausted",
        "empty_model_turn",
    }
)
CELLS = (
    ("A0", "A", ObservationPolicy.STATE_ONLY),
    ("A1", "A", ObservationPolicy.ALLOWED_ACTIONS),
    ("B0", "B", ObservationPolicy.STATE_ONLY),
    ("B1", "B", ObservationPolicy.ALLOWED_ACTIONS),
)


def _error_class(error_code: str | None) -> str | None:
    if not error_code:
        return None
    if error_code in SCHEMA_ERROR_CODES:
        return "schema"
    return "environment"


def metrics_from_run(result: Any) -> dict[str, Any]:
    schema_errors = 0
    environment_rejections = 0
    mutating_ok = 0
    tool_calls = 0
    for event in result.events:
        if event.get("kind") == "model_parse_error":
            schema_errors += 1
            continue
        if event.get("kind") != "tool_result":
            continue
        payload = event.get("payload") or {}
        tool_calls += 1
        code = payload.get("error_code")
        klass = _error_class(code)
        if klass == "schema":
            schema_errors += 1
        elif klass == "environment":
            environment_rejections += 1
        if payload.get("mutated") and payload.get("status") == "ok":
            mutating_ok += 1
    invalid = schema_errors + environment_rejections
    ratio: float | None
    if tool_calls == 0:
        ratio = None
    else:
        ratio = invalid / float(tool_calls)
    return {
        "tool_calls": tool_calls,
        "schema_errors": schema_errors,
        "environment_rejections": environment_rejections,
        "invalid_call_ratio": ratio,
        "mutating_ok": mutating_ok,
        "duplicate_state_changes": mutating_ok > 1,
        "model_calls": result.usage.model_calls,
        "input_tokens": result.usage.input_tokens,
        "output_tokens": result.usage.output_tokens,
        "run_status": result.status.value,
        "timed_out": result.status == RunStatus.TIMED_OUT,
        "early_stop": result.status != RunStatus.SUCCEEDED,
    }


def run_one_case(
    *,
    task_id: str,
    model_id: str,
    policy: ObservationPolicy,
    run_id: str,
    budget: RunBudget,
) -> dict[str, Any]:
    tmp = tempfile.TemporaryDirectory()
    try:
        store = RunStore(Path(tmp.name) / "run.sqlite")
        task, obs = load_agent_view(task_id)
        env = PlanSimulationEnvironment(
            obs,
            SimulationGateway(store),
            run_id=run_id,
            observation_policy=policy,
        )
        model = h2_model(model_id)
        harness = AgentHarness(
            store=store,
            model=model,
            environment=env,
            budget=budget,
            context_builder=None,
            compaction=CompactionConfig(policy="full"),
            memory=None,
            skills=None,
            skill_mode="none",
        )
        t0 = time.perf_counter()
        result = harness.run(task, run_id=run_id)
        latency_ms = (time.perf_counter() - t0) * 1000.0
        _, _, spec = load_for_eval(task_id)
        decision = result.decision
        if decision is None:
            decision = SubmittedDecision(kind=DecisionKind.IGNORE, evidence_refs=())
        score = verify_plan_decision(
            task_id=task_id,
            run_id=result.run_id,
            eval_spec=spec,
            decision=decision,
            final_snapshot=result.final_snapshot,
            trace=TraceSummary(
                tool_calls=result.usage.tool_calls,
                claimed_done_without_submit=result.claimed_done_without_submit,
            ),
        )
        first = model.requests[0] if model.requests else {}
        return {
            "cell_run_id": run_id,
            "task_id": task_id,
            "model_id": model_id,
            "model_version": model.version,
            "model_kind": "stub",
            "observation_policy": policy.value,
            "hard_pass": score.hard_pass,
            "terminal_reason": score.terminal_reason,
            "protocol_errors": list(score.protocol_errors),
            "semantic_errors": list(score.semantic_errors),
            "matched_outcome": score.matched_outcome,
            "latency_ms": latency_ms,
            "metrics": metrics_from_run(result),
            "prompt": {
                "has_allowed_actions": "allowed_actions" in first,
                "allowed_actions": first.get("allowed_actions"),
                "tool_names": sorted(t["name"] for t in first.get("tools") or []),
                "skill_catalog": first.get("skill_catalog") or [],
                "memories": first.get("memories") or [],
                "history_policy": (first.get("history") or {}).get("policy"),
            },
            "run_status": result.status.value,
            "submit_count": result.final_snapshot.submit_count,
        }
    finally:
        tmp.cleanup()


def summarize_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(cases)
    completions = sum(1 for c in cases if c["hard_pass"])
    schema = sum(c["metrics"]["schema_errors"] for c in cases)
    env_rej = sum(c["metrics"]["environment_rejections"] for c in cases)
    tokens = [c["metrics"]["input_tokens"] for c in cases]
    calls = [c["metrics"]["tool_calls"] for c in cases]
    ratios = [
        c["metrics"]["invalid_call_ratio"]
        for c in cases
        if c["metrics"]["invalid_call_ratio"] is not None
    ]
    return {
        "n": n,
        "completion_rate": (completions / n) if n else None,
        "completions": completions,
        "schema_errors": schema,
        "environment_rejections": env_rej,
        "mean_input_tokens": (sum(tokens) / n) if n else None,
        "mean_tool_calls": (sum(calls) / n) if n else None,
        "mean_invalid_call_ratio": (sum(ratios) / len(ratios)) if ratios else None,
        "timeouts": sum(1 for c in cases if c["metrics"]["timed_out"]),
    }


def interaction_notes(cells: dict[str, dict[str, Any]]) -> dict[str, Any]:
    def rate(cell: str) -> float | None:
        return cells[cell]["summary"]["completion_rate"]

    def env(cell: str) -> int:
        return int(cells[cell]["summary"]["environment_rejections"])

    def tokens(cell: str) -> float | None:
        return cells[cell]["summary"]["mean_input_tokens"]

    a0, a1, b0, b1 = rate("A0"), rate("A1"), rate("B0"), rate("B1")
    amb = {}
    for cell in ("A0", "A1", "B0", "B1"):
        row = next(
            (c for c in cells[cell]["per_case"] if c["task_id"] == "tl_f0_ambiguous_eth"),
            None,
        )
        amb[cell] = None if row is None else bool(row["hard_pass"])
    return {
        "A_completion_delta": None if a0 is None or a1 is None else a1 - a0,
        "B_completion_delta": None if b0 is None or b1 is None else b1 - b0,
        "A_environment_rejection_delta": env("A1") - env("A0"),
        "B_environment_rejection_delta": env("B1") - env("B0"),
        "A_mean_input_token_delta": None
        if tokens("A0") is None or tokens("A1") is None
        else tokens("A1") - tokens("A0"),
        "B_mean_input_token_delta": None
        if tokens("B0") is None or tokens("B1") is None
        else tokens("B1") - tokens("B0"),
        "ambiguous_hard_pass": amb,
        "interpretation": (
            "Stub A follows the first ETH plan unless allowed_actions marks it illegal. "
            "Stub B uses filled-ETH cardinality from world state. "
            "These are protocol stubs, not LLM results."
        ),
    }


def load_manifest(path: Path | None = None) -> dict[str, Any]:
    raw = json.loads((path or DEFAULT_MANIFEST).read_text(encoding="utf-8"))
    return raw


def run_h2_matrix(manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    man = manifest or load_manifest()
    budget_raw = ((man.get("frozen") or {}).get("budget")) or {}
    budget = RunBudget(
        max_model_calls=int(budget_raw.get("max_model_calls", 8)),
        max_tool_calls=int(budget_raw.get("max_tool_calls", 16)),
        max_mutates=int(budget_raw.get("max_mutates", 1)),
    )
    tasks = list(man.get("tasks") or [])
    cells: dict[str, dict[str, Any]] = {}
    for cell_id, model_id, policy in CELLS:
        cases = []
        for task_id in tasks:
            cases.append(
                run_one_case(
                    task_id=task_id,
                    model_id=model_id,
                    policy=policy,
                    run_id=f"h2_{cell_id}_{task_id}",
                    budget=budget,
                )
            )
        cells[cell_id] = {
            "model_id": model_id,
            "observation_policy": policy.value,
            "per_case": cases,
            "summary": summarize_cases(cases),
        }
    return {
        "experiment_id": man.get("experiment_id", "h2-observation-ablation-v1"),
        "model_kind": "stub",
        "frozen": man.get("frozen") or {},
        "tasks": tasks,
        "cells": cells,
        "interaction": interaction_notes(cells),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run H2 stub 2x2 observation ablation")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args(argv)
    report = run_h2_matrix(load_manifest(args.manifest))
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
