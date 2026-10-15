import unittest

from src.parser import parse_message
from src.models import Intent, Side, Venue


HL = {"BTC", "ETH", "SOL", "ENA", "HYPE"}


def p(text, channel="POPIGOGO", default="BTC", other_assets=None):
    return parse_message(
        channel=channel,
        message_id=1,
        date="",
        text=text,
        default_symbol=default,
        hl_universe=HL,
        other_assets=other_assets,
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

    def test_named_other_asset_setup_is_not_default_btc(self):
        # POPIGOGO #7683, 2026-09-14: chart prices 0.10-0.16, not BTC.
        s = p("牛來你想做合約的話 可以看看這個 條件1:漲過黃線 條件2:2H收線在上方 (4H是最好)",
              other_assets=["牛來", "PONS"])
        self.assertEqual(s.symbol, "牛來")
        self.assertNotEqual(s.venue, Venue.HYPERLIQUID)
        self.assertEqual(s.intent, Intent.IGNORE)
        self.assertEqual(s.skip_reason, "off_venue_asset")

    def test_dollar_non_ascii_asset_is_not_default_btc(self):
        # POPIGOGO #7788: "$牛來" with no configured list still must not become BTC.
        s = p("$牛來合約可能的機會 你們可以試試看 條件1:漲過黃線 條件2:4H收過黃線上方")
        self.assertIsNone(s.symbol)
        self.assertEqual(s.skip_reason, "unknown_asset")
        self.assertEqual(s.intent, Intent.IGNORE)

    def test_unnamed_conditional_setup_still_defaults_to_btc(self):
        # popi_2026_setups #7154: no symbol in text, it was a BTC setup.
        s = p("還是想等突破67250那個點在做 不過有機會的話 這單也可以觀察看看 記得要等12H收線才算成立喔 "
              "條件1:跌過這黃色線 條件2: 12H收在下方", other_assets=["牛來", "PONS"])
        self.assertEqual(s.symbol, "BTC")

    def test_dollar_price_is_not_an_asset(self):
        s = p("我的目標是$3 還有10倍", other_assets=["牛來"])
        self.assertNotEqual(s.skip_reason, "unknown_asset")

    def test_naked_hl_ticker_is_recognized(self):
        s = parse_message(channel="POPIGOGO", message_id=1, date="", default_symbol="BTC",
                          hl_universe=HL | {"ZEC"}, other_assets=["牛來"],
                          text="ZEC 可能的合約機會 條件1:漲過黃線 條件2: 8H收過黃線")
        self.assertEqual((s.symbol, s.venue), ("ZEC", Venue.HYPERLIQUID))

    def test_named_asset_on_hl_trades_on_hl(self):
        s = parse_message(channel="POPIGOGO", message_id=1, date="", default_symbol="BTC",
                          hl_universe=HL | {"PONS"}, other_assets=["牛來", "PONS"],
                          text="關於PONS 我覺得有一個合約的機會 條件1:漲過黃線 條件2:6H收線在上")
        self.assertEqual((s.symbol, s.venue), ("PONS", Venue.HYPERLIQUID))

    def test_named_asset_off_hl_is_skipped(self):
        s = p("關於PONS 我覺得有一個合約的機會 條件1:漲過黃線 條件2:6H收線在上", other_assets=["PONS"])
        self.assertEqual((s.symbol, s.skip_reason), ("PONS", "off_venue_asset"))

    def test_word_like_short_names_need_dollar(self):
        s = parse_message(channel="POPIGOGO", message_id=1, date="", default_symbol="BTC",
                          hl_universe=HL | {"NOT", "ME", "IO"}, text="NOT ME IO 不要做空 條件1:漲過黃線 條件2:8H收線")
        self.assertEqual(s.symbol, "BTC")

    def test_setup_with_referral_tail_is_not_an_ad(self):
        # POPIGOGO #8001
        s = parse_message(channel="POPIGOGO", message_id=1, date="", default_symbol="BTC",
                          hl_universe=HL | {"PONS"}, other_assets=["牛來", "PONS"],
                          text="關於PONS 我覺得有一個合約的機會 你們可以小倉試試看 PONS我還是建議盡量現貨為主的 別上頭哦 "
                               "條件1:漲過黃線 條件2:6H收線在上方 OURBIT也可以玩PONS合約哦: https://www.ourbit.com/zh-TW/register?inviteCode=POPI88")
        self.assertEqual((s.symbol, s.intent), ("PONS", Intent.CONFIRM_BREAKOUT))

    def test_spot_advice_is_not_a_perp_entry(self):
        # POPIGOGO #7950
        s = parse_message(channel="POPIGOGO", message_id=1, date="", default_symbol="BTC",
                          hl_universe=HL | {"PONS"}, other_assets=["PONS"],
                          text="PONS已經快接近 0.385~0.29的區域了 我建議著重等待這個區域 到的時候 買點現貨放著 "
                               "依我的經驗 通常都有不錯的收益 怕錯過就掛單掛好~ 合約我目前沒有想開就是了")
        self.assertEqual((s.skip_reason, s.intent), ("spot_only", Intent.IGNORE))

    def test_meme_word_is_not_a_ticker(self):
        s = parse_message(channel="POPIGOGO", message_id=1, date="", default_symbol="BTC",
                          hl_universe=HL | {"MEME"}, text="拿現貨MEME是比誰拿的久")
        self.assertNotEqual(s.symbol, "MEME")

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
