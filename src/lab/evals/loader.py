"""Load TaskSpec + observations separately from hidden EvalSpec labels."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.lab.domain import (
    AcceptableOutcome,
    DecisionKind,
    EnvironmentKind,
    EvalSpec,
    MessageView,
    ObservationBundle,
    PlanStatus,
    PlanView,
    TaskSpec,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OBS_DIR = REPO_ROOT / "experiments" / "fixtures" / "observations"
DEFAULT_LABEL_DIR = REPO_ROOT / "experiments" / "fixtures" / "labels"
DEFAULT_TASK_DIR = REPO_ROOT / "experiments" / "fixtures" / "tasks"


class LabelLeakError(ValueError):
    """Raised when a gold label appears on an agent-visible path."""


def _read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"expected object in {path}")
    return data


def _parse_plan(raw: dict[str, Any]) -> PlanView:
    return PlanView(
        plan_id=str(raw["plan_id"]),
        symbol=str(raw["symbol"]),
        status=PlanStatus(raw["status"]),
        side=str(raw["side"]),
        cost_basis=raw.get("cost_basis"),
        stop_loss=raw.get("stop_loss"),
        take_profit=raw.get("take_profit"),
        state_version=int(raw.get("state_version", 1)),
        note=str(raw.get("note", "")),
    )


def _parse_message(raw: dict[str, Any]) -> MessageView:
    return MessageView(
        message_id=str(raw["message_id"]),
        channel=str(raw["channel"]),
        visible_at=str(raw["visible_at"]),
        text=str(raw["text"]),
        image_refs=tuple(raw.get("image_refs") or ()),
    )


def _assert_no_label_keys(payload: dict[str, Any], path: Path) -> None:
    forbidden = {
        "acceptable_outcomes",
        "gold",
        "gold_label",
        "hidden_label",
        "eval_spec",
        "expected_decision",
        "expected_stop_loss",
    }
    hits = sorted(forbidden.intersection(payload))
    if hits:
        raise LabelLeakError(f"observation/task must not contain {hits}: {path}")


def load_task(task_id: str, task_dir: Path | None = None) -> TaskSpec:
    path = (task_dir or DEFAULT_TASK_DIR) / f"{task_id}.json"
    raw = _read_json(path)
    _assert_no_label_keys(raw, path)
    return TaskSpec(
        task_id=str(raw["task_id"]),
        family_id=str(raw["family_id"]),
        split=str(raw["split"]),
        environment_kind=EnvironmentKind(raw["environment_kind"]),
        objective=str(raw["objective"]),
        observation_bundle_id=str(raw["observation_bundle_id"]),
        visible_at=str(raw["visible_at"]),
        initial_snapshot_id=str(raw["initial_snapshot_id"]),
    )


def load_observation(
    bundle_id: str, obs_dir: Path | None = None
) -> ObservationBundle:
    path = (obs_dir or DEFAULT_OBS_DIR) / f"{bundle_id}.json"
    raw = _read_json(path)
    _assert_no_label_keys(raw, path)
    return ObservationBundle(
        bundle_id=str(raw["bundle_id"]),
        messages=tuple(_parse_message(m) for m in raw.get("messages", [])),
        plans=tuple(_parse_plan(p) for p in raw.get("plans", [])),
        chart_notes=tuple(raw.get("chart_notes") or ()),
        account_note=str(raw.get("account_note", "")),
    )


def load_eval_spec(task_id: str, label_dir: Path | None = None) -> EvalSpec:
    path = (label_dir or DEFAULT_LABEL_DIR) / f"{task_id}.json"
    raw = _read_json(path)
    outcomes = []
    for item in raw["acceptable_outcomes"]:
        outcomes.append(
            AcceptableOutcome(
                kind=DecisionKind(item["kind"]),
                referenced_plan_id=item.get("referenced_plan_id"),
                stop_loss=item.get("stop_loss"),
                require_state_change=bool(item.get("require_state_change", False)),
                allow_unchanged=bool(item.get("allow_unchanged", False)),
                notes=str(item.get("notes", "")),
            )
        )
    return EvalSpec(
        task_id=str(raw["task_id"]),
        verifier_version=str(raw["verifier_version"]),
        acceptable_outcomes=tuple(outcomes),
        evidence_constraints=dict(raw.get("evidence_constraints") or {}),
        initial_plans=tuple(_parse_plan(p) for p in raw.get("initial_plans", [])),
        family_id=str(raw.get("family_id", "")),
        material_version=str(raw.get("material_version", "f0.1")),
    )


def load_agent_view(task_id: str) -> tuple[TaskSpec, ObservationBundle]:
    """Agent path: task + observation only. Never loads labels."""
    task = load_task(task_id)
    obs = load_observation(task.observation_bundle_id)
    return task, obs


def load_for_eval(task_id: str) -> tuple[TaskSpec, ObservationBundle, EvalSpec]:
    """Eval path: may read labels. Do not feed EvalSpec to the agent."""
    task, obs = load_agent_view(task_id)
    spec = load_eval_spec(task_id)
    if spec.task_id != task.task_id:
        raise ValueError(
            f"task_id mismatch: task={task.task_id} label={spec.task_id}"
        )
    return task, obs, spec
