from __future__ import annotations

import time
import uuid
from typing import Any, Optional

from .hyperliquid_venue import HyperliquidVenue
from .models import Intent, Plan, Side, Signal, Status, Venue
from .parser import parse_message
from .risk import RiskGuard
from .solana_venue import SolanaVenue
from .store import JsonStore
from .util import setup_logging

log = setup_logging()


class Engine:
    def __init__(self, cfg: dict[str, Any], dry_run: bool):
        self.cfg = cfg
        self.dry_run = dry_run
        self.risk = RiskGuard(cfg.get("risk") or {}, channels=self._channel_map(cfg))
        self.store = JsonStore()
        self.hl = HyperliquidVenue(dry_run=dry_run)
        self.sol = SolanaVenue(
            dry_run=dry_run,
            poll_sec=float(((cfg.get("venues") or {}).get("solana") or {}).get("poll_sec") or 2.5),
        )
        self.channel_cfg = self._channel_map(cfg)
        self.context: dict[str, dict[str, Any]] = {}
        self.plans: dict[str, Plan] = {}
        self._next_hl_reconcile_at = 0.0
        self._hl_reconcile_interval = float(
            ((cfg.get("venues") or {}).get("hyperliquid") or {}).get("reconcile_interval_sec") or 30.0
        )

    @staticmethod
    def _channel_map(cfg: dict[str, Any]) -> dict[str, dict]:
        return {c["username"]: c for c in cfg.get("channels") or []}

    def start(self) -> None:
        venues = self.cfg.get("venues") or {}
        if (venues.get("hyperliquid") or {}).get("enabled", True):
            try:
                self.hl.connect()
            except Exception:
                log.exception("hyperliquid connect failed; HL disabled")
                if not self.dry_run:
                    raise
        if (venues.get("solana") or {}).get("enabled", True):
            self.sol.connect()
        self._restore_plans()
        self.reconcile_hyperliquid(force=True)

    def _restore_plans(self) -> None:
        mode = "paper" if self.dry_run else "live"
        for pid, raw in self.store.plans().items():
            if raw.get("execution_mode") != mode:
                log.warning("not restoring %s: stored mode=%s current=%s", pid, raw.get("execution_mode", "legacy"), mode)
                continue
            sig = raw.get("signal") or {}
            try:
                signal = Signal(
                    channel=sig["channel"],
                    message_id=int(sig["message_id"]),
                    date=sig.get("date") or "",
                    text=sig.get("text") or "",
                    side=Side(sig.get("side") or "ignore"),
                    intent=Intent(sig.get("intent") or "ignore"),
                    venue=Venue(sig.get("venue") or "none"),
                    symbol=sig.get("symbol"),
                    mint=sig.get("mint"),
                    trigger_price=sig.get("trigger_price"),
                    trigger_mcap=sig.get("trigger_mcap"),
                    limit_price=sig.get("limit_price"),
                    take_profit=sig.get("take_profit"),
                    stop_loss=sig.get("stop_loss"),
                    size_mult=float(sig.get("size_mult") or 1),
                    breakout=bool(sig.get("breakout")),
                    confirm_tf=sig.get("confirm_tf"),
                    invalidation=sig.get("invalidation"),
                    confidence=float(sig.get("confidence") or 0),
                    reason=sig.get("reason") or "",
                    skip_reason=sig.get("skip_reason"),
                )
            except Exception:
                continue
            if signal.intent in {Intent.WAIT_TARGET, Intent.CONFIRM_BREAKOUT}:
                plan = Plan(signal=signal, status=Status.PENDING, created_at=raw.get("created_at") or time.time())
                self.plans[pid] = plan
                self.risk.mark_pending(signal.key())
                log.info("restored pending plan %s %s", pid, signal.key())

    @staticmethod
    def _hl_key(coin: str) -> str:
        return f"hl:{coin.upper()}"

    def _record_hl_position(self, signal: Signal, result: dict[str, Any]) -> None:
        if not signal.symbol:
            return
        exits = result.get("exit_orders") or []
        managed = [
            {"kind": row["kind"], "oid": row.get("oid"), "price": row.get("price")}
            for row in exits
            if row.get("oid") is not None
        ]
        requested = [kind for kind, price in (("tp", signal.take_profit), ("sl", signal.stop_loss)) if price]
        failed = [row["kind"] for row in exits if row.get("error")]
        entry = {
            "coin": signal.symbol.upper(),
            "side": signal.side.value,
            "size": result.get("sz"),
            "source": "bot",
            "managed_exits": managed,
            "requested_exits": requested,
            "at_risk": bool(failed),
            "risk_reasons": [f"{kind}_attach_failed" for kind in failed],
            "updated_at": time.time(),
        }
        self.store.put_position(signal.key(), entry)

    def _set_hl_risk(self, key: str, entry: dict[str, Any], at_risk: bool, reasons: list[str]) -> None:
        changed = entry.get("at_risk") != at_risk or entry.get("risk_reasons") != reasons
        entry["at_risk"] = at_risk
        entry["risk_reasons"] = reasons
        entry["updated_at"] = time.time()
        if changed:
            level = log.error if at_risk else log.info
            level("HL exit protection %s %s: %s", "AT_RISK" if at_risk else "verified", key, ", ".join(reasons) or "TP/SL present")
        self.store.put_position(key, entry)

    def _cancel_managed_hl_exits(self, coin: str, entry: dict[str, Any], open_ids: set[int] | None = None) -> list[dict[str, Any]]:
        ids = [int(row["oid"]) for row in entry.get("managed_exits") or [] if row.get("oid") is not None]
        if open_ids is not None:
            ids = [oid for oid in ids if oid in open_ids]
        return self.hl.cancel_orders(coin, ids)

    def reconcile_hyperliquid(self, force: bool = False) -> None:
        if self.dry_run or not self.hl.info or not self.hl.account_address:
            return
        now = time.time()
        if not force and now < self._next_hl_reconcile_at:
            return
        self._next_hl_reconcile_at = now + self._hl_reconcile_interval
        try:
            state = self.hl.info.user_state(self.hl.account_address)
            open_orders = self.hl.open_orders()
        except Exception:
            log.exception("HL reconciliation failed")
            return

        actual: dict[str, dict[str, Any]] = {}
        for row in state.get("assetPositions") or []:
            position = row.get("position") or {}
            coin = str(position.get("coin") or "").upper()
            size = float(position.get("szi") or 0)
            if coin and size:
                actual[self._hl_key(coin)] = position
        orders_by_id = {int(row["oid"]): row for row in open_orders if row.get("oid") is not None}
        managed_at_risk: list[str] = []

        for key, position in actual.items():
            coin = str(position["coin"]).upper()
            entry = self.store.position(key)
            if entry is None:
                entry = {
                    "coin": coin,
                    "side": Side.BUY.value if float(position["szi"]) > 0 else Side.SELL.value,
                    "size": abs(float(position["szi"])),
                    "source": "external",
                    "managed_exits": [],
                    "requested_exits": [],
                    "at_risk": False,
                    "risk_reasons": [],
                    "updated_at": now,
                }
                self.store.put_position(key, entry)
                log.warning("HL external position detected %s; blocking duplicate entries without managing its exits", key)
            entry["size"] = abs(float(position["szi"]))
            entry["side"] = Side.BUY.value if float(position["szi"]) > 0 else Side.SELL.value
            if entry.get("source") == "bot":
                active = {
                    row.get("kind")
                    for row in entry.get("managed_exits") or []
                    if row.get("oid") in orders_by_id and orders_by_id[row["oid"]].get("reduceOnly")
                }
                missing = [kind for kind in entry.get("requested_exits") or [] if kind not in active]
                self._set_hl_risk(key, entry, bool(missing), [f"missing_{kind}" for kind in missing])
                if missing:
                    managed_at_risk.append(key)
            else:
                self.store.put_position(key, entry)
            self.risk.state.open_positions.setdefault(key, {"source": entry.get("source"), "exchange_position": position})

        for key, entry in list(self.store.positions().items()):
            if entry.get("coin") and key.startswith("hl:") and key not in actual:
                coin = str(entry["coin"]).upper()
                cancelled = self._cancel_managed_hl_exits(coin, entry, set(orders_by_id))
                if cancelled:
                    log.info("cancelled managed exits after %s disappeared: %s", key, cancelled)
                self.store.drop_position(key)
                self.risk.state.open_positions.pop(key, None)

        self.store.set_hl_reconciliation({
            "at": now,
            "account": self.hl.account_address,
            "positions": sorted(actual),
            "open_order_count": len(open_orders),
            "at_risk": managed_at_risk,
        })

    def handle_text(self, channel: str, message_id: int, date: str, text: str, image_path: Optional[str] = None) -> Optional[Plan]:
        if self.store.seen(channel, message_id):
            return None
        self.store.mark_seen(channel, message_id)
        ch = self.channel_cfg.get(channel) or {}
        if not ch.get("enabled", True):
            return None

        inherit = bool(ch.get("inherit_context", True))
        ctx = (self.context.get(channel) or {}) if inherit else {}
        signal = parse_message(
            channel=channel,
            message_id=message_id,
            date=date,
            text=text,
            default_symbol=ch.get("default_symbol") or ctx.get("symbol"),
            default_mint=ctx.get("mint") if inherit else None,
            hl_universe=set(self.hl.universe) or None,
        )
        boost = float(ch.get("confidence_boost") or 0)
        if boost and not signal.skip_reason:
            signal.confidence = max(0.0, min(0.99, signal.confidence + boost))
            signal.reason = (signal.reason + f";boost:{boost:+.2f}").strip(";")

        # 结构突破单带图时，用 deepseek vision 读图上的 trigger/SL/TP，
        # 不再用近期高/低点代理（读图失败才回退代理）。
        if (
            signal.intent == Intent.CONFIRM_BREAKOUT
            and signal.venue == Venue.HYPERLIQUID
            and signal.symbol
            and image_path
        ):
            try:
                from .chart_reader import read_levels

                lv = read_levels(image_path, signal.symbol, signal.side.value, signal.confirm_tf or "8h")
                if signal.trigger_price is None and lv.get("yellow_or_trigger"):
                    signal.trigger_price = float(lv["yellow_or_trigger"])
                if signal.stop_loss is None and lv.get("stop_loss"):
                    signal.stop_loss = float(lv["stop_loss"])
                if signal.take_profit is None and lv.get("take_profit"):
                    signal.take_profit = float(lv["take_profit"])
                signal.reason = (signal.reason + ";chart_vision").strip(";")
                log.info(
                    "chart vision #%s %s -> trig=%s sl=%s tp=%s",
                    message_id, signal.symbol, signal.trigger_price, signal.stop_loss, signal.take_profit,
                )
            except Exception:
                log.exception("chart vision failed #%s; falling back to proxy trigger", message_id)
        if signal.mint:
            self.context[channel] = {"mint": signal.mint, "symbol": signal.symbol}
        elif signal.symbol:
            self.context[channel] = {"symbol": signal.symbol, "mint": None}

        min_conf = self.risk.min_confidence(signal)
        if signal.confidence and signal.confidence < min_conf and not signal.skip_reason:
            signal.skip_reason = f"channel_min_confidence:{signal.confidence:.2f}"
            signal.intent = Intent.IGNORE

        log.info(
            "signal %s #%s side=%s intent=%s venue=%s symbol=%s mint=%s conf=%.2f skip=%s | %s",
            channel,
            message_id,
            signal.side.value,
            signal.intent.value,
            signal.venue.value,
            signal.symbol,
            (signal.mint or "")[:8],
            signal.confidence,
            signal.skip_reason,
            (text or "").replace("\n", " ")[:160],
        )
        return self.apply(signal)

    def apply(self, signal: Signal) -> Plan:
        plan = Plan(signal=signal, created_at=time.time())
        blocked = self.risk.allow(signal, updating=signal.intent in {Intent.UPDATE, Intent.CLOSE})
        if blocked:
            plan.status = Status.SKIPPED
            plan.note = blocked
            log.info("skip %s %s", signal.key(), blocked)
            return plan

        if signal.intent == Intent.UPDATE:
            return self._update_exits(plan)
        if signal.intent == Intent.CONFIRM_BREAKOUT and signal.venue == Venue.HYPERLIQUID:
            self._prepare_breakout(signal)
            return self._park(plan, self._breakout_note(signal))
        # HL: 限价/触发单立刻挂到交易所，不在本地等。
        # Solana 土狗没有稳定盘口，本地盯价/市值，触及再市价。
        if signal.venue == Venue.SOLANA and signal.intent in {Intent.WAIT_TARGET, Intent.LIMIT}:
            if signal.limit_price and not signal.trigger_price:
                signal.trigger_price = signal.limit_price
            signal.intent = Intent.WAIT_TARGET
            plan.signal = signal
            return self._park(plan, "solana wait for price/mcap")
        return self._fire(plan)

    def _update_exits(self, plan: Plan) -> Plan:
        signal = plan.signal
        if signal.venue != Venue.HYPERLIQUID or not signal.symbol:
            plan.status = Status.SKIPPED
            plan.note = "update_only_on_hl"
            return plan
        if signal.key() not in self.risk.state.open_positions and not self.dry_run:
            plan.status = Status.SKIPPED
            plan.note = "no_position_to_update"
            log.info("skip TP/SL update, not in position %s", signal.key())
            return plan
        if self.dry_run:
            plan.status = Status.WORKING
            plan.note = f"paper update tp={signal.take_profit} sl={signal.stop_loss}"
            plan.exchange_result = {"status": "paper", "tp": signal.take_profit, "sl": signal.stop_loss}
            log.info("PAPER update exits %s", plan.note)
            return plan
        entry = self.store.position(signal.key())
        if not entry or entry.get("source") != "bot":
            plan.status = Status.SKIPPED
            plan.note = "cannot_update_external_position"
            log.error("refusing TP/SL update for unmanaged HL position %s", signal.key())
            return plan
        is_buy = entry.get("side") == Side.BUY.value
        sz = float(entry.get("size") or 0)
        if not sz:
            plan.status = Status.FAILED
            plan.note = "missing_position_size"
            return plan
        old_orders = self._cancel_managed_hl_exits(signal.symbol.upper(), entry)
        exits = self.hl._maybe_attach_tpsl(signal.symbol.upper(), is_buy, sz, signal)
        entry["managed_exits"] = [
            {"kind": row["kind"], "oid": row.get("oid"), "price": row.get("price")}
            for row in exits
            if row.get("oid") is not None
        ]
        entry["requested_exits"] = [
            kind for kind, price in (("tp", signal.take_profit), ("sl", signal.stop_loss)) if price
        ]
        failed = [row["kind"] for row in exits if row.get("error")]
        entry["at_risk"] = bool(failed)
        entry["risk_reasons"] = [f"{kind}_attach_failed" for kind in failed]
        self.store.put_position(signal.key(), entry)
        self.reconcile_hyperliquid(force=True)
        plan.status = Status.WORKING
        plan.note = "replaced tp/sl"
        plan.exchange_result = {"status": "ok", "cancelled": old_orders, "exit_orders": exits}
        return plan

    def _park(self, plan: Plan, note: str) -> Plan:
        plan.status = Status.PENDING
        plan.note = note
        pid = str(uuid.uuid4())[:8]
        self.plans[pid] = plan
        self.risk.mark_pending(plan.signal.key())
        payload = plan.to_dict()
        payload["execution_mode"] = "paper" if self.dry_run else "live"
        self.store.put_plan(pid, payload)
        log.info("pending %s %s %s", pid, plan.signal.key(), note)
        return plan

    def _fire(self, plan: Plan) -> Plan:
        signal = plan.signal
        usd = self.risk.size_usd(signal)
        slippage = float(self.risk.cfg.get("max_slippage", 0.01))
        leverage = self.risk.leverage(signal)
        result: dict[str, Any]
        if signal.venue == Venue.HYPERLIQUID:
            if signal.side == Side.CLOSE:
                entry = self.store.position(signal.key())
                if entry:
                    self._cancel_managed_hl_exits(signal.symbol or "", entry)
                result = self.hl.close(signal.symbol or "")
            else:
                result = self.hl.execute(signal, usd=usd, leverage=leverage, slippage=slippage)
                if result.get("status") == "ok":
                    self._record_hl_position(signal, result)
                    self.reconcile_hyperliquid(force=True)
        elif signal.venue == Venue.SOLANA:
            result = self.sol.execute(
                signal,
                sol_amount=self.risk.size_sol(signal),
                slippage_bps=int(slippage * 10_000),
            )
        else:
            result = {"status": "skip", "reason": "no_venue"}

        plan.exchange_result = result
        status = result.get("status")
        if status in {"ok", "paper"}:
            plan.status = Status.FILLED if signal.intent != Intent.LIMIT else Status.WORKING
            plan.triggered_at = time.time()
            if signal.side in {Side.BUY, Side.SELL}:
                self.risk.mark_fill(signal.key(), {"signal": signal.to_dict(), "result": result})
            elif signal.side == Side.CLOSE:
                self.risk.mark_close(signal.key())
                if signal.venue == Venue.HYPERLIQUID:
                    self.store.drop_position(signal.key())
                    self.reconcile_hyperliquid(force=True)
        elif status == "skip":
            plan.status = Status.SKIPPED
            plan.note = str(result.get("reason") or "venue_skip")
        else:
            plan.status = Status.FAILED
            plan.note = str(result.get("error") or status or "failed")
        log.info(
            "exec %s %s usd=%.2f sol=%.4f lev=%s %s",
            signal.key(),
            plan.status.value,
            usd,
            self.risk.size_sol(signal),
            leverage,
            plan.note or status,
        )
        return plan

    def poll_pending(self) -> None:
        self.reconcile_hyperliquid()
        if not self.plans:
            return
        done = []
        for pid, plan in list(self.plans.items()):
            sig = plan.signal
            try:
                if sig.intent == Intent.CONFIRM_BREAKOUT:
                    if self._breakout_ready(plan):
                        sig.intent = Intent.MARKET
                        fired = self._fire(plan)
                        if fired.status in {Status.FILLED, Status.WORKING, Status.SKIPPED, Status.FAILED}:
                            done.append(pid)
                    continue
                if sig.intent != Intent.WAIT_TARGET:
                    continue
                if sig.venue == Venue.HYPERLIQUID and sig.symbol:
                    px = self.hl.mark(sig.symbol)
                    plan.last_price = px
                    if px is None:
                        continue
                    hit = False
                    if sig.trigger_price:
                        if sig.side == Side.BUY:
                            hit = px <= sig.trigger_price
                        else:
                            hit = px >= sig.trigger_price
                    if hit:
                        sig.intent = Intent.MARKET
                        fired = self._fire(plan)
                        if fired.status in {Status.FILLED, Status.WORKING, Status.SKIPPED, Status.FAILED}:
                            done.append(pid)
                elif sig.venue == Venue.SOLANA and sig.mint:
                    quote = self.sol.quote_token(sig.mint)
                    plan.last_price = quote.get("price")
                    plan.last_mcap = quote.get("mcap")
                    if self.sol.condition_met(sig, quote) or self._sol_price_hit(sig, quote):
                        sig.intent = Intent.MARKET
                        fired = self._fire(plan)
                        if fired.status in {Status.FILLED, Status.WORKING, Status.SKIPPED, Status.FAILED}:
                            done.append(pid)
            except Exception:
                log.exception("poll failed %s", pid)
        for pid in done:
            self.plans.pop(pid, None)
            self.store.drop_plan(pid)

    def _prepare_breakout(self, signal: Signal) -> None:
        if not signal.symbol:
            return
        mark = self.hl.mark(signal.symbol)
        if signal.stop_loss and mark:
            signal.stop_loss = self._scale_price(signal.stop_loss, mark)
            signal.invalidation = signal.stop_loss
        if not signal.trigger_price:
            # 「黄线 / 前高」没写数字：用 8H 近期前高代理。
            hi = self.hl.recent_high(signal.symbol, signal.confirm_tf or "8h", bars=10)
            if hi:
                signal.trigger_price = hi
                signal.reason = (signal.reason + f";level:{hi:.4g}").strip(";")

    def _breakout_note(self, signal: Signal) -> str:
        return (
            f"wait breakout {signal.symbol} > {signal.trigger_price} "
            f"then {signal.confirm_tf or '8h'} close confirm; sl={signal.stop_loss} size_mult={signal.size_mult}"
        )

    def _breakout_ready(self, plan: Plan) -> bool:
        sig = plan.signal
        if not sig.symbol:
            return False
        px = self.hl.mark(sig.symbol)
        plan.last_price = px
        if px is None:
            return False
        level = sig.trigger_price
        if not level:
            before = sig.trigger_price
            self._prepare_breakout(sig)
            level = sig.trigger_price
            if level and level != before:
                for k, v in self.plans.items():
                    if v is plan:
                        payload = plan.to_dict()
                        payload["execution_mode"] = "paper" if self.dry_run else "live"
                        self.store.put_plan(k, payload)
                        break
            if not level:
                return False
        if sig.side == Side.BUY and px < level:
            return False
        if sig.side == Side.SELL and px > level:
            return False
        tf = sig.confirm_tf or "8h"
        candle = self.hl.last_closed_candle(sig.symbol, tf)
        if not candle:
            return False
        close = candle["c"]
        high = candle["h"]
        low = candle["l"]
        if sig.side == Side.BUY:
            return close > level and high >= level
        return close < level and low <= level

    @staticmethod
    def _scale_price(raw: float, mark: float) -> float:
        if raw <= 0 or mark <= 0:
            return raw
        if 0.4 * mark <= raw <= 2.5 * mark:
            return raw
        for factor in (10, 100, 1000, 0.1, 0.01):
            scaled = raw * factor
            if 0.4 * mark <= scaled <= 2.5 * mark:
                return scaled
        return raw

    def _sol_price_hit(self, sig: Signal, quote: dict[str, Any]) -> bool:
        px = quote.get("price")
        if not px or not sig.trigger_price:
            return False
        if sig.side == Side.BUY:
            return px <= sig.trigger_price
        return px >= sig.trigger_price
