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
# Vision and OCR must read the same printed number (labels are exact strings;
# 0.05% let a one-dollar digit slip through on a 2345.73 stop).
AGREE_TOL = 0.000001


def _agrees(a: Optional[float], b: Optional[float]) -> bool:
    return a is not None and b is not None and abs(a - b) / b <= AGREE_TOL


# In "ocr_primary" mode the vision read only has to land this close to the OCR
# label it is confirming (fixed before the held-out check, not tuned on it).
ROLE_TOL = 0.01
# OCR is the source of the number in that mode, so it must be a confident read.
OCR_PRIMARY_MIN_VOTES = 3


def _nearest(value: Optional[float], candidates: list[float]) -> Optional[float]:
    if value is None or not candidates:
        return None
    return min(candidates, key=lambda c: abs(c - value))


def ocr_primary_gate(
    levels: dict[str, Any], ocr: dict[str, Any]
) -> tuple[dict[str, Optional[float]], list[str]]:
    """Numbers from OCR; the vision read only confirms which label is which.

    Entry: the grey/blue OCR label nearest the vision entry, within ROLE_TOL.
    Stop: the outermost red OCR label, which must also be the red label nearest the
    vision stop, within ROLE_TOL. Target: nearest green label within ROLE_TOL, else
    none. Fails closed if entry or stop is unconfirmed.
    """
    reasons: list[str] = []
    entry_v, stop_v, target_v = levels.get("entry"), levels.get("stop_loss"), levels.get("take_profit")
    entry = _nearest(entry_v, ocr["entry_candidates"])
    if entry is None or abs(entry - entry_v) / entry > ROLE_TOL:
        reasons.append(f"entry_role_unconfirmed:vision={entry_v},ocr={ocr['entry_candidates']}")
        entry = None
    stop = ocr["stop_loss"]
    near = _nearest(stop_v, ocr.get("stop_candidates") or [])
    if stop is None or near != stop or abs(stop - stop_v) / stop > ROLE_TOL:
        reasons.append(f"stop_role_unconfirmed:vision={stop_v},ocr={stop}")
        stop = None
    if entry is None or stop is None:
        return {"entry": None, "stop_loss": None, "take_profit": None}, reasons
    target = _nearest(target_v, ocr["target_candidates"])
    if target is not None and abs(target - target_v) / target > ROLE_TOL:
        reasons.append(f"target_role_unconfirmed:vision={target_v}")
        target = None
    return {"entry": entry, "stop_loss": stop, "take_profit": target}, reasons


def ocr_gate(levels: dict[str, Any], ocr: dict[str, Any]) -> tuple[dict[str, Optional[float]], list[str]]:
    """Keep entry/stop only if OCR read the same number; target is optional.

    Fails closed: if entry or stop is not confirmed, all levels are dropped, so a
    caller never trades on a half-verified chart.
    """
    reasons: list[str] = []
    entry, stop, target = levels.get("entry"), levels.get("stop_loss"), levels.get("take_profit")
    entry_ok = entry is not None and any(_agrees(entry, c) for c in ocr["entry_candidates"])
    stop_ok = _agrees(stop, ocr["stop_loss"])
    if not entry_ok:
        reasons.append(f"entry_unconfirmed:vision={entry},ocr={ocr['entry_candidates']}")
    if not stop_ok:
        reasons.append(f"stop_unconfirmed:vision={stop},ocr={ocr['stop_loss']}")
    if not (entry_ok and stop_ok):
        return {"entry": None, "stop_loss": None, "take_profit": None}, reasons
    if target is not None and not any(_agrees(target, c) for c in ocr["target_candidates"]):
        reasons.append(f"target_unconfirmed:vision={target}")
        target = None
    return {"entry": entry, "stop_loss": stop, "take_profit": target}, reasons


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
    require_ocr: bool = False,
    ocr_mode: Optional[str] = None,
) -> dict[str, Any]:
    """Vision read plus validation. `reference_price` is the symbol's current mark;
    when given, a chart on a different price scale is rejected. With `require_ocr`,
    entry and stop must also match an independent OCR read (fails closed when
    tesseract is missing)."""
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
    mode = ocr_mode or ("agree" if require_ocr else None)
    ocr_info: Optional[dict[str, Any]] = None
    if mode and any(out[k] is not None for k in ("entry", "stop_loss", "take_profit")):
        from .chart_ocr import ocr_levels, read_axis_labels, tesseract_available

        if not tesseract_available():
            out.update(entry=None, stop_loss=None, take_profit=None)
            rejected.append("ocr_unavailable")
        else:
            ocr_info = ocr_levels(read_axis_labels(path), side,
                                  min_votes=OCR_PRIMARY_MIN_VOTES if mode == "ocr_primary" else 1)
            gated, why = (ocr_primary_gate if mode == "ocr_primary" else ocr_gate)(out, ocr_info)
            rejected.extend(why)
            rejected.extend(_post_checks(gated, side, out.get("current_price"), reference_price))
            out.update(gated)
    return {
        **out,
        "raw": raw,
        "ocr": None if ocr_info is None else {k: v for k, v in ocr_info.items() if k != "labels"},
        "is_price_chart": is_chart,
        "rejected": rejected,
        "prompt_version": PROMPT_VERSION,
        "usage": data.get("usage") or {},
    }


def _post_checks(
    levels: dict[str, Optional[float]], side: str, current: Optional[float], reference: Optional[float]
) -> list[str]:
    """Re-run the side/current-price/scale checks on the gated (possibly OCR) values."""
    why: list[str] = []
    entry, stop, target = levels["entry"], levels["stop_loss"], levels["take_profit"]
    if entry is None or stop is None:
        return why
    long = side == "buy"
    bad = (
        (long and stop >= entry) or (not long and stop <= entry)
        or (current is not None and abs(stop - current) / current <= CURRENT_PRICE_TOL)
        or (reference and abs(entry - reference) / reference > SCALE_TOL)
    )
    if bad:
        levels.update(entry=None, stop_loss=None, take_profit=None)
        why.append("post_check_failed")
        return why
    if target is not None and ((long and target <= entry) or (not long and target >= entry)):
        levels["take_profit"] = None
        why.append("target_on_wrong_side")
    return why


def read_levels(
    image_path: str | Path,
    symbol: str,
    side: str,  # "buy" / "sell"
    timeframe: str = "8h",
    reference_price: Optional[float] = None,
    require_ocr: bool = False,
    ocr_mode: Optional[str] = None,
) -> dict[str, Optional[float]]:
    """Return {"yellow_or_trigger", "stop_loss", "take_profit"} (floats or None)."""
    d = read_levels_detail(image_path, symbol, side, timeframe, reference_price, require_ocr, ocr_mode)
    return {"yellow_or_trigger": d["entry"], "stop_loss": d["stop_loss"], "take_profit": d["take_profit"]}
