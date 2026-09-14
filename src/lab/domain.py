"""Lab domain contracts: tasks, labels, run/tool objects, and scores.

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


class RunStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class ToolStatus(str, Enum):
    OK = "ok"
    ERROR = "error"


class MemoryStatus(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    DELETED = "deleted"
    CANDIDATE = "candidate"


class MemoryType(str, Enum):
    FACT = "fact"
    EPISODE = "episode"
    PROCEDURE = "procedure"
    EVAL = "eval"


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


@dataclass(frozen=True)
class MemoryRecord:
    memory_id: str
    memory_type: MemoryType
    scope: str
    content: str
    source_event_id: Optional[str] = None
    confidence: float = 1.0
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    version: int = 1
    status: MemoryStatus = MemoryStatus.ACTIVE
    supersedes: Optional[str] = None
    snapshot_id: str = ""

    def to_agent_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "memory_type": self.memory_type.value,
            "scope": self.scope,
            "content": self.content,
            "source_event_id": self.source_event_id,
            "confidence": self.confidence,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "version": self.version,
            "status": self.status.value,
            "supersedes": self.supersedes,
        }


@dataclass(frozen=True)
class RunBudget:
    max_model_calls: int = 8
    max_tool_calls: int = 16
    max_mutates: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BudgetState:
    model_calls: int = 0
    tool_calls: int = 0
    mutates: int = 0

    def remaining(self, budget: RunBudget) -> dict[str, int]:
        return {
            "model_calls": budget.max_model_calls - self.model_calls,
            "tool_calls": budget.max_tool_calls - self.tool_calls,
            "mutates": budget.max_mutates - self.mutates,
        }


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    name: str
    status: ToolStatus
    payload: dict[str, Any] = field(default_factory=dict)
    error_code: Optional[str] = None
    retryable: bool = False
    evidence_ids: tuple[str, ...] = ()
    state_version: Optional[int] = None
    mutated: bool = False

    def to_observation(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "name": self.name,
            "status": self.status.value,
            "payload": self.payload,
            "error_code": self.error_code,
            "retryable": self.retryable,
            "evidence_ids": list(self.evidence_ids),
            "state_version": self.state_version,
            "mutated": self.mutated,
        }


@dataclass(frozen=True)
class ModelTurn:
    raw: dict[str, Any]
    tool_calls: tuple[ToolCall, ...] = ()
    final: Optional[dict[str, Any]] = None
    parse_error: Optional[str] = None


@dataclass(frozen=True)
class CompactionConfig:
    """When to rebuild the *current* model input. Raw RunStore events stay intact."""

    policy: str = "full"  # full | recent | compact
    token_threshold: int = 400
    keep_recent_pairs: int = 1
    chars_per_token: int = 4

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CompactionRecord:
    source_event_ids: tuple[int, ...]
    summary: str
    trigger_threshold: int
    tokens_before: int
    tokens_after: int
    summary_tokens: int
    template_version: str = "l2.extractive.v1"
    fallback: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Usage:
    model_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    compaction_calls: int = 0
    summary_tokens: int = 0


@dataclass
class RunState:
    run_id: str
    task_id: str
    status: RunStatus
    step: int = 0
    state_version: int = 1
    terminal_reason: Optional[str] = None
    decision: Optional[SubmittedDecision] = None
    events: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "status": self.status.value,
            "step": self.step,
            "state_version": self.state_version,
            "terminal_reason": self.terminal_reason,
            "decision": asdict(self.decision) if self.decision else None,
            "events": list(self.events),
        }


@dataclass(frozen=True)
class CommitReceipt:
    run_id: str
    decision_id: str
    decision_version: int
    idempotency_key: str
    applied: bool
    state_version: int
    payload_hash: str
    reused: bool = False
    error_code: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AgentResult:
    run_id: str
    task_id: str
    status: RunStatus
    terminal_reason: str
    decision: Optional[SubmittedDecision]
    final_snapshot: FinalEnvironmentSnapshot
    usage: Usage
    events: list[dict[str, Any]] = field(default_factory=list)
    claimed_done_without_submit: bool = False
    compaction_records: list[CompactionRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "status": self.status.value,
            "terminal_reason": self.terminal_reason,
            "decision": asdict(self.decision) if self.decision else None,
            "final_snapshot": {
                "plans": [p.to_dict() for p in self.final_snapshot.plans],
                "submit_count": self.final_snapshot.submit_count,
                "state_version": self.final_snapshot.state_version,
            },
            "usage": asdict(self.usage),
            "events": list(self.events),
            "claimed_done_without_submit": self.claimed_done_without_submit,
            "compaction_records": [c.to_dict() for c in self.compaction_records],
        }


def decision_to_dict(decision: SubmittedDecision) -> dict[str, Any]:
    d = asdict(decision)
    d["kind"] = decision.kind.value
    return d


def decision_from_dict(raw: dict[str, Any]) -> SubmittedDecision:
    return SubmittedDecision(
        kind=DecisionKind(raw["kind"]),
        referenced_plan_id=raw.get("referenced_plan_id"),
        proposed_changes=dict(raw.get("proposed_changes") or {}),
        evidence_refs=tuple(raw.get("evidence_refs") or ()),
        claimed_success=bool(raw.get("claimed_success", False)),
        expected_state_version=raw.get("expected_state_version"),
    )
