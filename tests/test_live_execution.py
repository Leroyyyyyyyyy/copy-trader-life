"""No-network checks for the Hyperliquid live order path."""

import unittest
from unittest import mock

from src.hyperliquid_venue import HyperliquidVenue
from src.models import Intent, Side, Signal, Venue


def signal(side=Side.BUY, **kwargs):
    base = dict(
        channel="POPIGOGO",
        message_id=1,
        date="",
        text="test",
        side=side,
        intent=Intent.MARKET,
        venue=Venue.HYPERLIQUID,
        symbol="ETH",
        confidence=0.9,
        take_profit=2200.0,
        stop_loss=1800.0,
    )
    base.update(kwargs)
    return Signal(**base)


class HyperliquidLiveExecutionTests(unittest.TestCase):
    def setUp(self):
        self.venue = HyperliquidVenue(dry_run=False)
        self.venue.universe = {"ETH": {"szDecimals": 3}}
        self.venue.mids = {"ETH": 2000.0}
        self.venue.exchange = mock.Mock()

    def test_accepted_market_order_attaches_reduce_only_tp_and_sl(self):
        self.venue.exchange.market_open.return_value = {"status": "ok"}
        self.venue.exchange.order.side_effect = [{"status": "ok"}, {"status": "ok"}]

        result = self.venue.execute(signal(), usd=80, leverage=3)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["sz"], 0.04)
        self.venue.exchange.update_leverage.assert_called_once_with(3, "ETH", is_cross=True)
        self.venue.exchange.market_open.assert_called_once_with("ETH", True, 0.04, None, 0.01)
        self.assertEqual(self.venue.exchange.order.call_count, 2)
        tp, sl = self.venue.exchange.order.call_args_list
        self.assertEqual(tp.args[:4], ("ETH", False, 0.04, 2200.0))
        self.assertEqual(tp.args[4]["trigger"]["tpsl"], "tp")
        self.assertTrue(tp.kwargs["reduce_only"])
        self.assertEqual(sl.args[:4], ("ETH", False, 0.04, 1800.0))
        self.assertEqual(sl.args[4]["trigger"]["tpsl"], "sl")

    def test_short_exits_are_buy_reduce_only_orders(self):
        self.venue.exchange.market_open.return_value = {"status": "ok"}
        self.venue.exchange.order.side_effect = [{"status": "ok"}, {"status": "ok"}]

        self.venue.execute(signal(Side.SELL, take_profit=1800, stop_loss=2200), usd=80)

        self.assertTrue(all(call.args[1] for call in self.venue.exchange.order.call_args_list))

    def test_rejected_market_order_does_not_attach_exit_orders(self):
        self.venue.exchange.market_open.return_value = {"status": "err", "response": "rejected"}

        result = self.venue.execute(signal(), usd=80)

        self.assertEqual(result["status"], "err")
        self.assertEqual(result["exit_orders"], [])
        self.venue.exchange.order.assert_not_called()

    def test_stop_order_is_attempted_when_take_profit_order_fails(self):
        self.venue.exchange.market_open.return_value = {"status": "ok"}
        self.venue.exchange.order.side_effect = [RuntimeError("tp rejected"), {"status": "ok"}]

        result = self.venue.execute(signal(), usd=80)

        self.assertEqual(self.venue.exchange.order.call_count, 2)
        self.assertEqual(result["exit_orders"][0]["kind"], "tp")
        self.assertEqual(result["exit_orders"][1]["kind"], "sl")
        self.assertEqual(result["exit_orders"][0]["error"], "tp rejected")


if __name__ == "__main__":
    unittest.main()
