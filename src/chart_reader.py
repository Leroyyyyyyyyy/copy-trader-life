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


ROLES_PROMPT_VERSION = "chart.roles.v1"
ROLE_MIN_VOTES = OCR_PRIMARY_MIN_VOTES


def _roles_prompt(symbol: str, side: str, timeframe: str, labels: list[dict[str, Any]], height: int) -> str:
    long_short = "LONG" if side == "buy" else "SHORT"
    rows = "\n".join(
        f"  [{i}] {lb['colour']} box at {round(100 * lb['y'] / max(1, height))}% from the top, OCR read: {lb['value']}"
        for i, lb in enumerate(labels)
    )
    return (
        f"The caption says this is a {symbol} {long_short} setup, timeframe {timeframe}. The image may or may "
        "not be a price chart.\n"
        "OCR found these coloured price labels on the right axis (top to bottom):\n"
        f"{rows}\n"
        "These charts use the TradingView long/short position tool. Say which label is which, by index:\n"
        "- entry: the GREY label at the boundary between the teal (profit) box and the red (loss) box, where a "
        "short yellow line usually starts.\n"
        f"- stop_loss: the RED label at the far edge of the red box ({'bottom' if side == 'buy' else 'top'} edge).\n"
        "- take_profit: the label at the far edge of the teal box, or null.\n"
        "- current_price: the label on the thin dotted horizontal line across the chart, or null. It is never the "
        "stop or the target.\n"
        "Do not read the numbers yourself. If an OCR number you assign is clearly different from what the image "
        "shows, list that index in misread.\n"
        "If the image is not a price chart, set is_price_chart to false and all roles to null.\n"
        "Reply with ONE json object: "
        '{"is_price_chart": true|false, "entry": <index|null>, "stop_loss": <index|null>, '
        '"take_profit": <index|null>, "current_price": <index|null>, "misread": [<index>, ...]}'
    )


def _vision_json(prompt: str, path: Path, retries: int = 1) -> tuple[dict[str, Any], dict[str, Any]]:
    """One vision call returning (parsed json, usage). An empty reply (the model spent
    max_tokens reasoning) or bad JSON is retried; then it raises."""
    load_env()
    key = os.getenv("DEEPSEEK_API_KEY")
    base = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    model = os.getenv("DEEPSEEK_VISION_MODEL", "deepseek-flash")
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY missing in .env")
    body = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": _to_data_url(path)}}]}],
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "max_tokens": 16000,
    }
    err = ""
    for _ in range(1 + retries):
        with httpx.Client(timeout=180) as http:
            r = http.post(f"{base.rstrip('/')}/chat/completions", headers={"Authorization": f"Bearer {key}"}, json=body)
        if r.status_code != 200:
            err = f"vision HTTP {r.status_code}"
            continue
        data = r.json()
        choice = data["choices"][0]
        content = (choice["message"].get("content") or "").strip()
        if not content:
            err = f"empty vision reply (finish_reason={choice.get('finish_reason')})"
            continue
        try:
            return _extract_json(content), data.get("usage") or {}
        except ValueError as exc:
            err = str(exc)[:200]
    raise RuntimeError(err)


def _idx(v: Any, n: int) -> Optional[int]:
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < n else None


def read_levels_roles(
    image_path: str | Path,
    symbol: str,
    side: str,
    timeframe: str = "8h",
    reference_price: Optional[float] = None,
    samples: int = 2,
) -> dict[str, Any]:
    """OCR reads every coloured axis label; the vision model only says which label is
    entry / stop / target / current price (by index), `samples` times in parallel.

    - Any sample failing (after one retry) -> no levels ("vision_failed").
    - Samples disagreeing on chart / entry / stop -> no levels and `review` is set,
      with each reading, so a human can pick.
    - The stop must be a red label read by >= ROLE_MIN_VOTES OCR variants and the
      outermost red box; the entry a grey/blue label with as many votes; a label the
      model flags as misread is not used. Then side / current-price / scale checks.
    """
    from concurrent.futures import ThreadPoolExecutor

    from PIL import Image

    from .chart_ocr import ocr_levels, read_axis_labels, tesseract_available

    path = Path(image_path)
    if not path.exists():
        raise FileNotFoundError(image_path)
    none: dict[str, Optional[float]] = {"entry": None, "stop_loss": None, "take_profit": None, "current_price": None}
    base_out = {"prompt_version": ROLES_PROMPT_VERSION, "review": None, "samples": [], "usage": {}}
    if not tesseract_available():
        return {**none, **base_out, "rejected": ["ocr_unavailable"], "ocr": None}
    labels = read_axis_labels(path)
    ocr = ocr_levels(labels, side, min_votes=ROLE_MIN_VOTES)
    ocr_brief = {k: v for k, v in ocr.items() if k != "labels"}
    if not any(lb["value"] is not None for lb in labels):
        return {**none, **base_out, "rejected": ["no_ocr_labels"], "ocr": ocr_brief}
    with Image.open(path) as im:
        height = im.size[1]
    prompt = _roles_prompt(symbol, side, timeframe, labels, height)

    def one(_: int) -> dict[str, Any]:
        try:
            parsed, usage = _vision_json(prompt, path)
        except Exception as exc:  # noqa: BLE001 - reported per sample
            return {"error": f"{type(exc).__name__}: {exc}"[:200]}
        n = len(labels)
        return {"is_price_chart": parsed.get("is_price_chart", True) is not False,
                **{k: _idx(parsed.get(k), n) for k in ("entry", "stop_loss", "take_profit", "current_price")},
                "misread": [i for i in (parsed.get("misread") or []) if _idx(i, n) is not None],
                "usage": usage}

    with ThreadPoolExecutor(max_workers=max(1, samples)) as pool:
        reads = list(pool.map(one, range(max(1, samples))))
    usage: dict[str, Any] = {}
    for rd in reads:
        for k, v in (rd.get("usage") or {}).items():
            if isinstance(v, (int, float)):
                usage[k] = usage.get(k, 0) + v
    base_out.update(samples=[{k: v for k, v in rd.items() if k != "usage"} for rd in reads], usage=usage)

    def value(i: Optional[int]) -> Optional[float]:
        return None if i is None else labels[i]["value"]

    if any("error" in rd for rd in reads):
        return {**none, **base_out, "rejected": ["vision_failed"], "ocr": ocr_brief}
    keys = {(rd["is_price_chart"], rd["entry"], rd["stop_loss"]) for rd in reads}
    if len(keys) > 1:
        def problems(rd: dict[str, Any]) -> list[str]:
            why = []
            for role, i, colours in (("entry", rd["entry"], ("grey", "blue")), ("stop_loss", rd["stop_loss"], ("red",))):
                if i is None:
                    why.append(f"{role}:none")
                    continue
                if labels[i]["colour"] not in colours:
                    why.append(f"{role}:colour_{labels[i]['colour']}")
                if labels[i]["votes"] < ROLE_MIN_VOTES:
                    why.append(f"{role}:ocr_votes_{labels[i]['votes']}")
                if i in rd["misread"]:
                    why.append(f"{role}:flagged_misread")
            if rd["stop_loss"] is not None and value(rd["stop_loss"]) != ocr["stop_loss"]:
                why.append("stop_loss:not_the_outermost_red_label")
            if rd["stop_loss"] is not None and rd["stop_loss"] == rd["current_price"]:
                why.append("stop_loss:is_current_price")
            return why

        options = [{"entry": value(rd["entry"]), "stop_loss": value(rd["stop_loss"]),
                    "take_profit": value(rd["take_profit"]), "is_price_chart": rd["is_price_chart"],
                    "fails_checks": problems(rd)} for rd in reads]
        return {**none, **base_out, "rejected": ["vision_samples_disagree"], "ocr": ocr_brief,
                "review": {"reason": "chart reads disagree", "options": options}}
    rd = reads[0]
    if not rd["is_price_chart"]:
        return {**none, **base_out, "rejected": ["not_a_price_chart"], "ocr": ocr_brief}
    rejected: list[str] = []
    misread = set().union(*(set(r["misread"]) for r in reads))
    e, sl, tp, cur = rd["entry"], rd["stop_loss"], rd["take_profit"], rd["current_price"]
    if e is None or labels[e]["colour"] not in ("grey", "blue") or labels[e]["votes"] < ROLE_MIN_VOTES or e in misread:
        rejected.append(f"entry_label_unusable:{e}")
    if (sl is None or labels[sl]["colour"] != "red" or labels[sl]["votes"] < ROLE_MIN_VOTES or sl in misread
            or sl == cur or value(sl) != ocr["stop_loss"]):
        rejected.append(f"stop_label_unusable:{sl},outermost_red={ocr['stop_loss']}")
    if rejected:
        return {**none, **base_out, "rejected": rejected, "ocr": ocr_brief}
    target = None
    if tp is not None and labels[tp]["colour"] == "green" and labels[tp]["votes"] >= ROLE_MIN_VOTES \
            and tp not in misread and tp != cur and all(r["take_profit"] == tp for r in reads):
        target = value(tp)
    elif tp is not None:
        rejected.append(f"target_label_unusable:{tp}")
    levels = {"entry": value(e), "stop_loss": value(sl), "take_profit": target}
    rejected += _post_checks(levels, side, value(cur), reference_price)
    return {**levels, "current_price": value(cur), **base_out, "rejected": rejected, "ocr": ocr_brief}


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
