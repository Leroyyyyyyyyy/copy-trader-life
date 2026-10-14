"""Native Python FSM adapter. Checkpoints live in RunStore, not the account."""

from __future__ import annotations

import time
from typing import Any

from src.lab.domain import AgentResult, RunStatus, TaskSpec, WorkflowCheckpoint
from src.lab.runtime.budget import BudgetExceeded
from src.lab.workflows.nodes import (
    FINISH_NODE,
    START_NODE,
    SUCCESSORS,
    InjectedCrash,
    WorkflowRuntime,
    append_event,
    execute_node,
    initial_state,
    result_from_state,
)


class NativeWorkflowAdapter:
    backend = "native"

    def __init__(self, *, runtime: WorkflowRuntime, store: Any):
        self.runtime = runtime
        self.store = store

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
        raw = self.store.get_checkpoint(run_id)
        if raw is None:
            state = initial_state(run_id=run_id, task_id=task.task_id, runtime=self.runtime)
            ckpt = WorkflowCheckpoint(
                run_id=run_id,
                task_id=task.task_id,
                next_node=START_NODE,
                status=RunStatus.RUNNING.value,
                state=state,
            )
            append_event(
                ckpt.state,
                "run_started",
                {"task_id": task.task_id, "mode": self.runtime.mode, "backend": self.backend},
            )
            skip_interrupt = False
        else:
            ckpt = WorkflowCheckpoint.from_dict(raw)
            skip_interrupt = ckpt.status == RunStatus.INTERRUPTED.value

        status = RunStatus.SUCCEEDED
        terminal = "succeeded"
        try:
            while ckpt.next_node != FINISH_NODE:
                node = ckpt.next_node
                if (
                    not skip_interrupt
                    and node in self.runtime.interrupt_before
                ):
                    ckpt.status = RunStatus.INTERRUPTED.value
                    append_event(ckpt.state, "interrupted", {"next_node": node})
                    self._save(ckpt)
                    elapsed_ms = (time.perf_counter() - t0) * 1000.0
                    return result_from_state(
                        task=task,
                        state=ckpt.state,
                        environment=self.runtime.environment,
                        status=RunStatus.INTERRUPTED,
                        terminal_reason=f"interrupted_before:{node}",
                        elapsed_ms=elapsed_ms,
                    )
                skip_interrupt = False
                execute_node(node, self.runtime, ckpt.state)
                ckpt.next_node = SUCCESSORS[node]
                ckpt.status = RunStatus.RUNNING.value
                self._save(ckpt)
        except InjectedCrash:
            raise
        except BudgetExceeded as exc:
            status = RunStatus.TIMED_OUT
            terminal = str(exc)
            append_event(ckpt.state, "budget_exceeded", {"kind": exc.kind})
            ckpt.status = status.value
            self._save(ckpt)
        except Exception as exc:  # noqa: BLE001
            status = RunStatus.FAILED
            terminal = f"failed:{exc}"
            append_event(ckpt.state, "run_failed", {"error": str(exc)})
            ckpt.status = status.value
            self._save(ckpt)

        if status == RunStatus.SUCCEEDED:
            ckpt.status = RunStatus.SUCCEEDED.value
            ckpt.next_node = FINISH_NODE
            self._save(ckpt)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return result_from_state(
            task=task,
            state=ckpt.state,
            environment=self.runtime.environment,
            status=status,
            terminal_reason=terminal,
            elapsed_ms=elapsed_ms,
        )

    def _save(self, ckpt: WorkflowCheckpoint) -> None:
        self.store.put_checkpoint(run_id=ckpt.run_id, payload=ckpt.to_dict())
