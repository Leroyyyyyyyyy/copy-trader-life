from __future__ import annotations

import json
from pathlib import Path
import threading
import time
import uuid
from collections import deque
from dataclasses import replace
from typing import Any, Optional

from .hyperliquid_venue import HyperliquidVenue
from .models import Intent, Plan, Side, Signal, Status, Venue
from .parser import parse_message
from .risk import RiskGuard
from .solana_venue import SolanaVenue
from .store import JsonStore
from .util import setup_logging

log = setup_logging()

# Parsed intents that would act; the LLM gate reviews these before execution.
GATED_INTENTS = {Intent.MARKET, Intent.LIMIT, Intent.CONFIRM_BREAKOUT, Intent.WAIT_TARGET, Intent.UPDATE, Intent.CLOSE}


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
        self.recent_texts: dict[str, deque] = {}
        self.plans: dict[str, Plan] = {}
        # handle_text runs in worker threads, poll_pending on the event loop: state changes take this lock.
        self._lock = threading.RLock()
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
            try:
                signal = self._signal_from_dict(raw.get("signal") or {})
            except Exception:
                continue
            if signal.intent in {Intent.WAIT_TARGET, Intent.CONFIRM_BREAKOUT}:
                plan = Plan(signal=signal, status=Status.PENDING, created_at=raw.get("created_at") or time.time())
                self.plans[pid] = plan
                self.risk.mark_pending(signal.key())
                log.info("restored pending plan %s %s", pid, signal.key())

    @staticmethod
    def _signal_from_dict(sig: dict[str, Any]) -> Signal:
        return Signal(
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
            size_usd=sig.get("size_usd"),
            size_mult=float(sig.get("size_mult") or 1),
            breakout=bool(sig.get("breakout")),
            confirm_tf=sig.get("confirm_tf"),
            invalidation=sig.get("invalidation"),
            confidence=float(sig.get("confidence") or 0),
            reason=sig.get("reason") or "",
            skip_reason=sig.get("skip_reason"),
        )

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
            "channel": signal.channel,
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

        recent = self.recent_texts.setdefault(channel, deque(maxlen=6))
        context = list(recent)
        if text:
            recent.append(text)

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
            other_assets=ch.get("other_assets"),
        )
        boost = float(ch.get("confidence_boost") or 0)
        if boost and not signal.skip_reason:
            signal.confidence = max(0.0, min(0.99, signal.confidence + boost))
            signal.reason = (signal.reason + f";boost:{boost:+.2f}").strip(";")

        review_reasons: list[str] = []
        review_options: dict[str, Any] = {}
        gate_cfg = self.cfg.get("llm_gate") or {}
        if (
            gate_cfg.get("enabled")
            and not signal.skip_reason
            and signal.intent in GATED_INTENTS
        ):
            from .llm_gate import review_many

            decision = review_many(
                samples=int(gate_cfg.get("samples", 3)),
                text=text,
                context=context,
                parsed={"intent": signal.intent.value, "side": signal.side.value, "symbol": signal.symbol},
                has_image=bool(image_path),
                on_error=str(gate_cfg.get("on_error", "skip")),
            )
            log.info("llm gate #%s action=%s verdict=%s %s", message_id, decision.action, decision.verdict, decision.reason)
            signal.reason = (signal.reason + f";llm_gate:{decision.action}:{decision.verdict}").strip(";")
            if decision.action in ("close", "cancel"):
                exited = self._exit_from_gate(signal, decision)
                if exited is not None:
                    return exited
                signal.skip_reason = f"llm_gate:{decision.verdict}:nothing_held"
                signal.intent = Intent.IGNORE
            elif decision.action == "review":
                review_reasons.append(f"llm_gate:{decision.reason}")
            elif not decision.allow:
                signal.skip_reason = f"llm_gate:{decision.verdict}" + (f":{decision.error}" if decision.error else "")
                signal.intent = Intent.IGNORE

        # 结构突破单带图时，用 deepseek vision 读图上的 trigger/SL/TP，
        # 不再用近期高/低点代理（读图失败才回退代理）。
        if (
            signal.intent == Intent.CONFIRM_BREAKOUT
            and signal.venue == Venue.HYPERLIQUID
            and signal.symbol
            and image_path
        ):
            chart_cfg = self.cfg.get("chart") or {}
            try:
                from .chart_reader import read_levels_detail, read_levels_roles

                try:
                    ref = self.hl.mark(signal.symbol)
                except Exception:  # noqa: BLE001 - scale check is best effort
                    ref = None
                tf = signal.confirm_tf or "8h"
                if chart_cfg.get("mode", "roles") == "roles":
                    lv = read_levels_roles(image_path, signal.symbol, signal.side.value, tf, reference_price=ref,
                                           samples=int(chart_cfg.get("samples", 2)))
                else:
                    lv = read_levels_detail(image_path, signal.symbol, signal.side.value, tf,
                                            reference_price=ref, ocr_mode="ocr_primary")
                if signal.trigger_price is None and lv.get("entry"):
                    signal.trigger_price = float(lv["entry"])
                if signal.stop_loss is None and lv.get("stop_loss"):
                    signal.stop_loss = float(lv["stop_loss"])
                if signal.take_profit is None and lv.get("take_profit"):
                    signal.take_profit = float(lv["take_profit"])
                if lv.get("review"):
                    review_reasons.append("chart:" + lv["review"]["reason"])
                    review_options["chart_options"] = lv["review"]["options"]
                signal.reason = (signal.reason + ";chart_vision").strip(";")
                log.info(
                    "chart vision #%s %s -> trig=%s sl=%s tp=%s rejected=%s",
                    message_id, signal.symbol, signal.trigger_price, signal.stop_loss, signal.take_profit,
                    lv.get("rejected"),
                )
            except Exception:
                log.exception("chart vision failed #%s", message_id)
            if signal.stop_loss is None and not signal.skip_reason and not review_reasons:
                # The chart carried the levels; without a stop that vision and OCR
                # agree on, do not arm the setup on a proxy trigger.
                signal.skip_reason = "chart_stop_unverified"
                signal.intent = Intent.IGNORE
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
        if review_reasons and not signal.skip_reason:
            return self._hold_for_review(signal, review_reasons, review_options)
        return self.apply(signal)

    def apply(self, signal: Signal) -> Plan:
        with self._lock:
            return self._apply(signal)

    def _apply(self, signal: Signal) -> Plan:
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
            superseded = self._supersede_pending(signal)
            note = self._breakout_note(signal) + (f"; replaces {','.join(superseded)}" if superseded else "")
            return self._park(plan, note)
        # HL: 限价/触发单立刻挂到交易所，不在本地等。
        # Solana 土狗没有稳定盘口，本地盯价/市值，触及再市价。
        if signal.venue == Venue.SOLANA and signal.intent in {Intent.WAIT_TARGET, Intent.LIMIT}:
            if signal.limit_price and not signal.trigger_price:
                signal.trigger_price = signal.limit_price
            signal.intent = Intent.WAIT_TARGET
            plan.signal = signal
            return self._park(plan, "solana wait for price/mcap")
        return self._fire(plan)

    def _channel_holds(self, channel: str, key: str) -> bool:
        """Does this channel own a bot position on `key`? (paper: risk state; live: store)."""
        meta = self.risk.state.open_positions.get(key) or {}
        if ((meta.get("signal") or {}).get("channel")) == channel:
            return True
        entry = self.store.position(key) or {}
        return entry.get("source") == "bot" and entry.get("channel") == channel

    def _drop_pending(self, channel: str, key: str, why: str) -> list[str]:
        dropped = [pid for pid, p in self.plans.items()
                   if p.status == Status.PENDING and p.signal.key() == key and p.signal.channel == channel]
        for pid in dropped:
            self.plans.pop(pid)
            self.store.drop_plan(pid)
            self.risk.clear_pending(key)
            log.info("pending %s dropped: %s", pid, why)
        return dropped

    def _exit_from_gate(self, signal: Signal, decision: Any) -> Optional[Plan]:
        """The author exits ("close") or voids a setup ("cancel_setup"). Act only on
        what this channel made the bot hold: cancel its pending setups on that
        symbol and, for close, close its position. Returns None if nothing is held."""
        if not decision.symbol:
            return None
        key = self._hl_key(decision.symbol)
        with self._lock:
            dropped = self._drop_pending(signal.channel, key, f"{decision.verdict} #{signal.message_id}")
            if decision.action == "close" and self._channel_holds(signal.channel, key):
                close = replace(signal, side=Side.CLOSE, intent=Intent.CLOSE, venue=Venue.HYPERLIQUID,
                                symbol=decision.symbol.upper(), mint=None, skip_reason=None,
                                take_profit=None, stop_loss=None, trigger_price=None, limit_price=None)
                plan = self._apply(close)
                if dropped:
                    plan.note = (plan.note + f"; cancelled pending {','.join(dropped)}").strip("; ")
                return plan
        if not dropped:
            return None
        return Plan(signal=signal, status=Status.CANCELLED, created_at=time.time(),
                    note=f"{decision.verdict}: cancelled pending {','.join(dropped)}")

    # ---- human review -------------------------------------------------------------
    # The engine is the only writer of state.json. `python -m src.review` drops a
    # decision file into <data>/review_inbox/; poll_pending applies it.

    @property
    def review_inbox(self) -> Path:
        return self.store.path.parent / "review_inbox"

    def _hold_for_review(self, signal: Signal, reasons: list[str], options: Optional[dict[str, Any]] = None) -> Plan:
        with self._lock:
            rid = str(uuid.uuid4())[:8]
            ttl = float((self.cfg.get("review") or {}).get("expire_sec") or 12 * 3600)
            now = time.time()
            self.store.data.setdefault("reviews", {})[rid] = {
                "signal": signal.to_dict(), "reasons": reasons, "options": options or {},
                "created_at": now, "expires_at": now + ttl,
                "execution_mode": "paper" if self.dry_run else "live",
                "pending_same_channel": [
                    {"plan_id": pid, "symbol": p.signal.symbol, "side": p.signal.side.value,
                     "message_id": p.signal.message_id}
                    for pid, p in self.plans.items() if p.signal.channel == signal.channel],
            }
            self.store.save()
        log.warning("REVIEW %s %s #%s %s %s: %s", rid, signal.channel, signal.message_id,
                    signal.side.value, signal.symbol, "; ".join(reasons))
        return Plan(signal=signal, status=Status.REVIEW, created_at=now, note=f"review {rid}: " + "; ".join(reasons))

    def process_reviews(self) -> list[dict[str, Any]]:
        inbox = self.review_inbox
        if not inbox.exists():
            return []
        results = []
        for path in sorted(inbox.glob("*.json")):
            try:
                cmd = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                log.exception("bad review command %s", path.name)
                path.rename(path.with_suffix(".bad"))
                continue
            with self._lock:
                result = self._apply_review(cmd)
                self.store.data.setdefault("review_log", []).append(result)
                self.store.save()
            path.unlink(missing_ok=True)
            log.info("review %s %s -> %s", cmd.get("review_id"), cmd.get("decision"), result["outcome"])
            results.append(result)
        return results

    def _apply_review(self, cmd: dict[str, Any]) -> dict[str, Any]:
        rid, decision = str(cmd.get("review_id")), cmd.get("decision")
        out = {"review_id": rid, "decision": decision, "command_id": cmd.get("command_id"), "at": time.time()}
        card = self.store.data.setdefault("reviews", {}).get(rid)
        if card is None:
            return {**out, "outcome": "unknown_or_done"}
        if card.get("execution_mode") != ("paper" if self.dry_run else "live"):
            return {**out, "outcome": "wrong_mode"}
        if time.time() > float(card["expires_at"]):
            self.store.data["reviews"].pop(rid)
            return {**out, "outcome": "expired"}
        if decision == "reject":
            self.store.data["reviews"].pop(rid)
            return {**out, "outcome": "rejected"}
        if decision != "approve":
            return {**out, "outcome": "bad_decision"}
        signal = self._signal_from_dict(card["signal"])
        for field_name in ("trigger_price", "stop_loss", "take_profit"):
            if cmd.get(field_name) is not None:
                setattr(signal, field_name, float(cmd[field_name]))
        if signal.side in {Side.BUY, Side.SELL} and signal.intent != Intent.UPDATE and signal.stop_loss is None:
            return {**out, "outcome": "need_stop_loss"}  # card stays open; approve again with --stop
        self.store.data["reviews"].pop(rid)
        signal.reason = (signal.reason + f";human_approved:{rid}").strip(";")
        plan = self._apply(signal)  # risk checks run again on today's state
        return {**out, "outcome": plan.status.value, "note": plan.note}

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

    def _supersede_pending(self, signal: Signal) -> list[str]:
        """A new setup for a symbol replaces this channel's pending one: authors
        repost the same setup with a moved stop, and each repost must not arm
        another plan (they would all fire on the same breakout)."""
        old = [pid for pid, p in self.plans.items()
               if p.status == Status.PENDING and p.signal.key() == signal.key()
               and p.signal.channel == signal.channel]
        for pid in old:
            self.plans.pop(pid)
            self.store.drop_plan(pid)
            log.info("pending %s superseded by %s #%s", pid, signal.channel, signal.message_id)
        return old

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
        self.process_reviews()
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
