"""Deterministic extractive summarizer. Does not call an LLM."""

from __future__ import annotations

from typing import Any, Callable, Protocol


class Summarizer(Protocol):
    def summarize(self, groups: list[dict[str, Any]]) -> dict[str, Any]:
        ...


def _walk(obj: Any, sink: dict[str, set[Any]]) -> None:
    if isinstance(obj, dict):
        if "plan_id" in obj:
            sink["plan_ids"].add(str(obj["plan_id"]))
        if "stop_loss" in obj and obj["stop_loss"] is not None:
            sink["stop_losses"].add(obj["stop_loss"])
        if "cost_basis" in obj and obj["cost_basis"] is not None:
            sink["cost_basis"].add(obj["cost_basis"])
        if "error_code" in obj and obj["error_code"]:
            sink["errors"].add(str(obj["error_code"]))
        if "name" in obj:
            sink["tools"].add(str(obj["name"]))
        for v in obj.values():
            _walk(v, sink)
    elif isinstance(obj, list):
        for item in obj:
            _walk(item, sink)


class ExtractiveSummarizer:
    """Keep plan IDs, prices, tools tried, and errors. Cite source event seqs."""

    version = "l2.extractive.v1"

    def summarize(self, groups: list[dict[str, Any]]) -> dict[str, Any]:
        sink: dict[str, set[Any]] = {
            "plan_ids": set(),
            "stop_losses": set(),
            "cost_basis": set(),
            "errors": set(),
            "tools": set(),
        }
        source_ids: list[int] = []
        for group in groups:
            source_ids.extend(group.get("event_seqs") or [])
            _walk(group, sink)
        lines = [
            "Working-memory summary of older turns (not a gold label).",
            f"plan_ids={sorted(sink['plan_ids'])}",
            f"stop_loss_values_seen={sorted(sink['stop_losses'], key=lambda x: str(x))}",
            f"cost_basis_values_seen={sorted(sink['cost_basis'], key=lambda x: str(x))}",
            f"tools_tried={sorted(sink['tools'])}",
            f"errors={sorted(sink['errors'])}",
            f"source_event_ids={source_ids}",
        ]
        return {
            "text": "\n".join(lines),
            "source_event_ids": source_ids,
            "plan_ids": sorted(sink["plan_ids"]),
        }


class InjectedSummarizer:
    """Test double: wrap a real summarizer or return a fixed (possibly wrong) text."""

    def __init__(
        self,
        inner: Summarizer | None = None,
        *,
        text: str | None = None,
        fail: bool = False,
    ):
        self.inner = inner or ExtractiveSummarizer()
        self.text = text
        self.fail = fail
        self.version = "l2.injected.v1"

    def summarize(self, groups: list[dict[str, Any]]) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("summary_failed")
        out = self.inner.summarize(groups)
        if self.text is not None:
            out = dict(out)
            out["text"] = self.text
        return out


SummarizerFactory = Callable[[], Summarizer]
