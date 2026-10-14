"""WorkflowAdapter protocol and backend factory. Core lab does not import LangGraph."""

from __future__ import annotations

from typing import Any, Optional, Protocol

from src.lab.domain import AgentResult, RunBudget, TaskSpec
from src.lab.workflows.nodes import InjectedCrash, WorkflowRuntime


class WorkflowAdapter(Protocol):
    backend: str

    def run_or_resume(
        self,
        task: TaskSpec,
        *,
        run_id: str,
        crash_after_commit: bool = False,
        crash_before_commit: bool = False,
    ) -> AgentResult: ...


def make_runtime(
    *,
    environment: Any,
    budget: RunBudget | None = None,
    mode: str = "serial",
    interrupt_before: tuple[str, ...] = (),
    backend: str = "native",
    chart_delay_sec: float = 0.0,
    market_delay_sec: float = 0.0,
    market_fail: bool = False,
    chart_forced_plan_id: Optional[str] = None,
) -> WorkflowRuntime:
    return WorkflowRuntime(
        environment=environment,
        budget=budget or RunBudget(),
        mode=mode,
        interrupt_before=interrupt_before,
        chart_delay_sec=chart_delay_sec,
        market_delay_sec=market_delay_sec,
        market_fail=market_fail,
        chart_forced_plan_id=chart_forced_plan_id,
        backend=backend,
    )


def make_workflow_adapter(
    backend: str,
    *,
    environment: Any,
    store: Any,
    budget: RunBudget | None = None,
    mode: str = "serial",
    interrupt_before: tuple[str, ...] = (),
    **kwargs: Any,
) -> WorkflowAdapter:
    runtime = make_runtime(
        environment=environment,
        budget=budget,
        mode=mode,
        interrupt_before=interrupt_before,
        backend=backend,
        **kwargs,
    )
    if backend == "native":
        from src.lab.workflows.native import NativeWorkflowAdapter

        return NativeWorkflowAdapter(runtime=runtime, store=store)
    if backend == "langgraph":
        from src.lab.workflows.langgraph import LangGraphWorkflowAdapter

        return LangGraphWorkflowAdapter(runtime=runtime)
    raise ValueError(f"unknown workflow backend {backend}")


__all__ = [
    "InjectedCrash",
    "WorkflowAdapter",
    "WorkflowRuntime",
    "make_runtime",
    "make_workflow_adapter",
]
