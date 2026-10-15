import json
import os
import unittest
from unittest import mock

import httpx

import threading

from src.llm_gate import review, review_many

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



def seq_transport(replies):
    """Serve replies in order (None = HTTP 500), safely across the parallel samples."""
    lock, it = threading.Lock(), iter(replies)

    def handler(request):
        with lock:
            reply = next(it)
        if reply is None:
            return httpx.Response(500, text="err")
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(reply)}}]})
    return httpx.MockTransport(handler)


class ReviewManyTests(unittest.TestCase):
    PARSED = {"intent": "confirm_breakout", "side": "buy", "symbol": "ETH"}
    SETUP = {"verdict": "conditional_setup", "side": "buy", "symbol": "ETH", "one_of": []}
    BRAG = {"verdict": "not_actionable", "side": None, "symbol": None, "one_of": []}

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-test-000000000000000000"}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def many(self, replies, parsed=None, **kw):
        return review_many(text="t", context=[], parsed=parsed or self.PARSED, retries=0,
                           transport=seq_transport(replies), **kw)

    def test_unanimous_allow(self):
        d = self.many([self.SETUP] * 3)
        self.assertEqual((d.action, d.allow, len(d.samples)), ("allow", True, 3))

    def test_split_goes_to_review(self):
        # POPIGOGO #7048 style: 2 of 3 say adjust, 1 says not actionable
        d = self.many([self.SETUP, self.BRAG, self.SETUP])
        self.assertEqual((d.action, d.allow), ("review", False))
        self.assertTrue(d.reason.startswith("split:"))

    def test_unanimous_close_names_the_symbol(self):
        close = {"verdict": "close", "side": "sell", "symbol": None, "one_of": []}
        d = self.many([close, dict(close, side="buy"), close], parsed={"intent": "market", "side": "buy", "symbol": "ETH"})
        self.assertEqual((d.action, d.symbol), ("close", "ETH"))

    def test_alternatives_go_to_review(self):
        # POPIGOGO #8027: "BTC ETH 選一個開就好"
        d = self.many([dict(self.SETUP, one_of=["BTC", "ETH"])] + [self.SETUP] * 2)
        self.assertEqual(d.action, "review")
        self.assertEqual(d.one_of, ["BTC", "ETH"])

    def test_alternatives_without_a_symbol_still_go_to_review(self):
        # #8027 as answered: symbol null, one_of [BTC, ETH]
        d = self.many([dict(self.SETUP, symbol=None, one_of=["BTC", "ETH"])] * 3)
        self.assertEqual(d.action, "review")

    def test_all_errors_follow_on_error(self):
        d = self.many([None] * 3)
        self.assertEqual((d.action, d.allow, d.verdict), ("block", False, "error"))

    def test_one_error_and_two_agreeing_answers(self):
        d = self.many([None, self.SETUP, self.SETUP])
        self.assertEqual(d.action, "allow")


if __name__ == "__main__":
    unittest.main()
