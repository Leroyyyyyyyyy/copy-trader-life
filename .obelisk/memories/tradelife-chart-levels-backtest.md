# Tradelife: chart-level backtest of POPIGOGO 2026 structure trades

## Conclusion

Final chart-based backtest of the 7 POPIGOGO 2026 structure trades is **-$9.29**
(6 filled / 2 wins), with #5718 skipped. Saved in
`data/popi_2026_chart_bt.json`; levels in `data/charts/levels.json`.

## How levels were extracted

1. Telethon login (session `tradelife.session`) downloads ORIGINAL chart images
   to `data/charts/orig_*.jpg`. The public t.me/s + embed pages only give
   ~800px thumbnails (and some are 160x160 avatars); the CDN URLs in
   `meta.json` expire (404). Telethon `download_media` is the only path.
2. `src/read_charts_vision.py` calls deepseek-v4-flash-vision-exp with the
   image and merges JSON into `levels.json`.
3. macOS Vision OCR (`/tmp/ocr.swift`, upscaled axis crops) cross-checks the
   numbers — the model's axis readings matched OCR, the earlier thumbnail OCR
   had read many numbers wrong.

## Key decisions

- **#5718 skipped**: its chart axis (83k-99k) is from when BTC was 88k-92k,
  but the post time (2026-01-31) BTC was already 81k — trigger/TP already
  blown through. Chart replay is invalid for it. (Post text has no numeric
  trigger; do NOT proxy with recent high/low.)
- **#7154 = only the yellow-line trade**: trigger 66056.28 (yellow line),
  SL 68037.25 (top of red zone, above), no TP. The text's "67250" is a
  SEPARATE breakout trade, deliberately not followed.
- **#7217 entry**: Ourbit card avg 1876.62 is used for the main result; the
  engine-faithful path (wait 8h close > 1912/1868 then first 1m) enters ~2086.
- Per handoff policy: no swing stop, no synthetic 20% TP; 6h -> 4h proxy.

## Final per-trade pnl

#6014 -8.61, #6059 -6.77, #6190 -4.04, #6361 +7.31, #7154 -2.69, #7217 +5.50.

## Tooling gotchas

- deepseek-v4-flash-vision-exp is a reasoning model: final JSON goes to
  `message.content`, but it is EMPTY unless `max_tokens` is large enough
  (>=8000, use 16000); otherwise the answer is truncated inside
  `reasoning_content`.
- `PROMPT_TMPL.format()` must escape JSON braces as `{{ }}` or it raises
  `KeyError: '"kind"'`.
- Hyperliquid candleSnapshot rate-limits (429); retry with backoff.

## Files

- `src/tg_login.py` two-step Telethon login (`--phone` / `--code`, saves
  `phone_code_hash` to `data/.tg_login_pending.json`).
- `src/fetch_orig_images.py` CDN-then-Telethon original download.
- `src/read_charts_vision.py` vision -> levels.json merge.
- `src/bt_chart_levels.py` chart-level replay runner.
- `.env` holds TELEGRAM_API_ID/HASH and DEEPSEEK_API_KEY (gitignored).
