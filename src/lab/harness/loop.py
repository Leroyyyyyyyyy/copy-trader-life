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
    MemoryRecord,
    MemoryType,
    RunBudget,
    RunStatus,
    SubmittedDecision,
    TaskSpec,
    ToolResult,
    ToolStatus,
    Usage,
)
from src.lab.execution.simulation import SimulationGateway
from src.lab.memory.store import LabelInMemoryError, MemoryStore, memories_for_prompt
from src.lab.models.base import ModelAdapter
from src.lab.runtime.budget import BudgetExceeded, BudgetTracker
from src.lab.runtime.store import RunStore
from src.lab.skills.registry import SkillRegistry
from src.lab.tools.dispatcher import ToolDispatcher
from src.lab.tools.registry import ToolSpec


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
        memory: MemoryStore | None = None,
        skills: SkillRegistry | None = None,
        skill_mode: str = "none",
    ):
        self.store = store
        self.model = model
        self.environment = environment
        self.budget = BudgetTracker(budget or RunBudget())
        self.context = context_builder or ContextBuilder(compaction or CompactionConfig())
        self.memory = memory
        self.skills = skills
        self.skill_mode = skill_mode
        self._loaded_skill_names: set[str] = set()
        self.dispatcher = ToolDispatcher(self._tool_registry(), environment)

    def _tool_registry(self):
        registry = self.environment.tool_registry()
        if self.skills is not None and self.skill_mode != "none":
            registry.register(
                ToolSpec(
                    name="invoke_skill",
                    description="Load one skill body by name. Does not add tools.",
                    mutating=False,
                    required_args=("name",),
                    handler=self._handle_invoke_skill,
                )
            )
        if self.memory is not None:
            registry.register(
                ToolSpec(
                    name="propose_memory",
                    description="Propose a candidate memory for this run only",
                    mutating=False,
                    required_args=("content",),
                    handler=self._handle_propose_memory,
                )
            )
        return registry

    def _handle_invoke_skill(self, arguments: dict[str, Any]) -> Any:
        assert self.skills is not None
        out = self.skills.invoke(str(arguments["name"]))
        if out.get("error_code"):
            return ToolResult(
                call_id="",
                name="invoke_skill",
                status=ToolStatus.ERROR,
                error_code=str(out["error_code"]),
                retryable=True,
                payload=out,
            )
        self._loaded_skill_names.add(str(out["name"]))
        return out

    def _handle_propose_memory(self, arguments: dict[str, Any]) -> Any:
        assert self.memory is not None
        try:
            rec = self.memory.propose(
                MemoryRecord(
                    memory_id="",
                    memory_type=MemoryType(arguments.get("memory_type", "fact")),
                    scope=str(arguments.get("scope", "*")),
                    content=str(arguments["content"]),
                    source_event_id=arguments.get("source_event_id"),
                )
            )
        except LabelInMemoryError as exc:
            return ToolResult(
                call_id="",
                name="propose_memory",
                status=ToolStatus.ERROR,
                error_code="label_in_memory",
                retryable=False,
                payload={"error": str(exc)},
            )
        return rec.to_agent_dict()

    def _skill_prompt_bits(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if self.skills is None or self.skill_mode == "none":
            return [], []
        catalog = self.skills.catalog()
        loaded: list[dict[str, Any]] = []
        names = (
            self.skills.names()
            if self.skill_mode == "preload"
            else sorted(self._loaded_skill_names)
        )
        for name in names:
            body = self.skills.invoke(name)
            if not body.get("error_code"):
                loaded.append(body)
        return catalog, loaded

    def _memory_prompt_bits(self, task: TaskSpec) -> list[dict[str, Any]]:
        if self.memory is None:
            return []
        hits = self.memory.retrieve(visible_at=task.visible_at, include_overlay=True)
        return memories_for_prompt(hits)

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
        self._loaded_skill_names = set()
        self.dispatcher = ToolDispatcher(self._tool_registry(), self.environment)
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
                catalog, loaded = self._skill_prompt_bits()
                try:
                    request, crecord = self.context.build(
                        task,
                        world=env_prompt.get("world") or {},
                        tools=self.dispatcher.registry.schemas(),
                        state_version=int(env_prompt.get("state_version") or 0),
                        events=events,
                        memories=self._memory_prompt_bits(task),
                        skill_catalog=catalog,
                        loaded_skills=loaded,
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
