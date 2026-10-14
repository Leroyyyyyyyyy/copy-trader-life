"""Ingress dedupe: one external event starts at most one run.

Commit receipts are keyed by run, so two runs for the same Telegram message
would each be allowed to commit. The inbox closes that gap before a run exists.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Optional

from src.lab.runtime.store import RunStore


@dataclass(frozen=True)
class Delivery:
    event_id: str
    run_id: str
    accepted: bool
    error_code: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "run_id": self.run_id,
            "accepted": self.accepted,
            "error_code": self.error_code,
        }


class EventInbox:
    def __init__(self, store: RunStore):
        self.store = store

    def deliver(self, event_id: str, payload: dict[str, Any]) -> Delivery:
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        run_id = f"evt_{hashlib.sha256(event_id.encode('utf-8')).hexdigest()[:12]}"
        claimed, owner, stored_hash = self.store.claim_event(
            event_id=event_id, run_id=run_id, payload_hash=digest
        )
        if claimed:
            return Delivery(event_id=event_id, run_id=owner, accepted=True)
        if stored_hash != digest:
            # Same id, different content: an edited message is a new decision, not a retry.
            return Delivery(event_id, owner, False, "event_payload_mismatch")
        return Delivery(event_id, owner, False, "duplicate_event")
