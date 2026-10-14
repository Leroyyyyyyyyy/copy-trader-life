"""Deterministic L4 roles. ContextResolver is the main agent; others are tools."""

from __future__ import annotations

import re
import time
from typing import Any, Optional

from src.lab.domain import ObservationBundle, PlanStatus, RoleOutput

_SYMBOLS = ("ETH", "BTC", "SOL")
_CHATTER = ("不操作", "顺便聊聊")
_FOLLOWUP = ("止损", "移到成本", "这张")


def _timed(role: str, fn) -> RoleOutput:
    t0 = time.perf_counter()
    out: RoleOutput = fn()
    return RoleOutput(
        role=out.role or role,
        status=out.status,
        fields=out.fields,
        evidence_refs=out.evidence_refs,
        conflicts=out.conflicts,
        missing_fields=out.missing_fields,
        latency_ms=(time.perf_counter() - t0) * 1000.0,
        error_code=out.error_code,
    )


def _symbols_in(text: str) -> list[str]:
    upper = text.upper()
    return [s for s in _SYMBOLS if s in upper]


def _is_chatter(text: str) -> bool:
    return any(p in text for p in _CHATTER)


def _is_followup(text: str) -> bool:
    return any(p in text for p in _FOLLOWUP)


def classify(bundle: ObservationBundle) -> RoleOutput:
    def run() -> RoleOutput:
        if not bundle.messages:
            return RoleOutput(role="classifier", status="ok", fields={"intent": {"value": "unknown", "source": "empty"}})
        last = max(bundle.messages, key=lambda m: m.visible_at)
        intent = "update_stop_to_cost" if _is_followup(last.text) else "other"
        return RoleOutput(
            role="classifier",
            status="ok",
            fields={"intent": {"value": intent, "source": f"message:{last.message_id}"}},
            evidence_refs=(f"message:{last.message_id}",),
        )

    return _timed("classifier", run)


def resolve_context(bundle: ObservationBundle) -> RoleOutput:
    def run() -> RoleOutput:
        msgs = sorted(bundle.messages, key=lambda m: m.visible_at)
        if not msgs:
            return RoleOutput(
                role="context_resolver",
                status="ok",
                fields={"kind": {"value": "review", "source": "empty"}},
                missing_fields=("messages",),
            )
        follow = msgs[-1]
        setup_symbols: list[str] = []
        for msg in msgs[:-1]:
            if _is_chatter(msg.text):
                continue
            setup_symbols.extend(_symbols_in(msg.text))
        setup_symbols = list(dict.fromkeys(setup_symbols))
        candidates = [p for p in bundle.plans if p.symbol in setup_symbols]
        filled = [p for p in candidates if p.status == PlanStatus.FILLED]
        refs = [f"message:{follow.message_id}"]
        fields: dict[str, Any] = {
            "setup_symbols": {"value": setup_symbols, "source": "messages"},
            "candidate_plan_ids": {
                "value": [p.plan_id for p in candidates],
                "source": "plans",
            },
        }
        conflicts: tuple[str, ...] = ()
        if len(filled) == 1:
            plan = filled[0]
            refs.append(f"plan:{plan.plan_id}")
            fields["kind"] = {"value": "update", "source": f"message:{follow.message_id}"}
            fields["referenced_plan_id"] = {"value": plan.plan_id, "source": f"plan:{plan.plan_id}"}
            fields["proposed_stop_loss"] = {
                "value": plan.cost_basis,
                "source": f"plan:{plan.plan_id}:cost_basis",
            }
            fields["symbol"] = {"value": plan.symbol, "source": f"plan:{plan.plan_id}"}
        elif len(filled) >= 2:
            conflicts = tuple(f"ambiguous_plan:{p.plan_id}" for p in filled)
            fields["kind"] = {"value": "review", "source": "context_resolver"}
            fields["referenced_plan_id"] = {"value": None, "source": "unresolved"}
            fields["symbol"] = {"value": setup_symbols[0] if setup_symbols else None, "source": "messages"}
        elif candidates:
            fields["kind"] = {"value": "ignore", "source": f"plan:{candidates[0].plan_id}:status"}
            fields["referenced_plan_id"] = {"value": candidates[0].plan_id, "source": f"plan:{candidates[0].plan_id}"}
            refs.append(f"plan:{candidates[0].plan_id}")
        else:
            fields["kind"] = {"value": "review", "source": "no_candidate"}
        return RoleOutput(
            role="context_resolver",
            status="ok",
            fields=fields,
            evidence_refs=tuple(refs),
            conflicts=conflicts,
        )

    return _timed("context_resolver", run)


def read_chart(
    bundle: ObservationBundle,
    *,
    symbol: Optional[str] = None,
    delay_sec: float = 0.0,
    forced_plan_id: Optional[str] = None,
) -> RoleOutput:
    def run() -> RoleOutput:
        if delay_sec:
            time.sleep(delay_sec)
        notes = list(bundle.chart_notes)
        fields: dict[str, Any] = {}
        refs: list[str] = []
        sl = None
        trigger = None
        for note in notes:
            refs.append(f"chart:{note.split(':', 1)[0]}")
            m_sl = re.search(r"SL near (\d+)", note, re.I)
            m_tr = re.search(r"trigger near (\d+)", note, re.I)
            if m_sl:
                sl = float(m_sl.group(1))
            if m_tr:
                trigger = float(m_tr.group(1))
        if sl is not None:
            fields["chart_invalidation_sl"] = {"value": sl, "source": "chart_notes"}
        if trigger is not None:
            fields["chart_trigger"] = {"value": trigger, "source": "chart_notes"}
        if forced_plan_id:
            fields["plan_id"] = {"value": forced_plan_id, "source": "chart_injected"}
        if symbol:
            fields["symbol"] = {"value": symbol, "source": "resolver"}
        missing = () if notes else ("chart_notes",)
        return RoleOutput(
            role="chart_agent",
            status="ok",
            fields=fields,
            evidence_refs=tuple(refs),
            missing_fields=missing,
        )

    return _timed("chart_agent", run)


def read_market(
    env: Any,
    *,
    delay_sec: float = 0.0,
    fail: bool = False,
) -> RoleOutput:
    def run() -> RoleOutput:
        if delay_sec:
            time.sleep(delay_sec)
        if fail:
            return RoleOutput(
                role="market_context",
                status="error",
                missing_fields=("account_snapshot",),
                error_code="market_unavailable",
            )
        snap = env.handle_tool("get_account_snapshot", {})
        if not isinstance(snap, dict):
            return RoleOutput(
                role="market_context",
                status="error",
                error_code="market_unavailable",
                missing_fields=("account_snapshot",),
            )
        costs = {
            p["plan_id"]: p.get("cost_basis")
            for p in (snap.get("plans") or [])
        }
        return RoleOutput(
            role="market_context",
            status="ok",
            fields={"cost_basis_by_plan": {"value": costs, "source": "account_snapshot"}},
            evidence_refs=("account_snapshot",),
        )

    return _timed("market_context", run)


def review_risk(bundle: ObservationBundle, resolver: RoleOutput) -> RoleOutput:
    def run() -> RoleOutput:
        kind = (resolver.fields.get("kind") or {}).get("value")
        plan_id = (resolver.fields.get("referenced_plan_id") or {}).get("value")
        by_id = {p.plan_id: p for p in bundle.plans}
        fields: dict[str, Any] = {"gate": {"value": "allow", "source": "risk"}}
        if kind == "update" and plan_id:
            plan = by_id.get(str(plan_id))
            if plan is None:
                fields["gate"] = {"value": "review", "source": "unknown_plan"}
            elif plan.status != PlanStatus.FILLED:
                fields["gate"] = {"value": "ignore", "source": f"plan:{plan.plan_id}:not_filled"}
        if resolver.conflicts:
            fields["gate"] = {"value": "review", "source": "resolver_conflicts"}
        return RoleOutput(role="risk_reviewer", status="ok", fields=fields, evidence_refs=resolver.evidence_refs)

    return _timed("risk_reviewer", run)
