"""Shared run budgets."""

from __future__ import annotations

from src.lab.domain import BudgetState, RunBudget


class BudgetExceeded(Exception):
    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(f"budget_exceeded:{kind}")


class BudgetTracker:
    def __init__(self, budget: RunBudget | None = None):
        self.budget = budget or RunBudget()
        self.state = BudgetState()

    def check_model(self) -> None:
        if self.state.model_calls >= self.budget.max_model_calls:
            raise BudgetExceeded("model_calls")

    def check_tool(self) -> None:
        if self.state.tool_calls >= self.budget.max_tool_calls:
            raise BudgetExceeded("tool_calls")

    def check_mutate(self) -> None:
        if self.state.mutates >= self.budget.max_mutates:
            raise BudgetExceeded("mutates")

    def record_model(self) -> None:
        self.check_model()
        self.state.model_calls += 1

    def record_tool(self, *, mutated: bool, expanded_calls: int = 0) -> None:
        extra = max(0, int(expanded_calls))
        if self.state.tool_calls + 1 + extra > self.budget.max_tool_calls:
            raise BudgetExceeded("tool_calls")
        if extra and self.state.program_tool_calls + extra > self.budget.max_program_tool_calls:
            raise BudgetExceeded("program_tool_calls")
        if mutated and self.state.mutates >= self.budget.max_mutates:
            raise BudgetExceeded("mutates")
        self.state.tool_calls += 1 + extra
        self.state.program_tool_calls += extra
        if mutated:
            self.state.mutates += 1

    def restore(self, snapshot: dict[str, int] | BudgetState | None) -> None:
        if snapshot is None:
            return
        if isinstance(snapshot, BudgetState):
            self.state = BudgetState(
                model_calls=snapshot.model_calls,
                tool_calls=snapshot.tool_calls,
                mutates=snapshot.mutates,
                program_tool_calls=snapshot.program_tool_calls,
            )
            return
        self.state = BudgetState(
            model_calls=int(snapshot.get("model_calls", 0)),
            tool_calls=int(snapshot.get("tool_calls", 0)),
            mutates=int(snapshot.get("mutates", 0)),
            program_tool_calls=int(snapshot.get("program_tool_calls", 0)),
        )

    def snapshot(self) -> dict[str, int]:
        return {
            "model_calls": self.state.model_calls,
            "tool_calls": self.state.tool_calls,
            "mutates": self.state.mutates,
            "program_tool_calls": self.state.program_tool_calls,
        }

    def reserve_tools(self, n: int) -> None:
        """Hold tool budget before fan-out so concurrent roles cannot overspend."""
        hold = max(0, int(n))
        if hold == 0:
            return
        if self.state.tool_calls + hold > self.budget.max_tool_calls:
            raise BudgetExceeded("tool_calls")
        self.state.tool_calls += hold
