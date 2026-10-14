"""Shared L5 workflow nodes. Native and LangGraph adapters call these functions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from src.lab.agents.merger import merge_evidence
from src.lab.agents.roles import (
    classify,
    read_chart,
    read_market,
    resolve_context,
    review_risk,
)
from src.lab.domain import (
    AgentResult,
    BudgetState,
    DecisionKind,
    MergedEvidence,
    RoleOutput,
    RunBudget,
    RunStatus,
    SubmittedDecision,
    TaskSpec,
    ToolResult,
    Usage,
    decision_from_dict,
    decision_to_dict,
    merged_from_dict,
    role_output_from_dict,
)
from src.lab.runtime.budget import BudgetTracker

FINISH_NODE = "finish"
START_NODE = "classify"
SUCCESSORS = {
    "classify": "resolve",
    "resolve": "fan_out",
    "fan_out": "risk",
    "risk": "merge",
    "merge": "commit",
    "commit": FINISH_NODE,
}
NODE_ORDER = tuple(SUCCESSORS)


class InjectedCrash(Exception):
    """Test hook: side effect may have happened, workflow checkpoint has not."""

    def __init__(self, where: str):
        self.where = where
        super().__init__(f"injected_crash:{where}")


@dataclass
class WorkflowRuntime:
    environment: Any
    budget: RunBudget = field(default_factory=RunBudget)
    mode: str = "serial"
    interrupt_before: tuple[str, ...] = ()
    chart_delay_sec: float = 0.0
    market_delay_sec: float = 0.0
    market_fail: bool = False
    chart_forced_plan_id: Optional[str] = None
    crash_after_commit: bool = False
    crash_before_commit: bool = False
    backend: str = "native"


def append_event(state: dict[str, Any], kind: str, payload: dict[str, Any]) -> None:
    seq = int(state.get("event_seq") or 0) + 1
    events = list(state.get("events") or [])
    events.append({"seq": seq, "kind": kind, "payload": payload})
    state["event_seq"] = seq
    state["events"] = events


def initial_state(*, run_id: str, task_id: str, runtime: WorkflowRuntime) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "task_id": task_id,
        "mode": runtime.mode,
        "backend": runtime.backend,
        "classified": None,
        "resolver": None,
        "chart": None,
        "market": None,
        "risk": None,
        "merged": None,
        "decision": None,
        "events": [],
        "event_seq": 0,
        "budget": BudgetState().__dict__.copy(),
    }


def _tracker(runtime: WorkflowRuntime, state: dict[str, Any]) -> BudgetTracker:
    tracker = BudgetTracker(runtime.budget)
    tracker.restore(state.get("budget"))
    return tracker


def fan_out_reads(
    *,
    environment: Any,
    budget: BudgetTracker,
    mode: str,
    symbol: str,
    chart_delay_sec: float = 0.0,
    market_delay_sec: float = 0.0,
    market_fail: bool = False,
    chart_forced_plan_id: Optional[str] = None,
) -> tuple[RoleOutput, RoleOutput]:
    """Read-only chart/market fan-out. Reserves tool budget before either branch."""

    def chart_job() -> RoleOutput:
        return read_chart(
            environment.bundle,
            symbol=symbol,
            delay_sec=chart_delay_sec,
            forced_plan_id=chart_forced_plan_id,
        )

    def market_job() -> RoleOutput:
        return read_market(environment, delay_sec=market_delay_sec, fail=market_fail)

    budget.reserve_tools(2)
    emit_before = environment.gateway.submit_count
    if mode == "concurrent":
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_c = pool.submit(chart_job)
            fut_m = pool.submit(market_job)
            chart = fut_c.result()
            market = fut_m.result()
    else:
        chart = chart_job()
        market = market_job()
    if environment.gateway.submit_count != emit_before:
        raise RuntimeError("fan_out_submitted")
    return chart, market


def commit_merged(
    environment: Any,
    budget: BudgetTracker,
    merged: MergedEvidence,
    *,
    crash_before_commit: bool = False,
    crash_after_commit: bool = False,
) -> SubmittedDecision | None:
    if crash_before_commit:
        raise InjectedCrash("before_commit")
    budget.record_tool(mutated=True)
    args: dict[str, Any] = {
        "kind": merged.kind.value,
        "evidence_refs": list(merged.evidence_refs),
        "expected_state_version": environment.gateway.state_version,
    }
    if merged.kind == DecisionKind.UPDATE:
        result = environment.handle_tool(
            "submit_plan_update",
            {
                "plan_id": merged.referenced_plan_id,
                "stop_loss": merged.proposed_changes["stop_loss"],
                "evidence_refs": args["evidence_refs"],
                "expected_state_version": args["expected_state_version"],
            },
        )
    else:
        result = environment.handle_tool("submit_decision", args)
    if crash_after_commit:
        raise InjectedCrash("after_commit_before_checkpoint")
    if isinstance(result, ToolResult) and result.status.value == "error":
        return SubmittedDecision(
            kind=merged.kind,
            referenced_plan_id=merged.referenced_plan_id,
            proposed_changes=dict(merged.proposed_changes),
            evidence_refs=merged.evidence_refs,
            expected_state_version=args["expected_state_version"],
        )
    return SubmittedDecision(
        kind=merged.kind,
        referenced_plan_id=merged.referenced_plan_id,
        proposed_changes=dict(merged.proposed_changes),
        evidence_refs=merged.evidence_refs,
        expected_state_version=args["expected_state_version"],
    )


def execute_node(name: str, runtime: WorkflowRuntime, state: dict[str, Any]) -> None:
    bundle = runtime.environment.bundle
    budget = _tracker(runtime, state)
    if name == "classify":
        out = classify(bundle)
        state["classified"] = out.to_dict()
        append_event(state, "role_finished", out.to_dict())
    elif name == "resolve":
        out = resolve_context(bundle)
        state["resolver"] = out.to_dict()
        append_event(state, "role_finished", out.to_dict())
    elif name == "fan_out":
        resolver = role_output_from_dict(state["resolver"])
        symbol = (resolver.fields.get("symbol") or {}).get("value")
        if runtime.mode != "single" and symbol:
            chart, market = fan_out_reads(
                environment=runtime.environment,
                budget=budget,
                mode=runtime.mode,
                symbol=str(symbol),
                chart_delay_sec=runtime.chart_delay_sec,
                market_delay_sec=runtime.market_delay_sec,
                market_fail=runtime.market_fail,
                chart_forced_plan_id=runtime.chart_forced_plan_id,
            )
            state["chart"] = chart.to_dict()
            state["market"] = market.to_dict()
            append_event(state, "role_finished", chart.to_dict())
            append_event(state, "role_finished", market.to_dict())
        else:
            state["chart"] = None
            state["market"] = None
            if runtime.mode != "single":
                append_event(state, "fan_out_skipped", {"reason": "unresolved_symbol"})
    elif name == "risk":
        out = review_risk(bundle, role_output_from_dict(state["resolver"]))
        state["risk"] = out.to_dict()
        append_event(state, "role_finished", out.to_dict())
    elif name == "merge":
        outputs = {
            "classifier": role_output_from_dict(state["classified"]),
            "context_resolver": role_output_from_dict(state["resolver"]),
            "risk_reviewer": role_output_from_dict(state["risk"]),
        }
        if state.get("chart"):
            outputs["chart_agent"] = role_output_from_dict(state["chart"])
        if state.get("market"):
            outputs["market_context"] = role_output_from_dict(state["market"])
        merged = merge_evidence(outputs)
        state["merged"] = merged.to_dict()
        append_event(state, "evidence_merged", merged.to_dict())
    elif name == "commit":
        merged = merged_from_dict(state["merged"])
        decision = commit_merged(
            runtime.environment,
            budget,
            merged,
            crash_before_commit=runtime.crash_before_commit,
            crash_after_commit=runtime.crash_after_commit,
        )
        state["decision"] = decision_to_dict(decision) if decision else None
        append_event(
            state,
            "committed",
            {"kind": decision.kind.value if decision else None},
        )
    else:
        raise ValueError(f"unknown workflow node {name}")
    state["budget"] = budget.snapshot()


def result_from_state(
    *,
    task: TaskSpec,
    state: dict[str, Any],
    environment: Any,
    status: RunStatus,
    terminal_reason: str,
    elapsed_ms: float,
) -> AgentResult:
    budget = state.get("budget") or {}
    append_event(
        state,
        "run_finished",
        {"status": status.value, "latency_ms": elapsed_ms, "backend": state.get("backend")},
    )
    raw = state.get("decision")
    decision = decision_from_dict(raw) if raw else environment.last_decision()
    return AgentResult(
        run_id=str(state["run_id"]),
        task_id=task.task_id,
        status=status,
        terminal_reason=terminal_reason,
        decision=decision,
        final_snapshot=environment.final_snapshot(),
        usage=Usage(
            model_calls=int(budget.get("model_calls", 0)),
            tool_calls=int(budget.get("tool_calls", 0)),
            program_tool_calls=int(budget.get("program_tool_calls", 0)),
        ),
        events=list(state.get("events") or []),
    )
