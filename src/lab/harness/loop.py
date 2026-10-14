"""AgentHarness: one loop for both teaching environments."""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
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
from src.lab.models.base import ModelAdapter, ModelCallError
from src.lab.observability.events import Redactor
from src.lab.runtime.budget import BudgetExceeded, BudgetTracker
from src.lab.runtime.cancel import CancelToken
from src.lab.runtime.store import RunStore
from src.lab.skills.registry import SkillRegistry
from src.lab.tools.dispatcher import ToolDispatcher
from src.lab.tools.policy import ToolPolicy
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
        policy: ToolPolicy | None = None,
        cancel_token: CancelToken | None = None,
        model_timeout_sec: float | None = None,
    ):
        self.store = store
        self.policy = policy
        self.cancel_token = cancel_token
        self.model_timeout_sec = model_timeout_sec
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

    def run(
        self,
        task: TaskSpec,
        *,
        run_id: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> AgentResult:
        run_id = run_id or f"run_{uuid.uuid4().hex[:12]}"
        trace_id = trace_id or f"trace_{uuid.uuid4().hex[:16]}"
        # `events` is the model's working history (raw). `trace_events` is the
        # redacted copy that is persisted and returned for audit.
        events: list[dict[str, Any]] = []
        trace_events: list[dict[str, Any]] = []
        observations: list[dict[str, Any]] = []
        usage = Usage()
        claimed_done_without_submit = False
        compaction_records: list[CompactionRecord] = []
        seq = 0
        lock = threading.Lock()
        t0 = time.perf_counter()
        redactor = self._redactor()
        workers = _ModelWorkers()

        def emit(kind: str, payload: dict[str, Any], actor: str = "harness") -> None:
            nonlocal seq
            with lock:
                seq += 1
                t_ms = round((time.perf_counter() - t0) * 1000.0, 3)
                events.append({"seq": seq, "kind": kind, "payload": payload})
                clean, hits = redactor.redact(payload)
                if hits:
                    clean["_redactions"] = hits
                trace_events.append(
                    {
                        "seq": seq,
                        "kind": kind,
                        "payload": clean,
                        "trace_id": trace_id,
                        "run_id": run_id,
                        "t_ms": t_ms,
                        "actor": actor,
                    }
                )
                self.store.append_event(
                    run_id, seq, kind, clean, trace_id=trace_id, t_ms=t_ms, actor=actor
                )

        self.store.upsert_run(
            run_id=run_id,
            task_id=task.task_id,
            status=RunStatus.RUNNING.value,
            terminal_reason=None,
            payload={"task_id": task.task_id, "trace_id": trace_id},
        )
        self._loaded_skill_names = set()
        self.dispatcher = ToolDispatcher(self._tool_registry(), self.environment)
        emit(
            "run_started",
            {
                "task_id": task.task_id,
                "policy": None
                if self.policy is None
                else {"mode": self.policy.mode, "version": self.policy.version},
                "model": getattr(self.model, "version", type(self.model).__name__),
                "model_timeout_sec": self.model_timeout_sec,
            },
        )

        terminal_reason = "succeeded"
        status = RunStatus.SUCCEEDED
        fence_record: Optional[dict[str, Any]] = None
        model_call_n = 0

        def stop_for_cancel(where: dict[str, Any]) -> None:
            nonlocal status, terminal_reason, fence_record
            reason = self.cancel_token.reason if self.cancel_token else "cancelled"
            emit("cancel_observed", {"reason": reason, **where}, actor="operator")
            fence_record = fence_record or self._fence(f"cancelled:{reason}")
            status = RunStatus.CANCELLED
            terminal_reason = f"cancelled:{reason}"

        try:
            while True:
                if self.cancel_token is not None and self.cancel_token.is_set():
                    stop_for_cancel({"before": "model_call"})
                    break
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
                        allowed_actions=env_prompt.get("allowed_actions"),
                        observation_policy=env_prompt.get("observation_policy"),
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
                        program_tool_calls=usage.program_tool_calls,
                    )
                    emit("compaction", crecord.to_dict())
                active_tokens = estimate_tokens(
                    request, self.context.config.chars_per_token
                )
                model_call_n += 1
                emit("model_call_started", {"model_call": model_call_n}, actor="harness")
                try:
                    turn, turn_usage = self._invoke_model(request, workers)
                except _Abandoned as ab:
                    emit(
                        "model_call_abandoned",
                        {
                            "model_call": model_call_n,
                            "reason": ab.reason,
                            "still_running": not ab.future.done(),
                        },
                    )
                    fence_record = fence_record or self._fence(ab.reason)
                    workers.abandon(ab.future, self._late_callback(emit, ab.reason, model_call_n))
                    if ab.reason == "cancelled":
                        stop_for_cancel({"during": "model_call", "model_call": model_call_n})
                    else:
                        status = RunStatus.TIMED_OUT
                        terminal_reason = "model_timeout"
                    break
                except ModelCallError as exc:
                    emit(
                        "model_error",
                        {
                            "model_call": model_call_n,
                            "code": exc.code,
                            "retryable": exc.retryable,
                            "attempts": exc.attempts,
                            "detail": exc.detail,
                        },
                        actor="model_provider",
                    )
                    status = RunStatus.FAILED
                    terminal_reason = f"model_error:{exc.code}"
                    break
                usage = Usage(
                    model_calls=usage.model_calls + turn_usage.model_calls,
                    tool_calls=usage.tool_calls,
                    input_tokens=usage.input_tokens
                    + (turn_usage.input_tokens or active_tokens),
                    output_tokens=usage.output_tokens + turn_usage.output_tokens,
                    compaction_calls=usage.compaction_calls,
                    summary_tokens=usage.summary_tokens,
                    program_tool_calls=usage.program_tool_calls,
                )
                emit(
                    "model_turn",
                    {
                        "raw": turn.raw,
                        "parse_error": turn.parse_error,
                        "active_tokens": active_tokens,
                        "manifest": request.get("manifest"),
                    },
                    actor="model",
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
                        if self.cancel_token is not None and self.cancel_token.is_set():
                            # Check right before a possible side effect.
                            stop_for_cancel({"before_tool": call.name, "call_id": call.call_id})
                            raise StopIteration
                        try:
                            # Pre-check tool budget; mutate checked after we know mutating.
                            self.budget.check_tool()
                        except BudgetExceeded as exc:
                            terminal_reason = str(exc)
                            status = RunStatus.TIMED_OUT
                            emit("budget_exceeded", {"kind": exc.kind})
                            raise StopIteration from exc

                        emit(
                            "tool_call_started",
                            {"tool": call.name, "call_id": call.call_id},
                        )
                        result, actor = self._dispatch(call, not mutate_used, emit)
                        mutated = bool(result.mutated and result.status == ToolStatus.OK)
                        expanded = int(result.expanded_calls or 0)
                        try:
                            self.budget.record_tool(mutated=mutated, expanded_calls=expanded)
                        except BudgetExceeded as exc:
                            # Tool already executed; still record observation then stop.
                            observations.append(result.to_observation())
                            emit("tool_result", result.to_observation(), actor=actor)
                            usage = Usage(
                                model_calls=usage.model_calls,
                                tool_calls=usage.tool_calls + 1 + expanded,
                                input_tokens=usage.input_tokens,
                                output_tokens=usage.output_tokens,
                                compaction_calls=usage.compaction_calls,
                                summary_tokens=usage.summary_tokens,
                                program_tool_calls=usage.program_tool_calls + expanded,
                            )
                            terminal_reason = str(exc)
                            status = RunStatus.TIMED_OUT
                            emit("budget_exceeded", {"kind": exc.kind})
                            raise StopIteration from exc

                        if mutated:
                            mutate_used = True
                        usage = Usage(
                            model_calls=usage.model_calls,
                            tool_calls=usage.tool_calls + 1 + expanded,
                            input_tokens=usage.input_tokens,
                            output_tokens=usage.output_tokens,
                            compaction_calls=usage.compaction_calls,
                            summary_tokens=usage.summary_tokens,
                            program_tool_calls=usage.program_tool_calls + expanded,
                        )
                        observations.append(result.to_observation())
                        emit("tool_result", result.to_observation(), actor=actor)
                    continue

                if turn.final is not None:
                    final = turn.final
                    if final.get("claimed_success") and self.environment.last_decision() is None:
                        claimed_done_without_submit = True
                    if "kind" in final and self.environment.last_decision() is None:
                        # Allow final structured decision without tool only as a claim.
                        claimed_done_without_submit = True
                    emit("final", final, actor="model")
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

        emit("resource_cleanup", {"items": self._cleanup(workers, fence_record)})

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

        emit("run_finished", {"status": status.value, "terminal_reason": terminal_reason})
        result = AgentResult(
            run_id=run_id,
            task_id=task.task_id,
            status=status,
            terminal_reason=terminal_reason,
            decision=decision,
            final_snapshot=self.environment.final_snapshot(),
            usage=usage,
            events=trace_events,
            claimed_done_without_submit=claimed_done_without_submit,
            compaction_records=compaction_records,
            trace_id=trace_id,
        )
        self.store.upsert_run(
            run_id=run_id,
            task_id=task.task_id,
            status=status.value,
            terminal_reason=terminal_reason,
            payload=result.to_dict(),
        )
        return result

    # --- L6 helpers ----------------------------------------------------------

    def _redactor(self) -> Redactor:
        redactor = Redactor()
        values = getattr(self.model, "redaction_values", None)
        if callable(values):
            for v in values():
                redactor.add(v)
        credential = getattr(self.environment, "credential", None)
        if credential is not None:
            redactor.add(credential.api_secret)
        return redactor

    def _invoke_model(self, request: dict[str, Any], workers: "_ModelWorkers"):
        if self.cancel_token is None and self.model_timeout_sec is None:
            return self.model.generate(request)
        future = workers.submit(self.model.generate, request)
        deadline = (
            None
            if self.model_timeout_sec is None
            else time.monotonic() + self.model_timeout_sec
        )
        while True:
            try:
                return future.result(timeout=0.01)
            except FuturesTimeout:
                pass
            if self.cancel_token is not None and self.cancel_token.is_set():
                raise _Abandoned("cancelled", future)
            if deadline is not None and time.monotonic() >= deadline:
                raise _Abandoned("model_timeout", future)

    def _late_callback(self, emit, reason: str, model_call: int):
        def on_done(future: Future) -> None:
            if future.cancelled():
                payload: dict[str, Any] = {"never_started": True}
            else:
                try:
                    turn, _ = future.result()
                    payload = {
                        "had_tool_calls": bool(turn.tool_calls),
                        "tool_names": [c.name for c in turn.tool_calls],
                        "had_final": turn.final is not None,
                    }
                except Exception as exc:  # noqa: BLE001
                    payload = {"error": type(exc).__name__}
            try:
                emit(
                    "late_result_discarded",
                    {"model_call": model_call, "reason": reason, "dispatched": False, **payload},
                )
            except Exception:  # noqa: BLE001 - store may already be closed
                pass

        return on_done

    def _fence(self, reason: str) -> Optional[dict[str, Any]]:
        fence = getattr(self.environment, "fence", None)
        if callable(fence):
            return fence(reason)
        return None

    def _dispatch(self, call, mutate_allowed: bool, emit) -> tuple[ToolResult, str]:
        actor = f"tool:{call.name}"
        if self.policy is not None:
            violations = self.policy.check(call)
            if violations:
                blocked = self.policy.mode == "enforce"
                emit(
                    "policy_violation",
                    {
                        "call_id": call.call_id,
                        "tool": call.name,
                        "arguments": call.arguments,
                        "violations": [v.to_dict() for v in violations],
                        "mode": self.policy.mode,
                        "blocked": blocked,
                        "policy_version": self.policy.version,
                    },
                    actor="gate",
                )
                if blocked:
                    return self.policy.blocked_result(call, violations), "gate"
        return self.dispatcher.execute(call, mutate_allowed=mutate_allowed), actor

    def _cleanup(
        self, workers: "_ModelWorkers", fence_record: Optional[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        items = workers.shutdown()
        if fence_record is not None:
            items.append(fence_record)
        env_cleanup = getattr(self.environment, "cleanup", None)
        if callable(env_cleanup):
            items.extend(env_cleanup())
        return items


class _Abandoned(Exception):
    def __init__(self, reason: str, future: Future):
        self.reason = reason
        self.future = future
        super().__init__(reason)


class _ModelWorkers:
    """Lazily created worker for model calls that must be cancellable."""

    def __init__(self) -> None:
        self._pool: Optional[ThreadPoolExecutor] = None
        self.abandoned: list[Future] = []

    def submit(self, fn, *args):
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lab-model")
        return self._pool.submit(fn, *args)

    def abandon(self, future: Future, callback) -> None:
        self.abandoned.append(future)
        future.add_done_callback(callback)

    def shutdown(self) -> list[dict[str, Any]]:
        if self._pool is None:
            return []
        # Python threads cannot be killed: an in-flight provider call keeps
        # running until its own HTTP timeout. We stop waiting and record that.
        self._pool.shutdown(wait=False, cancel_futures=True)
        return [
            {
                "resource": "model_worker",
                "action": "shutdown(wait=False, cancel_futures=True)",
                "abandoned_calls": len(self.abandoned),
                "still_running": sum(1 for f in self.abandoned if not f.done()),
            }
        ]


def make_gateway(store: RunStore) -> SimulationGateway:
    return SimulationGateway(store)
