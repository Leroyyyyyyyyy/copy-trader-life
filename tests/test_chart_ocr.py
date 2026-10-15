"""OCR cross-check for chart levels (src/chart_ocr.py, chart_reader.ocr_gate)."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw, ImageFont

from src.chart_ocr import ocr_levels, read_axis_labels, tesseract_available
from src.chart_reader import ocr_gate, read_levels_detail

OCR = {"stop_loss": 2598.88, "entry_candidates": [2704.08], "target_candidates": [3504.16, 2648.76]}


def synthetic_chart(path: Path) -> None:
    im = Image.new("RGB", (400, 300), (19, 23, 34))
    d = ImageDraw.Draw(im)
    font = ImageFont.load_default(size=13)
    for y, colour, text in ((40, (8, 153, 129), "3,504.16"), (120, (120, 123, 134), "2,704.08"),
                            (160, (242, 54, 69), "2,676.67"), (250, (242, 54, 69), "2,598.88")):
        d.rectangle((318, y, 399, y + 16), fill=colour)
        d.text((322, y + 1), text, fill=(255, 255, 255), font=font)
    im.save(path)


class GateTests(unittest.TestCase):
    def test_agreeing_levels_are_kept(self):
        out, why = ocr_gate({"entry": 2704.08, "stop_loss": 2598.88, "take_profit": 3504.16}, OCR)
        self.assertEqual(out, {"entry": 2704.08, "stop_loss": 2598.88, "take_profit": 3504.16})
        self.assertEqual(why, [])

    def test_one_digit_stop_misread_drops_everything(self):
        # POPIGOGO #7841 v2: vision read 2679.32 for a 2629.32 stop.
        out, why = ocr_gate({"entry": 2704.08, "stop_loss": 2679.32, "take_profit": None},
                            {**OCR, "stop_loss": 2629.32})
        self.assertEqual(out, {"entry": None, "stop_loss": None, "take_profit": None})
        self.assertTrue(why[0].startswith("stop_unconfirmed"))

    def test_one_cent_off_is_not_agreement(self):
        # POPIGOGO #7742: vision 2346.73 vs printed 2345.73.
        out, _ = ocr_gate({"entry": 2411.53, "stop_loss": 2346.73, "take_profit": None},
                          {**OCR, "stop_loss": 2345.73, "entry_candidates": [2411.53]})
        self.assertIsNone(out["stop_loss"])

    def test_unconfirmed_entry_drops_everything(self):
        out, why = ocr_gate({"entry": 3500.0, "stop_loss": 2598.88, "take_profit": None}, OCR)
        self.assertIsNone(out["stop_loss"])
        self.assertTrue(why[0].startswith("entry_unconfirmed"))

    def test_unconfirmed_target_is_dropped_alone(self):
        out, why = ocr_gate({"entry": 2704.08, "stop_loss": 2598.88, "take_profit": 2658.76}, OCR)
        self.assertEqual((out["stop_loss"], out["take_profit"]), (2598.88, None))
        self.assertTrue(why[0].startswith("target_unconfirmed"))

    def test_missing_tesseract_fails_closed(self):
        reply = {"entry": 2704.08, "stop_loss": 2598.88, "take_profit": None, "current_price": 2676.67}
        response = mock.Mock(status_code=200)
        response.json.return_value = {"choices": [{"message": {"content": json.dumps(reply)}}]}
        client = mock.MagicMock()
        client.post.return_value = response
        ctx = mock.MagicMock()
        ctx.__enter__.return_value = client
        with tempfile.NamedTemporaryFile(suffix=".jpg") as f, \
                mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=False), \
                mock.patch("src.chart_reader.httpx.Client", return_value=ctx), \
                mock.patch("src.chart_ocr.tesseract_available", return_value=False):
            d = read_levels_detail(f.name, "ETH", "buy", "8h", require_ocr=True)
        self.assertEqual((d["entry"], d["stop_loss"]), (None, None))
        self.assertIn("ocr_unavailable", d["rejected"])


@unittest.skipUnless(tesseract_available(), "tesseract not installed")
class OcrTests(unittest.TestCase):
    def test_reads_coloured_axis_labels(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "chart.png"
            synthetic_chart(path)
            levels = ocr_levels(read_axis_labels(path), "buy")
        self.assertEqual(levels["stop_loss"], 2598.88)  # lowest red, not the red current price
        self.assertEqual(levels["entry_candidates"], [2704.08])
        self.assertEqual(levels["target_candidates"], [3504.16])


if __name__ == "__main__":
    unittest.main()
