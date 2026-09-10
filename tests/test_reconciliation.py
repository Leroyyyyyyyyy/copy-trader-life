import unittest
from unittest import mock

import yaml

from src.engine import Engine

CFG = yaml.safe_load(open("config.yaml"))


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine(CFG, dry_run=False)
        self.engine.store.data = {"plans": {}, "positions": {}, "seen": []}
        self.engine.store.save = mock.Mock()
        self.engine.hl.info = mock.Mock()
        self.engine.hl.account_address = "0xabc"
        self.engine.hl.cancel_orders = mock.Mock(return_value=[])

    def set_state(self, positions, orders):
        self.engine.hl.info.user_state.return_value = {"assetPositions": positions}
        self.engine.hl.open_orders = mock.Mock(return_value=orders)

    @staticmethod
    def position(coin="ETH", size="0.04"):
        return {"position": {"coin": coin, "szi": size, "entryPx": "2000"}}

    @staticmethod
    def order(oid, reduce_only=True):
        return {"coin": "ETH", "oid": oid, "reduceOnly": reduce_only}

    def test_complete_bot_protection_is_verified(self):
        self.engine.store.put_position("hl:ETH", {
            "coin": "ETH", "side": "buy", "size": 0.04, "source": "bot",
            "managed_exits": [{"kind": "tp", "oid": 11}, {"kind": "sl", "oid": 12}],
            "requested_exits": ["tp", "sl"], "at_risk": True, "risk_reasons": ["old"],
        })
        self.set_state([self.position()], [self.order(11), self.order(12)])

        self.engine.reconcile_hyperliquid(force=True)

        saved = self.engine.store.position("hl:ETH")
        self.assertFalse(saved["at_risk"])
        self.assertEqual(saved["risk_reasons"], [])
        self.assertEqual(self.engine.store.data["hyperliquid_reconciliation"]["positions"], ["hl:ETH"])

    def test_missing_exit_marks_position_at_risk(self):
        self.engine.store.put_position("hl:ETH", {
            "coin": "ETH", "side": "buy", "size": 0.04, "source": "bot",
            "managed_exits": [{"kind": "tp", "oid": 11}, {"kind": "sl", "oid": 12}],
            "requested_exits": ["tp", "sl"], "at_risk": False, "risk_reasons": [],
        })
        self.set_state([self.position()], [self.order(11)])

        self.engine.reconcile_hyperliquid(force=True)

        saved = self.engine.store.position("hl:ETH")
        self.assertTrue(saved["at_risk"])
        self.assertEqual(saved["risk_reasons"], ["missing_sl"])

    def test_external_position_is_recorded_without_managing_orders(self):
        self.set_state([self.position()], [self.order(99)])

        self.engine.reconcile_hyperliquid(force=True)

        saved = self.engine.store.position("hl:ETH")
        self.assertEqual(saved["source"], "external")
        self.assertEqual(saved["managed_exits"], [])
        self.engine.hl.cancel_orders.assert_not_called()
        self.assertIn("hl:ETH", self.engine.risk.state.open_positions)

    def test_disappeared_bot_position_cancels_only_tracked_orders(self):
        self.engine.store.put_position("hl:ETH", {
            "coin": "ETH", "side": "buy", "size": 0.04, "source": "bot",
            "managed_exits": [{"kind": "tp", "oid": 11}, {"kind": "sl", "oid": 12}],
            "requested_exits": ["tp", "sl"], "at_risk": False, "risk_reasons": [],
        })
        self.set_state([], [self.order(11), self.order(12), self.order(99)])

        self.engine.reconcile_hyperliquid(force=True)

        self.engine.hl.cancel_orders.assert_called_once_with("ETH", [11, 12])
        self.assertIsNone(self.engine.store.position("hl:ETH"))
        self.assertNotIn("hl:ETH", self.engine.risk.state.open_positions)


if __name__ == "__main__":
    unittest.main()
