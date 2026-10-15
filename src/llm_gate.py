"""LLM check between the rule parser and execution: is this post an instruction to trade?

The rule parser is keyword-based and fires on recaps, brags, news, negations and
warnings ("I won't short", "you'd be a fool to short here"). Before a parsed
signal can act, DeepSeek reads the post with recent channel context and the
parser's reading and answers whether the author is telling followers to trade,
in which direction and on which symbol. The signal proceeds only if the model
agrees with the parser. Errors fail closed unless configured otherwise.

Synchronous (httpx); the engine already runs handle_text in a worker thread.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

import httpx

GATE_VERSION = "llm_gate.v2"
VERDICTS = ("enter_now", "conditional_setup", "adjust_exit", "close", "not_actionable", "unclear")
ACTIONABLE = {"enter_now", "conditional_setup", "adjust_exit", "close"}

SYSTEM = """You review posts from a crypto trading channel before a bot copies them.
A keyword parser has already read the latest post as a trade. Your job is to say
whether the author is actually telling followers to trade.

Actionable means the author instructs or invites followers to take a trade now,
to prepare a stated conditional entry, to change a stop/target, or to close a
position. Everything else is not actionable: commentary, opinions, stories about
past trades, reactions to news, and statements about what the author or others
should NOT do. Use the earlier posts only as context; judge the latest post.

Reply with ONE json object:
{"verdict": "enter_now" | "conditional_setup" | "adjust_exit" | "close" | "not_actionable" | "unclear",
 "side": "buy" | "sell" | null,
 "symbol": "<ticker as the author means it>" | null,
 "reason": "<one sentence>"}"""


@dataclass
class GateDecision:
    allow: bool
    verdict: str
    side: Optional[str] = None
    symbol: Optional[str] = None
    reason: str = ""
    error: Optional[str] = None
    latency_ms: float = 0.0
    version: str = GATE_VERSION
    usage: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _intent_matches(verdict: str, intent: str) -> bool:
    if intent == "confirm_breakout":
        return verdict in {"conditional_setup", "enter_now"}
    if intent == "market":  # a market order fills now; "wait for zone 2" is not that
        return verdict == "enter_now"
    if intent in {"limit", "wait_target"}:
        return verdict in {"enter_now", "conditional_setup"}
    if intent == "update":
        return verdict == "adjust_exit"
    if intent == "close":
        return verdict == "close"
    return verdict in ACTIONABLE


def decide(out: dict[str, Any], parsed: dict[str, Any]) -> tuple[bool, str, Optional[str], Optional[str], list[str]]:
    """Pure check of a model reply against the parser's reading."""
    verdict = str(out.get("verdict") or "unclear")
    if verdict not in VERDICTS:
        verdict = "unclear"
    side = out.get("side") if out.get("side") in ("buy", "sell") else None
    symbol = str(out["symbol"]).upper().lstrip("$") if out.get("symbol") else None
    reasons = []
    allow = _intent_matches(verdict, str(parsed.get("intent")))
    if not allow:
        reasons.append(f"verdict:{verdict}")
    p_side, p_sym = parsed.get("side"), (parsed.get("symbol") or "").upper()
    if allow and p_side in ("buy", "sell") and side and side != p_side:
        allow = False
        reasons.append(f"side:{side}!={p_side}")
    if allow and p_sym and symbol != p_sym:  # the model must name the same asset, not leave it open
        allow = False
        reasons.append(f"symbol:{symbol}!={p_sym}")
    return allow, verdict, side, symbol, reasons


def review(
    *,
    text: str,
    context: list[str],
    parsed: dict[str, Any],
    has_image: bool = False,
    on_error: str = "skip",
    timeout_sec: float = 60.0,
    retries: int = 1,
    transport: Optional[httpx.BaseTransport] = None,
) -> GateDecision:
    """`parsed` = {"intent", "side", "symbol"} from the rule parser."""
    t0 = time.perf_counter()
    key = os.getenv("DEEPSEEK_API_KEY")
    base = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    model = os.getenv("DEEPSEEK_GATE_MODEL") or os.getenv("DEEPSEEK_LAB_MODEL") or "deepseek-flash"

    def failed(err: str) -> GateDecision:
        return GateDecision(allow=on_error == "allow", verdict="error", error=err,
                            latency_ms=round((time.perf_counter() - t0) * 1000, 1))

    if not key:
        return failed("missing_api_key")
    user = {
        "earlier_posts": context[-6:],
        "latest_post": text,
        "latest_post_has_image": has_image,
        "parser_reading": parsed,
    }
    body = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": json.dumps(user, ensure_ascii=False)}],
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "max_tokens": 8000,  # reasoning models spend most of it thinking; 4000 ran out (empty content)
    }
    err = ""
    for _ in range(1 + retries):  # one retry: a bad JSON reply or a dropped connection is usually transient
        try:
            with httpx.Client(timeout=timeout_sec, transport=transport) as http:
                r = http.post(f"{base}/chat/completions", headers={"Authorization": f"Bearer {key}"}, json=body)
            if r.status_code != 200:
                err = f"http_{r.status_code}"
                continue
            data = r.json()
            choice = data["choices"][0]
            if choice.get("finish_reason") == "length":
                err = "truncated_output"
                continue
            out = json.loads((choice["message"].get("content") or "").strip())
            break
        except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
            err = type(exc).__name__
    else:
        return failed(err)

    allow, verdict, side, symbol, reasons = decide(out, parsed)
    return GateDecision(
        allow=allow, verdict=verdict, side=side, symbol=symbol,
        reason=(str(out.get("reason") or "") + (" | " + ";".join(reasons) if reasons else "")).strip(" |"),
        latency_ms=round((time.perf_counter() - t0) * 1000, 1), usage=data.get("usage") or {},
    )
