"""Read original chart images with DeepSeek vision and merge levels into levels.json.

Usage:
  python -m src.read_charts_vision --id 6190          # smoke test one chart
  python -m src.read_charts_vision                     # all 7 setup charts
  python -m src.read_charts_vision --no-merge          # print only, don't write levels.json
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
CHARTS = ROOT / "data" / "charts"
LEVELS = CHARTS / "levels.json"

# post id -> (image path, symbol, side, timeframe)
SETUPS: dict[str, dict] = {
    "5718": {"img": "orig_5718.jpg", "symbol": "BTC", "side": "short", "tf": "4h"},
    "6014": {"img": "orig_6014.jpg", "symbol": "ETH", "side": "short", "tf": "6h"},
    "6059": {"img": "orig_6059.jpg", "symbol": "BTC", "side": "short", "tf": "8h"},
    "6190": {"img": "orig_6190.jpg", "symbol": "BTC", "side": "short", "tf": "12h"},
    "6361": {"img": "orig_6361.jpg", "symbol": "BTC", "side": "short", "tf": "1d"},
    "7154": {"img": "orig_7154.jpg", "symbol": "BTC", "side": "short", "tf": "12h"},
    "7217": {"img": "orig_7217.jpg", "symbol": "ETH", "side": "long", "tf": "8h"},
}

SYSTEM = (
    "You read cryptocurrency trading charts. The image is a TradingView chart screenshot "
    "posted in a Telegram signals channel. Extract ONLY the horizontal price levels drawn "
    "on the chart and the price-axis labels. Be precise and conservative: if a line is "
    "ambiguous or a number is unreadable, use null and explain in notes. Do NOT guess "
    "round numbers just to fill fields."
)

PROMPT_TMPL = """Chart for post #{pid}: {symbol} {side} setup, timeframe {tf}.

Extract these from the image:
1. yellow_or_trigger: the YELLOW or ORANGE horizontal line (the breakout/breakdown trigger the author says "涨过/跌过").
2. stop_loss: the RED horizontal line (invalidation). For a short it is ABOVE price, for a long it is BELOW price.
3. take_profit: the GREEN or CYAN horizontal line (target). If multiple, give the nearest one and list others in axis_lines.
4. axis_lines: ALL non-round price numbers printed on the right price axis (e.g. 68792.95, 68633.18). These are usually drawn-line prices. Round grid labels (68000.00, 69000.00) can be included but mark them as grid.
5. kind: "short" or "long" (just confirm the given side if visible on the chart).

Reply with ONLY a JSON object, no markdown fences, no extra text:
{{"kind": "...", "symbol": "...", "timeframe": "...", "yellow_or_trigger": null, "stop_loss": null, "take_profit": null, "axis_lines": [], "notes": "..."}}"""


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


def to_data_url(path: Path) -> str:
    data = base64.b64encode(path.read_bytes()).decode()
    return f"data:image/jpeg;base64,{data}"


def extract_json(text: str) -> dict:
    text = text.strip()
    # strip code fences
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"no JSON object in reply: {text[:200]}")
    return json.loads(m.group(0))


def call_vision(http: httpx.Client, key: str, base: str, model: str, pid: str, meta: dict, image: Path) -> dict:
    prompt = PROMPT_TMPL.format(pid=pid, symbol=meta["symbol"], side=meta["side"], tf=meta["tf"])
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": to_data_url(image)}},
                ],
            },
        ],
        "temperature": 0,
        "max_tokens": 16000,
    }
    r = http.post(f"{base.rstrip('/')}/chat/completions", headers={"Authorization": f"Bearer {key}"}, json=body, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:400]}")
    data = r.json()
    msg = data["choices"][0]["message"]
    content = (msg.get("content") or "").strip()
    if not content:
        content = (msg.get("reasoning_content") or "").strip()
    if not content:
        raise RuntimeError(f"empty reply: {json.dumps(data)[:400]}")
    return extract_json(content)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", help="only process this post id")
    ap.add_argument("--no-merge", action="store_true", help="print results, do not write levels.json")
    args = ap.parse_args()

    load_env()
    key = os.getenv("DEEPSEEK_API_KEY")
    base = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    model = os.getenv("DEEPSEEK_VISION_MODEL", "deepseek-flash")  # V4.1-Flash, multimodal
    if not key:
        raise SystemExit("DEEPSEEK_API_KEY missing in .env")

    ids = [args.id] if args.id else list(SETUPS)
    http = httpx.Client(timeout=120)

    # load existing levels (keep _meta and any manual overrides)
    existing = json.loads(LEVELS.read_text(encoding="utf-8")) if LEVELS.exists() else {}

    merged: dict = {}
    for pid in ids:
        meta = SETUPS[pid]
        img = CHARTS / meta["img"]
        if not img.exists():
            print(f"#{pid}: missing {img.name}, skip")
            continue
        print(f"#{pid} {meta['symbol']} {meta['side']} tf={meta['tf']} reading {img.name}…")
        try:
            parsed = call_vision(http, key, base, model, pid, meta, img)
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL: {exc}")
            continue
        # force the known symbol/side/tf from setup (model may misread)
        parsed["symbol"] = meta["symbol"]
        parsed["timeframe"] = meta["tf"]
        parsed.setdefault("kind", "short" if meta["side"] == "sell" else "long")
        parsed["source"] = f"deepseek_vision:{meta['img']}"
        # keep prior notes if useful
        prev = existing.get(pid) or {}
        merged[pid] = {**prev, **{k: v for k, v in parsed.items() if v is not None}}
        print(f"  -> {json.dumps(parsed, ensure_ascii=False)}")

    if args.id:
        return

    if merged and not args.no_merge:
        # preserve _meta and non-setup entries
        out = {k: v for k, v in existing.items() if k != "_meta"}
        out.update(merged)
        out["_meta"] = existing.get("_meta", {})
        out["_meta"]["note"] = (
            "levels refreshed from Telegram ORIGINAL images via deepseek vision; "
            "prior OCR-based numbers are kept where the model returned null."
        )
        LEVELS.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nmerged into {LEVELS}")


if __name__ == "__main__":
    main()
