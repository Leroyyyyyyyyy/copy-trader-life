"""Test the live chart-vision path in Engine.handle_text (mocked vision)."""

import json
import unittest
from unittest import mock

import yaml

from src.engine import Engine
from src.models import Intent, Side

CFG = yaml.safe_load(open("config.yaml"))

ETH_LONG = (
    "ETH這張多單 雖然走了個頂部結構 但還在黏的話可能就是還沒結束 "
    "正巧這裡附近是上一個前高的地方 可能可以做一個多單 由於我整體看空的 "
    "所以多單的部位不會跟平常一樣大 會開小一些 "
    "條件1:漲過黃線 條件2:8H收線在上方 更: 止損我還是拉到1800好了"
)


class ChartVisionPathTests(unittest.TestCase):
    def setUp(self):
        self.eng = Engine(CFG, dry_run=True)
        # avoid network: stub HL mark and connect
        self.eng.hl.mark = mock.Mock(return_value=2000.0)
        self.eng.hl.recent_high = mock.Mock(return_value=2490.4)
        # in-memory store: no cross-run seen collisions, no disk writes
        self.eng.store.data["seen"] = []
        self.eng.store.save = mock.Mock()
        # the LLM gate has its own tests; keep these offline
        self.eng.cfg = {**self.eng.cfg, "llm_gate": {"enabled": False}}

    def test_chart_vision_fills_trigger_and_tp_keeps_text_sl(self):
        with mock.patch("src.chart_reader.read_levels_roles", return_value={
            "entry": 1868.66,
            "stop_loss": 1799.82,
            "take_profit": 2200.9,
        }):
            plan = self.eng.handle_text(
                "POPIGOGO", 7217, "2026-08-19T15:13:24+00:00", ETH_LONG,
                image_path="data/charts/orig_7217.jpg",
            )
        sig = plan.signal
        self.assertEqual(sig.intent, Intent.CONFIRM_BREAKOUT)
        self.assertEqual(sig.side, Side.BUY)
        # trigger from vision (not the 2490 recent-high proxy)
        self.assertAlmostEqual(sig.trigger_price, 1868.66)
        # text SL wins over vision SL
        self.assertAlmostEqual(sig.stop_loss, 1800.0)
        # TP from vision
        self.assertAlmostEqual(sig.take_profit, 2200.9)
        self.assertIn("chart_vision", sig.reason)

    def test_chart_without_verified_stop_is_not_armed(self):
        text = "ETH 條件1:漲過黃線 條件2:8H收線在上方"
        with mock.patch("src.chart_reader.read_levels_roles", return_value={
            "entry": None, "stop_loss": None, "take_profit": None, "review": None,
        }):
            plan = self.eng.handle_text("POPIGOGO", 7219, "2026-10-01T00:00:00+00:00", text,
                                        image_path="data/charts/orig_7217.jpg")
        self.assertEqual(plan.signal.skip_reason, "chart_stop_unverified")
        self.assertEqual(plan.signal.intent, Intent.IGNORE)

    def test_llm_gate_blocks_a_brag(self):
        from src.llm_gate import GateDecision

        self.eng.cfg = {**self.eng.cfg, "llm_gate": {"enabled": True}}
        brag = "ETH 到我的絕殺買點 說過我有內線了 你們不信"
        with mock.patch("src.llm_gate.review", return_value=GateDecision(allow=False, verdict="not_actionable")) as gate:
            plan = self.eng.handle_text("POPIGOGO", 7301, "2026-10-01T00:00:00+00:00", brag)
        self.assertEqual(gate.call_count, 3)  # review_many asks three times
        self.assertEqual(gate.call_args.kwargs["parsed"]["intent"], "market")
        self.assertEqual(plan.signal.skip_reason, "llm_gate:not_actionable")
        self.assertEqual(plan.signal.intent, Intent.IGNORE)

    def test_llm_gate_passes_a_setup_with_context(self):
        from src.llm_gate import GateDecision

        self.eng.cfg = {**self.eng.cfg, "llm_gate": {"enabled": True}}
        self.eng.handle_text("POPIGOGO", 7302, "", "嘿嘿嘿")
        with mock.patch("src.llm_gate.review",
                        return_value=GateDecision(allow=True, verdict="conditional_setup", side="buy", symbol="ETH")) as gate:
            plan = self.eng.handle_text("POPIGOGO", 7303, "2026-08-19T15:13:24+00:00", ETH_LONG)
        kwargs = gate.call_args.kwargs
        self.assertEqual(kwargs["context"], ["嘿嘿嘿"])
        self.assertEqual(kwargs["parsed"]["intent"], "confirm_breakout")
        self.assertEqual(plan.signal.intent, Intent.CONFIRM_BREAKOUT)
        self.assertIn("llm_gate:allow:conditional_setup", plan.signal.reason)

    def test_repost_replaces_pending_setup(self):
        # POPIGOGO #7841/#7843/#7862/#7885: the same ETH setup posted four times with
        # stops moved; only the newest may stay armed.
        stops = iter([2629.32, 2591.85])
        with mock.patch("src.chart_reader.read_levels_roles", side_effect=lambda *a, **k: {
                "entry": 2704.08, "stop_loss": next(stops), "take_profit": None}):
            text = ("ETH的踏空者們 我覺得可以關注這單的機會 這算是突破後的回調 如果只是回調的話就可以看這條件介入 "
                    "條件1:漲超過黃線 條件2:8H收縣在上方 成立條件很重要哦")
            self.eng.handle_text("POPIGOGO", 7841, "", text, image_path="x.jpg")
            plan = self.eng.handle_text("POPIGOGO", 7843, "", text, image_path="x.jpg")
        self.assertEqual([p.signal.message_id for p in self.eng.plans.values()], [7843])
        self.assertAlmostEqual(plan.signal.stop_loss, 2591.85)
        self.assertIn("replaces", plan.note)

    def test_chart_reads_that_disagree_go_to_review(self):
        import tempfile
        from pathlib import Path

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.eng.store.path = Path(tmp.name) / "state.json"
        options = [{"entry": 2704.08, "stop_loss": 2598.88}, {"entry": 2704.08, "stop_loss": 2648.76}]
        with mock.patch("src.chart_reader.read_levels_roles", return_value={
                "entry": None, "stop_loss": None, "take_profit": None,
                "review": {"reason": "chart reads disagree", "options": options}}):
            plan = self.eng.handle_text("POPIGOGO", 7862, "", "ETH 條件1:漲過黃線 條件2:8H收線在上方",
                                        image_path="x.jpg")
        self.assertEqual(plan.status.value, "review")
        [card] = self.eng.store.data["reviews"].values()
        self.assertEqual(card["options"]["chart_options"], options)
        self.assertEqual(self.eng.plans, {})

    def test_no_image_uses_proxy(self):
        plan = self.eng.handle_text("POPIGOGO", 7218, "2026-08-19T15:13:24+00:00", ETH_LONG)
        sig = plan.signal
        self.assertAlmostEqual(sig.trigger_price, 2490.4)
        self.assertNotIn("chart_vision", sig.reason)



class GateExitAndReviewTests(unittest.TestCase):
    """close / cancel_setup from the gate, and human review through the inbox."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.eng = Engine(CFG, dry_run=True)
        self.eng.hl.mark = mock.Mock(return_value=2000.0)
        self.eng.hl.recent_high = mock.Mock(return_value=2490.4)
        self.eng.store.path = Path(self.tmp.name) / "state.json"  # inbox and saves go to the temp dir
        self.eng.store.data = {"plans": {}, "positions": {}, "seen": []}
        self.eng.cfg = {**self.eng.cfg, "llm_gate": {"enabled": True, "samples": 3}}

    def gate(self, **kw):
        from src.llm_gate import GateDecision

        return mock.patch("src.llm_gate.review_many", return_value=GateDecision(**kw))

    def arm_eth_setup(self, mid=7841):
        with self.gate(allow=True, verdict="conditional_setup", action="allow", symbol="ETH"):
            return self.eng.handle_text("POPIGOGO", mid, "", ETH_LONG)

    def test_close_closes_only_what_the_channel_holds(self):
        exit_text = "我把我的ETH多單出光"  # POPIGOGO #8041, parsed as a buy
        with self.gate(allow=False, verdict="close", action="close", symbol="ETH"):
            plan = self.eng.handle_text("POPIGOGO", 8040, "", exit_text)
        self.assertEqual(plan.signal.skip_reason, "llm_gate:close:nothing_held")
        self.eng.risk.mark_fill("hl:ETH", {"signal": {"channel": "POPIGOGO"}})
        with self.gate(allow=False, verdict="close", action="close", symbol="ETH"):
            plan = self.eng.handle_text("POPIGOGO", 8041, "", exit_text)
        self.assertEqual((plan.signal.side, plan.status.value), (Side.CLOSE, "filled"))
        self.assertNotIn("hl:ETH", self.eng.risk.state.open_positions)

    def test_other_channels_position_is_not_closed(self):
        self.eng.risk.mark_fill("hl:ETH", {"signal": {"channel": "SOMEONE_ELSE"}})
        with self.gate(allow=False, verdict="close", action="close", symbol="ETH"):
            plan = self.eng.handle_text("POPIGOGO", 8041, "", "我把我的ETH多單出光")
        self.assertEqual(plan.signal.skip_reason, "llm_gate:close:nothing_held")
        self.assertIn("hl:ETH", self.eng.risk.state.open_positions)

    def test_cancel_setup_drops_the_pending_plan(self):
        self.arm_eth_setup()
        self.assertEqual(len(self.eng.plans), 1)
        with self.gate(allow=False, verdict="cancel_setup", action="cancel", symbol="ETH"):
            plan = self.eng.handle_text("POPIGOGO", 7977, "", "ETH 條件1:漲過黃線 條件2: 6H收線在上方 此單失效且無觸發")
        self.assertEqual(plan.status.value, "cancelled")
        self.assertEqual(self.eng.plans, {})

    def review_card(self, mid=7048):
        with self.gate(allow=False, verdict="split", action="review", reason="split:allow,block", symbol="ETH"):
            plan = self.eng.handle_text("POPIGOGO", mid, "", ETH_LONG)
        self.assertEqual(plan.status.value, "review")
        rid = next(iter(self.eng.store.data["reviews"]))
        return rid

    def decide(self, rid, decision, **extra):
        inbox = self.eng.review_inbox
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / f"{rid}-{decision}.json").write_text(json.dumps({"review_id": rid, "decision": decision, **extra}))
        return self.eng.process_reviews()

    def test_split_vote_is_held_then_approved(self):
        rid = self.review_card()
        self.assertEqual(self.eng.plans, {})  # nothing armed while waiting
        [res] = self.decide(rid, "approve")
        self.assertEqual(res["outcome"], "pending")
        [plan] = self.eng.plans.values()
        self.assertIn(f"human_approved:{rid}", plan.signal.reason)
        self.assertEqual(self.decide(rid, "approve")[0]["outcome"], "unknown_or_done")  # idempotent

    def test_reject_and_expiry(self):
        rid = self.review_card()
        self.assertEqual(self.decide(rid, "reject")[0]["outcome"], "rejected")
        rid2 = self.review_card(7049)
        self.eng.store.data["reviews"][rid2]["expires_at"] = 0
        self.assertEqual(self.decide(rid2, "approve")[0]["outcome"], "expired")
        self.assertEqual(self.eng.plans, {})

    def test_cli_round_trip(self):
        import contextlib
        import io

        from src import review as cli

        rid = self.review_card()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--data-dir", self.tmp.name, "list"]), 0)
            self.assertEqual(cli.main(["--data-dir", self.tmp.name, "approve", rid, "--stop", "1795"]), 0)
        self.assertIn(rid, out.getvalue())
        [res] = self.eng.process_reviews()
        self.assertEqual(res["outcome"], "pending")
        self.assertAlmostEqual(next(iter(self.eng.plans.values())).signal.stop_loss, 1795.0)
        self.assertEqual(list(self.eng.review_inbox.glob("*")), [])

    def test_entry_without_stop_needs_one_from_the_reviewer(self):
        rid = self.review_card()
        self.eng.store.data["reviews"][rid]["signal"]["stop_loss"] = None
        self.assertEqual(self.decide(rid, "approve")[0]["outcome"], "need_stop_loss")
        self.assertIn(rid, self.eng.store.data["reviews"])
        res = self.decide(rid, "approve", stop_loss=1790)
        self.assertEqual(res[0]["outcome"], "pending")
        self.assertAlmostEqual(next(iter(self.eng.plans.values())).signal.stop_loss, 1790.0)


if __name__ == "__main__":
    unittest.main()
