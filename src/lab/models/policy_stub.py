"""Fixed-policy stubs for H2 observation ablation. Not LLM results."""

from __future__ import annotations

from typing import Any

from src.lab.domain import ModelTurn, ToolCall, Usage


def _plans(request: dict[str, Any]) -> list[dict[str, Any]]:
    return list((request.get("world") or {}).get("plans") or [])


def _latest_message_id(request: dict[str, Any]) -> str:
    messages = list((request.get("world") or {}).get("messages") or [])
    if not messages:
        return "message:unknown"
    last = max(messages, key=lambda m: str(m.get("visible_at") or ""))
    return f"message:{last.get('message_id')}"


def _state_version(request: dict[str, Any]) -> int:
    return int(request.get("state_version") or 1)


def _eth_plans(request: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for p in _plans(request) if str(p.get("symbol")) == "ETH"]


def _update_entry(allowed: list[dict[str, Any]] | None, plan_id: str) -> dict[str, Any] | None:
    if not allowed:
        return None
    for item in allowed:
        if item.get("tool") == "submit_plan_update" and item.get("plan_id") == plan_id:
            return item
    return None


def _already_submitted(request: dict[str, Any]) -> bool:
    for turn in (request.get("history") or {}).get("turns") or []:
        for result in turn.get("tool_results") or []:
            if result.get("name") in {"submit_plan_update", "submit_decision"}:
                return True
    return False


class PolicyStubAdapter:
    """One-shot submit then done. Inspects allowed_actions only when present."""

    def __init__(self, *, model_id: str, version: str, policy: str):
        self.model_id = model_id
        self.version = version
        self.policy = policy
        self.requests: list[dict[str, Any]] = []

    def generate(self, request: dict[str, Any]) -> tuple[ModelTurn, Usage]:
        self.requests.append(request)
        if _already_submitted(request):
            turn = ModelTurn(raw={"final": {"done": True}}, final={"done": True})
            return turn, Usage(model_calls=1, output_tokens=4)
        call = self._choose(request)
        turn = ModelTurn(
            raw={"tool_calls": [{"name": call.name, "arguments": call.arguments}]},
            tool_calls=(call,),
        )
        return turn, Usage(model_calls=1, output_tokens=8)

    def _choose(self, request: dict[str, Any]) -> ToolCall:
        raise NotImplementedError


class NaiveEthUpdateAdapter(PolicyStubAdapter):
    """Model A: always try the first ETH plan; skip if the list marks it illegal."""

    def __init__(self) -> None:
        super().__init__(
            model_id="A",
            version="stub.naive_eth.v1",
            policy="naive_eth_update",
        )

    def _choose(self, request: dict[str, Any]) -> ToolCall:
        eth = _eth_plans(request)
        if not eth:
            return _decision_call("review", request)
        target = eth[0]
        plan_id = str(target["plan_id"])
        allowed = request.get("allowed_actions")
        entry = _update_entry(allowed, plan_id)
        if entry is not None and not entry.get("legal"):
            return _decision_call("ignore", request)
        cost = target.get("cost_basis")
        return ToolCall(
            call_id="a_update",
            name="submit_plan_update",
            arguments={
                "plan_id": plan_id,
                "stop_loss": float(cost if cost is not None else 0),
                "evidence_refs": [_latest_message_id(request), f"plan:{plan_id}"],
                "expected_state_version": _state_version(request),
            },
        )


class EthCardinalityAdapter(PolicyStubAdapter):
    """Model B: unique filled ETH → update; many filled ETH → review; none → ignore."""

    def __init__(self) -> None:
        super().__init__(
            model_id="B",
            version="stub.eth_cardinality.v1",
            policy="eth_cardinality",
        )

    def _choose(self, request: dict[str, Any]) -> ToolCall:
        eth = _eth_plans(request)
        filled = [p for p in eth if p.get("status") == "filled"]
        allowed = request.get("allowed_actions")
        if len(filled) == 1:
            target = filled[0]
            plan_id = str(target["plan_id"])
            entry = _update_entry(allowed, plan_id)
            if entry is not None and not entry.get("legal"):
                return _decision_call("ignore", request)
            cost = target.get("cost_basis")
            return ToolCall(
                call_id="b_update",
                name="submit_plan_update",
                arguments={
                    "plan_id": plan_id,
                    "stop_loss": float(cost if cost is not None else 0),
                    "evidence_refs": [_latest_message_id(request), f"plan:{plan_id}"],
                    "expected_state_version": _state_version(request),
                },
            )
        if len(filled) >= 2:
            return _decision_call("review", request)
        return _decision_call("ignore", request)


def _decision_call(kind: str, request: dict[str, Any]) -> ToolCall:
    return ToolCall(
        call_id=f"d_{kind}",
        name="submit_decision",
        arguments={
            "kind": kind,
            "evidence_refs": [_latest_message_id(request)],
            "expected_state_version": _state_version(request),
        },
    )


def h2_model(model_id: str) -> PolicyStubAdapter:
    if model_id == "A":
        return NaiveEthUpdateAdapter()
    if model_id == "B":
        return EthCardinalityAdapter()
    raise ValueError(f"unknown_h2_model:{model_id}")
