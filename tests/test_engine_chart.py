"""Test the live chart-vision path in Engine.handle_text (mocked vision)."""

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
        with mock.patch("src.chart_reader.read_levels", return_value={
            "yellow_or_trigger": 1868.66,
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
        with mock.patch("src.chart_reader.read_levels", return_value={
            "yellow_or_trigger": None, "stop_loss": None, "take_profit": None,
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
        gate.assert_called_once()
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
        self.assertIn("llm_gate:conditional_setup", plan.signal.reason)

    def test_repost_replaces_pending_setup(self):
        # POPIGOGO #7841/#7843/#7862/#7885: the same ETH setup posted four times with
        # stops moved; only the newest may stay armed.
        stops = iter([2629.32, 2591.85])
        with mock.patch("src.chart_reader.read_levels", side_effect=lambda *a, **k: {
                "yellow_or_trigger": 2704.08, "stop_loss": next(stops), "take_profit": None}):
            text = ("ETH的踏空者們 我覺得可以關注這單的機會 這算是突破後的回調 如果只是回調的話就可以看這條件介入 "
                    "條件1:漲超過黃線 條件2:8H收縣在上方 成立條件很重要哦")
            self.eng.handle_text("POPIGOGO", 7841, "", text, image_path="x.jpg")
            plan = self.eng.handle_text("POPIGOGO", 7843, "", text, image_path="x.jpg")
        self.assertEqual([p.signal.message_id for p in self.eng.plans.values()], [7843])
        self.assertAlmostEqual(plan.signal.stop_loss, 2591.85)
        self.assertIn("replaces", plan.note)

    def test_no_image_uses_proxy(self):
        plan = self.eng.handle_text("POPIGOGO", 7218, "2026-08-19T15:13:24+00:00", ETH_LONG)
        sig = plan.signal
        self.assertAlmostEqual(sig.trigger_price, 2490.4)
        self.assertNotIn("chart_vision", sig.reason)


if __name__ == "__main__":
    unittest.main()
