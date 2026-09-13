"""Idempotent simulation commits for teaching accounts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

from src.lab.domain import (
    CommitReceipt,
    DecisionKind,
    PlanView,
    SubmittedDecision,
    decision_to_dict,
)
from src.lab.execution.validator import DecisionValidator
from src.lab.runtime.store import RunStore


def _payload_hash(decision: SubmittedDecision) -> str:
    blob = json.dumps(decision_to_dict(decision), sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class SimulationGateway:
    def __init__(self, store: RunStore, validator: DecisionValidator | None = None):
        self.store = store
        self.validator = validator or DecisionValidator()
        self._plans: dict[str, PlanView] = {}
        self._state_version = 1
        self._submit_count = 0

    def reset(self, plans: tuple[PlanView, ...] | list[PlanView], state_version: int) -> None:
        self._plans = {p.plan_id: p for p in plans}
        self._state_version = state_version
        self._submit_count = 0

    @property
    def state_version(self) -> int:
        return self._state_version

    @property
    def submit_count(self) -> int:
        return self._submit_count

    def plans(self) -> tuple[PlanView, ...]:
        return tuple(self._plans.values())

    def snapshot(self) -> dict[str, Any]:
        return {
            "plans": [p.to_dict() for p in self.plans()],
            "state_version": self._state_version,
            "submit_count": self._submit_count,
        }

    def commit(
        self,
        *,
        run_id: str,
        decision: SubmittedDecision,
        decision_id: str = "decision",
        decision_version: int = 1,
        expected_state_version: int | None = None,
    ) -> CommitReceipt:
        key = f"{run_id}:{decision_id}:{decision_version}"
        digest = _payload_hash(decision)
        existing = self.store.get_commit(key)
        if existing is not None:
            if existing["payload_hash"] != digest:
                return CommitReceipt(
                    run_id=run_id,
                    decision_id=decision_id,
                    decision_version=decision_version,
                    idempotency_key=key,
                    applied=False,
                    state_version=self._state_version,
                    payload_hash=digest,
                    reused=False,
                    error_code="idempotency_payload_mismatch",
                )
            receipt = CommitReceipt(**existing["receipt"])
            return replace(receipt, reused=True)

        exp = (
            expected_state_version
            if expected_state_version is not None
            else decision.expected_state_version
        )
        errors = self.validator.validate(
            decision,
            self.plans(),
            expected_state_version=exp,
            current_state_version=self._state_version,
        )
        if errors:
            return CommitReceipt(
                run_id=run_id,
                decision_id=decision_id,
                decision_version=decision_version,
                idempotency_key=key,
                applied=False,
                state_version=self._state_version,
                payload_hash=digest,
                error_code=";".join(errors),
            )

        if decision.kind == DecisionKind.UPDATE and decision.referenced_plan_id:
            if "stop_loss" in decision.proposed_changes:
                plan = self._plans[decision.referenced_plan_id]
                new_sl = float(decision.proposed_changes["stop_loss"])
                self._state_version += 1
                self._plans[plan.plan_id] = replace(
                    plan,
                    stop_loss=new_sl,
                    state_version=self._state_version,
                )
            self._submit_count += 1
        elif decision.kind in {DecisionKind.IGNORE, DecisionKind.REVIEW, DecisionKind.ANALYZE}:
            self._submit_count += 1
        else:
            return CommitReceipt(
                run_id=run_id,
                decision_id=decision_id,
                decision_version=decision_version,
                idempotency_key=key,
                applied=False,
                state_version=self._state_version,
                payload_hash=digest,
                error_code="unsupported_decision_kind",
            )

        receipt = CommitReceipt(
            run_id=run_id,
            decision_id=decision_id,
            decision_version=decision_version,
            idempotency_key=key,
            applied=True,
            state_version=self._state_version,
            payload_hash=digest,
            reused=False,
        )
        self.store.put_commit(
            idempotency_key=key,
            run_id=run_id,
            decision_id=decision_id,
            decision_version=decision_version,
            payload_hash=digest,
            receipt=receipt.to_dict(),
        )
        return receipt
