"""One month of POPIGOGO through the paper Engine: parser -> LLM gate -> chart v4 -> risk -> paper venue.

Same posts as month_replay (2026-09-14..10-14), in order, through `Engine.handle_text`
with dry_run=True. Isolation from the production engine:
  - Engine.start() / venue connect are never called; no Hyperliquid client exists.
  - JsonStore writes to <out-dir>/state.json, not data/state.json.
  - The "tradelife" logger writes to <out-dir>/engine.log, not logs/tradelife.log.
  - DEEPSEEK_* only are read from .env.
The risk guard runs on a clock set to each post's timestamp, so daily caps and
cooldowns follow the channel's real timing.

Not simulated: no historical prices, so breakout setups are only armed (never
triggered), fills happen at a stand-in mark, and positions never close by stop
or target. Marks are the same stand-ins chart_eval uses (BTC 80000, ETH 2500,
ZEC 1350); other symbols have no mark (no chart scale check, nominal size).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from src.lab.models.provider import REPO_ROOT, _read_env_file

POSTS = REPO_ROOT / "data" / "lab" / "popigogo_30d.json"
IMAGES = REPO_ROOT / "data" / "lab" / "popi_images"
UNIVERSE = REPO_ROOT / "data" / "lab" / "hl_universe.json"
OUT_DIR = REPO_ROOT / "data" / "lab" / "engine_replay"
CHANNEL = "POPIGOGO"
MARKS = {"BTC": 80000.0, "ETH": 2500.0, "ZEC": 1350.0}


class PostClock:
    """Stands in for the `time` module inside src.risk."""

    def __init__(self) -> None:
        self.ts = 0.0

    def time(self) -> float:
        return self.ts

    def strftime(self, fmt: str) -> str:
        return time.strftime(fmt, time.gmtime(self.ts))


def _isolate(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("tradelife")  # setup_logging() skips once handlers exist
    if not logger.handlers:
        fh = logging.FileHandler(out_dir / "engine.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(fh)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    import src.store

    src.store.DATA_DIR = out_dir
    for k, v in _read_env_file(REPO_ROOT / ".env").items():  # DEEPSEEK_* only
        os.environ.setdefault(k, v)
    os.environ.pop("LIVE_TRADING", None)


def run(out_dir: Path, limit: int | None) -> Path:
    _isolate(out_dir)
    import src.chart_reader as chart_reader
    import src.llm_gate as llm_gate
    import src.risk as risk_mod
    from src.engine import Engine

    state = out_dir / "state.json"
    state.unlink(missing_ok=True)
    rows_path = out_dir / "rows.jsonl"
    rows_path.unlink(missing_ok=True)

    cfg = yaml.safe_load((REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))
    cfg["dry_run"] = True
    eng = Engine(cfg, dry_run=True)
    assert eng.store.path == state, eng.store.path
    eng.hl.universe = {s.upper(): {"name": s, "szDecimals": 4}
                       for s in json.loads(UNIVERSE.read_text(encoding="utf-8"))["mainnet"]}
    eng.hl.mids = dict(MARKS)
    eng.hl.size_from_usd = lambda coin, usd: (usd / MARKS[coin]) if coin in MARKS else 1.0
    clock = PostClock()
    risk_mod.time = clock

    seen: dict[str, Any] = {}
    real_review, real_detail = llm_gate.review, chart_reader.read_levels_detail

    def review(**kw: Any) -> Any:
        d = real_review(**kw)
        seen["gate"] = d.to_dict()
        return d

    def detail(*a: Any, **kw: Any) -> dict[str, Any]:
        try:
            d = real_detail(*a, **kw)
        except Exception as exc:  # noqa: BLE001
            seen["chart"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
            raise
        seen["chart"] = {k: d.get(k) for k in ("entry", "stop_loss", "take_profit", "current_price", "rejected", "ocr")}
        return d

    llm_gate.review, chart_reader.read_levels_detail = review, detail
    posts = [p for p in json.loads(POSTS.read_text(encoding="utf-8")) if p["text"]]
    if limit:
        posts = posts[:limit]
    try:
        for n, p in enumerate(posts, 1):
            seen.clear()
            clock.ts = datetime.fromisoformat(p["date"]).timestamp()
            img = IMAGES / f"{p['id']}.jpg"
            image = str(img) if p["has_photo"] and img.exists() else None
            t0 = time.perf_counter()
            plan = eng.handle_text(CHANNEL, p["id"], p["date"], p["text"], image_path=image)
            sig = plan.signal if plan else None
            row = {
                "id": p["id"], "date": p["date"], "text": p["text"], "has_photo": p["has_photo"],
                "image": bool(image),
                "gate": seen.get("gate"), "chart": seen.get("chart"),
                "signal": None if sig is None else {
                    "intent": sig.intent.value, "side": sig.side.value, "symbol": sig.symbol,
                    "skip": sig.skip_reason, "reason": sig.reason, "confidence": round(sig.confidence, 2),
                    "trigger": sig.trigger_price, "stop_loss": sig.stop_loss, "take_profit": sig.take_profit,
                },
                "plan": None if plan is None else {"status": plan.status.value, "note": plan.note,
                                                   "result": plan.exchange_result},
                "sec": round(time.perf_counter() - t0, 1),
            }
            with rows_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            status = row["plan"]["status"] if row["plan"] else "-"
            if row["gate"] or status not in ("skipped", "-"):
                print(f"[{n}] #{p['id']} {status} {sig.intent.value if sig else ''} {sig.symbol if sig else ''} "
                      f"gate={(row['gate'] or {}).get('verdict')} skip={sig.skip_reason if sig else ''} {row['sec']}s",
                      flush=True)
    finally:
        llm_gate.review, chart_reader.read_levels_detail = real_review, real_detail
    return rows_path


ACTED = {"filled", "working", "pending"}


def _rule_acts(row: dict[str, Any]) -> bool:
    """Baseline: the current parser alone, with the engine's universe, boost and
    confidence floor (no gate, no chart reading, no risk caps)."""
    from src.parser import parse_message

    global _CH, _HL
    if "_CH" not in globals():
        cfg = yaml.safe_load((REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))
        _CH = next(c for c in cfg["channels"] if c["username"] == CHANNEL)
        _HL = set(s.upper() for s in json.loads(UNIVERSE.read_text(encoding="utf-8"))["mainnet"])
    sig = parse_message(channel=CHANNEL, message_id=row["id"], date=row["date"], text=row["text"],
                        default_symbol=_CH.get("default_symbol"), hl_universe=_HL,
                        other_assets=_CH.get("other_assets"))
    conf = min(0.99, sig.confidence + float(_CH.get("confidence_boost") or 0))
    return not sig.skip_reason and sig.intent.value != "ignore" and conf >= float(_CH.get("min_confidence") or 0)


def _armed_at_end(state: Path) -> list[dict[str, Any]]:
    """Pending plans left in the replay store (superseded reposts are gone)."""
    if not state.exists():
        return []
    plans = json.loads(state.read_text(encoding="utf-8")).get("plans") or {}
    return [{"id": p["signal"]["message_id"], "symbol": p["signal"]["symbol"], "side": p["signal"]["side"],
             "trigger": p["signal"]["trigger_price"], "stop_loss": p["signal"]["stop_loss"]}
            for p in plans.values()]


def summarize(rows_path: Path) -> dict[str, Any]:
    rows = [json.loads(x) for x in rows_path.read_text(encoding="utf-8").splitlines() if x.strip()]
    acted = [r for r in rows if r["plan"] and r["plan"]["status"] in ACTED]
    gated = [r for r in rows if r["gate"]]
    charts = [r for r in rows if r["chart"]]
    skips = Counter((r["signal"] or {}).get("skip") or "" for r in rows if r["plan"] and r["plan"]["status"] == "skipped")
    rule_acts = {r["id"] for r in rows if _rule_acts(r)}
    acted_ids = {r["id"] for r in acted}

    def brief(r: dict[str, Any]) -> dict[str, Any]:
        s = r["signal"] or {}
        return {"id": r["id"], "status": r["plan"]["status"], "intent": s.get("intent"), "side": s.get("side"),
                "symbol": s.get("symbol"), "trigger": s.get("trigger"), "stop_loss": s.get("stop_loss"),
                "take_profit": s.get("take_profit"), "gate": (r["gate"] or {}).get("verdict"),
                "text": r["text"][:120]}

    return {
        "posts": len(rows),
        "acted": len(acted),
        "armed_at_end": _armed_at_end(rows_path.parent / "state.json"),
        "acted_by_status": Counter(r["plan"]["status"] for r in acted),
        "acted_by_intent": Counter(r["signal"]["intent"] for r in acted),
        "entries_without_stop": sum(1 for r in acted if r["signal"]["side"] in ("buy", "sell")
                                    and r["signal"]["intent"] != "update" and r["signal"]["stop_loss"] is None),
        "gate": {"calls": len(gated), "allowed": sum(r["gate"]["allow"] for r in gated),
                 "verdicts": Counter(r["gate"]["verdict"] for r in gated),
                 "errors": sum(bool(r["gate"]["error"]) for r in gated)},
        "chart": {"reads": len(charts),
                  "stop_accepted": sum(1 for r in charts if r["chart"].get("stop_loss") is not None),
                  "errors": sum(1 for r in charts if r["chart"].get("error"))},
        "skips": dict(skips.most_common()),
        "vs_parser_only": {"parser_acts": len(rule_acts), "still_acts": len(rule_acts & acted_ids),
                         "removed": sorted(rule_acts - acted_ids), "new": sorted(acted_ids - rule_acts)},
        "actions": [brief(r) for r in acted],
        "minutes": round(sum(r["sec"] for r in rows) / 60, 1),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Month of POPIGOGO through the paper Engine")
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--summary-only", action="store_true")
    ap.add_argument("--report", type=Path, default=None)
    args = ap.parse_args(argv)
    rows_path = args.out_dir / "rows.jsonl"
    if not args.summary_only:
        rows_path = run(args.out_dir, args.limit)
    summary = summarize(rows_path)
    blob = json.dumps(summary, ensure_ascii=False, indent=1, default=str)
    if args.report:
        args.report.write_text(blob + "\n", encoding="utf-8")
    sys.stdout.write(blob + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
