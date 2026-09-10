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


def read_levels(
    image_path: str | Path,
    symbol: str,
    side: str,  # "buy" / "sell"
    timeframe: str = "8h",
) -> dict[str, Optional[float]]:
    """Return {"yellow_or_trigger", "stop_loss", "take_profit"} (floats or None)."""
    load_env()
    key = os.getenv("DEEPSEEK_API_KEY")
    base = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    model = os.getenv("DEEPSEEK_VISION_MODEL", "deepseek-flash")  # V4.1-Flash, multimodal
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY missing in .env")

    path = Path(image_path)
    if not path.exists():
        raise FileNotFoundError(image_path)

    long_short = "LONG (做多)" if side == "buy" else "SHORT (做空)"
    direction_rule = (
        "the stop loss must be BELOW the trigger"
        if side == "buy"
        else "the stop loss must be ABOVE the trigger"
    )
    prompt = (
        f"This is a {symbol} {long_short} setup chart, timeframe {timeframe}.\n"
        "Extract these drawn horizontal lines with exact prices:\n"
        "1. yellow_or_trigger: the YELLOW or ORANGE horizontal line (the breakout/breakdown trigger).\n"
        f"2. stop_loss: the RED line or top of the red shaded zone. For this setup {direction_rule}.\n"
        "3. take_profit: the GREEN or CYAN line (if none, null).\n"
        "Reply with ONLY a JSON object, no markdown fences, no extra text:\n"
        '{"yellow_or_trigger": <number|null>, "stop_loss": <number|null>, "take_profit": <number|null>}'
    )

    body = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": _to_data_url(path)}},
                ],
            }
        ],
        "temperature": 0,
        "max_tokens": 16000,
    }
    with httpx.Client(timeout=180) as http:
        r = http.post(f"{base.rstrip('/')}/chat/completions", headers={"Authorization": f"Bearer {key}"}, json=body)
    if r.status_code != 200:
        raise RuntimeError(f"vision HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    msg = data["choices"][0]["message"]
    content = (msg.get("content") or "").strip()
    if not content:
        content = (msg.get("reasoning_content") or "").strip()
    if not content:
        raise RuntimeError(f"empty vision reply: {json.dumps(data)[:300]}")

    parsed = _extract_json(content)
    out: dict[str, Optional[float]] = {}
    for field in ("yellow_or_trigger", "stop_loss", "take_profit"):
        v = parsed.get(field)
        value = float(v) if isinstance(v, (int, float)) else None
        out[field] = value if value is not None and math.isfinite(value) and value > 0 else None

    trigger = out["yellow_or_trigger"]
    if trigger and out["stop_loss"]:
        invalid_stop = (side == "buy" and out["stop_loss"] >= trigger) or (
            side != "buy" and out["stop_loss"] <= trigger
        )
        if invalid_stop:
            out["stop_loss"] = None
    if trigger and out["take_profit"]:
        invalid_target = (side == "buy" and out["take_profit"] <= trigger) or (
            side != "buy" and out["take_profit"] >= trigger
        )
        if invalid_target:
            out["take_profit"] = None
    return out
