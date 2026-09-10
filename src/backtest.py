from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
import yaml

from .models import Intent, Side, Signal, Venue
from .parser import parse_message
from .risk import RiskGuard
from .util import DATA_DIR, ROOT, setup_logging

log = setup_logging()

HL_INFO = "https://api.hyperliquid.xyz/info"
GECKO = "https://api.geckoterminal.com/api/v2"
HL_TAKER = 0.00045
SOL_FEE = 0.01


def parse_ts(iso: str) -> Optional[float]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def load_posts() -> dict[str, list[dict]]:
    path = DATA_DIR / "channel_posts.json"
    return json.loads(path.read_text(encoding="utf-8"))


def load_cfg() -> dict:
    with (ROOT / "config.yaml").open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def collect_signals(cfg: dict, posts: dict[str, list[dict]], hl_universe: set[str]) -> list[Signal]:
    channels = {c["username"]: c for c in cfg.get("channels") or []}
    risk = RiskGuard(cfg.get("risk") or {}, channels)
    ctx: dict[str, dict] = {}
    out: list[Signal] = []
    for name, plist in posts.items():
        ch = channels.get(name) or {}
        inherit = bool(ch.get("inherit_context", True))
        for p in plist:
            c = ctx.get(name, {}) if inherit else {}
            s = parse_message(
                channel=name,
                message_id=p["id"],
                date=p.get("date") or "",
                text=p.get("text") or "",
                default_symbol=ch.get("default_symbol") or c.get("symbol"),
                default_mint=c.get("mint") if inherit else None,
                hl_universe=hl_universe,
            )
            boost = float(ch.get("confidence_boost") or 0)
            if boost and not s.skip_reason:
                s.confidence = max(0.0, min(0.99, s.confidence + boost))
            if s.mint:
                ctx[name] = {"mint": s.mint, "symbol": s.symbol}
            elif s.symbol:
                ctx[name] = {"symbol": s.symbol, "mint": None}
            blocked = risk.allow(s, updating=s.intent in {Intent.UPDATE, Intent.CLOSE})
            if blocked:
                s.skip_reason = blocked
                s.intent = Intent.IGNORE
            else:
                risk.mark_fill(s.key(), {"ts": s.date})
            out.append(s)
    return out


def hl_universe(http: httpx.Client) -> set[str]:
    r = http.post(HL_INFO, json={"type": "meta"}, timeout=20)
    r.raise_for_status()
    return {c["name"].upper() for c in (r.json().get("universe") or [])}


def hl_candles(http: httpx.Client, coin: str, start: float, end: float, interval: str = "1h") -> list[dict]:
    body = {
        "type": "candleSnapshot",
        "req": {
            "coin": coin,
            "interval": interval,
            "startTime": int(start * 1000),
            "endTime": int(end * 1000),
        },
    }
    r = http.post(HL_INFO, json=body, timeout=30)
    r.raise_for_status()
    rows = r.json() or []
    out = []
    for c in rows:
        out.append(
            {
                "t": int(c["t"]) / 1000.0,
                "o": float(c["o"]),
                "h": float(c["h"]),
                "l": float(c["l"]),
                "c": float(c["c"]),
            }
        )
    return out


def gecko_ohlcv(http: httpx.Client, mint: str, before_ts: float) -> list[dict]:
    pools = http.get(
        f"{GECKO}/networks/solana/tokens/{mint}/pools",
        params={"page": 1},
        timeout=20,
        headers={"Accept": "application/json"},
    )
    if pools.status_code != 200:
        return []
    data = pools.json().get("data") or []
    if not data:
        return []
    pool_id = data[0]["id"]  # e.g. solana_xxx
    pool_addr = pool_id.split("_", 1)[-1]
    r = http.get(
        f"{GECKO}/networks/solana/pools/{pool_addr}/ohlcv/hour",
        params={
            "aggregate": 1,
            "limit": 168,
            "currency": "usd",
            "before_timestamp": int(before_ts + 48 * 3600),
        },
        timeout=20,
        headers={"Accept": "application/json"},
    )
    if r.status_code != 200:
        return []
    ohlcv = (((r.json().get("data") or {}).get("attributes") or {}).get("ohlcv_list")) or []
    rows = []
    for item in ohlcv:
        # [ts, o, h, l, c, vol]
        rows.append({"t": float(item[0]), "o": float(item[1]), "h": float(item[2]), "l": float(item[3]), "c": float(item[4])})
    rows.sort(key=lambda x: x["t"])
    return rows


def first_after(candles: list[dict], ts: float) -> Optional[dict]:
    for c in candles:
        if c["t"] >= ts:
            return c
    return candles[-1] if candles else None


def candle_at_or_after(candles: list[dict], ts: float, hours: float) -> Optional[dict]:
    target = ts + hours * 3600
    last = None
    for c in candles:
        if c["t"] <= target:
            last = c
        elif last:
            return last
        else:
            return c
    return last


@dataclass
class Trade:
    channel: str
    message_id: int
    date: str
    venue: str
    symbol: str
    side: str
    intent: str
    size_usd: float
    entry_px: Optional[float]
    exit_px: Optional[float]
    entry_ts: Optional[float]
    exit_ts: Optional[float]
    pnl_usd: float
    ret: float
    reason: str
    text: str


def simulate(cfg: dict, signals: list[Signal], http: httpx.Client, now: float) -> list[Trade]:
    channels = {c["username"]: c for c in cfg.get("channels") or []}
    risk = RiskGuard(cfg.get("risk") or {}, channels)
    candle_cache: dict[str, list[dict]] = {}
    trades: list[Trade] = []

    actionable = [
        s
        for s in signals
        if s.intent in {Intent.MARKET, Intent.LIMIT, Intent.WAIT_TARGET, Intent.CLOSE}
        and not s.skip_reason
        and s.side in {Side.BUY, Side.SELL, Side.CLOSE}
    ]

    for s in actionable:
        ts = parse_ts(s.date) or now
        ch = channels.get(s.channel) or {}
        usd = risk.size_usd(s)
        sol = risk.size_sol(s)
        size = usd if s.venue != Venue.SOLANA else max(usd, sol * 150)  # sol notional fallback if no sol px yet
        text = (s.text or "")[:120]

        if s.venue == Venue.HYPERLIQUID:
            coin = (s.symbol or "").upper()
            if not coin:
                continue
            key = f"hl:{coin}"
            if key not in candle_cache:
                candle_cache[key] = hl_candles(http, coin, ts - 6 * 3600, now + 3600)
                time.sleep(0.15)
            candles = candle_cache[key]
            if not candles:
                trades.append(Trade(s.channel, s.message_id, s.date, "hyperliquid", coin, s.side.value, s.intent.value, usd, None, None, ts, None, 0, 0, "no_ohlcv", text))
                continue
            fill = None
            if s.intent == Intent.WAIT_TARGET or s.intent == Intent.LIMIT:
                trigger = s.trigger_price or s.limit_price
                horizon = ts + 7 * 86400
                for c in candles:
                    if c["t"] < ts:
                        continue
                    if c["t"] > horizon:
                        break
                    if trigger and s.side == Side.BUY and c["l"] <= trigger:
                        fill = {"t": c["t"], "px": trigger}
                        break
                    if trigger and s.side == Side.SELL and c["h"] >= trigger:
                        fill = {"t": c["t"], "px": trigger}
                        break
                if not fill:
                    trades.append(Trade(s.channel, s.message_id, s.date, "hyperliquid", coin, s.side.value, s.intent.value, usd, None, None, ts, None, 0, 0, "not_filled", text))
                    continue
            else:
                c0 = first_after(candles, ts)
                if not c0:
                    trades.append(Trade(s.channel, s.message_id, s.date, "hyperliquid", coin, s.side.value, s.intent.value, usd, None, None, ts, None, 0, 0, "no_ohlcv", text))
                    continue
                fill = {"t": c0["t"], "px": c0["c"]}

            entry = fill["px"]
            hold_h = 24 * 7
            sl_pct = 0.08
            tp_pct = 0.20
            if s.stop_loss and entry:
                sl_pct = abs(s.stop_loss - entry) / entry
            if s.take_profit and entry:
                # POPIGOGO 3000 target is BTC in thousands shorthand sometimes; skip insane TP
                if 0.02 < abs(s.take_profit - entry) / entry < 3:
                    tp_pct = abs(s.take_profit - entry) / entry
            exit_px = None
            exit_ts = None
            why = "time_stop"
            for c in candles:
                if c["t"] <= fill["t"]:
                    continue
                if s.side == Side.BUY:
                    if c["l"] <= entry * (1 - sl_pct):
                        exit_px, exit_ts, why = entry * (1 - sl_pct), c["t"], "stop"
                        break
                    if c["h"] >= entry * (1 + tp_pct):
                        exit_px, exit_ts, why = entry * (1 + tp_pct), c["t"], "take_profit"
                        break
                else:
                    if c["h"] >= entry * (1 + sl_pct):
                        exit_px, exit_ts, why = entry * (1 + sl_pct), c["t"], "stop"
                        break
                    if c["l"] <= entry * (1 - tp_pct):
                        exit_px, exit_ts, why = entry * (1 - tp_pct), c["t"], "take_profit"
                        break
                if c["t"] >= fill["t"] + hold_h * 3600:
                    exit_px, exit_ts, why = c["c"], c["t"], "time_stop"
                    break
            if exit_px is None:
                last = candles[-1]
                exit_px, exit_ts, why = last["c"], last["t"], "open_mark"
            sign = 1 if s.side == Side.BUY else -1
            ret = sign * (exit_px - entry) / entry - 2 * HL_TAKER
            pnl = usd * ret
            trades.append(Trade(s.channel, s.message_id, s.date, "hyperliquid", coin, s.side.value, s.intent.value, usd, entry, exit_px, fill["t"], exit_ts, pnl, ret, why, text))

        elif s.venue == Venue.SOLANA and s.mint:
            key = f"sol:{s.mint}"
            if key not in candle_cache:
                candle_cache[key] = gecko_ohlcv(http, s.mint, ts)
                time.sleep(0.35)
            candles = candle_cache[key]
            if not candles:
                trades.append(Trade(s.channel, s.message_id, s.date, "solana", s.symbol or s.mint[:6], s.side.value, s.intent.value, usd, None, None, ts, None, 0, 0, "no_ohlcv", text))
                continue
            c0 = first_after(candles, ts)
            if not c0:
                trades.append(Trade(s.channel, s.message_id, s.date, "solana", s.symbol or s.mint[:6], s.side.value, s.intent.value, usd, None, None, ts, None, 0, 0, "no_ohlcv", text))
                continue
            if s.intent in {Intent.WAIT_TARGET, Intent.LIMIT} and s.trigger_mcap:
                # no mcap series; skip unless already at target by using price proxy unavailable
                trades.append(Trade(s.channel, s.message_id, s.date, "solana", s.symbol or s.mint[:6], s.side.value, s.intent.value, usd, None, None, ts, None, 0, 0, "mcap_wait_unpriced", text))
                continue
            entry = c0["c"]
            notional = usd if usd > 0 else 12.0
            exit_px = None
            exit_ts = None
            why = "time_stop"
            sl_pct, tp_pct, hold_h = 0.45, 1.0, 24
            for c in candles:
                if c["t"] <= c0["t"]:
                    continue
                if c["l"] <= entry * (1 - sl_pct):
                    exit_px, exit_ts, why = entry * (1 - sl_pct), c["t"], "stop"
                    break
                if c["h"] >= entry * (1 + tp_pct):
                    exit_px, exit_ts, why = entry * (1 + tp_pct), c["t"], "take_profit"
                    break
                if c["t"] >= c0["t"] + hold_h * 3600:
                    exit_px, exit_ts, why = c["c"], c["t"], "time_stop"
                    break
            if exit_px is None:
                last = candles[-1]
                exit_px, exit_ts, why = last["c"], last["t"], "open_mark"
            ret = (exit_px - entry) / entry - 2 * SOL_FEE
            if s.side == Side.SELL:
                ret = -ret
            pnl = notional * ret
            trades.append(Trade(s.channel, s.message_id, s.date, "solana", s.symbol or s.mint[:8], s.side.value, s.intent.value, notional, entry, exit_px, c0["t"], exit_ts, pnl, ret, why, text))
    return trades


def summarize(trades: list[Trade]) -> dict[str, Any]:
    by = {}
    for t in trades:
        by.setdefault(t.channel, []).append(t)
    summary = {}
    for ch, rows in by.items():
        priced = [x for x in rows if x.entry_px]
        pnl = sum(x.pnl_usd for x in priced)
        wins = [x for x in priced if x.pnl_usd > 0]
        losses = [x for x in priced if x.pnl_usd < 0]
        summary[ch] = {
            "signals": len(rows),
            "filled": len(priced),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": (len(wins) / len(priced) if priced else 0),
            "pnl_usd": round(pnl, 2),
            "avg_ret": (sum(x.ret for x in priced) / len(priced) if priced else 0),
            "best": max((x.ret for x in priced), default=0),
            "worst": min((x.ret for x in priced), default=0),
        }
    priced = [x for x in trades if x.entry_px]
    summary["ALL"] = {
        "signals": len(trades),
        "filled": len(priced),
        "wins": sum(1 for x in priced if x.pnl_usd > 0),
        "losses": sum(1 for x in priced if x.pnl_usd < 0),
        "win_rate": (sum(1 for x in priced if x.pnl_usd > 0) / len(priced) if priced else 0),
        "pnl_usd": round(sum(x.pnl_usd for x in priced), 2),
        "avg_ret": (sum(x.ret for x in priced) / len(priced) if priced else 0),
    }
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(DATA_DIR / "backtest.json"))
    args = parser.parse_args()
    cfg = load_cfg()
    posts = load_posts()
    now = time.time()
    with httpx.Client(timeout=30.0, headers={"User-Agent": "tradelife-backtest"}) as http:
        universe = hl_universe(http)
        log.info("hl universe %s", len(universe))
        signals = collect_signals(cfg, posts, universe)
        actionable = [s for s in signals if not s.skip_reason and s.intent != Intent.IGNORE]
        log.info("signals %s actionable %s", len(signals), len(actionable))
        trades = simulate(cfg, signals, http, now)
    summary = summarize(trades)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "assumptions": {
            "meme_hold_hours": 24,
            "meme_sl": 0.45,
            "meme_tp": 1.0,
            "perp_hold_hours": 168,
            "perp_sl": 0.08,
            "perp_tp": 0.20,
            "hl_taker": HL_TAKER,
            "sol_fee": SOL_FEE,
            "entry": "next hourly close after message, or limit/trigger fill",
        },
        "summary": summary,
        "trades": [asdict(t) for t in trades],
        "actionable": [
            {
                "channel": s.channel,
                "id": s.message_id,
                "date": s.date,
                "side": s.side.value,
                "intent": s.intent.value,
                "venue": s.venue.value,
                "symbol": s.symbol,
                "mint": s.mint,
                "text": (s.text or "")[:180],
            }
            for s in signals
            if not s.skip_reason and s.intent != Intent.IGNORE
        ],
    }
    Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("--- trades ---")
    for t in trades:
        print(
            f"{t.date} {t.channel:22} {t.side:4} {t.symbol:10} {t.reason:16} "
            f"entry={t.entry_px} exit={t.exit_px} ret={t.ret*100:7.1f}% pnl={t.pnl_usd:7.2f} | {t.text[:70]}"
        )


if __name__ == "__main__":
    main()
