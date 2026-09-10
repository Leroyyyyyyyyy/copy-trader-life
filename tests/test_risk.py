import unittest

from src.models import Intent, Side, Signal, Venue
from src.risk import RiskGuard


def sig(**kwargs):
    base = dict(
        channel="POPIGOGO",
        message_id=1,
        date="",
        text="buy",
        side=Side.BUY,
        intent=Intent.MARKET,
        venue=Venue.HYPERLIQUID,
        symbol="BTC",
        confidence=0.85,
        raw={"tickers": ["BTC"], "mints": []},
    )
    base.update(kwargs)
    return Signal(**base)


CHANNELS = {
    "POPIGOGO": {
        "min_confidence": 0.68,
        "allow_perp": True,
        "allow_meme": True,
        "require_explicit": False,
        "perp": {"usd": 80, "leverage": 3},
        "meme": {"usd": 60, "sol": 0.08},
    },
    "Theabyssofgambling": {
        "min_confidence": 0.74,
        "allow_perp": False,
        "allow_meme": True,
        "require_explicit": False,
        "perp": {"usd": 0, "leverage": 1},
        "meme": {"usd": 12, "sol": 0.012},
    },
    "sleepingclub0": {
        "min_confidence": 0.84,
        "allow_perp": True,
        "allow_meme": False,
        "require_explicit": True,
        "perp": {"usd": 40, "leverage": 2},
        "meme": {"usd": 0, "sol": 0.0},
    },
}


class ChannelPolicyTests(unittest.TestCase):
    def setUp(self):
        self.risk = RiskGuard({"min_confidence": 0.7, "max_leverage": 2}, CHANNELS)

    def test_popi_perp_is_full_size(self):
        s = sig()
        self.assertIsNone(self.risk.allow(s))
        self.assertEqual(self.risk.size_usd(s), 80)
        self.assertEqual(self.risk.leverage(s), 3)

    def test_popi_meme_is_full_size(self):
        s = sig(venue=Venue.SOLANA, mint="A" * 32, symbol="TOAD", raw={"tickers": [], "mints": ["A" * 32]})
        self.assertIsNone(self.risk.allow(s))
        self.assertEqual(self.risk.size_sol(s), 0.08)

    def test_abyss_meme_is_probe(self):
        s = sig(
            channel="Theabyssofgambling",
            venue=Venue.SOLANA,
            mint="A" * 32,
            symbol="TOAD",
            raw={"tickers": [], "mints": ["A" * 32]},
        )
        self.assertIsNone(self.risk.allow(s))
        self.assertEqual(self.risk.size_usd(s), 12)
        self.assertEqual(self.risk.size_sol(s), 0.012)

    def test_abyss_rejects_perp(self):
        s = sig(channel="Theabyssofgambling")
        self.assertEqual(self.risk.allow(s), "channel_no_perp")

    def test_sleeping_requires_ticker(self):
        s = sig(channel="sleepingclub0", symbol="BTC", raw={"tickers": [], "mints": []})
        self.assertEqual(self.risk.allow(s), "need_explicit_ticker")

    def test_sleeping_explicit_perp_ok(self):
        s = sig(channel="sleepingclub0", confidence=0.86)
        self.assertIsNone(self.risk.allow(s))
        self.assertEqual(self.risk.size_usd(s), 40)

    def test_small_size_mult_applies(self):
        s = sig(size_mult=0.4)
        self.assertEqual(self.risk.size_usd(s), 32.0)

    def test_sleeping_rejects_meme(self):
        s = sig(
            channel="sleepingclub0",
            venue=Venue.SOLANA,
            mint="A" * 32,
            confidence=0.9,
            raw={"tickers": [], "mints": ["A" * 32]},
        )
        self.assertEqual(self.risk.allow(s), "channel_no_meme")


if __name__ == "__main__":
    unittest.main()
