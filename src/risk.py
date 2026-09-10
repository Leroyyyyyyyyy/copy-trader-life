from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .models import Intent, Side, Signal, Venue


@dataclass
class RiskState:
    day: str = ""
    trades_today: int = 0
    realized_pnl_usd: float = 0.0
    open_positions: dict[str, dict] = field(default_factory=dict)
    last_trade_at: dict[str, float] = field(default_factory=dict)
    pending_keys: set[str] = field(default_factory=set)


class RiskGuard:
    def __init__(self, cfg: dict, channels: Optional[dict[str, dict]] = None):
        self.cfg = cfg
        self.channels = channels or {}
        self.state = RiskState()

    def channel(self, name: str) -> dict[str, Any]:
        return self.channels.get(name) or {}

    def _roll_day(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if self.state.day != today:
            self.state.day = today
            self.state.trades_today = 0
            self.state.realized_pnl_usd = 0.0

    def size_usd(self, signal: Optional[Signal] = None) -> float:
        if signal is None:
            return float(self.cfg.get("max_usd_per_trade", 50))
        ch = self.channel(signal.channel)
        bucket = "meme" if signal.venue == Venue.SOLANA else "perp"
        override = (ch.get(bucket) or {}).get("usd")
        if override is None:
            usd = float(self.cfg.get("max_usd_per_trade", 50))
        else:
            usd = float(override)
        if signal.size_usd is not None:
            usd = float(signal.size_usd)
        return max(0.0, usd * float(signal.size_mult or 1.0))

    def size_sol(self, signal: Optional[Signal] = None) -> float:
        if signal is None:
            return float(self.cfg.get("max_sol_per_trade", 0.05))
        ch = self.channel(signal.channel)
        override = (ch.get("meme") or {}).get("sol")
        if override is None:
            return float(self.cfg.get("max_sol_per_trade", 0.05))
        return float(override)

    def leverage(self, signal: Signal) -> int:
        ch = self.channel(signal.channel)
        lev = (ch.get("perp") or {}).get("leverage")
        if lev is None:
            lev = self.cfg.get("max_leverage", 2)
        cap = int(self.cfg.get("max_leverage", 5) or 5)
        # 频道杠杆可以高于全局默认，但不超过 5。
        return max(1, min(int(lev), max(cap, 5)))

    def min_confidence(self, signal: Signal) -> float:
        ch = self.channel(signal.channel)
        if ch.get("min_confidence") is not None:
            return float(ch["min_confidence"])
        return float(self.cfg.get("min_confidence", 0.7))

    def allow(self, signal: Signal, updating: bool = False) -> Optional[str]:
        self._roll_day()
        ch = self.channel(signal.channel)
        if signal.skip_reason:
            return signal.skip_reason
        if signal.intent == Intent.IGNORE and signal.side != Side.CLOSE:
            return signal.reason or "ignored"
        if signal.confidence < self.min_confidence(signal):
            return f"low_confidence:{signal.confidence:.2f}"

        if signal.side in {Side.BUY, Side.SELL} and not updating:
            if signal.venue == Venue.HYPERLIQUID and not ch.get("allow_perp", True):
                return "channel_no_perp"
            if signal.venue == Venue.SOLANA and not ch.get("allow_meme", True):
                return "channel_no_meme"
            if self.size_usd(signal) <= 0 and signal.venue == Venue.HYPERLIQUID:
                return "channel_zero_size"
            if self.size_sol(signal) <= 0 and signal.venue == Venue.SOLANA:
                return "channel_zero_size"
            if ch.get("require_explicit"):
                tickers = (signal.raw or {}).get("tickers") or []
                mints = (signal.raw or {}).get("mints") or []
                if not tickers and not mints:
                    return "need_explicit_ticker"

        if self.state.trades_today >= int(self.cfg.get("max_daily_trades", 8)):
            return "daily_trade_cap"
        if self.state.realized_pnl_usd <= -abs(float(self.cfg.get("max_daily_loss_usd", 80))):
            return "daily_loss_cap"
        key = signal.key()
        if updating:
            return None
        if key in self.state.open_positions and signal.side in {Side.BUY, Side.SELL}:
            if signal.intent in {Intent.UPDATE, Intent.CLOSE}:
                return None
            return "already_in_position"
        if key in self.state.pending_keys and not self.cfg.get("allow_update_pending", True):
            return "already_pending"
        if len(self.state.open_positions) >= int(self.cfg.get("max_open_positions", 3)):
            if signal.side != Side.CLOSE:
                return "max_open_positions"
        last = self.state.last_trade_at.get(key, 0)
        if last and time.time() - last < float(self.cfg.get("cooldown_sec", 900)):
            if signal.intent not in {Intent.UPDATE, Intent.CLOSE}:
                return "cooldown"
        return None

    def mark_pending(self, key: str) -> None:
        self.state.pending_keys.add(key)

    def clear_pending(self, key: str) -> None:
        self.state.pending_keys.discard(key)

    def mark_fill(self, key: str, meta: dict) -> None:
        self._roll_day()
        self.state.trades_today += 1
        self.state.last_trade_at[key] = time.time()
        self.state.open_positions[key] = meta
        self.clear_pending(key)

    def mark_close(self, key: str, pnl_usd: float = 0.0) -> None:
        self.state.open_positions.pop(key, None)
        self.state.realized_pnl_usd += pnl_usd
        self.clear_pending(key)
