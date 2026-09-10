from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .models import Intent, Side, Signal
from .util import setup_logging

log = setup_logging()


class HyperliquidVenue:
    def __init__(self, dry_run: bool = True):
        self.dry_run = dry_run
        self.network = os.getenv("HL_NETWORK", "testnet").strip().lower()
        self.account_address = os.getenv("HL_ACCOUNT_ADDRESS", "").strip()
        self.secret_key = os.getenv("HL_SECRET_KEY", "").strip()
        self.info = None
        self.exchange = None
        self.universe: dict[str, dict] = {}
        self.mids: dict[str, float] = {}
        self._ready = False

    def connect(self) -> None:
        from hyperliquid.info import Info
        from hyperliquid.utils import constants
        from eth_account import Account

        base = constants.TESTNET_API_URL if self.network != "main" else constants.MAINNET_API_URL
        self.info = Info(base, skip_ws=True)
        meta = self.info.meta()
        self.universe = {c["name"].upper(): c for c in meta.get("universe", [])}
        self.refresh_mids()

        if self.dry_run:
            log.info("Hyperliquid paper mode on %s, %s perps", self.network, len(self.universe))
            self._ready = True
            return

        if not self.secret_key:
            raise RuntimeError("HL_SECRET_KEY missing for live trading")
        from hyperliquid.exchange import Exchange

        account = Account.from_key(self.secret_key)
        address = self.account_address or account.address
        self.account_address = address
        self.exchange = Exchange(account, base, account_address=address)
        self._ready = True
        log.info("Hyperliquid live connected %s agent=%s", address, account.address)

    def refresh_mids(self) -> dict[str, float]:
        if not self.info:
            return self.mids
        raw = self.info.all_mids()
        self.mids = {k.upper(): float(v) for k, v in raw.items()}
        return self.mids

    def has(self, symbol: Optional[str]) -> bool:
        return bool(symbol) and symbol.upper() in self.universe

    def mark(self, symbol: str) -> Optional[float]:
        px = self.mids.get(symbol.upper())
        if px:
            return px
        self.refresh_mids()
        return self.mids.get(symbol.upper())

    def _round_size(self, symbol: str, sz: float) -> float:
        info = self.universe.get(symbol.upper(), {})
        decimals = int(info.get("szDecimals", 4))
        q = 10 ** decimals
        rounded = int(sz * q) / q
        return rounded

    def _round_px(self, px: float) -> float:
        # Hyperliquid prices typically 5 significant figures.
        if px <= 0:
            return px
        return float(f"{px:.5g}")

    def candles(self, symbol: str, interval: str, start: datetime, end: Optional[datetime] = None) -> list[dict]:
        if not self.info:
            return []
        end = end or datetime.now(timezone.utc)
        try:
            raw = self.info.candles_snapshot(symbol.upper(), interval, int(start.timestamp() * 1000), int(end.timestamp() * 1000))
        except Exception as exc:  # noqa: BLE001
            log.warning("candles failed %s %s: %s", symbol, interval, exc)
            return []
        out = []
        for c in raw or []:
            out.append(
                {
                    "t": int(c["t"]) / 1000.0,
                    "T": int(c.get("T") or c["t"]) / 1000.0,
                    "o": float(c["o"]),
                    "h": float(c["h"]),
                    "l": float(c["l"]),
                    "c": float(c["c"]),
                }
            )
        out.sort(key=lambda x: x["t"])
        return out

    def last_closed_candle(self, symbol: str, interval: str) -> Optional[dict]:
        now = datetime.now(timezone.utc)
        lookback = {"8h": timedelta(days=5), "4h": timedelta(days=3), "1h": timedelta(days=2)}.get(interval, timedelta(days=5))
        rows = self.candles(symbol, interval, now - lookback, now)
        if not rows:
            return None
        # drop in-progress bar: close time still in the future
        closed = [r for r in rows if r.get("T", r["t"]) <= time.time() + 1]
        return (closed or rows)[-1]

    def recent_high(self, symbol: str, interval: str, bars: int = 8) -> Optional[float]:
        now = datetime.now(timezone.utc)
        rows = self.candles(symbol, interval, now - timedelta(days=8), now)
        if not rows:
            return None
        closed = [r for r in rows if r.get("T", r["t"]) <= time.time()]
        window = (closed or rows)[-bars:]
        if len(window) >= 2:
            window = window[:-1]  # exclude current forming bar / last close used for confirm
        return max(r["h"] for r in window) if window else None

    def size_from_usd(self, symbol: str, usd: float) -> Optional[float]:
        px = self.mark(symbol)
        if not px:
            return None
        return self._round_size(symbol, usd / px)

    def execute(self, signal: Signal, usd: float, leverage: int = 2, slippage: float = 0.01) -> dict[str, Any]:
        if not signal.symbol or not self.has(signal.symbol):
            return {"status": "skip", "reason": "unknown_hl_symbol", "symbol": signal.symbol}
        coin = signal.symbol.upper()
        sz = self.size_from_usd(coin, usd)
        if not sz or sz <= 0:
            return {"status": "skip", "reason": "size_too_small", "usd": usd, "symbol": coin}

        is_buy = signal.side == Side.BUY
        if signal.side == Side.CLOSE:
            return self.close(coin)

        payload = {
            "venue": "hyperliquid",
            "coin": coin,
            "side": signal.side.value,
            "intent": signal.intent.value,
            "sz": sz,
            "usd": usd,
            "mark": self.mark(coin),
            "limit_price": signal.limit_price,
            "trigger_price": signal.trigger_price,
            "take_profit": signal.take_profit,
            "stop_loss": signal.stop_loss,
        }
        if self.dry_run or not self.exchange:
            payload["status"] = "paper"
            log.info("PAPER HL %s", payload)
            return payload

        try:
            self.exchange.update_leverage(leverage, coin, is_cross=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("leverage set failed %s: %s", coin, exc)

        if signal.intent == Intent.LIMIT and signal.limit_price:
            px = self._round_px(signal.limit_price)
            result = self.exchange.order(coin, is_buy, sz, px, {"limit": {"tif": "Gtc"}})
            payload["exchange"] = result
            payload["status"] = result.get("status", "unknown")
            return payload

        if signal.intent == Intent.WAIT_TARGET and signal.trigger_price:
            trigger = self._round_px(signal.trigger_price)
            mark = self.mark(coin) or trigger
            # 买回调：现价上方挂限价等跌下来；买突破：现价下方触发市价追多。
            if is_buy and trigger < mark:
                result = self.exchange.order(coin, True, sz, trigger, {"limit": {"tif": "Gtc"}})
            else:
                tpsl = "sl"
                order_type = {"trigger": {"triggerPx": trigger, "isMarket": True, "tpsl": tpsl}}
                result = self.exchange.order(coin, is_buy, sz, trigger, order_type)
            payload["exchange"] = result
            payload["status"] = result.get("status", "unknown")
            return payload

        result = self.exchange.market_open(coin, is_buy, sz, None, slippage)
        payload["exchange"] = result
        payload["status"] = result.get("status", "unknown")
        if payload["status"] == "ok":
            payload["exit_orders"] = self._maybe_attach_tpsl(coin, is_buy, sz, signal)
        else:
            payload["exit_orders"] = []
            log.warning("market open rejected %s: %s", coin, result)
        return payload

    @staticmethod
    def _order_oid(result: Any) -> Optional[int]:
        try:
            statuses = result["response"]["data"]["statuses"]
            for status in statuses:
                for key in ("resting", "filled"):
                    if key in status and status[key].get("oid") is not None:
                        return int(status[key]["oid"])
        except (KeyError, TypeError, ValueError):
            return None
        return None

    def open_orders(self) -> list[dict[str, Any]]:
        if not self.info or not self.account_address:
            return []
        return list(self.info.open_orders(self.account_address) or [])

    def _maybe_attach_tpsl(self, coin: str, is_buy: bool, sz: float, signal: Signal) -> list[dict[str, Any]]:
        if not self.exchange or self.dry_run:
            return []
        outcomes = []
        for kind, price in (("tp", signal.take_profit), ("sl", signal.stop_loss)):
            if not price:
                continue
            try:
                px = self._round_px(price)
                result = self.exchange.order(
                    coin,
                    not is_buy,
                    sz,
                    px,
                    {"trigger": {"triggerPx": px, "isMarket": True, "tpsl": kind}},
                    reduce_only=True,
                )
                outcome = {"kind": kind, "price": px, "result": result}
                if result.get("status") != "ok":
                    outcome["error"] = str(result.get("response") or result.get("status") or "rejected")
                oid = self._order_oid(result)
                if oid is not None:
                    outcome["oid"] = oid
                outcomes.append(outcome)
            except Exception as exc:  # noqa: BLE001
                log.warning("%s attach failed %s: %s", kind, coin, exc)
                outcomes.append({"kind": kind, "price": price, "error": str(exc)})
        return outcomes

    def cancel_orders(self, coin: str, order_ids: list[int]) -> list[dict[str, Any]]:
        if not self.exchange or self.dry_run:
            return []
        outcomes = []
        for oid in order_ids:
            try:
                outcomes.append({"oid": oid, "result": self.exchange.cancel(coin, oid)})
            except Exception as exc:  # noqa: BLE001
                log.warning("cancel failed %s oid=%s: %s", coin, oid, exc)
                outcomes.append({"oid": oid, "error": str(exc)})
        return outcomes

    def close(self, coin: str) -> dict[str, Any]:
        payload = {"venue": "hyperliquid", "coin": coin, "intent": "close"}
        if self.dry_run or not self.exchange:
            payload["status"] = "paper"
            return payload
        result = self.exchange.market_close(coin)
        payload["exchange"] = result
        payload["status"] = result.get("status", "unknown")
        return payload
