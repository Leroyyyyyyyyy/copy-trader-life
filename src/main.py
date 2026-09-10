from __future__ import annotations

import argparse
import asyncio
import os

import yaml
from dotenv import load_dotenv

from .engine import Engine
from .util import ROOT, env_bool, setup_logging

log = setup_logging()


def load_config() -> dict:
    path = ROOT / "config.yaml"
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Telegram channel -> Hyperliquid / Solana executor")
    p.add_argument("--live", action="store_true", help="send real orders; also requires LIVE_TRADING=1")
    p.add_argument("--paper", action="store_true", help="force paper trading")
    p.add_argument("--replay", type=int, default=0, help="parse last N public posts then exit")
    p.add_argument("--poll", type=float, default=2.5, help="seconds between pending-plan checks")
    return p.parse_args()


def resolve_dry_run(cfg: dict, args: argparse.Namespace) -> bool:
    if args.paper:
        return True
    if args.live and env_bool("LIVE_TRADING"):
        return False
    if args.live and not env_bool("LIVE_TRADING"):
        log.warning("--live ignored because LIVE_TRADING is not 1")
    # A config value must never enable trading by itself. Sending an order
    # always requires both an explicit CLI flag and the environment interlock.
    return True


async def replay(engine: Engine, n: int, usernames: list[str]) -> None:
    import html
    import httpx
    import re

    async with httpx.AsyncClient(timeout=20.0, headers={"User-Agent": "tradelife/0.1"}) as http:
        for name in usernames:
            r = await http.get(f"https://t.me/s/{name}")
            r.raise_for_status()
            chunks = re.split(r'class="tgme_widget_message ', r.text)
            posts = []
            for chunk in chunks[1:]:
                text_m = re.search(
                    r'class="tgme_widget_message_text[^"]*" dir="auto">([\s\S]*?)</div>', chunk
                )
                id_m = re.search(r'href="https://t\.me/' + re.escape(name) + r'/(\d+)"', chunk)
                date_m = re.search(r'datetime="([^"]+)"', chunk)
                if not text_m:
                    continue
                text = re.sub(r"<br\s*/?>", "\n", text_m.group(1), flags=re.I)
                text = re.sub(r"<[^>]+>", " ", text)
                text = html.unescape(text)
                posts.append(
                    {
                        "id": int(id_m.group(1)) if id_m else len(posts) + 1,
                        "date": date_m.group(1) if date_m else "",
                        "text": re.sub(r"\s+", " ", text).strip(),
                    }
                )
            for post in posts[-n:]:
                engine.handle_text(name, post["id"], post["date"], post["text"])
    engine.poll_pending()


async def amain() -> None:
    load_dotenv(ROOT / ".env")
    args = parse_args()
    cfg = load_config()
    dry_run = resolve_dry_run(cfg, args)
    usernames = [c["username"] for c in cfg.get("channels") or [] if c.get("enabled", True)]
    log.info("dry_run=%s channels=%s", dry_run, usernames)

    engine = Engine(cfg, dry_run=dry_run)
    engine.start()

    if args.replay:
        await replay(engine, args.replay, usernames)
        return

    api_id = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")
    session = os.getenv("TELEGRAM_SESSION", "tradelife")
    if not api_id or not api_hash:
        raise SystemExit("TELEGRAM_API_ID / TELEGRAM_API_HASH missing. Copy .env.example to .env")

    from telethon import TelegramClient
    from .bot import ChannelBot

    client = TelegramClient(str(ROOT / session), int(api_id), api_hash)
    bot = ChannelBot(client, engine, usernames)
    await client.start()
    poll_task = asyncio.create_task(bot.poll_loop(args.poll))
    try:
        await bot.run()
    finally:
        poll_task.cancel()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
