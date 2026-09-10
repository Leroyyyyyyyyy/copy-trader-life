"""Two-step Telethon login: send code, then sign in and save tradelife.session.

Usage:
  python -m src.tg_login --phone +8613800138000   # step 1: send code to Telegram app
  python -m src.tg_login --code 12345             # step 2: complete sign-in

After step 2 a session file (tradelife.session) is written to the repo root,
then `python -m src.fetch_orig_images` can download originals without prompts.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PENDING = ROOT / "data" / ".tg_login_pending.json"


def load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phone", help="phone with country code, e.g. +8613800138000")
    ap.add_argument("--code", help="login code from the Telegram app")
    args = ap.parse_args()

    load_env()
    api_id = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")
    session = os.getenv("TELEGRAM_SESSION", "tradelife")
    if not api_id or not api_hash:
        raise SystemExit("TELEGRAM_API_ID/HASH missing in .env")

    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError

    client = TelegramClient(str(ROOT / session), int(api_id), api_hash)

    async def run():
        await client.connect()
        if args.phone:
            sent = await client.send_code_request(args.phone)
            PENDING.parent.mkdir(parents=True, exist_ok=True)
            PENDING.write_text(
                json.dumps({"phone": args.phone, "phone_code_hash": sent.phone_code_hash}),
                encoding="utf-8",
            )
            print(f"SENT code to {args.phone} (phone_code_hash={sent.phone_code_hash})")
            print("Check your Telegram app for the login code, then run:")
            print(f"  python -m src.tg_login --code <CODE>")
            return
        if args.code:
            pending = json.loads(PENDING.read_text(encoding="utf-8")) if PENDING.exists() else {}
            phone = pending.get("phone") or os.getenv("TG_PHONE") or input("phone (same as step 1): ").strip()
            phash = pending.get("phone_code_hash")
            try:
                await client.sign_in(phone=phone, code=args.code, phone_code_hash=phash)
            except SessionPasswordNeededError:
                pwd = input("2FA password: ").strip()
                await client.sign_in(password=pwd)
            me = await client.get_me()
            print(f"SIGNED IN as {me.first_name} (@{me.username})")
            print("session saved:", ROOT / f"{session}.session")
            return
        # already signed in?
        if await client.is_user_authorized():
            me = await client.get_me()
            print(f"already authorized as {me.first_name} (@{me.username})")
        else:
            print("provide --phone first to request a login code")
        await client.disconnect()

    import asyncio

    asyncio.run(run())


if __name__ == "__main__":
    main()
