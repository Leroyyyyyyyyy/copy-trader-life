"""L4 native orchestrator: serial or concurrent read-only fan-out, one commit."""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal, Optional

from src.lab.agents.merger import merge_evidence
from src.lab.agents.roles import (
    classify,
    resolve_context,
    review_risk,
)
from src.lab.domain import (
    AgentResult,
    RoleOutput,
    RunBudget,
    RunStatus,
    TaskSpec,
    Usage,
)
from src.lab.runtime.budget import BudgetExceeded, BudgetTracker
from src.lab.workflows.nodes import commit_merged, fan_out_reads

OrchestratorMode = Literal["single", "serial", "concurrent"]


class MultiAgentOrchestrator:
    def __init__(
        self,
        *,
        environment: Any,
        budget: RunBudget | None = None,
        mode: OrchestratorMode = "serial",
        chart_delay_sec: float = 0.0,
        market_delay_sec: float = 0.0,
        market_fail: bool = False,
        chart_forced_plan_id: Optional[str] = None,
    ):
        self.environment = environment
        self.budget = BudgetTracker(budget or RunBudget())
        self.mode = mode
        self.chart_delay_sec = chart_delay_sec
        self.market_delay_sec = market_delay_sec
        self.market_fail = market_fail
        self.chart_forced_plan_id = chart_forced_plan_id

    def run(self, task: TaskSpec, *, run_id: Optional[str] = None) -> AgentResult:
        run_id = run_id or f"l4_{uuid.uuid4().hex[:12]}"
        events: list[dict[str, Any]] = []
        seq = 0
        t0 = time.perf_counter()

        def emit(kind: str, payload: dict[str, Any]) -> None:
            nonlocal seq
            seq += 1
            events.append({"seq": seq, "kind": kind, "payload": payload})

        status = RunStatus.SUCCEEDED
        terminal = "succeeded"
        decision = None
        emit("run_started", {"task_id": task.task_id, "mode": self.mode})
        try:
            bundle = self.environment.bundle
            classified = classify(bundle)
            emit("role_finished", classified.to_dict())
            resolver = resolve_context(bundle)
            emit("role_finished", resolver.to_dict())

            chart: RoleOutput | None = None
            market: RoleOutput | None = None
            symbol = (resolver.fields.get("symbol") or {}).get("value")
            if self.mode != "single" and symbol:
                chart, market = self._fan_out(symbol)
                emit("role_finished", chart.to_dict())
                emit("role_finished", market.to_dict())
            elif self.mode != "single":
                emit("fan_out_skipped", {"reason": "unresolved_symbol"})

            risk = review_risk(bundle, resolver)
            emit("role_finished", risk.to_dict())
            outputs = {
                "classifier": classified,
                "context_resolver": resolver,
                "risk_reviewer": risk,
            }
            if chart:
                outputs["chart_agent"] = chart
            if market:
                outputs["market_context"] = market
            merged = merge_evidence(outputs)
            emit("evidence_merged", merged.to_dict())
            decision = self._commit(merged)
            emit("committed", {"kind": decision.kind.value if decision else None})
        except BudgetExceeded as exc:
            status = RunStatus.TIMED_OUT
            terminal = str(exc)
            emit("budget_exceeded", {"kind": exc.kind})
        except Exception as exc:  # noqa: BLE001
            status = RunStatus.FAILED
            terminal = f"failed:{exc}"
            emit("run_failed", {"error": str(exc)})

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        usage = Usage(
            model_calls=self.budget.state.model_calls,
            tool_calls=self.budget.state.tool_calls,
            program_tool_calls=self.budget.state.program_tool_calls,
        )
        emit("run_finished", {"status": status.value, "latency_ms": elapsed_ms})
        return AgentResult(
            run_id=run_id,
            task_id=task.task_id,
            status=status,
            terminal_reason=terminal,
            decision=decision,
            final_snapshot=self.environment.final_snapshot(),
            usage=usage,
            events=events,
        )

    def _fan_out(self, symbol: str) -> tuple[RoleOutput, RoleOutput]:
        return fan_out_reads(
            environment=self.environment,
            budget=self.budget,
            mode=self.mode,
            symbol=symbol,
            chart_delay_sec=self.chart_delay_sec,
            market_delay_sec=self.market_delay_sec,
            market_fail=self.market_fail,
            chart_forced_plan_id=self.chart_forced_plan_id,
        )

    def _commit(self, merged):
        return commit_merged(self.environment, self.budget, merged)
