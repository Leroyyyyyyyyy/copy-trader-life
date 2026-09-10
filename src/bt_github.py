"""Event-driven backtest of Telegram signals via kernc/backtesting.py."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
import pandas as pd
import yaml
from backtesting import Strategy
from backtesting.lib import FractionalBacktest

from .parser import parse_message
from .models import Intent, Side, Venue
from .risk import RiskGuard
from .util import DATA_DIR, ROOT, setup_logging

log = setup_logging()

HL_INFO = "https://api.hyperliquid.xyz/info"
GECKO = "https://api.geckoterminal.com/api/v2"


def _ts(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)


class SignalBarStrategy(Strategy):
    """One instrument. Signals keyed by UTC timestamp of the bar we trade on."""

    signal_map: dict = {}
    sl_pct: float = 0.08
    tp_pct: float = 0.20

    def init(self):
        return

    def next(self):
        ts = pd.Timestamp(self.data.index[-1])
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        sig = self.signal_map.get(ts)
        if not sig:
            return
        price = float(self.data.Close[-1])
        sl = sig.get("sl_pct", self.sl_pct)
        tp = sig.get("tp_pct", self.tp_pct)
        size = float(sig.get("size_frac", 0.05))
        size = min(max(size, 0.001), 0.99)
        if sig["side"] == "buy":
            if self.position.is_short:
                self.position.close()
            self.buy(size=size, sl=price * (1 - sl), tp=price * (1 + tp), tag=sig.get("tag"))
        elif sig["side"] == "sell":
            if self.position.is_long:
                self.position.close()
            self.sell(size=size, sl=price * (1 + sl), tp=price * (1 - tp), tag=sig.get("tag"))
        elif sig["side"] == "close":
            self.position.close()


def gecko_pool(http: httpx.Client, mint: str) -> Optional[str]:
    for _ in range(4):
        r = http.get(f"{GECKO}/networks/solana/tokens/{mint}/pools", params={"page": 1})
        if r.status_code == 429:
            time.sleep(1.2)
            continue
        if r.status_code != 200:
            return None
        data = r.json().get("data") or []
        if not data:
            return None
        return data[0]["id"].split("_", 1)[-1]
    return None


def gecko_ohlcv(http: httpx.Client, mint: str, kind: str = "hour", before: Optional[int] = None, limit: int = 168) -> pd.DataFrame:
    pool = gecko_pool(http, mint)
    if not pool:
        return pd.DataFrame()
    params: dict[str, Any] = {"aggregate": 1, "limit": limit, "currency": "usd"}
    if before:
        params["before_timestamp"] = int(before)
    for _ in range(4):
        r = http.get(f"{GECKO}/networks/solana/pools/{pool}/ohlcv/{kind}", params=params)
        if r.status_code == 429:
            time.sleep(1.2)
            continue
        if r.status_code != 200:
            return pd.DataFrame()
        lst = (((r.json().get("data") or {}).get("attributes") or {}).get("ohlcv_list")) or []
        rows = []
        for i in lst:
            rows.append(
                {
                    "Date": datetime.fromtimestamp(float(i[0]), tz=timezone.utc),
                    "Open": float(i[1]),
                    "High": float(i[2]),
                    "Low": float(i[3]),
                    "Close": float(i[4]),
                    "Volume": float(i[5]) if len(i) > 5 else 0.0,
                }
            )
        df = pd.DataFrame(rows).drop_duplicates("Date").sort_values("Date").set_index("Date")
        return df
    return pd.DataFrame()


def gecko_ohlcv_range(
    http: httpx.Client,
    mint: str,
    start: datetime,
    end: datetime,
    kind: str = "minute",
    page_limit: int = 1000,
) -> pd.DataFrame:
    """Page Gecko OHLCV backwards until the window covers start..end."""
    frames = []
    cursor = int(end.timestamp()) + 60
    seen = set()
    for _ in range(24):
        page = gecko_ohlcv(http, mint, kind, before=cursor, limit=page_limit)
        time.sleep(0.35)
        if page.empty:
            break
        frames.append(page)
        oldest = int(page.index.min().timestamp())
        if oldest in seen:
            break
        seen.add(oldest)
        if oldest <= int(start.timestamp()) - 60:
            break
        # step strictly backward so Gecko does not return the same page
        cursor = oldest - 60
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames).sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return df.loc[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]


def hl_ohlcv(http: httpx.Client, coin: str, start: datetime, end: datetime, interval: str = "1h") -> pd.DataFrame:
    r = http.post(
        HL_INFO,
        json={
            "type": "candleSnapshot",
            "req": {
                "coin": coin,
                "interval": interval,
                "startTime": int(start.timestamp() * 1000),
                "endTime": int(end.timestamp() * 1000),
            },
        },
        timeout=30,
    )
    r.raise_for_status()
    rows = []
    for c in r.json() or []:
        rows.append(
            {
                "Date": datetime.fromtimestamp(int(c["t"]) / 1000, tz=timezone.utc),
                "Open": float(c["o"]),
                "High": float(c["h"]),
                "Low": float(c["l"]),
                "Close": float(c["c"]),
                "Volume": float(c.get("v") or 0),
            }
        )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).drop_duplicates("Date").sort_values("Date").set_index("Date")


def hl_ohlcv_range(http: httpx.Client, coin: str, start: datetime, end: datetime, interval: str = "1m") -> pd.DataFrame:
    step = timedelta(hours=12) if interval == "1m" else timedelta(days=14)
    frames = []
    cur = start
    while cur < end:
        nxt = min(cur + step, end)
        try:
            page = hl_ohlcv(http, coin, cur, nxt, interval)
        except Exception as exc:  # noqa: BLE001
            log.warning("hl ohlcv chunk failed %s %s-%s: %s", coin, cur, nxt, exc)
            page = pd.DataFrame()
        if not page.empty:
            frames.append(page)
        cur = nxt
        time.sleep(0.12)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames).sort_index()
    return df[~df.index.duplicated(keep="last")]


def hl_universe(http: httpx.Client) -> set[str]:
    r = http.post(HL_INFO, json={"type": "meta"}, timeout=20)
    r.raise_for_status()
    return {c["name"].upper() for c in (r.json().get("universe") or [])}


def collect_actionable(cfg: dict, posts: dict, universe: set[str]) -> list[dict]:
    channels = {c["username"]: c for c in cfg.get("channels") or []}
    risk = RiskGuard(cfg.get("risk") or {}, channels)
    ctx: dict[str, dict] = {}
    out = []
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
                hl_universe=universe,
            )
            boost = float(ch.get("confidence_boost") or 0)
            if boost and not s.skip_reason:
                s.confidence = max(0.0, min(0.99, s.confidence + boost))
            if s.mint:
                ctx[name] = {"mint": s.mint, "symbol": s.symbol}
            elif s.symbol:
                ctx[name] = {"symbol": s.symbol, "mint": None}
            blocked = risk.allow(s, updating=s.intent in {Intent.UPDATE, Intent.CLOSE})
            if blocked or s.intent in {Intent.IGNORE, Intent.UPDATE}:
                continue
            if s.side not in {Side.BUY, Side.SELL, Side.CLOSE}:
                continue
            usd = risk.size_usd(s)
            out.append(
                {
                    "channel": name,
                    "id": p["id"],
                    "date": p.get("date"),
                    "side": s.side.value,
                    "intent": s.intent.value,
                    "venue": s.venue.value,
                    "symbol": s.symbol,
                    "mint": s.mint,
                    "usd": usd,
                    "text": (s.text or "")[:140],
                }
            )
            if s.side in {Side.BUY, Side.SELL}:
                risk.mark_fill(s.key(), {})
    return out


def extra_human_calls() -> list[dict]:
    """Explicit same-message mint/ticker calls the parser still misses."""
    return [
        {
            "channel": "Theabyssofgambling",
            "id": 0,
            "date": "2026-08-08T16:51:25+00:00",
            "side": "buy",
            "intent": "market",
            "venue": "solana",
            "symbol": "TOAD",
            "mint": "A13oRB9FFaiUjfi6LdCg6p9ka1u8SfGkUFs4SKvPpump",
            "usd": 12,
            "text": "兄弟，这次真的有角度 TOAD",
        },
        {
            "channel": "sleepingclub0",
            "id": 0,
            "date": "2026-08-12T05:45:44+00:00",
            "side": "buy",
            "intent": "market",
            "venue": "hyperliquid",
            "symbol": "ENA",
            "mint": None,
            "usd": 40,
            "text": "如果向上波动了，就加仓干了 $ENA",
        },
    ]


def align_signal(index: pd.DatetimeIndex, when: datetime) -> Optional[pd.Timestamp]:
    """Fill on the bar that contains the message time (floor), not the next hour close."""
    idx = index.tz_convert("UTC") if index.tz is not None else index.tz_localize("UTC")
    target = pd.Timestamp(when)
    if target.tzinfo is None:
        target = target.tz_localize("UTC")
    else:
        target = target.tz_convert("UTC")
    pos = int(idx.searchsorted(target, side="right")) - 1
    if pos < 0:
        return None
    if pos >= len(idx):
        return idx[-1]
    return idx[pos]


def run_one(df: pd.DataFrame, signals: list[dict], cash: float, commission: float, sl: float, tp: float, name: str) -> dict:
    smap = {}
    for s in signals:
        bar = align_signal(df.index, _ts(s["date"]))
        if bar is None:
            continue
        smap[bar] = {
            "side": s["side"],
            "sl_pct": sl,
            "tp_pct": tp,
            "size_frac": min(0.5, max(0.002, float(s["usd"]) / cash)),
            "tag": f"{s['channel']}:{s['id']}",
        }
    if not smap:
        return {"instrument": name, "error": "no_aligned_signals", "signals": len(signals)}

    class S(SignalBarStrategy):
        signal_map = smap
        sl_pct = sl
        tp_pct = tp

    bt = FractionalBacktest(
        df,
        S,
        cash=cash,
        commission=commission,
        exclusive_orders=True,
        trade_on_close=True,
        finalize_trades=True,
        fractional_unit=1e-08,
    )
    stats = bt.run()
    trades = stats["_trades"] if "_trades" in stats else pd.DataFrame()
    rec = {
        "instrument": name,
        "signals": len(signals),
        "aligned": len(smap),
        "start": str(stats.get("Start")),
        "end": str(stats.get("End")),
        "equity_final": float(stats.get("Equity Final [$]", cash)),
        "return_pct": float(stats.get("Return [%]", 0) or 0),
        "buy_hold_pct": float(stats.get("Buy & Hold Return [%]", 0) or 0),
        "max_dd_pct": float(stats.get("Max. Drawdown [%]", 0) or 0),
        "sharpe": None if pd.isna(stats.get("Sharpe Ratio")) else float(stats.get("Sharpe Ratio")),
        "win_rate": None if pd.isna(stats.get("Win Rate [%]")) else float(stats.get("Win Rate [%]")),
        "n_trades": int(stats.get("# Trades", 0) or 0),
        "profit_factor": None if pd.isna(stats.get("Profit Factor")) else float(stats.get("Profit Factor")),
        "avg_trade_pct": None if pd.isna(stats.get("Avg. Trade [%]")) else float(stats.get("Avg. Trade [%]")),
        "exposure_pct": None if pd.isna(stats.get("Exposure Time [%]")) else float(stats.get("Exposure Time [%]")),
        "sqn": None if pd.isna(stats.get("SQN")) else float(stats.get("SQN")),
    }
    trade_rows = []
    if isinstance(trades, pd.DataFrame) and not trades.empty:
        for _, row in trades.iterrows():
            trade_rows.append({k: (None if pd.isna(v) else (v.isoformat() if hasattr(v, "isoformat") else v)) for k, v in row.to_dict().items()})
    rec["trades"] = trade_rows
    rec["_stats_str"] = str(stats)
    return rec


def main() -> None:
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    posts = json.loads((DATA_DIR / "channel_posts.json").read_text())
    cash = 1000.0
    now = datetime.now(timezone.utc)

    with httpx.Client(timeout=30.0, headers={"User-Agent": "tradelife-bt", "Accept": "application/json"}) as http:
        universe = hl_universe(http)
        signals = collect_actionable(cfg, posts, universe)
        # merge human overlay if not already present
        have = {(s["channel"], s["date"], s.get("mint") or s.get("symbol")) for s in signals}
        for extra in extra_human_calls():
            key = (extra["channel"], extra["date"], extra.get("mint") or extra.get("symbol"))
            if key not in have:
                signals.append(extra)

        groups: dict[str, list[dict]] = {}
        for s in signals:
            if s["venue"] == "hyperliquid" and s.get("symbol"):
                groups.setdefault(f"HL:{s['symbol']}", []).append(s)
            elif s["venue"] == "solana" and s.get("mint"):
                groups.setdefault(f"SOL:{s['mint'][:8]}:{s.get('symbol') or ''}", []).append(s)

        results = []
        for gname, gs in groups.items():
            gs = sorted(gs, key=lambda x: x["date"] or "")
            first = _ts(gs[0]["date"])
            if gname.startswith("HL:"):
                coin = gname.split(":", 1)[1]
                start = first - timedelta(hours=2)
                end = min(now, first + timedelta(days=7))
                df = hl_ohlcv_range(http, coin, start, end, "1m")
                sl, tp, comm = 0.08, 0.20, 0.00045
                tf = "1m"
            else:
                mint = gs[0]["mint"]
                start = first - timedelta(minutes=10)
                end = min(now, first + timedelta(hours=36))
                df = gecko_ohlcv_range(http, mint, start, end, "minute")
                sl, tp, comm = 0.45, 1.0, 0.01
                tf = "1m"
            if df.empty:
                results.append({"instrument": gname, "error": "no_1m_ohlcv", "signals": len(gs), "timeframe": tf})
                continue
            if align_signal(df.index, first) is None:
                results.append({
                    "instrument": gname,
                    "error": "signal_not_in_1m_window",
                    "signals": len(gs),
                    "timeframe": tf,
                    "ohlcv_start": str(df.index.min()),
                    "ohlcv_end": str(df.index.max()),
                    "signal_time": str(first),
                })
                continue
            rec = run_one(df, gs, cash=cash, commission=comm, sl=sl, tp=tp, name=gname)
            rec["bars"] = int(len(df))
            rec["timeframe"] = tf
            rec["ohlcv_start"] = str(df.index.min())
            rec["ohlcv_end"] = str(df.index.max())
            results.append(rec)
            log.info("%s trades=%s ret=%.2f%% dd=%.2f%%", gname, rec.get("n_trades"), rec.get("return_pct") or 0, rec.get("max_dd_pct") or 0)

    pnl = sum((r.get("equity_final") or cash) - cash for r in results if "equity_final" in r)
    out = {
        "engine": "kernc/backtesting.py 0.6.6",
        "github": "https://github.com/kernc/backtesting.py",
        "cash_per_instrument": cash,
        "timeframe": "1m",
        "fill_rule": "1-minute bar containing the Telegram timestamp (no hourly fallback)",
        "pnl_usd_sum": round(pnl, 2),
        "results": [{k: v for k, v in r.items() if k != "_stats_str"} for r in results],
        "raw_stats": {r["instrument"]: r.get("_stats_str") for r in results},
        "signals": signals,
    }
    path = DATA_DIR / "backtest_github.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: out[k] for k in ("engine", "cash_per_instrument", "pnl_usd_sum")}, indent=2))
    for r in results:
        print(
            f"{r.get('instrument'):28} trades={r.get('n_trades')} ret={r.get('return_pct')} "
            f"dd={r.get('max_dd_pct')} sharpe={r.get('sharpe')} win={r.get('win_rate')} err={r.get('error')}"
        )
        if r.get("_stats_str"):
            print(r["_stats_str"])
            print("-" * 60)
    print("wrote", path)


if __name__ == "__main__":
    main()
