import json
import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.chart_reader import read_levels


class ChartReaderValidationTests(unittest.TestCase):
    def read_reply(self, reply, side):
        response = mock.Mock(status_code=200)
        response.json.return_value = {"choices": [{"message": {"content": json.dumps(reply)}}]}
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


if __name__ == "__main__":
    unittest.main()
