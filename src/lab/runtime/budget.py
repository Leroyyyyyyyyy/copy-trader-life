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

    def record_tool(self, *, mutated: bool) -> None:
        self.check_tool()
        if mutated:
            self.check_mutate()
        self.state.tool_calls += 1
        if mutated:
            self.state.mutates += 1
