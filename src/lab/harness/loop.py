"""AgentHarness: one loop for both teaching environments."""

from __future__ import annotations

import uuid
from typing import Any, Optional

from src.lab.context.builder import ContextBudgetError, ContextBuilder, estimate_tokens
from src.lab.domain import (
    AgentResult,
    CompactionConfig,
    CompactionRecord,
    DecisionKind,
    RunBudget,
    RunStatus,
    SubmittedDecision,
    TaskSpec,
    ToolStatus,
    Usage,
)
from src.lab.execution.simulation import SimulationGateway
from src.lab.models.base import ModelAdapter
from src.lab.runtime.budget import BudgetExceeded, BudgetTracker
from src.lab.runtime.store import RunStore
from src.lab.tools.dispatcher import ToolDispatcher


class AgentHarness:
    def __init__(
        self,
        *,
        store: RunStore,
        model: ModelAdapter,
        environment: Any,
        budget: RunBudget | None = None,
        context_builder: ContextBuilder | None = None,
        compaction: CompactionConfig | None = None,
    ):
        self.store = store
        self.model = model
        self.environment = environment
        self.budget = BudgetTracker(budget or RunBudget())
        self.dispatcher = ToolDispatcher(environment.tool_registry(), environment)
        self.context = context_builder or ContextBuilder(compaction or CompactionConfig())

    def run(self, task: TaskSpec, *, run_id: Optional[str] = None) -> AgentResult:
        run_id = run_id or f"run_{uuid.uuid4().hex[:12]}"
        events: list[dict[str, Any]] = []
        observations: list[dict[str, Any]] = []
        usage = Usage()
        claimed_done_without_submit = False
        compaction_records: list[CompactionRecord] = []
        seq = 0

        def emit(kind: str, payload: dict[str, Any]) -> None:
            nonlocal seq
            seq += 1
            event = {"seq": seq, "kind": kind, "payload": payload}
            events.append(event)
            self.store.append_event(run_id, seq, kind, payload)

        self.store.upsert_run(
            run_id=run_id,
            task_id=task.task_id,
            status=RunStatus.RUNNING.value,
            terminal_reason=None,
            payload={"task_id": task.task_id},
        )
        emit("run_started", {"task_id": task.task_id})

        terminal_reason = "succeeded"
        status = RunStatus.SUCCEEDED

        try:
            while True:
                try:
                    self.budget.record_model()
                except BudgetExceeded as exc:
                    terminal_reason = str(exc)
                    status = RunStatus.TIMED_OUT
                    emit("budget_exceeded", {"kind": exc.kind})
                    break

                env_prompt = self.environment.build_prompt(task, observations)
                try:
                    request, crecord = self.context.build(
                        task,
                        world=env_prompt.get("world") or {},
                        tools=env_prompt.get("tools") or [],
                        state_version=int(env_prompt.get("state_version") or 0),
                        events=events,
                    )
                except ContextBudgetError as exc:
                    terminal_reason = f"context_budget_error:{exc}"
                    status = RunStatus.FAILED
                    emit("context_budget_error", {"error": str(exc)})
                    break
                if crecord is not None:
                    compaction_records.append(crecord)
                    usage = Usage(
                        model_calls=usage.model_calls,
                        tool_calls=usage.tool_calls,
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        compaction_calls=usage.compaction_calls + 1,
                        summary_tokens=usage.summary_tokens + crecord.summary_tokens,
                    )
                    emit("compaction", crecord.to_dict())
                active_tokens = estimate_tokens(
                    request, self.context.config.chars_per_token
                )
                turn, turn_usage = self.model.generate(request)
                usage = Usage(
                    model_calls=usage.model_calls + turn_usage.model_calls,
                    tool_calls=usage.tool_calls,
                    input_tokens=usage.input_tokens
                    + (turn_usage.input_tokens or active_tokens),
                    output_tokens=usage.output_tokens + turn_usage.output_tokens,
                    compaction_calls=usage.compaction_calls,
                    summary_tokens=usage.summary_tokens,
                )
                emit(
                    "model_turn",
                    {
                        "raw": turn.raw,
                        "parse_error": turn.parse_error,
                        "active_tokens": active_tokens,
                        "manifest": request.get("manifest"),
                    },
                )

                if turn.parse_error:
                    obs = {
                        "error_code": turn.parse_error,
                        "retryable": True,
                        "message": "model output could not be parsed",
                    }
                    observations.append(obs)
                    emit("model_parse_error", obs)
                    continue

                if turn.tool_calls:
                    mutate_used = False
                    for call in turn.tool_calls:
                        try:
                            # Pre-check tool budget; mutate checked after we know mutating.
                            self.budget.check_tool()
                        except BudgetExceeded as exc:
                            terminal_reason = str(exc)
                            status = RunStatus.TIMED_OUT
                            emit("budget_exceeded", {"kind": exc.kind})
                            raise StopIteration from exc

                        mutate_allowed = not mutate_used
                        result = self.dispatcher.execute(call, mutate_allowed=mutate_allowed)
                        mutated = bool(result.mutated and result.status == ToolStatus.OK)
                        try:
                            self.budget.record_tool(mutated=mutated)
                        except BudgetExceeded as exc:
                            # Tool already executed; still record observation then stop.
                            observations.append(result.to_observation())
                            emit("tool_result", result.to_observation())
                            usage = Usage(
                                model_calls=usage.model_calls,
                                tool_calls=usage.tool_calls + 1,
                                input_tokens=usage.input_tokens,
                                output_tokens=usage.output_tokens,
                                compaction_calls=usage.compaction_calls,
                                summary_tokens=usage.summary_tokens,
                            )
                            terminal_reason = str(exc)
                            status = RunStatus.TIMED_OUT
                            emit("budget_exceeded", {"kind": exc.kind})
                            raise StopIteration from exc

                        if mutated:
                            mutate_used = True
                        usage = Usage(
                            model_calls=usage.model_calls,
                            tool_calls=usage.tool_calls + 1,
                            input_tokens=usage.input_tokens,
                            output_tokens=usage.output_tokens,
                            compaction_calls=usage.compaction_calls,
                            summary_tokens=usage.summary_tokens,
                        )
                        observations.append(result.to_observation())
                        emit("tool_result", result.to_observation())
                    continue

                if turn.final is not None:
                    final = turn.final
                    if final.get("claimed_success") and self.environment.last_decision() is None:
                        claimed_done_without_submit = True
                    if "kind" in final and self.environment.last_decision() is None:
                        # Allow final structured decision without tool only as a claim.
                        claimed_done_without_submit = True
                    emit("final", final)
                    terminal_reason = "final_output"
                    status = RunStatus.SUCCEEDED
                    break

                observations.append({"error_code": "empty_model_turn", "retryable": True})
                emit("empty_model_turn", {})
        except StopIteration:
            pass
        except Exception as exc:  # noqa: BLE001
            terminal_reason = f"failed:{exc}"
            status = RunStatus.FAILED
            emit("run_failed", {"error": str(exc)})

        decision = self.environment.last_decision()
        if decision is None and claimed_done_without_submit:
            # Preserve verbal claim for verifier.
            decision = SubmittedDecision(
                kind=DecisionKind.UPDATE,
                referenced_plan_id=None,
                proposed_changes={},
                evidence_refs=(),
                claimed_success=True,
            )

        result = AgentResult(
            run_id=run_id,
            task_id=task.task_id,
            status=status,
            terminal_reason=terminal_reason,
            decision=decision,
            final_snapshot=self.environment.final_snapshot(),
            usage=usage,
            events=events,
            claimed_done_without_submit=claimed_done_without_submit,
            compaction_records=compaction_records,
        )
        self.store.upsert_run(
            run_id=run_id,
            task_id=task.task_id,
            status=status.value,
            terminal_reason=terminal_reason,
            payload=result.to_dict(),
        )
        emit("run_finished", {"status": status.value, "terminal_reason": terminal_reason})
        return result


def make_gateway(store: RunStore) -> SimulationGateway:
    return SimulationGateway(store)
