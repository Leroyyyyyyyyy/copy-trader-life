import unittest

from src.parser import parse_message
from src.models import Intent, Side, Venue


HL = {"BTC", "ETH", "SOL", "ENA", "HYPE"}


def p(text, channel="POPIGOGO", default="BTC"):
    return parse_message(
        channel=channel,
        message_id=1,
        date="",
        text=text,
        default_symbol=default,
        hl_universe=HL,
    )


class ParserTests(unittest.TestCase):
    def test_skip_ads(self):
        s = p("前往 metawin.com 存款，即可分得一份")
        self.assertEqual(s.skip_reason, "ad_or_referral")
        self.assertEqual(s.intent, Intent.IGNORE)

    def test_spot_buy_coin_is_stock_not_hl(self):
        s = p("给踏空的人 买点现货 $COIN 这里是周线级别的买点 可以买点放着")
        self.assertEqual(s.skip_reason, "off_venue_stock")
        self.assertEqual(s.intent, Intent.IGNORE)

    def test_avoid_short_is_not_a_buy(self):
        s = p("记得记得 别轻易的去做空喔 钱不要可以给我")
        self.assertEqual(s.skip_reason, "avoid_short_not_a_buy")

    def test_dont_long_is_not_a_buy(self):
        s = p("亲爱的牛宝们 绝对不要做多喔")
        self.assertEqual(s.intent, Intent.IGNORE)
        self.assertEqual(s.skip_reason, "avoid_long_not_a_buy")

    def test_dont_short_red_candle_is_not_a_short(self):
        s = p("不要看到一點紅K就想做空哦 這是你們的壞習慣")
        self.assertEqual(s.intent, Intent.IGNORE)
        self.assertEqual(s.skip_reason, "avoid_short_not_a_buy")

    def test_btc_target_is_update_not_market(self):
        s = p("别忘囉 2400已经是过去式了\n我的目标现在是3000~3250")
        self.assertEqual(s.symbol, "BTC")
        self.assertEqual(s.intent, Intent.UPDATE)
        self.assertEqual(s.take_profit, 3250)

    def test_wait_callback_limit(self):
        s = p("BTC 回踩 108000 再买")
        self.assertEqual(s.side, Side.BUY)
        self.assertIn(s.intent, {Intent.WAIT_TARGET, Intent.LIMIT})
        self.assertTrue(s.trigger_price == 108000 or s.limit_price == 108000)
        self.assertEqual(s.venue, Venue.HYPERLIQUID)

    def test_memecoin_mint_buy(self):
        mint = "A13oRB9FFaiUjfi6LdCg6p9ka1u8SfGkUFs4SKvPpump"
        s = p(f"兄弟，这次真的有角度 {mint} TOAD TOAD", channel="Theabyssofgambling", default=None)
        self.assertEqual(s.venue, Venue.SOLANA)
        self.assertEqual(s.mint, mint)
        self.assertEqual(s.side, Side.BUY)
        self.assertEqual(s.intent, Intent.MARKET)

    def test_mcap_target_waits(self):
        mint = "A13oRB9FFaiUjfi6LdCg6p9ka1u8SfGkUFs4SKvPpump"
        s = p(f"买入 {mint} 等到 28M 再买", channel="Theabyssofgambling", default=None)
        self.assertEqual(s.venue, Venue.SOLANA)
        self.assertEqual(s.intent, Intent.WAIT_TARGET)
        self.assertEqual(s.trigger_mcap, 28_000_000)

    def test_ena_review_is_not_auto_buy(self):
        s = p(
            "回看了一下 $ENA ，之前逻辑是对的，而且机构也一直在加仓，但是思路错了，买太快了。",
            channel="sleepingclub0",
            default=None,
        )
        self.assertEqual(s.intent, Intent.IGNORE)
        self.assertEqual(s.skip_reason, "review_not_entry")

    def test_spot_coin_without_dollar_is_not_btc(self):
        s = p("你怕踏空就去买现货COIN 绝对不要去做空任何东西")
        self.assertIn(s.skip_reason, {"off_venue_stock", "avoid_short_not_a_buy", "ad_or_referral"})
        self.assertEqual(s.intent, Intent.IGNORE)

    def test_hold_skips_order(self):
        s = p("p是不会p的，我只会买入并持有。", channel="Theabyssofgambling", default=None)
        self.assertEqual(s.intent, Intent.IGNORE)

    def test_eth_confirm_breakout_from_popi_7217(self):
        s = p(
            "ETH這張多單 雖然走了個頂部結構 但還在黏的話可能就是還沒結束 "
            "正巧這裡附近是上一個前高的地方 可能可以做一個多單 "
            "由於我整體看空的 所以多單的部位不會跟平常一樣大 會開小一些 "
            "理想狀態是ETH往上走 然後BTC剛好走個破新高 那就可以開始關注BTC的空單機會了 "
            "條件1:漲過黃線 條件2:8H收線在上方 更: 止損我還是拉到1800好了"
        )
        self.assertEqual(s.symbol, "ETH")
        self.assertEqual(s.side, Side.BUY)
        self.assertEqual(s.intent, Intent.CONFIRM_BREAKOUT)
        self.assertEqual(s.venue, Venue.HYPERLIQUID)
        self.assertEqual(s.confirm_tf, "8h")
        self.assertTrue(s.breakout)
        self.assertEqual(s.size_mult, 0.4)
        self.assertEqual(s.stop_loss, 1800)
        self.assertIsNone(s.skip_reason)

    def test_btc_short_setup_is_confirm_not_market(self):
        s = p("$BTC 想開空單的話我會等這裡 條件1:跌過這區間 條件2:4H收線在下方 (1D有更好)")
        self.assertEqual(s.symbol, "BTC")
        self.assertEqual(s.side, Side.SELL)
        self.assertEqual(s.intent, Intent.CONFIRM_BREAKOUT)
        self.assertEqual(s.confirm_tf, "4h")

    def test_wait_breakdown_without_open_word(self):
        s = p("$BTC 這裡我會等待他有一個下破的動作 條件1:跌過這區間 條件2:8H收線在下方 (1D有更好)")
        self.assertEqual(s.symbol, "BTC")
        self.assertEqual(s.side, Side.SELL)
        self.assertEqual(s.intent, Intent.CONFIRM_BREAKOUT)
        self.assertEqual(s.confirm_tf, "8h")

    def test_yellow_line_short_with_level(self):
        s = p("還是想等突破67250那個點在做 記得要等12H收線才算成立喔 條件1:跌過這黃色線 條件2: 12H收在下方")
        self.assertEqual(s.symbol, "BTC")
        self.assertEqual(s.side, Side.SELL)
        self.assertEqual(s.intent, Intent.CONFIRM_BREAKOUT)
        self.assertEqual(s.trigger_price, 67250)
        self.assertEqual(s.confirm_tf, "12h")

    def test_level_star_number(self):
        s = p("BTC 下破後的楔形走弱 你可以先在4H成立的時候入場 條件1:跌過這低點 *68300 條件2:1日收線在下方")
        self.assertEqual(s.intent, Intent.CONFIRM_BREAKOUT)
        self.assertEqual(s.side, Side.SELL)
        self.assertEqual(s.trigger_price, 68300)
        self.assertEqual(s.confirm_tf, "1d")

    def test_eth_cheer_is_not_a_market_buy(self):
        s = p("ETH 衝")
        self.assertTrue(s.intent in {Intent.IGNORE, Intent.CONFIRM_BREAKOUT} or s.skip_reason)
        self.assertNotEqual(s.intent, Intent.MARKET)


class ScalePriceTests(unittest.TestCase):
    def test_eth_1800_stays_if_mark_near_3k(self):
        from src.engine import Engine
        self.assertEqual(Engine._scale_price(1800, 3200), 1800)

    def test_btc_shorthand_scales_into_mark_band(self):
        from src.engine import Engine
        self.assertEqual(Engine._scale_price(6200, 70000), 62000)


if __name__ == "__main__":
    unittest.main()
