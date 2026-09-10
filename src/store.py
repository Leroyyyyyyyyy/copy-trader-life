from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .util import DATA_DIR, dump_json


class JsonStore:
    def __init__(self, name: str = "state.json"):
        self.path = DATA_DIR / name
        self.data: dict[str, Any] = {"plans": {}, "positions": {}, "seen": []}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            backup = self.path.with_suffix(".broken.json")
            self.path.replace(backup)

    def save(self) -> None:
        dump_json(self.path, self.data)

    def seen(self, channel: str, message_id: int) -> bool:
        key = f"{channel}:{message_id}"
        return key in self.data.setdefault("seen", [])

    def mark_seen(self, channel: str, message_id: int, keep: int = 4000) -> None:
        key = f"{channel}:{message_id}"
        seen = self.data.setdefault("seen", [])
        if key not in seen:
            seen.append(key)
        if len(seen) > keep:
            self.data["seen"] = seen[-keep:]
        self.save()

    def put_plan(self, plan_id: str, payload: dict[str, Any]) -> None:
        self.data.setdefault("plans", {})[plan_id] = payload
        self.save()

    def drop_plan(self, plan_id: str) -> None:
        self.data.setdefault("plans", {}).pop(plan_id, None)
        self.save()

    def plans(self) -> dict[str, Any]:
        return self.data.setdefault("plans", {})

    def positions(self) -> dict[str, Any]:
        return self.data.setdefault("positions", {})

    def position(self, key: str) -> dict[str, Any] | None:
        return self.positions().get(key)

    def put_position(self, key: str, payload: dict[str, Any]) -> None:
        self.positions()[key] = payload
        self.save()

    def drop_position(self, key: str) -> None:
        self.positions().pop(key, None)
        self.save()

    def set_hl_reconciliation(self, payload: dict[str, Any]) -> None:
        self.data["hyperliquid_reconciliation"] = payload
        self.save()
