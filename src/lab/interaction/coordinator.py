"""Run an agent task with a review policy; hand held proposals to ReviewService."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from src.lab.domain import AgentResult, RunBudget, RunStatus  # noqa: F401
from src.lab.environments.plan_simulation import PlanSimulationEnvironment
from src.lab.evals.loader import load_agent_view
from src.lab.execution.credentials import SimulatedCredential
from src.lab.execution.simulation import SimulationGateway
from src.lab.harness.loop import AgentHarness
from src.lab.interaction.review import ReviewService
from src.lab.runtime.store import RunStore
from src.lab.tools.policy import ToolPolicy


@dataclass
class TaskRun:
    run_id: str
    account_id: str
    result: AgentResult
    card: Optional[dict[str, Any]]
    initial_plans: tuple[Any, ...]
    environment: PlanSimulationEnvironment

    @property
    def status(self) -> str:
        return RunStatus.WAITING_REVIEW.value if self.card else self.result.status.value


def run_task(
    *,
    store: RunStore,
    task_id: str,
    model: Any,
    run_id: str,
    review_policy: str = "on_ambiguity",
    account_id: Optional[str] = None,
    budget: RunBudget | None = None,
    service: ReviewService | None = None,
    credential: bool = True,
    model_timeout_sec: float | None = None,
) -> TaskRun:
    account_id = account_id or f"acct_{run_id}"
    task, obs = load_agent_view(task_id)
    env = PlanSimulationEnvironment(
        obs,
        SimulationGateway(store, account_id=account_id),
        run_id=run_id,
        credential=SimulatedCredential() if credential else None,
        review_policy=review_policy,
    )
    harness = AgentHarness(
        store=store,
        model=model,
        environment=env,
        budget=budget or RunBudget(max_model_calls=6, max_tool_calls=8),
        policy=ToolPolicy(mode="enforce", plans=env.plan_views),
        model_timeout_sec=model_timeout_sec,
    )
    result = harness.run(task, run_id=run_id)
    card = None
    if env.pending_proposal is not None and store.get_fence(run_id) is None:
        # A queued proposal is the agent's output even if the run later ran out of
        # budget. Cancelled/timed-out-by-fence runs are not revived for review.
        # The worker is done here; the run now lives only as a persisted card.
        card = (service or ReviewService(store)).open_card(
            run_id=run_id,
            task_id=task_id,
            account_id=account_id,
            proposal=env.pending_proposal,
            bundle=obs,
            review_policy=review_policy,
        )
    return TaskRun(
        run_id=run_id,
        account_id=account_id,
        result=result,
        card=card,
        initial_plans=obs.plans,
        environment=env,
    )
