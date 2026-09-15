"""Rebuild current model input. Never rewrite raw RunStore events."""

from __future__ import annotations

import json
from typing import Any, Optional

from src.lab.context.compactor import ExtractiveSummarizer, Summarizer
from src.lab.domain import CompactionConfig, CompactionRecord, TaskSpec


class ContextBudgetError(Exception):
    def __init__(self, message: str = "uncompactable_prefix_exceeds_budget"):
        super().__init__(message)


def estimate_tokens(payload: Any, chars_per_token: int = 4) -> int:
    blob = json.dumps(payload, ensure_ascii=False, default=str)
    n = max(chars_per_token, int(chars_per_token))
    return (len(blob) + n - 1) // n


def group_history(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group each model tool turn with all of its tool_result events. Never split a pair."""
    groups: list[dict[str, Any]] = []
    pending: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal pending
        if pending is not None:
            groups.append(pending)
            pending = None

    for event in events:
        kind = event.get("kind")
        seq = event.get("seq")
        payload = event.get("payload") or {}
        if kind == "model_turn":
            flush()
            raw = payload.get("raw") or {}
            pending = {
                "kind": "turn",
                "event_seqs": [seq],
                "model_turn": payload,
                "tool_results": [],
                "has_tool_calls": bool(raw.get("tool_calls")),
                "complete": not bool(raw.get("tool_calls")),
            }
        elif kind == "tool_result":
            if pending is None:
                groups.append(
                    {
                        "kind": "orphan_result",
                        "event_seqs": [seq],
                        "tool_results": [payload],
                        "complete": True,
                    }
                )
            else:
                pending["event_seqs"].append(seq)
                pending["tool_results"].append(payload)
                n_calls = len((pending["model_turn"].get("raw") or {}).get("tool_calls") or [])
                if len(pending["tool_results"]) >= n_calls:
                    pending["complete"] = True
        elif kind in {"model_parse_error", "empty_model_turn"}:
            flush()
            groups.append(
                {
                    "kind": kind,
                    "event_seqs": [seq],
                    "payload": payload,
                    "complete": True,
                }
            )
        # Other event kinds (run_started, compaction, …) stay only in the raw trace.
    flush()
    return groups


class ContextBuilder:
    def __init__(
        self,
        config: CompactionConfig | None = None,
        summarizer: Summarizer | None = None,
    ):
        self.config = config or CompactionConfig()
        self.summarizer = summarizer or ExtractiveSummarizer()

    def build(
        self,
        task: TaskSpec,
        *,
        world: dict[str, Any],
        tools: list[dict[str, Any]],
        state_version: int,
        events: list[dict[str, Any]],
        memories: list[dict[str, Any]] | None = None,
        skill_catalog: list[dict[str, Any]] | None = None,
        loaded_skills: list[dict[str, Any]] | None = None,
        allowed_actions: list[dict[str, Any]] | None = None,
        observation_policy: str | None = None,
    ) -> tuple[dict[str, Any], Optional[CompactionRecord]]:
        groups = group_history(events)
        pinned = {
            "task": task.to_dict(),
            "objective": task.objective,
            "world": world,
            "tools": tools,
            "state_version": state_version,
            "memories": list(memories or []),
            "skill_catalog": list(skill_catalog or []),
            "loaded_skills": list(loaded_skills or []),
        }
        if observation_policy is not None:
            pinned["observation_policy"] = observation_policy
        if allowed_actions is not None:
            pinned["allowed_actions"] = list(allowed_actions)
        record: Optional[CompactionRecord] = None
        history_for_model: dict[str, Any]

        if self.config.policy == "full":
            history_for_model = {"policy": "full", "turns": groups}
        elif self.config.policy == "recent":
            keep_n = max(1, self.config.keep_recent_pairs)
            keep = groups[-keep_n:] if groups else []
            older = groups[:-keep_n] if len(groups) > keep_n else []
            history_for_model = {
                "policy": "recent",
                "turns": keep,
                "dropped_event_seqs": [s for g in older for s in g.get("event_seqs") or []],
            }
        else:
            history_for_model, record = self._compact(pinned, groups)

        request = {**pinned, "history": history_for_model}
        request["manifest"] = {
            "policy": self.config.policy,
            "included_event_seqs": _included_seqs(history_for_model),
            "active_tokens": estimate_tokens(request, self.config.chars_per_token),
            "compaction": record.to_dict() if record else None,
        }
        return request, record

    def _compact(
        self,
        pinned: dict[str, Any],
        groups: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], Optional[CompactionRecord]]:
        keep_n = max(1, self.config.keep_recent_pairs)
        draft = {"policy": "compact", "working_memory": None, "turns": groups}
        tokens_before = estimate_tokens(
            {**pinned, "history": draft}, self.config.chars_per_token
        )
        if tokens_before <= self.config.token_threshold or len(groups) <= keep_n:
            return {"policy": "compact", "working_memory": None, "turns": groups}, None

        older = groups[:-keep_n]
        recent = groups[-keep_n:]
        uncompactable = estimate_tokens(
            {**pinned, "history": {"turns": recent}}, self.config.chars_per_token
        )
        if uncompactable > self.config.token_threshold:
            raise ContextBudgetError()
        source_ids = tuple(s for g in older for s in g.get("event_seqs") or [])
        try:
            summarized = self.summarizer.summarize(older)
            summary_text = str(summarized.get("text") or "")
            fallback = None
        except Exception as exc:  # noqa: BLE001
            summary_text = (
                "summary_failed; older turns dropped from active input, "
                f"source_event_ids={list(source_ids)}"
            )
            summarized = {"source_event_ids": list(source_ids), "plan_ids": []}
            fallback = f"declared_fallback:{exc}"

        history = {
            "policy": "compact",
            "working_memory": {
                "text": summary_text,
                "source_event_ids": list(summarized.get("source_event_ids") or source_ids),
                "plan_ids": list(summarized.get("plan_ids") or []),
            },
            "turns": recent,
        }
        tokens_after = estimate_tokens(
            {**pinned, "history": history}, self.config.chars_per_token
        )
        record = CompactionRecord(
            source_event_ids=source_ids,
            summary=summary_text,
            trigger_threshold=self.config.token_threshold,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            summary_tokens=estimate_tokens(summary_text, self.config.chars_per_token),
            template_version=getattr(self.summarizer, "version", "l2.extractive.v1"),
            fallback=fallback,
        )
        return history, record


def _included_seqs(history: dict[str, Any]) -> list[int]:
    seqs: list[int] = []
    wm = history.get("working_memory") or {}
    seqs.extend(wm.get("source_event_ids") or [])
    for group in history.get("turns") or []:
        seqs.extend(group.get("event_seqs") or [])
    return seqs
