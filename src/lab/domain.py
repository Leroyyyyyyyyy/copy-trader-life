"""F0 domain contracts: tasks, labels, decisions, and scores.

Agent-visible objects never embed gold labels. EvalSpec is for EvalRunner only.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional


class EnvironmentKind(str, Enum):
    MESSAGE_ANALYSIS = "message_analysis"
    PLAN_SIMULATION = "plan_simulation"


class DecisionKind(str, Enum):
    UPDATE = "update"
    IGNORE = "ignore"
    REVIEW = "review"
    ANALYZE = "analyze"


class PlanStatus(str, Enum):
    FILLED = "filled"
    CLOSED = "closed"
    PENDING = "pending"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    family_id: str
    split: str
    environment_kind: EnvironmentKind
    objective: str
    observation_bundle_id: str
    visible_at: str
    initial_snapshot_id: str

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["environment_kind"] = self.environment_kind.value
        return d


@dataclass(frozen=True)
class MessageView:
    message_id: str
    channel: str
    visible_at: str
    text: str
    image_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanView:
    plan_id: str
    symbol: str
    status: PlanStatus
    side: str
    cost_basis: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    state_version: int = 1
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "symbol": self.symbol,
            "status": self.status.value,
            "side": self.side,
            "cost_basis": self.cost_basis,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "state_version": self.state_version,
            "note": self.note,
        }


@dataclass(frozen=True)
class ObservationBundle:
    """Everything an agent may see for one task. No gold labels."""

    bundle_id: str
    messages: tuple[MessageView, ...]
    plans: tuple[PlanView, ...]
    chart_notes: tuple[str, ...] = ()
    account_note: str = ""


@dataclass(frozen=True)
class AcceptableOutcome:
    kind: DecisionKind
    referenced_plan_id: Optional[str] = None
    stop_loss: Optional[float] = None
    require_state_change: bool = False
    allow_unchanged: bool = False
    notes: str = ""


@dataclass(frozen=True)
class EvalSpec:
    """Hidden from the agent. Loaded only by EvalRunner."""

    task_id: str
    verifier_version: str
    acceptable_outcomes: tuple[AcceptableOutcome, ...]
    evidence_constraints: dict[str, Any] = field(default_factory=dict)
    initial_plans: tuple[PlanView, ...] = ()
    family_id: str = ""
    material_version: str = "f0.1"


@dataclass(frozen=True)
class SubmittedDecision:
    kind: DecisionKind
    referenced_plan_id: Optional[str] = None
    proposed_changes: dict[str, Any] = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    claimed_success: bool = False
    expected_state_version: Optional[int] = None


@dataclass(frozen=True)
class FinalEnvironmentSnapshot:
    plans: tuple[PlanView, ...]
    submit_count: int = 0
    state_version: int = 1


@dataclass(frozen=True)
class TraceSummary:
    """Minimal F0 trace. Full event log arrives with harness (F1)."""

    events: tuple[dict[str, Any], ...] = ()
    tool_calls: int = 0
    claimed_done_without_submit: bool = False


@dataclass
class CaseScore:
    task_id: str
    run_id: str
    hard_pass: bool
    protocol_errors: list[str]
    semantic_errors: list[str]
    evidence_score: Optional[float]
    usage: dict[str, Any]
    latency_ms: Optional[float]
    terminal_reason: str
    verifier_version: str
    matched_outcome: Optional[str] = None

    @property
    def completion(self) -> bool:
        return self.hard_pass

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def plan_map(plans: tuple[PlanView, ...] | list[PlanView]) -> dict[str, PlanView]:
    return {p.plan_id: p for p in plans}
