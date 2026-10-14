"""L7 human review: evidence cards, versioned responses, stale approvals.

Flow: an agent run ends with a held proposal -> `open_card` persists a card and
marks the run WAITING_REVIEW (no worker waits on it) -> a reviewer answers via
`respond` (CLI or scripted) -> the service re-reads the account, re-validates,
and commits through SimulationGateway like any other caller.

A human confirmation is not a bypass: the same validator, risk rule, fence and
idempotency checks apply to it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from src.lab.domain import (
    DecisionKind,
    ObservationBundle,
    RunStatus,
    SubmittedDecision,
    plan_map,
)
from src.lab.execution.simulation import SimulationGateway
from src.lab.execution.validator import DecisionValidator
from src.lab.runtime.store import RunStore
from src.lab.sandbox.snapshot_sim import simulate_action
from src.lab.tools.policy import stop_loss_widens

CARD_VERSION = "l7.card.v1"
EDITABLE_FIELDS = frozenset({"plan_id", "stop_loss"})
ACTIONS = frozenset({"confirm", "modify", "reject"})
DEFAULT_TTL = timedelta(hours=24)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ReviewResult:
    status: str
    card_id: str
    revision: Optional[int] = None
    new_revision: Optional[int] = None
    decision: Optional[dict[str, Any]] = None
    receipt: Optional[dict[str, Any]] = None
    errors: list[str] = field(default_factory=list)
    response_id: Optional[str] = None
    reused: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "card_id": self.card_id,
            "revision": self.revision,
            "new_revision": self.new_revision,
            "decision": self.decision,
            "receipt": self.receipt,
            "errors": list(self.errors),
            "response_id": self.response_id,
            "reused": self.reused,
        }

    @property
    def committed(self) -> bool:
        return self.status in {"confirmed", "modified", "rejected"}


class ReviewService:
    def __init__(
        self,
        store: RunStore,
        *,
        clock: Callable[[], datetime] = _now,
        ttl: timedelta = DEFAULT_TTL,
        validator: DecisionValidator | None = None,
    ):
        self.store = store
        self.clock = clock
        self.ttl = ttl
        self.validator = validator or DecisionValidator()

    # --- opening ---------------------------------------------------------------

    def open_card(
        self,
        *,
        run_id: str,
        task_id: str,
        account_id: str,
        proposal: dict[str, Any],
        bundle: ObservationBundle,
        review_policy: str,
    ) -> dict[str, Any]:
        fence = self.store.get_fence(run_id)
        if fence is not None:
            raise ValueError(f"run_fenced:{fence}")
        gateway = self._gateway(account_id)
        now = self.clock()
        card = {
            "card_id": f"card_{run_id}",
            "card_version": CARD_VERSION,
            "run_id": run_id,
            "task_id": task_id,
            "account_id": account_id,
            "review_policy": review_policy,
            "status": "pending",
            "revision": 1,
            "opened_at": _iso(now),
            "expires_at": _iso(now + self.ttl),
            "messages": [
                {"message_id": m.message_id, "visible_at": m.visible_at, "text": m.text}
                for m in bundle.messages
            ],
            "proposal": dict(proposal),
            "proposal_history": [dict(proposal)],
            "history": [{"at": _iso(now), "event": "opened", "revision": 1}],
            "resolution": None,
        }
        self._refresh(card, gateway)
        self.store.put_review(card)
        self._set_run_status(run_id, task_id, RunStatus.WAITING_REVIEW.value, "waiting_review", card)
        return card

    def _refresh(self, card: dict[str, Any], gateway: SimulationGateway) -> None:
        """(Re)compute everything on the card that depends on the account."""
        proposal = card["proposal"]
        plans = plan_map(gateway.plans())
        card["state_version"] = gateway.state_version
        target = plans.get(proposal.get("plan_id") or "")
        card["candidates"] = [
            p.to_dict()
            for p in gateway.plans()
            if p.plan_id in set(proposal.get("candidates") or [])
            or (target is not None and p.plan_id == target.plan_id)
        ]
        card["field_sources"] = _field_sources(proposal, target)
        card["missing"] = _missing(proposal)
        card["conflicts"] = _conflicts(proposal, target)
        card["simulated_impact"] = _impact(proposal, gateway)

    # --- responding ------------------------------------------------------------

    def respond(
        self,
        card_id: str,
        *,
        revision: int,
        action: str,
        changes: Optional[dict[str, Any]] = None,
        reviewer: str = "cli",
        response_id: Optional[str] = None,
        reason: str = "",
    ) -> ReviewResult:
        changes = dict(changes or {})
        body = {
            "card_id": card_id,
            "revision": int(revision),
            "action": action,
            "changes": changes,
            "reviewer": reviewer,
        }
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()
        response_id = response_id or f"resp_{digest[:16]}"

        # 1. Same response twice -> same result, nothing re-executed.
        prior = self.store.get_review_response(response_id)
        if prior is not None:
            if prior["payload_hash"] != digest:
                return ReviewResult("response_payload_mismatch", card_id, response_id=response_id)
            out = ReviewResult(**prior["result"])
            out.reused = True
            return out

        result = self._respond(card_id, int(revision), action, changes, reviewer, reason)
        result.response_id = response_id
        # Only terminal answers are remembered; a stale or invalid answer must be
        # re-sent against the new revision, not replayed.
        if result.committed:
            self.store.put_review_response(
                response_id=response_id, card_id=card_id, payload_hash=digest, result=result.to_dict()
            )
        return result

    def _respond(
        self,
        card_id: str,
        revision: int,
        action: str,
        changes: dict[str, Any],
        reviewer: str,
        reason: str,
    ) -> ReviewResult:
        card = self.store.get_review(card_id)
        if card is None:
            return ReviewResult("unknown_card", card_id)
        now = self.clock()

        # 2. Card lifecycle.
        if card["status"] != "pending":
            return ReviewResult("already_resolved", card_id, revision=card["revision"], errors=[card["status"]])
        if now >= datetime.fromisoformat(card["expires_at"]):
            card["status"] = "expired"
            card["history"].append({"at": _iso(now), "event": "expired"})
            self.store.put_review(card)
            self._set_run_status(card["run_id"], card["task_id"], RunStatus.TIMED_OUT.value, "review_expired", card)
            return ReviewResult("expired", card_id, revision=card["revision"])
        if revision != card["revision"]:
            return ReviewResult("stale_revision", card_id, revision=card["revision"],
                                errors=[f"answered_r{revision}_current_r{card['revision']}"])

        # 3. What the reviewer may touch.
        if action not in ACTIONS:
            return ReviewResult("invalid_action", card_id, revision=revision, errors=[action])
        illegal = sorted(set(changes) - EDITABLE_FIELDS)
        if illegal:
            return ReviewResult("field_not_editable", card_id, revision=revision, errors=illegal)
        if action == "modify" and not changes:
            return ReviewResult("invalid_action", card_id, revision=revision, errors=["modify_without_changes"])

        # 4. Re-read the account. If it moved since the card was shown, the
        #    approval was given for a different world: rebase and ask again.
        gateway = self._gateway(card["account_id"])
        if gateway.state_version != card["state_version"]:
            return self._rebase(card, gateway, now, action, reviewer, revision)

        # 5. Build the decision this answer stands for.
        proposal = dict(card["proposal"])
        if action == "modify":
            proposal = {
                **proposal,
                **{k: (float(v) if k == "stop_loss" else str(v)) for k, v in changes.items()},
                "kind": "update",
                "version": int(card["proposal"]["version"]) + 1,
                "source": f"human:{reviewer}",
            }
        if action == "reject":
            decision = SubmittedDecision(
                kind=DecisionKind.REVIEW,
                referenced_plan_id=None,
                evidence_refs=tuple(proposal.get("evidence_refs") or ()) + (f"review:{card_id}:r{revision}",),
                expected_state_version=card["state_version"],
            )
        else:
            if action == "confirm" and proposal["kind"] == "review":
                # Confirm means "do what the agent proposed". A review with a
                # concrete best guess proposes that update; without one there is
                # nothing to execute, so the human must modify or reject.
                proposal = {**proposal, "kind": "update"}
            if proposal["kind"] == "update" and (not proposal.get("plan_id") or proposal.get("stop_loss") is None):
                return ReviewResult("nothing_to_confirm", card_id, revision=revision, errors=_missing(proposal))
            decision = SubmittedDecision(
                kind=DecisionKind(proposal["kind"]),
                referenced_plan_id=proposal.get("plan_id") if proposal["kind"] == "update" else None,
                proposed_changes=(
                    {"stop_loss": float(proposal["stop_loss"])} if proposal["kind"] == "update" else {}
                ),
                evidence_refs=tuple(proposal.get("evidence_refs") or ()) + (f"review:{card_id}:r{revision}",),
                expected_state_version=card["state_version"],
            )

        # 6. Re-validate exactly like an agent commit would be.
        errors = self.validator.validate(
            decision,
            gateway.plans(),
            expected_state_version=card["state_version"],
            current_state_version=gateway.state_version,
        )
        target = plan_map(gateway.plans()).get(decision.referenced_plan_id or "")
        if decision.kind == DecisionKind.UPDATE and target is not None:
            if stop_loss_widens(target, float(decision.proposed_changes["stop_loss"])):
                errors.append("risk_stop_widened")
        if errors:
            card["history"].append(
                {"at": _iso(now), "event": "revalidation_failed", "action": action,
                 "attempted": proposal, "errors": errors, "reviewer": reviewer}
            )
            self.store.put_review(card)
            return ReviewResult("revalidation_failed", card_id, revision=revision, errors=errors,
                                decision=_decision_dict(decision))

        # 7. Commit through the same gateway as every other caller.
        receipt = gateway.commit(
            run_id=card["run_id"],
            decision=decision,
            decision_id="review",
            decision_version=int(proposal["version"]),
            expected_state_version=card["state_version"],
        )
        if not receipt.applied and not receipt.reused:
            code = receipt.error_code or "commit_rejected"
            if code.startswith("stale_state_version"):
                # Another process moved the account between our read and the commit
                # (caught by the compare-and-swap in RunStore). Same as step 4.
                gateway.load()
                return self._rebase(card, gateway, now, action, reviewer, revision)
            status = "run_fenced" if code.startswith("run_fenced") else "commit_rejected"
            card["history"].append({"at": _iso(now), "event": status, "error": code})
            if status == "run_fenced":
                card["status"] = "closed_fenced"
            self.store.put_review(card)
            return ReviewResult(status, card_id, revision=revision, errors=[code],
                                decision=_decision_dict(decision))

        final_status = {"confirm": "confirmed", "modify": "modified", "reject": "rejected"}[action]
        if action == "modify":
            card["proposal"] = proposal
            card["proposal_history"].append(proposal)
        card["status"] = final_status
        card["resolution"] = {
            "action": action,
            "reviewer": reviewer,
            "reason": reason,
            "at": _iso(now),
            "decision": _decision_dict(decision),
            "receipt": receipt.to_dict(),
        }
        card["history"].append({"at": _iso(now), "event": final_status, "reviewer": reviewer})
        self.store.put_review(card)
        run_status = RunStatus.REJECTED.value if action == "reject" else RunStatus.SUCCEEDED.value
        self._set_run_status(card["run_id"], card["task_id"], run_status, f"review_{final_status}", card)
        return ReviewResult(final_status, card_id, revision=revision,
                            decision=_decision_dict(decision), receipt=receipt.to_dict())

    # --- helpers ---------------------------------------------------------------

    def _rebase(
        self,
        card: dict[str, Any],
        gateway: SimulationGateway,
        now: datetime,
        action: str,
        reviewer: str,
        revision: int,
    ) -> ReviewResult:
        """The approval was for an older account state: new revision, ask again."""
        old = card["state_version"]
        card["revision"] += 1
        self._refresh(card, gateway)
        card["history"].append(
            {"at": _iso(now), "event": "stale_rebased", "from_state_version": old,
             "to_state_version": card["state_version"], "revision": card["revision"],
             "rejected_action": action, "reviewer": reviewer}
        )
        self.store.put_review(card)
        return ReviewResult("stale_state", card["card_id"], revision=revision,
                            new_revision=card["revision"],
                            errors=[f"state_version:{old}->{card['state_version']}"])

    def _gateway(self, account_id: str) -> SimulationGateway:
        gateway = SimulationGateway(self.store, self.validator, account_id=account_id)
        if not gateway.load():
            raise ValueError(f"unknown_account:{account_id}")
        return gateway

    def _set_run_status(
        self, run_id: str, task_id: str, status: str, reason: str, card: dict[str, Any]
    ) -> None:
        prior = self.store.get_run(run_id)
        payload = dict((prior or {}).get("payload") or {})
        payload["review"] = {
            "card_id": card["card_id"],
            "status": card["status"],
            "revision": card["revision"],
            "human_assisted": card["status"] in {"confirmed", "modified", "rejected"},
        }
        self.store.upsert_run(
            run_id=run_id, task_id=task_id, status=status, terminal_reason=reason, payload=payload
        )


def _decision_dict(decision: SubmittedDecision) -> dict[str, Any]:
    return {
        "kind": decision.kind.value,
        "referenced_plan_id": decision.referenced_plan_id,
        "proposed_changes": dict(decision.proposed_changes),
        "evidence_refs": list(decision.evidence_refs),
        "expected_state_version": decision.expected_state_version,
    }


def _field_sources(proposal: dict[str, Any], target: Any) -> dict[str, Any]:
    refs = list(proposal.get("evidence_refs") or [])
    out: dict[str, Any] = {
        "kind": {"value": proposal.get("kind"), "source": proposal.get("source"), "evidence_refs": refs},
        "plan_id": {"value": proposal.get("plan_id"), "source": proposal.get("source"), "evidence_refs": refs},
    }
    sl = proposal.get("stop_loss")
    derived = None
    if target is not None and sl is not None and target.cost_basis is not None:
        if abs(float(sl) - float(target.cost_basis)) < 1e-9:
            derived = f"plan:{target.plan_id}.cost_basis"
    out["stop_loss"] = {"value": sl, "source": proposal.get("source"), "derived_from": derived, "evidence_refs": refs}
    return out


def _missing(proposal: dict[str, Any]) -> list[str]:
    missing = []
    if proposal.get("kind") in {"update", "review"}:
        if not proposal.get("plan_id"):
            missing.append("plan_id")
        if proposal.get("stop_loss") is None:
            missing.append("stop_loss")
    return missing


def _conflicts(proposal: dict[str, Any], target: Any) -> list[str]:
    conflicts = []
    if len(proposal.get("candidates") or []) > 1:
        conflicts.append("multiple_candidate_plans")
    sl = proposal.get("stop_loss")
    if target is not None and sl is not None and stop_loss_widens(target, float(sl)):
        conflicts.append("risk:stop_widened")
    if target is not None and target.cost_basis is None:
        conflicts.append(f"plan:{target.plan_id}.cost_basis_unknown")
    return conflicts


def _impact(proposal: dict[str, Any], gateway: SimulationGateway) -> dict[str, Any]:
    if proposal.get("kind") != "update" or not proposal.get("plan_id") or proposal.get("stop_loss") is None:
        return {"applicable": False, "note": "no concrete state change proposed"}
    before = gateway.snapshot()
    sim = simulate_action(
        before,
        {
            "kind": "update",
            "plan_id": proposal["plan_id"],
            "stop_loss": proposal["stop_loss"],
            "evidence_refs": proposal.get("evidence_refs") or ["review"],
        },
    )
    old = next((p for p in before["plans"] if p["plan_id"] == proposal["plan_id"]), {})
    new = next((p for p in sim["simulated_state"]["plans"] if p["plan_id"] == proposal["plan_id"]), {})
    return {
        "applicable": True,
        "legal": sim["legal"],
        "constraint_results": sim["constraint_results"],
        "plan_id": proposal["plan_id"],
        "stop_loss_before": old.get("stop_loss"),
        "stop_loss_after": new.get("stop_loss"),
        "cost_basis": old.get("cost_basis"),
    }
