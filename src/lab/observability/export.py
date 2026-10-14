"""Export a run's persisted (already redacted) trace as JSONL."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.lab.runtime.store import RunStore


def export_jsonl(events: list[dict[str, Any]], path: Path | str) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event, ensure_ascii=False, default=str))
            f.write("\n")
    return out


def export_run(store: RunStore, run_id: str, path: Path | str) -> Path:
    return export_jsonl(store.list_events(run_id), path)
