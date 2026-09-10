from __future__ import annotations

import html
import re
from typing import Optional

from .models import Intent, Side, Signal, Venue

SOLANA_MINT_RE = re.compile(r"\b([1-9A-HJ-NP-Za-km-z]{32,44})\b")
TICKER_RE = re.compile(r"(?<![A-Za-z0-9])\$([A-Z][A-Z0-9]{1,14})\b")
MCAP_RE = re.compile(
    r"(?<![A-Za-z0-9.])(\d+(?:\.\d+)?)\s*([mM]{1,6}|[kK]|[bB]|百万|亿)(?![A-Za-z])"
)
PRICE_RANGE_RE = re.compile(
    r"(?<!\d)(\d{2,6}(?:\.\d+)?)\s*(?:~|-|—|到|至)\s*(\d{2,6}(?:\.\d+)?)"
)
PRICE_HINT_RE = re.compile(
    r"(?:买点|挂单|回踩|回调|跌到|到了|价格|目标|止盈|止损|tp|sl|entry)[^\d]{0,8}(\d{2,6}(?:\.\d+)?)",
    re.I,
)
PLAIN_PRICE_RE = re.compile(r"(?<![\d.])(\d{2,6}(?:\.\d+)?)(?!\d)")

AD_PATTERNS = [
    r"metawin",
    r"backpack\.exchange",
    r"fomo\.family",
    r"廣告",
    r"广告",
    r"赌场",
    r"贈送",
    r"赠送",
    r"註冊",
    r"注册",
    r"接广告",
    r"不會主動私訊",
    r"不会主动私讯",
    r"開戶",
    r"开户",
    r"mexc",
    r"ourbit",
    r"空投",
    r"專屬開戶",
    r"新用戶可領",
    r"新户最高領",
]
NAKED_TICKER_RE = re.compile(
    r"(?<![A-Za-z0-9])(BTC|ETH|SOL|HYPE|ENA)(?![A-Za-z0-9])", re.I
)
TF_RE = re.compile(r"(\d+)\s*([HhDdWw]|小时|小時|日|周)")
SMALL_SIZE_RE = re.compile(r"开小一些|開小一些|部位不会跟平常一样大|部位不會跟平常一樣大|小仓|小倉|仓位.*小|倉位.*小")
BREAKOUT_RE = re.compile(r"涨过|漲過|突破|破新高|前高|黄线|黃線|收线在上方|收線在上方")
BREAKDOWN_RE = re.compile(r"跌过|跌過|跌破|下破|收线在下方|收線在下方")
LEVEL_NUM_RE = re.compile(
    r"(?:低点|低點|区间|區間|黄线|黃線)\s*\*?\s*(\d{4,6}(?:\.\d+)?)|\*\s*(\d{4,6}(?:\.\d+)?)"
)
LONG_POS_RE = re.compile(r"多单|多單|这张多|這張多")
SHORT_POS_RE = re.compile(r"空单|空單|这张空|這張空")
STOP_PULL_RE = re.compile(r"(?:止损|止損).{0,12}?(\d{3,6}(?:\.\d+)?)")

BUY_PATTERNS = [
    r"买入",
    r"買入",
    r"买点",
    r"買點",
    r"做多",
    r"开多",
    r"開多",
    r"抄底",
    r"加仓",
    r"加倉",
    r"建仓",
    r"建倉",
    r"有角度",
    r"买现货",
    r"買現貨",
    r"再买",
    r"再買",
    r"去买",
    r"去買",
    r"可以买",
    r"可以買",
    r"\blong\b",
    r"\bbuy\b",
    r"一起建设",
    r"一起建設",
    r"多单",
    r"多單",
    r"做一个多",
    r"做一個多",
]
SELL_PATTERNS = [
    r"做空",
    r"开空",
    r"開空",
    r"卖出",
    r"賣出",
    r"减仓",
    r"減倉",
    r"\bshort\b",
    r"\bsell\b",
    r"空单",
    r"空單",
]
HOLD_PATTERNS = [
    r"\bhold\b",
    r"持有",
    r"拿着",
    r"拿著",
    r"不卖",
    r"不賣",
    r"继续拿",
    r"繼續拿",
]
CLOSE_PATTERNS = [
    r"平仓",
    r"平倉",
    r"清仓",
    r"清倉",
    r"止损离场",
    r"把.*平掉",
    r"空单平掉",
    r"空單平掉",
]
AVOID_LONG_PATTERNS = [
    r"不要做多",
    r"不要.*做多",
    r"别.*做多",
    r"別.*做多",
    r"绝对不要做多",
    r"絕對不要做多",
    r"不要輕易做多",
    r"不要轻易做多",
    r"喊你做多",
    r"那些喊你做多",
]
NSFW_SKIP = [r"色圖", r"色图", r"18\+"]
AVOID_SHORT_PATTERNS = [
    r"不要做空",
    r"不要.*做空",
    r"别.*做空",
    r"別.*做空",
    r"绝对不要去做空",
    r"絕對不要去做空",
    r"别轻易的去做空",
    r"別輕易的去做空",
    r"就想做空",
    r"想做空哦",
    r"不能做空",
    r"不会做空",
    r"不會做空",
    r"結構失效",
    r"结构失效",
]
WAIT_PATTERNS = [
    r"回踩",
    r"回调",
    r"回調",
    r"跌到",
    r"等到",
    r"挂单",
    r"掛單",
    r"触及",
    r"觸及",
    r"突破再",
    r"等.*再买",
    r"等.*再買",
    r"到了.*再",
]
TARGET_PATTERNS = [
    r"目标",
    r"目標",
    r"止盈",
    r"\btp\b",
    r"take profit",
]
STOP_PATTERNS = [r"止损", r"止損", r"\bsl\b", r"stop loss"]
REVIEW_PATTERNS = [
    r"回看",
    r"买太快",
    r"買太快",
    r"思路错了",
    r"思路錯了",
    r"之前逻辑",
    r"之前邏輯",
    r"看对做错",
    r"看對做錯",
]
MACRO_SKIP = [
    r"etf flows",
    r"jackson hole",
    r"fed says",
    r"美联储",
    r"美聯儲",
    r"https://x\.com/",
    r"https://twitter\.com/",
]

ALIAS = {
    "BTC": "BTC",
    "BITCOIN": "BTC",
    "ETH": "ETH",
    "ETHEREUM": "ETH",
    "SOL": "SOL",
    "SOLANA": "SOL",
    "HYPE": "HYPE",
    "ENA": "ENA",
    "COIN": "COIN",
    "比特币": "BTC",
    "比特": "BTC",
    "以太": "ETH",
    "以太坊": "ETH",
}


def _has(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, re.I) for p in patterns)


def _mcap_to_usd(num: str, unit: str) -> float:
    n = float(num)
    u = unit.lower()
    if u in {"k"}:
        return n * 1_000
    if u.startswith("m") or u == "百万":
        return n * 1_000_000
    if u in {"b", "亿"}:
        return n * 1_000_000_000
    return n


def extract_mcaps(text: str) -> list[float]:
    out = []
    for num, unit in MCAP_RE.findall(text):
        # ignore view counts like 1.25K that aren't m/b
        if unit.lower() == "k":
            continue
        out.append(_mcap_to_usd(num, unit))
    return out


def extract_prices(text: str) -> list[float]:
    prices: list[float] = []
    for a, b in PRICE_RANGE_RE.findall(text):
        prices.extend([float(a), float(b)])
    for m in PRICE_HINT_RE.findall(text):
        prices.append(float(m))
    # "已经160多了" style
    for m in re.findall(r"(\d{2,6}(?:\.\d+)?)多", text):
        prices.append(float(m))
    # de-dup while keeping order
    seen = set()
    uniq = []
    for p in prices:
        if p in seen:
            continue
        seen.add(p)
        uniq.append(p)
    return uniq


def extract_range(text: str) -> Optional[tuple[float, float]]:
    m = PRICE_RANGE_RE.search(text)
    if not m:
        return None
    lo, hi = float(m.group(1)), float(m.group(2))
    return (min(lo, hi), max(lo, hi))


def looks_like_solana_mint(token: str) -> bool:
    if not SOLANA_MINT_RE.fullmatch(token):
        return False
    if token.lower() in {"http", "https", "t.me"}:
        return False
    # ticker-like all letters short
    if token.isalpha() and len(token) < 12:
        return False
    return True


def extract_mints(text: str) -> list[str]:
    found = []
    for tok in SOLANA_MINT_RE.findall(text):
        if looks_like_solana_mint(tok):
            found.append(tok)
    return found


def normalize_symbol(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    key = raw.strip().upper().lstrip("$")
    return ALIAS.get(key, key)


def parse_message(
    *,
    channel: str,
    message_id: int,
    date: str,
    text: str,
    default_symbol: Optional[str] = None,
    default_mint: Optional[str] = None,
    hl_universe: Optional[set[str]] = None,
) -> Signal:
    raw = html.unescape(text or "")
    compact = raw.strip()
    tickers = [normalize_symbol(t) for t in TICKER_RE.findall(raw)]
    tickers += [normalize_symbol(t) for t in NAKED_TICKER_RE.findall(raw)]
    tickers = [t for t in tickers if t]
    # preserve order, drop dupes
    seen_t = set()
    uniq_t = []
    for t in tickers:
        if t in seen_t:
            continue
        seen_t.add(t)
        uniq_t.append(t)
    tickers = uniq_t
    mints = extract_mints(raw)
    mcaps = extract_mcaps(raw)
    prices = extract_prices(raw)
    rng = extract_range(raw)

    skip = None
    if not compact:
        skip = "empty"
    elif _has(AD_PATTERNS, compact):
        skip = "ad_or_referral"
    elif _has(NSFW_SKIP, compact):
        skip = "nsfw"
    elif _has(MACRO_SKIP, compact) and not tickers and not mints and not _has(BUY_PATTERNS + SELL_PATTERNS, compact):
        skip = "macro_or_link_only"
    elif re.fullmatch(r"https?://\S+", compact):
        skip = "link_only"

    venue = Venue.NONE
    symbol = None
    mint = mints[0] if mints else None
    if not mint and default_mint and re.search(
        r"有角度|一起建设|一起建設|买入并持有|買入並持有|#TOAD|\\btoad\\b|这个盘子|這個盤子",
        compact,
        re.I,
    ):
        mint = default_mint
        mints = [mint]
    if mint:
        venue = Venue.SOLANA
        symbol = tickers[0] if tickers else None
    elif tickers:
        symbol = tickers[0]
        if hl_universe is None or symbol in hl_universe:
            venue = Venue.HYPERLIQUID
        else:
            venue = Venue.SOLANA
    elif default_symbol and (
        re.search(r"\bBTC\b|比特币|大饼", compact, re.I)
        or (_has(TARGET_PATTERNS, compact) and prices and not tickers)
        or (_has([r"做多", r"做空", r"开多", r"开空", r"開多", r"開空"], compact) and not tickers)
        or (
            re.search(r"条件\s*1|條件\s*1", compact)
            and re.search(r"条件\s*2|條件\s*2|收线|收線", compact)
            and not mint
        )
    ):
        symbol = normalize_symbol(default_symbol)
        venue = Venue.HYPERLIQUID

    if re.search(r"股票|现货\s*\$?COIN|現貨\s*\$?COIN", compact) and not mint:
        skip = skip or "off_venue_stock"
        venue = Venue.NONE
        symbol = None
    if re.search(r"海力士", compact) and not mint and not tickers:
        skip = skip or "off_venue_stock"
        venue = Venue.NONE
        symbol = None

    side = Side.IGNORE
    intent = Intent.IGNORE
    reason_bits = []
    confidence = 0.35

    avoid_short = _has(AVOID_SHORT_PATTERNS, compact)
    avoid_long = _has(AVOID_LONG_PATTERNS, compact)
    is_buy = _has(BUY_PATTERNS, compact) and not avoid_long
    if re.search(r"不要.*抄底|别去抄底|別去抄底", compact):
        is_buy = False
    is_sell = _has(SELL_PATTERNS, compact) and not avoid_short
    is_hold = _has(HOLD_PATTERNS, compact)
    is_close = _has(CLOSE_PATTERNS, compact)
    is_wait = _has(WAIT_PATTERNS, compact)
    is_target = _has(TARGET_PATTERNS, compact)
    is_stop = _has(STOP_PATTERNS, compact)

    if _has(REVIEW_PATTERNS, compact) and not re.search(r"现在买|現在買|立刻买|马上买|馬上買", compact):
        is_buy = False
        skip = skip or "review_not_entry"

    # 「ETH 这张多单 … 再关注 BTC 空单机会」主单仍是 ETH 多，后面的空单只是备注。
    if is_buy and is_sell:
        if LONG_POS_RE.search(compact) and re.search(r"关注.*空单|關注.*空單|空单机会|空單機會", compact):
            is_sell = False
        elif SHORT_POS_RE.search(compact) and re.search(r"关注.*多单|關注.*多單|多单机会|多單機會", compact):
            is_buy = False

    if skip:
        pass
    elif is_close and not re.search(r"不应该把|不應該把|有罪", compact):
        side = Side.CLOSE
        intent = Intent.CLOSE
        confidence = 0.8
        reason_bits.append("close_keyword")
    elif is_buy and is_sell:
        skip = "mixed_side"
    elif is_sell:
        side = Side.SELL
        intent = Intent.MARKET
        confidence = 0.78
        reason_bits.append("sell_keyword")
    elif is_buy or LONG_POS_RE.search(compact) or (BREAKOUT_RE.search(compact) and re.search(r"入場|入场|做多|多单|多單", compact)):
        side = Side.BUY
        intent = Intent.MARKET
        confidence = 0.82 if mint or tickers else 0.7
        reason_bits.append("buy_keyword")
    elif BREAKDOWN_RE.search(compact) and re.search(r"入場|入场|开空|開空|空单|空單|做空|想开空|想開空", compact):
        side = Side.SELL
        intent = Intent.MARKET
        confidence = 0.82 if mint or tickers else 0.7
        reason_bits.append("breakdown_entry")
    elif mint and re.search(r"有角度|建设|建設|冲|衝", compact):
        side = Side.BUY
        intent = Intent.MARKET
        venue = Venue.SOLANA
        confidence = 0.76
        reason_bits.append("memecoin_call")
    elif is_wait and (prices or mcaps) and (symbol or mint):
        side = Side.BUY if not is_sell else Side.SELL
        intent = Intent.WAIT_TARGET
        confidence = 0.82
        reason_bits.append("wait_without_buy_word")
    elif is_hold:
        side = Side.HOLD
        intent = Intent.IGNORE
        confidence = 0.6
        reason_bits.append("hold_only")
        skip = skip or "hold_no_order"
    elif is_target and (symbol or mint):
        side = Side.HOLD
        intent = Intent.UPDATE
        confidence = 0.78
        reason_bits.append("target_update")
    elif avoid_short:
        skip = "avoid_short_not_a_buy"
        reason_bits.append("avoid_short")
    elif avoid_long:
        skip = "avoid_long_not_a_buy"
        reason_bits.append("avoid_long")
    elif re.search(r"条件\s*[12]|條件\s*[12]", compact) and (BREAKOUT_RE.search(compact) or BREAKDOWN_RE.search(compact)):
        if re.search(r"收线在下方|收線在下方|收在下方|跌过|跌過|跌破|下破", compact):
            side = Side.SELL
            intent = Intent.MARKET
            reason_bits.append("structure_short")
        elif re.search(r"收线在上方|收線在上方|收在上方|涨过|漲過|突破", compact):
            side = Side.BUY
            intent = Intent.MARKET
            reason_bits.append("structure_long")
        else:
            skip = skip or "no_action"
    else:
        skip = skip or "no_action"

    take_profit = None
    stop_loss = None
    limit_price = None
    trigger_price = None
    trigger_mcap = None

    if rng and (is_target or intent == Intent.UPDATE):
        take_profit = rng[1]
        reason_bits.append(f"tp_range:{rng[0]}-{rng[1]}")
    elif rng and side == Side.BUY:
        # 买点区间：挂下限
        limit_price = rng[0]
        take_profit = rng[1]
        reason_bits.append("entry_range")

    pulled = STOP_PULL_RE.search(compact)
    if pulled:
        stop_loss = float(pulled.group(1))
        prices.append(stop_loss)
        reason_bits.append("sl_pull")
    elif is_stop and prices:
        stop_loss = prices[-1]
    if is_target and prices and take_profit is None:
        take_profit = max(prices)

    confirm_tf = None
    tf_m = re.search(r"(?:条件|條件)\s*2[:：]?[\s\S]{0,24}?(\d+)\s*([HhDd]|小时|小時|日)", compact, re.I)
    if not tf_m:
        tf_m = TF_RE.search(compact) if re.search(r"收线|收線|收盘|收盤", compact) else None
    if tf_m:
        n, unit = tf_m.group(1), tf_m.group(2).lower()
        confirm_tf = {"h": f"{n}h", "小时": f"{n}h", "小時": f"{n}h", "d": f"{n}d", "日": f"{n}d"}.get(unit, f"{n}h")
    breakout = bool(BREAKOUT_RE.search(compact) or BREAKDOWN_RE.search(compact))
    size_mult = 0.4 if SMALL_SIZE_RE.search(compact) else 1.0
    if size_mult < 1:
        reason_bits.append(f"small_size:{size_mult}")

    if side in {Side.BUY, Side.SELL} and is_wait:
        intent = Intent.WAIT_TARGET if (prices or mcaps) else Intent.LIMIT
        if prices:
            trigger_price = prices[0]
            limit_price = prices[0]
        if mcaps:
            trigger_mcap = mcaps[0]
        reason_bits.append("conditional_entry")
        confidence = min(0.92, confidence + 0.05)
    elif side in {Side.BUY, Side.SELL} and prices and re.search(r"挂单|掛單|买点|買點", compact):
        intent = Intent.LIMIT
        limit_price = prices[0]
        reason_bits.append("limit_hint")
    elif side in {Side.BUY, Side.SELL} and mcaps and venue == Venue.SOLANA:
        # 例如「过两天 28M」当作市值目标，到了再动手，而不是立刻市价。
        if re.search(r"过两天|等到|到.?[0-9.]+m|目标|目標", compact, re.I):
            intent = Intent.WAIT_TARGET
            trigger_mcap = max(mcaps)
            reason_bits.append("mcap_target")

    lvl = LEVEL_NUM_RE.search(compact)
    if lvl and not trigger_price:
        trigger_price = float(next(g for g in lvl.groups() if g))
        reason_bits.append(f"level:{trigger_price}")
    if not trigger_price:
        m = re.search(r"(?:突破|跌过|跌過|涨过|漲過)\s*(\d{4,6}(?:\.\d+)?)", compact)
        if m:
            trigger_price = float(m.group(1))
            reason_bits.append(f"level:{trigger_price}")

    if re.search(r"等看看.*更好|更好的位置|耐心点等看看|耐心點等看看", compact) and not confirm_tf:
        skip = skip or "wait_better"
    if re.search(r"专注等|專注等|空单之类的机会|空單之類的機會", compact) and not confirm_tf and not trigger_price:
        skip = skip or "wait_no_setup"

    waiting_setup = bool(re.search(r"想开空单.*等|想開空單.*等|我会等这里|我會等這裡|等这里|等這裡|等做空|等.*机会|等.*機會", compact))
    hold_existing = bool(re.search(r"请耐心持有|請耐心持有|拿好|放成本损|放成本損|碰.*闪人|碰.*閃人|空单走的原因|空單走的原因", compact))
    if hold_existing and side in {Side.BUY, Side.SELL} and not breakout:
        if re.search(r"碰\s*(\d{4,6})", compact):
            m = re.search(r"碰\s*(\d{4,6})", compact)
            stop_loss = float(m.group(1))
            intent = Intent.UPDATE
            side = Side.HOLD
            reason_bits.append("trail_or_be")
            skip = None
        else:
            intent = Intent.IGNORE
            skip = skip or "hold_existing"
            reason_bits.append("hold_existing")

    if side in {Side.BUY, Side.SELL} and (breakout or (waiting_setup and confirm_tf)) and venue == Venue.HYPERLIQUID:
        intent = Intent.CONFIRM_BREAKOUT
        reason_bits.append("confirm_breakout")
        if confirm_tf:
            reason_bits.append(f"confirm:{confirm_tf}")
        confidence = min(0.93, max(confidence, 0.86))

    if skip in {"wait_better", "wait_no_setup"}:
        intent = Intent.IGNORE
        side = Side.IGNORE

    if venue == Venue.NONE and not skip:
        skip = "no_venue"
    if side in {Side.BUY, Side.SELL} and not symbol and not mint:
        skip = skip or "no_symbol"

    if skip:
        intent = Intent.IGNORE
        if side not in {Side.HOLD, Side.CLOSE}:
            side = Side.IGNORE

    return Signal(
        channel=channel,
        message_id=message_id,
        date=date,
        text=compact,
        side=side,
        intent=intent,
        venue=venue,
        symbol=symbol,
        mint=mint,
        trigger_price=trigger_price,
        trigger_mcap=trigger_mcap,
        limit_price=limit_price,
        take_profit=take_profit,
        stop_loss=stop_loss,
        size_mult=size_mult,
        breakout=breakout,
        confirm_tf=confirm_tf,
        invalidation=stop_loss,
        confidence=0.0 if skip else confidence,
        reason=";".join(reason_bits) or (skip or "parsed"),
        skip_reason=skip,
        raw={
            "tickers": tickers,
            "mints": mints,
            "prices": prices,
            "mcaps": mcaps,
            "range": rng,
            "confirm_tf": confirm_tf,
            "breakout": breakout,
            "size_mult": size_mult,
        },
    )
