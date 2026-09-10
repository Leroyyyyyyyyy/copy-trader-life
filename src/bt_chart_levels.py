"""Replay POPIGOGO 2026 structure trades using chart levels only.

No swing stop, no synthetic 20% TP. Trigger / SL / TP come from
data/charts/levels.json. Confirmation uses the post's candle timeframe
(6h -> 4h because Hyperliquid has no 6h).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from .util import DATA_DIR

HL_INFO = "https://api.hyperliquid.xyz/info"
TAKER = 0.00045
USD_DEFAULT = 80.0
INTERVAL = {"4h": "4h", "6h": "4h", "8h": "8h", "12h": "12h", "1d": "1d", "1h": "1h"}
FINE_TF = ("1m", "15m", "5m")


def _dt(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def candles(http: httpx.Client, coin: str, interval: str, start: datetime, end: datetime) -> list[dict]:
    frames: list[dict] = []
    cur = start
    step = timedelta(hours=12) if interval == "1m" else timedelta(days=40)
    while cur < end:
        nxt = min(cur + step, end)
        r = http.post(
            HL_INFO,
            json={
                "type": "candleSnapshot",
                "req": {
                    "coin": coin,
                    "interval": interval,
                    "startTime": int(cur.timestamp() * 1000),
                    "endTime": int(nxt.timestamp() * 1000),
                },
            },
            timeout=30,
        )
        for attempt in range(6):
            if r.status_code != 429:
                break
            time.sleep(1.5 * (attempt + 1))
            r = http.post(
                HL_INFO,
                json={
                    "type": "candleSnapshot",
                    "req": {
                        "coin": coin,
                        "interval": interval,
                        "startTime": int(cur.timestamp() * 1000),
                        "endTime": int(nxt.timestamp() * 1000),
                    },
                },
                timeout=30,
            )
        r.raise_for_status()
        for c in r.json() or []:
            frames.append(
                {
                    "t": int(c["t"]) / 1000,
                    "T": int(c.get("T") or c["t"]) / 1000,
                    "o": float(c["o"]),
                    "h": float(c["h"]),
                    "l": float(c["l"]),
                    "c": float(c["c"]),
                }
            )
        cur = nxt
        time.sleep(0.25)
    frames.sort(key=lambda x: x["t"])
    seen: set[float] = set()
    out: list[dict] = []
    for row in frames:
        if row["t"] in seen:
            continue
        seen.add(row["t"])
        out.append(row)
    return out


def first_available_fine(
    http: httpx.Client, coin: str, start: datetime, end: datetime
) -> tuple[Optional[str], list[dict]]:
    for tf in FINE_TF:
        rows = candles(http, coin, tf, start, end)
        if rows:
            return tf, rows
    return None, []


def confirm_bar(rows: list[dict], when_ts: float, side: str, level: float) -> Optional[dict]:
    for r in rows:
        if r["T"] <= when_ts:
            continue
        if side == "sell" and r["c"] < level and r["l"] <= level:
            return r
        if side == "buy" and r["c"] > level and r["h"] >= level:
            return r
    return None


def refine_entry(fine: list[dict], fill: dict, side: str, level: float) -> Optional[dict]:
    """First fine bar at/after confirm-bar close that is still on the right side of the level."""
    for r in fine:
        if r["T"] < fill["T"] - 1:
            continue
        if r["t"] < fill["T"] - 1:
            continue
        if side == "sell" and r["c"] <= level:
            return r
        if side == "buy" and r["c"] >= level:
            return r
        if r["T"] > fill["T"] + 4 * 3600:
            break
    return None


def path_exit(
    rows: list[dict],
    entry_t: float,
    side: str,
    sl: Optional[float],
    tp: Optional[float],
) -> tuple[Optional[float], Optional[float], Optional[str]]:
    for r in rows:
        if r["t"] <= entry_t:
            continue
        hit_sl = bool(sl is not None and ((side == "sell" and r["h"] >= sl) or (side == "buy" and r["l"] <= sl)))
        hit_tp = bool(tp is not None and ((side == "sell" and r["l"] <= tp) or (side == "buy" and r["h"] >= tp)))
        if hit_sl and hit_tp:
            # same bar: conservative, stop first (no 1m path here)
            return sl, r["t"], "stop_and_tp_same_bar"
        if hit_sl:
            return sl, r["t"], "stop"
        if hit_tp:
            return tp, r["t"], "tp"
    return None, None, None


def mark_at(rows: list[dict], when_ts: float) -> Optional[float]:
    closed = [r for r in rows if r["T"] <= when_ts]
    if closed:
        return closed[-1]["c"]
    return rows[0]["c"] if rows else None


def run(now: Optional[datetime] = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    setups = json.loads((DATA_DIR / "popi_2026_setups.json").read_text(encoding="utf-8"))
    levels = json.loads((DATA_DIR / "charts" / "levels.json").read_text(encoding="utf-8"))
    http = httpx.Client(timeout=30, headers={"User-Agent": "tradelife-chart-bt"})
    results: list[dict] = []

    for s in setups:
        pid = str(s["id"])
        lv = levels.get(pid) or {}
        when = _dt(s["date"])
        coin = s["symbol"]
        side = s["side"]
        tf_req = s.get("tf") or lv.get("timeframe") or "8h"
        tf = INTERVAL.get(tf_req, "8h")
        trigger = lv.get("yellow_or_trigger")
        sl = lv.get("stop_loss")
        tp = lv.get("take_profit")
        chart_entry = lv.get("entry")
        usd = USD_DEFAULT * float(s.get("mult") or 1)
        rec: dict[str, Any] = {
            "id": s["id"],
            "bj": s.get("bj"),
            "symbol": coin,
            "side": side,
            "tf_req": tf_req,
            "tf_used": tf,
            "usd": usd,
            "trigger": trigger,
            "sl": sl,
            "tp": tp,
            "chart_entry": chart_entry,
            "source": lv.get("source"),
            "confidence": lv.get("confidence"),
            "policy": "chart_levels_only",
        }
        if tf_req == "6h":
            rec["note"] = "6h not on HL, used 4h"
        if lv.get("skip"):
            rec.update({"filled": False, "reason": "skip", "skip_reason": lv.get("skip_reason")})
            results.append(rec)
            print(f"#{pid} {coin} {side} SKIP {lv.get('skip_reason')}")
            continue
        if trigger is None:
            rec.update({"filled": False, "reason": "chart_trigger_unreadable"})
            results.append(rec)
            print(f"#{pid} {coin} {side} SKIP no trigger")
            continue

        rows = candles(http, coin, tf, when - timedelta(days=5), min(now, when + timedelta(days=90)))
        rec["mark_at_signal"] = mark_at(rows, when.timestamp())
        if not rows:
            rec.update({"filled": False, "reason": "no_ohlcv"})
            results.append(rec)
            print(f"#{pid} FAIL no ohlcv")
            continue

        fill = confirm_bar(rows, when.timestamp(), side, float(trigger))
        if not fill:
            rec.update({"filled": False, "reason": "never_confirmed"})
            results.append(rec)
            print(f"#{pid} {coin} {side} NEVER trigger={trigger} mark={rec['mark_at_signal']}")
            continue

        entry = float(fill["c"])
        entry_t = float(fill["T"])
        entry_src = f"{tf}_close"

        if chart_entry:
            # Prefer the author's actual fill if it is on the correct side of trigger
            # and not wildly off the confirm bar.
            ce = float(chart_entry)
            ok = (side == "buy" and ce >= float(trigger) * 0.9) or (side == "sell" and ce <= float(trigger) * 1.1)
            if ok:
                entry = ce
                entry_src = "chart_card"
        else:
            fine_tf, fine = first_available_fine(
                http, coin, datetime.fromtimestamp(fill["T"] - 60, timezone.utc),
                datetime.fromtimestamp(fill["T"] + 6 * 3600, timezone.utc),
            )
            if fine:
                refined = refine_entry(fine, fill, side, float(trigger))
                if refined:
                    entry = float(refined["c"])
                    entry_t = float(refined["T"])
                    entry_src = f"{fine_tf}_after_confirm"
                    rec["fine_tf"] = fine_tf

        # Exit path: prefer finer bars after entry when we have them.
        path = rows
        path_tf = tf
        fine_tf2, fine2 = first_available_fine(
            http,
            coin,
            datetime.fromtimestamp(entry_t, timezone.utc),
            min(now, datetime.fromtimestamp(entry_t, timezone.utc) + timedelta(days=14)),
        )
        if fine2 and fine_tf2 in {"15m", "5m", "1m"}:
            # stitch: fine window then remaining confirm-tf bars
            rest = [r for r in rows if r["t"] > fine2[-1]["t"]]
            path = fine2 + rest
            path_tf = f"{fine_tf2}+{tf}"

        exit_px, exit_t, why = path_exit(path, entry_t, side, sl, tp)
        if exit_px is None:
            last = path[-1]
            exit_px, exit_t, why = last["c"], last["t"], "open_mark"

        sign = -1 if side == "sell" else 1
        ret = sign * (float(exit_px) - entry) / entry - 2 * TAKER
        pnl = usd * ret
        rec.update(
            {
                "filled": True,
                "entry": entry,
                "entry_t": _iso(entry_t),
                "entry_src": entry_src,
                "exit": exit_px,
                "exit_t": _iso(float(exit_t)),
                "why": why,
                "path_tf": path_tf,
                "ret": ret,
                "pnl": pnl,
            }
        )
        results.append(rec)
        print(
            f"#{pid} {s.get('bj')} {coin} {side} tf={tf} trig={trigger} sl={sl} tp={tp} "
            f"@{entry:.4g} ({entry_src}) -> {why} {float(exit_px):.4g} ret={ret*100:.1f}% pnl={pnl:+.2f}"
        )

    filled = [r for r in results if r.get("filled")]
    wins = [r for r in filled if (r.get("pnl") or 0) > 0]
    pnl = sum(r.get("pnl") or 0 for r in results)
    out = {
        "policy": "chart levels only; no swing SL; no 20% TP; skip if trigger missing",
        "pnl_usd": round(pnl, 2),
        "filled": len(filled),
        "wins": len(wins),
        "n": len(results),
        "results": results,
    }
    dest = DATA_DIR / "popi_2026_chart_bt.json"
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nsetups {len(results)} filled {len(filled)} wins {len(wins)} pnl {round(pnl, 2)}")
    print("wrote", dest)
    return out


if __name__ == "__main__":
    run()
