import json
import os
import unittest
from unittest import mock

import httpx

from src.llm_gate import review

PARSED_SHORT = {"intent": "market", "side": "sell", "symbol": "BTC"}


def transport(reply=None, status=200):
    def handler(request):
        if status != 200:
            return httpx.Response(status, text="err")
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(reply)}}],
                                         "usage": {"prompt_tokens": 10}})
    return httpx.MockTransport(handler)


class LlmGateTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-test-000000000000000000"}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_gate(self, reply=None, parsed=PARSED_SHORT, **kw):
        return review(text="t", context=["a"], parsed=parsed, transport=transport(reply, **kw))

    def test_not_actionable_blocks(self):
        d = self.run_gate({"verdict": "not_actionable", "side": None, "symbol": None, "reason": "warns against shorting"})
        self.assertFalse(d.allow)
        self.assertIn("verdict:not_actionable", d.reason)

    def test_agreeing_entry_allows(self):
        d = self.run_gate({"verdict": "enter_now", "side": "sell", "symbol": "$btc", "reason": "x"})
        self.assertTrue(d.allow)
        self.assertEqual(d.symbol, "BTC")

    def test_opposite_side_blocks(self):
        # e.g. an exit ("I closed my ETH long") parsed as a buy
        d = self.run_gate({"verdict": "enter_now", "side": "buy", "symbol": "BTC", "reason": "x"})
        self.assertFalse(d.allow)
        self.assertIn("side:buy!=sell", d.reason)

    def test_other_symbol_blocks(self):
        d = self.run_gate({"verdict": "enter_now", "side": "sell", "symbol": "ETH", "reason": "x"})
        self.assertFalse(d.allow)

    def test_breakout_needs_setup_or_entry(self):
        parsed = {"intent": "confirm_breakout", "side": "buy", "symbol": "ETH"}
        self.assertTrue(self.run_gate({"verdict": "conditional_setup", "side": "buy", "symbol": "ETH"}, parsed).allow)
        self.assertFalse(self.run_gate({"verdict": "adjust_exit", "side": "buy", "symbol": "ETH"}, parsed).allow)

    def test_errors_fail_closed_by_default(self):
        d = self.run_gate(status=500)
        self.assertEqual((d.allow, d.verdict, d.error), (False, "error", "http_500"))
        d2 = review(text="t", context=[], parsed=PARSED_SHORT, on_error="allow", transport=transport(status=500))
        self.assertTrue(d2.allow)

    def test_bad_json_is_retried_once(self):
        calls = []

        def handler(request):
            calls.append(1)
            content = "{not json" if len(calls) == 1 else json.dumps({"verdict": "enter_now", "side": "sell", "symbol": "BTC"})
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
        d = review(text="t", context=[], parsed=PARSED_SHORT, transport=httpx.MockTransport(handler))
        self.assertEqual((d.allow, len(calls)), (True, 2))
        d2 = review(text="t", context=[], parsed=PARSED_SHORT, retries=0,
                    transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "{"}}]})))
        self.assertEqual((d2.allow, d2.error), (False, "JSONDecodeError"))

    def test_truncated_reply_is_an_error(self):
        t = httpx.MockTransport(lambda r: httpx.Response(200, json={"choices": [
            {"finish_reason": "length", "message": {"content": ""}}]}))
        d = review(text="t", context=[], parsed=PARSED_SHORT, transport=t)
        self.assertEqual((d.allow, d.error), (False, "truncated_output"))

    def test_market_needs_enter_now(self):
        # POPIGOGO #8060: "if zone 1 fails I'll look for a long at 2" parsed as a market buy
        parsed = {"intent": "market", "side": "buy", "symbol": "BTC"}
        d = self.run_gate({"verdict": "conditional_setup", "side": "buy", "symbol": "BTC"}, parsed)
        self.assertFalse(d.allow)

    def test_model_must_name_the_symbol(self):
        # #7408: target 3000 is ETH's; parser said BTC, model left the symbol open
        parsed = {"intent": "update", "side": "hold", "symbol": "BTC"}
        d = self.run_gate({"verdict": "adjust_exit", "side": "buy", "symbol": None}, parsed)
        self.assertFalse(d.allow)
        self.assertIn("symbol:None!=BTC", d.reason)

    def test_garbage_verdict_is_unclear_and_blocks(self):
        d = self.run_gate({"verdict": "maybe", "side": "sell", "symbol": "BTC"})
        self.assertEqual((d.allow, d.verdict), (False, "unclear"))


if __name__ == "__main__":
    unittest.main()
