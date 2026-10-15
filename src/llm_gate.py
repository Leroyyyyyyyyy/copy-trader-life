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

GATE_VERSION = "llm_gate.v3"
VERDICTS = ("enter_now", "conditional_setup", "adjust_exit", "close", "cancel_setup", "not_actionable", "unclear")
ACTIONABLE = {"enter_now", "conditional_setup", "adjust_exit", "close"}

SYSTEM = """You review posts from a crypto trading channel before a bot copies them.
A keyword parser has already read the latest post as a trade. Your job is to say
whether the author is actually telling followers to trade.

Actionable means the author instructs or invites followers to take a trade now,
to prepare a stated conditional entry, to change a stop/target, or to close a
position. Everything else is not actionable: commentary, opinions, stories about
past trades, reactions to news, and statements about what the author or others
should NOT do. Use the earlier posts only as context; judge the latest post.

Two verdicts are about earlier calls: "close" when the author exits or tells
followers to exit a position, and "cancel_setup" when the author says an earlier
conditional setup is void, invalidated or should no longer be taken. For both,
"symbol" is the asset of that position or setup.

If the author offers several assets as alternatives and followers should take
only one of them (e.g. "pick one of BTC or ETH"), list them in "one_of";
otherwise "one_of" is [].

Reply with ONE json object:
{"verdict": "enter_now" | "conditional_setup" | "adjust_exit" | "close" | "cancel_setup" | "not_actionable" | "unclear",
 "side": "buy" | "sell" | null,
 "symbol": "<ticker as the author means it>" | null,
 "one_of": ["<ticker>", ...],
 "reason": "<one sentence>"}"""


@dataclass
class GateDecision:
    allow: bool
    verdict: str
    side: Optional[str] = None
    symbol: Optional[str] = None
    reason: str = ""
    error: Optional[str] = None
    one_of: list[str] = field(default_factory=list)
    # allow | block | close | cancel | review (review_many only; review() leaves it "")
    action: str = ""
    samples: list[dict[str, Any]] = field(default_factory=list)
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

    one_of = sorted({str(x).upper().lstrip("$") for x in (out.get("one_of") or []) if x})
    p_sym = (parsed.get("symbol") or "").upper()
    if not out.get("symbol") and p_sym in one_of:
        out["symbol"] = p_sym  # "pick one of BTC or ETH": the model names the set, not one symbol
    allow, verdict, side, symbol, reasons = decide(out, parsed)
    return GateDecision(
        allow=allow, verdict=verdict, side=side, symbol=symbol, one_of=one_of,
        reason=(str(out.get("reason") or "") + (" | " + ";".join(reasons) if reasons else "")).strip(" |"),
        latency_ms=round((time.perf_counter() - t0) * 1000, 1), usage=data.get("usage") or {},
    )


def _action(d: GateDecision, parsed: dict[str, Any]) -> tuple:
    if d.error:
        return ("error",)
    if d.allow:
        return ("allow",)
    if d.verdict in ("close", "cancel_setup"):
        sym = d.symbol or (parsed.get("symbol") or "").upper() or None
        return ("close" if d.verdict == "close" else "cancel", sym)
    return ("block",)


def review_many(*, samples: int = 3, on_error: str = "skip", **kw: Any) -> GateDecision:
    """Ask `samples` times in parallel. The model is not deterministic even at
    temperature 0, so the outcome is used only when every answer agrees:
    action = allow | block | close | cancel. Any disagreement, or alternatives
    ("pick one of BTC or ETH") on an allowed entry, gives action = "review": a
    human decides. If every call errors, on_error applies; a minority of errors
    is ignored when at least two answers remain and agree."""
    from concurrent.futures import ThreadPoolExecutor

    t0 = time.perf_counter()
    parsed = kw["parsed"]
    if samples <= 1:
        ds = [review(on_error=on_error, **kw)]
    else:
        with ThreadPoolExecutor(max_workers=samples) as pool:
            ds = list(pool.map(lambda _: review(on_error=on_error, **kw), range(samples)))
    acts = [_action(d, parsed) for d in ds]
    ok = [(a, d) for a, d in zip(acts, ds) if a[0] != "error"]
    latency = round((time.perf_counter() - t0) * 1000, 1)
    sample_dicts = [{k: d.to_dict()[k] for k in ("allow", "verdict", "side", "symbol", "one_of", "reason", "error")}
                    for d in ds]
    if not ok:
        first = ds[0]
        return GateDecision(allow=on_error == "allow", verdict="error", error=first.error,
                            action="allow" if on_error == "allow" else "block",
                            samples=sample_dicts, latency_ms=latency)
    distinct = {a for a, _ in ok}
    rep = ok[0][1]
    one_of = sorted({s for _, d in ok for s in d.one_of})
    usage: dict[str, Any] = {}
    for d in ds:
        for k, v in (d.usage or {}).items():
            if isinstance(v, (int, float)):
                usage[k] = usage.get(k, 0) + v
    if len(distinct) > 1 or (samples > 1 and len(ok) < 2):
        action, reason = "review", "split:" + ",".join(sorted("/".join(str(x) for x in a) for a in distinct))
    elif ok[0][0][0] == "allow" and len(one_of) > 1:
        action, reason = "review", "alternatives:" + ",".join(one_of)
    else:
        action, reason = ok[0][0][0], rep.reason
    sym = ok[0][0][1] if ok[0][0][0] in ("close", "cancel") and len(distinct) == 1 else rep.symbol
    return GateDecision(allow=action == "allow", verdict=rep.verdict if len(distinct) == 1 else "split",
                        side=rep.side, symbol=sym, reason=reason, one_of=one_of, action=action,
                        samples=sample_dicts, latency_ms=latency, usage=usage)
