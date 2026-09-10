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

    def test_no_image_uses_proxy(self):
        plan = self.eng.handle_text("POPIGOGO", 7218, "2026-08-19T15:13:24+00:00", ETH_LONG)
        sig = plan.signal
        self.assertAlmostEqual(sig.trigger_price, 2490.4)
        self.assertNotIn("chart_vision", sig.reason)


if __name__ == "__main__":
    unittest.main()
