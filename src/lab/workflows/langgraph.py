"""LangGraph adapter. Same node functions as native; only the graph expression changes."""

from __future__ import annotations

import time
from typing import Any, TypedDict

from src.lab.domain import AgentResult, RunStatus, TaskSpec
from src.lab.runtime.budget import BudgetExceeded
from src.lab.workflows.nodes import (
    InjectedCrash,
    WorkflowRuntime,
    append_event,
    execute_node,
    initial_state,
    result_from_state,
)


class GraphState(TypedDict, total=False):
    run_id: str
    task_id: str
    mode: str
    backend: str
    classified: dict[str, Any] | None
    resolver: dict[str, Any] | None
    chart: dict[str, Any] | None
    market: dict[str, Any] | None
    risk: dict[str, Any] | None
    merged: dict[str, Any] | None
    decision: dict[str, Any] | None
    events: list[dict[str, Any]]
    event_seq: int
    budget: dict[str, int]


def _require_langgraph():
    try:
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.graph import END, START, StateGraph
    except ImportError as exc:  # pragma: no cover - exercised only when extra is missing
        raise RuntimeError(
            "LangGraphWorkflowAdapter requires the langgraph package"
        ) from exc
    return END, START, StateGraph, InMemorySaver


class LangGraphWorkflowAdapter:
    backend = "langgraph"

    def __init__(self, *, runtime: WorkflowRuntime, checkpointer: Any | None = None):
        self.runtime = runtime
        END, START, StateGraph, InMemorySaver = _require_langgraph()
        self._checkpointer = checkpointer or InMemorySaver()
        graph = StateGraph(GraphState)
        for name in ("classify", "resolve", "fan_out", "risk", "merge", "commit"):
            graph.add_node(name, self._node(name))
        graph.add_edge(START, "classify")
        graph.add_edge("classify", "resolve")
        graph.add_edge("resolve", "fan_out")
        graph.add_edge("fan_out", "risk")
        graph.add_edge("risk", "merge")
        graph.add_edge("merge", "commit")
        graph.add_edge("commit", END)
        interrupt_before = list(runtime.interrupt_before) or None
        self._graph = graph.compile(
            checkpointer=self._checkpointer,
            interrupt_before=interrupt_before,
        )

    def _node(self, name: str):
        def node(state: GraphState) -> GraphState:
            updated = dict(state)
            execute_node(name, self.runtime, updated)
            return updated

        node.__name__ = name
        return node

    def run_or_resume(
        self,
        task: TaskSpec,
        *,
        run_id: str,
        crash_after_commit: bool = False,
        crash_before_commit: bool = False,
    ) -> AgentResult:
        self.runtime.crash_after_commit = crash_after_commit
        self.runtime.crash_before_commit = crash_before_commit
        t0 = time.perf_counter()
        config = {"configurable": {"thread_id": run_id}}
        snap = self._graph.get_state(config)
        status = RunStatus.SUCCEEDED
        terminal = "succeeded"
        try:
            if snap.next:
                payload = self._graph.invoke(None, config)
            elif snap.values:
                payload = dict(snap.values)
            else:
                start = initial_state(
                    run_id=run_id, task_id=task.task_id, runtime=self.runtime
                )
                append_event(
                    start,
                    "run_started",
                    {
                        "task_id": task.task_id,
                        "mode": self.runtime.mode,
                        "backend": self.backend,
                    },
                )
                payload = self._graph.invoke(start, config)
        except InjectedCrash:
            raise
        except BudgetExceeded as exc:
            status = RunStatus.TIMED_OUT
            terminal = str(exc)
            payload = dict(self._graph.get_state(config).values)
            append_event(payload, "budget_exceeded", {"kind": exc.kind})
        except Exception as exc:  # noqa: BLE001
            status = RunStatus.FAILED
            terminal = f"failed:{exc}"
            payload = dict(self._graph.get_state(config).values)
            append_event(payload, "run_failed", {"error": str(exc)})

        snap = self._graph.get_state(config)
        if snap.next:
            payload = dict(snap.values)
            nxt = snap.next[0]
            append_event(payload, "interrupted", {"next_node": nxt})
            status = RunStatus.INTERRUPTED
            terminal = f"interrupted_before:{nxt}"
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return result_from_state(
            task=task,
            state=dict(payload),
            environment=self.runtime.environment,
            status=status,
            terminal_reason=terminal,
            elapsed_ms=elapsed_ms,
        )
