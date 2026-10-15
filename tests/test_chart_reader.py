import json
import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.chart_reader import read_levels, read_levels_detail


class ChartReaderValidationTests(unittest.TestCase):
    def read_reply(self, reply, side, detail=False, reference_price=None):
        response = mock.Mock(status_code=200)
        content = reply if isinstance(reply, str) else json.dumps(reply)
        response.json.return_value = {"choices": [{"message": {"content": content}}]}
        client = mock.MagicMock()
        client.post.return_value = response
        context = mock.MagicMock()
        context.__enter__.return_value = client
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as image:
            image.write(b"not-a-real-jpeg")
            path = Path(image.name)
        try:
            with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=False), mock.patch(
                "src.chart_reader.httpx.Client", return_value=context
            ):
                if detail:
                    return read_levels_detail(path, "ETH", side, "8h", reference_price=reference_price)
                return read_levels(path, "ETH", side, "8h")
        finally:
            path.unlink(missing_ok=True)

    def test_long_discards_reversed_stop_and_target(self):
        result = self.read_reply(
            {"yellow_or_trigger": 2000, "stop_loss": 2100, "take_profit": 1900}, "buy"
        )
        self.assertEqual(result, {"yellow_or_trigger": 2000.0, "stop_loss": None, "take_profit": None})

    def test_short_keeps_correctly_oriented_levels(self):
        result = self.read_reply(
            {"yellow_or_trigger": 2000, "stop_loss": 2100, "take_profit": 1800}, "sell"
        )
        self.assertEqual(result, {"yellow_or_trigger": 2000.0, "stop_loss": 2100.0, "take_profit": 1800.0})

    def test_discards_non_positive_and_non_finite_levels(self):
        result = self.read_reply(
            {"yellow_or_trigger": 2000, "stop_loss": 0, "take_profit": math.inf}, "buy"
        )
        self.assertEqual(result, {"yellow_or_trigger": 2000.0, "stop_loss": None, "take_profit": None})

    def test_stop_on_current_price_label_is_rejected(self):
        # POPIGOGO #7712 v1 misread: stop taken from the current-price label.
        d = self.read_reply({"entry": 77343.44, "stop_loss": 76051.1, "take_profit": 81820.87,
                             "current_price": 76051.1}, "buy", detail=True)
        self.assertIsNone(d["stop_loss"])
        self.assertIn("stop_loss_equals_current_price", d["rejected"])
        self.assertEqual(d["take_profit"], 81820.87)

    def test_not_a_chart_returns_no_levels(self):
        d = self.read_reply({"is_price_chart": False, "entry": 82824.0, "stop_loss": None,
                             "take_profit": None, "current_price": 83480.8}, "buy", detail=True)
        self.assertEqual((d["entry"], d["stop_loss"], d["take_profit"]), (None, None, None))

    def test_wrong_price_scale_is_rejected(self):
        # POPIGOGO #8015: caption says BTC, chart is ETH-scale.
        d = self.read_reply({"entry": 2520.54, "stop_loss": 2398.12, "take_profit": 3504.98,
                             "current_price": 2510.88}, "buy", detail=True, reference_price=80000.0)
        self.assertEqual((d["entry"], d["stop_loss"]), (None, None))
        self.assertTrue(d["rejected"][0].startswith("price_scale_mismatch"))

    def test_empty_content_is_an_error_not_reasoning(self):
        with self.assertRaises(RuntimeError):
            self.read_reply("", "buy")


if __name__ == "__main__":
    unittest.main()
