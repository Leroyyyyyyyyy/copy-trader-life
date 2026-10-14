"""Idempotent simulation commits for teaching accounts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

from src.lab.domain import (
    CommitReceipt,
    DecisionKind,
    PlanStatus,
    PlanView,
    SubmittedDecision,
    decision_to_dict,
)
from src.lab.execution.validator import DecisionValidator
from src.lab.runtime.store import RunStore, StaleAccountError


def _payload_hash(decision: SubmittedDecision) -> str:
    blob = json.dumps(decision_to_dict(decision), sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class SimulationGateway:
    def __init__(
        self,
        store: RunStore,
        validator: DecisionValidator | None = None,
        *,
        account_id: str | None = None,
    ):
        self.store = store
        self.validator = validator or DecisionValidator()
        # L7: with an account_id the account survives the process, so a review
        # answered later from the CLI sees (and changes) the same account.
        self.account_id = account_id
        self._plans: dict[str, PlanView] = {}
        self._state_version = 1
        self._submit_count = 0

    def reset(self, plans: tuple[PlanView, ...] | list[PlanView], state_version: int) -> None:
        self._plans = {p.plan_id: p for p in plans}
        self._state_version = state_version
        self._submit_count = 0
        if self.account_id is not None:
            self.store.put_account(self.account_id, self._account_payload())

    def load(self) -> bool:
        """Re-read the persisted account. Returns False if there is none."""
        if self.account_id is None:
            return False
        raw = self.store.get_account(self.account_id)
        if raw is None:
            return False
        self._plans = {p["plan_id"]: _plan_from_dict(p) for p in raw["plans"]}
        self._state_version = int(raw["state_version"])
        self._submit_count = int(raw["submit_count"])
        return True

    def external_update(self, plan_id: str, *, stop_loss: float) -> int:
        """A change made outside this run (another operator, venue sync).

        No receipt: it is not a decision of this run. It bumps state_version, which
        is what makes an older review approval stale.
        """
        self.load()
        plan = self._plans[plan_id]
        self._state_version += 1
        self._plans[plan_id] = replace(plan, stop_loss=float(stop_loss), state_version=self._state_version)
        if self.account_id is not None:
            self.store.put_account(self.account_id, self._account_payload())
        return self._state_version

    def _account_payload(
        self,
        plans: dict[str, PlanView] | None = None,
        state_version: int | None = None,
        submit_count: int | None = None,
    ) -> dict[str, Any]:
        return {
            "plans": [p.to_dict() for p in (plans if plans is not None else self._plans).values()],
            "state_version": self._state_version if state_version is None else state_version,
            "submit_count": self._submit_count if submit_count is None else submit_count,
        }

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

        def rejected(error_code: str) -> CommitReceipt:
            return CommitReceipt(
                run_id=run_id,
                decision_id=decision_id,
                decision_version=decision_version,
                idempotency_key=key,
                applied=False,
                state_version=self._state_version,
                payload_hash=digest,
                reused=False,
                error_code=error_code,
            )

        fence = self.store.get_fence(run_id)
        if fence is not None:
            # Cancelled/timed-out runs: a late model result must not land a commit.
            return rejected(f"run_fenced:{fence}")

        existing = self.store.get_commit(key)
        if existing is not None:
            if existing["payload_hash"] != digest:
                return rejected("idempotency_payload_mismatch")
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
            return rejected(";".join(errors))

        # Compute the next account state without applying it yet.
        next_plans = dict(self._plans)
        next_version = self._state_version
        if decision.kind == DecisionKind.UPDATE and decision.referenced_plan_id:
            if "stop_loss" in decision.proposed_changes:
                plan = next_plans[decision.referenced_plan_id]
                next_version += 1
                next_plans[plan.plan_id] = replace(
                    plan,
                    stop_loss=float(decision.proposed_changes["stop_loss"]),
                    state_version=next_version,
                )
        elif decision.kind not in {DecisionKind.IGNORE, DecisionKind.REVIEW, DecisionKind.ANALYZE}:
            return rejected("unsupported_decision_kind")

        receipt = CommitReceipt(
            run_id=run_id,
            decision_id=decision_id,
            decision_version=decision_version,
            idempotency_key=key,
            applied=True,
            state_version=next_version,
            payload_hash=digest,
            reused=False,
        )
        # L6: persist the receipt before applying. If the write fails, nothing is
        # applied, so a retry cannot double-apply a mutation whose receipt was lost.
        account = None
        if self.account_id is not None:
            account = self._account_payload(next_plans, next_version, self._submit_count + 1)
        try:
            # With a persisted account, receipt and account change are one SQL
            # transaction: either both land or neither does.
            self.store.put_commit(
                idempotency_key=key,
                run_id=run_id,
                decision_id=decision_id,
                decision_version=decision_version,
                payload_hash=digest,
                receipt=receipt.to_dict(),
                account_id=self.account_id,
                account=account,
                account_expected_version=self._state_version if account is not None else None,
            )
        except StaleAccountError as exc:
            return rejected(f"stale_state_version:db:{exc}")
        except Exception as exc:  # noqa: BLE001 - surfaced as a retryable commit error
            return rejected(f"state_write_failed:{type(exc).__name__}")
        self._plans = next_plans
        self._state_version = next_version
        self._submit_count += 1
        return receipt


def _plan_from_dict(raw: dict[str, Any]) -> PlanView:
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
