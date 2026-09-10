from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"
    CLOSE = "close"
    IGNORE = "ignore"


class Intent(str, Enum):
    MARKET = "market"  # 立刻市价
    LIMIT = "limit"  # 指定价挂单
    WAIT_TARGET = "wait_target"  # 等价格/市值触及再成交
    CONFIRM_BREAKOUT = "confirm_breakout"  # 突破 + 收线确认后再市价
    CLOSE = "close"
    UPDATE = "update"
    IGNORE = "ignore"


class Venue(str, Enum):
    HYPERLIQUID = "hyperliquid"
    SOLANA = "solana"
    NONE = "none"


class Status(str, Enum):
    NEW = "new"
    PENDING = "pending"
    WORKING = "working"
    FILLED = "filled"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass
class Signal:
    channel: str
    message_id: int
    date: str
    text: str
    side: Side
    intent: Intent
    venue: Venue
    symbol: Optional[str] = None
    mint: Optional[str] = None
    trigger_price: Optional[float] = None
    trigger_mcap: Optional[float] = None
    limit_price: Optional[float] = None
    take_profit: Optional[float] = None
    stop_loss: Optional[float] = None
    size_usd: Optional[float] = None
    size_mult: float = 1.0
    breakout: bool = False
    confirm_tf: Optional[str] = None  # e.g. 8h / 4h
    invalidation: Optional[float] = None
    confidence: float = 0.0
    reason: str = ""
    skip_reason: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)

    def key(self) -> str:
        if self.mint:
            return f"sol:{self.mint}"
        if self.symbol:
            return f"hl:{self.symbol.upper()}"
        return f"unk:{self.channel}:{self.message_id}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["side"] = self.side.value
        d["intent"] = self.intent.value
        d["venue"] = self.venue.value
        return d


@dataclass
class Plan:
    signal: Signal
    status: Status = Status.NEW
    note: str = ""
    created_at: float = 0.0
    triggered_at: Optional[float] = None
    last_price: Optional[float] = None
    last_mcap: Optional[float] = None
    exchange_result: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "note": self.note,
            "created_at": self.created_at,
            "triggered_at": self.triggered_at,
            "last_price": self.last_price,
            "last_mcap": self.last_mcap,
            "signal": self.signal.to_dict(),
            "exchange_result": self.exchange_result,
        }
