"""Read a single chart image via DeepSeek vision and return trade levels.

Reused by the live engine to fill Signal.trigger_price / stop_loss / take_profit
from a chart attached to a Telegram message, replacing the recent-high/low proxy
in Engine._prepare_breakout.

This module is deliberately synchronous (httpx): the caller is expected to run it
inside asyncio.to_thread() so a slow vision call does not block the event loop.
"""

from __future__ import annotations

import base64
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Optional

import httpx

ROOT = Path(__file__).resolve().parents[1]


def load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _to_data_url(path: Path) -> str:
    data = base64.b64encode(path.read_bytes()).decode()
    return f"data:image/jpeg;base64,{data}"


def _extract_json(text: str) -> dict:
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"no JSON object in vision reply: {text[:200]}")
    return json.loads(m.group(0))


PROMPT_VERSION = "chart.v2"
# Reject a stop/target that sits on the current-price label (a common misread).
CURRENT_PRICE_TOL = 0.0015
# Reject all levels when the chart's price scale does not match the symbol.
SCALE_TOL = 0.4


def _prompt(symbol: str, side: str, timeframe: str) -> str:
    long_short = "LONG" if side == "buy" else "SHORT"
    return (
        f"The caption says this is a {symbol} {long_short} setup, timeframe {timeframe}. The image may or may "
        "not be a price chart.\n"
        "These charts use the TradingView long/short position tool. Read numbers ONLY from the price labels "
        "on the right axis:\n"
        "- entry: the GREY label at the boundary between the teal (profit) box and the red (loss) box. A short "
        "yellow line usually starts at the same price. If there is no label there, estimate from the axis.\n"
        "- stop_loss: the RED label at the far edge of the red box "
        f"({'bottom' if side == 'buy' else 'top'} edge).\n"
        "- take_profit: the label at the far edge of the teal box, only if a label is printed there; else null.\n"
        "- current_price: the label on the thin dotted horizontal line that runs across the chart. It is NOT the "
        "stop or the target; never reuse it for them.\n"
        "- Long yellow lines or shaded zones elsewhere are context, not the entry.\n"
        "If the image is not a price chart (illustration, P&L card, screenshot of an app), set is_price_chart to "
        "false and all prices to null.\n"
        "Reply with ONE json object: "
        '{"is_price_chart": true|false, "entry": <number|null>, "stop_loss": <number|null>, '
        '"take_profit": <number|null>, "current_price": <number|null>}'
    )


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) and f > 0 else None


def read_levels_detail(
    image_path: str | Path,
    symbol: str,
    side: str,
    timeframe: str = "8h",
    reference_price: Optional[float] = None,
) -> dict[str, Any]:
    """Vision read plus validation. `reference_price` is the symbol's current mark;
    when given, a chart on a different price scale is rejected."""
    load_env()
    key = os.getenv("DEEPSEEK_API_KEY")
    base = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    model = os.getenv("DEEPSEEK_VISION_MODEL", "deepseek-flash")  # V4.1-Flash, multimodal
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY missing in .env")

    path = Path(image_path)
    if not path.exists():
        raise FileNotFoundError(image_path)

    body = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _prompt(symbol, side, timeframe)},
                    {"type": "image_url", "image_url": {"url": _to_data_url(path)}},
                ],
            }
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "max_tokens": 16000,
    }
    with httpx.Client(timeout=180) as http:
        r = http.post(f"{base.rstrip('/')}/chat/completions", headers={"Authorization": f"Bearer {key}"}, json=body)
    if r.status_code != 200:
        raise RuntimeError(f"vision HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    choice = data["choices"][0]
    content = (choice["message"].get("content") or "").strip()
    if not content:
        # Do not parse reasoning text as an answer (v1 did, and misread it).
        raise RuntimeError(f"empty vision reply (finish_reason={choice.get('finish_reason')})")
    parsed = _extract_json(content)

    raw = {
        "entry": _num(parsed.get("entry", parsed.get("yellow_or_trigger"))),
        "stop_loss": _num(parsed.get("stop_loss")),
        "take_profit": _num(parsed.get("take_profit")),
        "current_price": _num(parsed.get("current_price")),
    }
    is_chart = parsed.get("is_price_chart", True) is not False
    out = dict(raw)
    rejected: list[str] = []

    if not is_chart:
        out.update(entry=None, stop_loss=None, take_profit=None)
        rejected.append("not_a_price_chart")
    entry, cur = out["entry"], out["current_price"]
    if reference_price and entry and abs(entry - reference_price) / reference_price > SCALE_TOL:
        out.update(entry=None, stop_loss=None, take_profit=None)
        rejected.append(f"price_scale_mismatch:{entry}vs{reference_price}")
        entry = None
    for field in ("stop_loss", "take_profit"):
        v = out[field]
        if v and cur and abs(v - cur) / cur <= CURRENT_PRICE_TOL:
            out[field] = None
            rejected.append(f"{field}_equals_current_price")
    if entry and out["stop_loss"]:
        if (side == "buy" and out["stop_loss"] >= entry) or (side != "buy" and out["stop_loss"] <= entry):
            out["stop_loss"] = None
            rejected.append("stop_on_wrong_side")
    if entry and out["take_profit"]:
        if (side == "buy" and out["take_profit"] <= entry) or (side != "buy" and out["take_profit"] >= entry):
            out["take_profit"] = None
            rejected.append("target_on_wrong_side")
    return {
        **out,
        "raw": raw,
        "is_price_chart": is_chart,
        "rejected": rejected,
        "prompt_version": PROMPT_VERSION,
        "usage": data.get("usage") or {},
    }


def read_levels(
    image_path: str | Path,
    symbol: str,
    side: str,  # "buy" / "sell"
    timeframe: str = "8h",
    reference_price: Optional[float] = None,
) -> dict[str, Optional[float]]:
    """Return {"yellow_or_trigger", "stop_loss", "take_profit"} (floats or None)."""
    d = read_levels_detail(image_path, symbol, side, timeframe, reference_price)
    return {"yellow_or_trigger": d["entry"], "stop_loss": d["stop_loss"], "take_profit": d["take_profit"]}
