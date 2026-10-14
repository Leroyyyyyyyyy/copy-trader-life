"""Merge independent role outputs. Conflicts become review; no majority vote."""

from __future__ import annotations

from src.lab.domain import DecisionKind, MergedEvidence, RoleOutput


def merge_evidence(outputs: dict[str, RoleOutput]) -> MergedEvidence:
    resolver = outputs["context_resolver"]
    risk = outputs.get("risk_reviewer")
    chart = outputs.get("chart_agent")
    market = outputs.get("market_context")

    conflicts = list(resolver.conflicts)
    missing = list(resolver.missing_fields)
    refs = list(resolver.evidence_refs)
    sources: dict[str, object] = {}

    kind_raw = (resolver.fields.get("kind") or {}).get("value") or "review"
    kind = DecisionKind(kind_raw)
    plan_id = (resolver.fields.get("referenced_plan_id") or {}).get("value")
    stop = (resolver.fields.get("proposed_stop_loss") or {}).get("value")
    sources["kind"] = resolver.fields.get("kind")
    sources["referenced_plan_id"] = resolver.fields.get("referenced_plan_id")
    sources["proposed_stop_loss"] = resolver.fields.get("proposed_stop_loss")

    if chart:
        missing.extend(chart.missing_fields)
        refs.extend(chart.evidence_refs)
        chart_plan = (chart.fields.get("plan_id") or {}).get("value")
        if chart_plan and plan_id and chart_plan != plan_id:
            conflicts.append(f"plan_id:{plan_id}!={chart_plan}")
        if chart.fields.get("chart_invalidation_sl"):
            sources["chart_invalidation_sl"] = chart.fields["chart_invalidation_sl"]

    if market:
        missing.extend(market.missing_fields)
        refs.extend(market.evidence_refs)
        if market.status == "error":
            missing.append("market_context")

    if risk:
        gate = (risk.fields.get("gate") or {}).get("value")
        sources["risk_gate"] = risk.fields.get("gate")
        if gate == "ignore":
            kind = DecisionKind.IGNORE
        elif gate == "review":
            kind = DecisionKind.REVIEW

    if conflicts:
        kind = DecisionKind.REVIEW
        plan_id = None
        stop = None

    changes = {}
    if kind == DecisionKind.UPDATE and stop is not None:
        changes["stop_loss"] = float(stop)

    # Dedup refs while preserving order.
    seen: set[str] = set()
    uniq_refs: list[str] = []
    for ref in refs:
        if ref not in seen:
            seen.add(ref)
            uniq_refs.append(ref)

    return MergedEvidence(
        kind=kind,
        referenced_plan_id=str(plan_id) if plan_id else None,
        proposed_changes=changes,
        evidence_refs=tuple(uniq_refs) or ("message:unknown",),
        conflicts=tuple(dict.fromkeys(conflicts)),
        missing_fields=tuple(dict.fromkeys(missing)),
        field_sources=sources,
    )
