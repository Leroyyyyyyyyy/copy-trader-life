"""Download original chart images for the 7 POPIGOGO structure setups.

Priority:
  1. Direct CDN download from meta.json photos[0] (no auth needed, usually full-res).
     Each post's photos[1] is a repeated channel banner (same URL everywhere), so it is skipped.
  2. Telethon download_media fallback when creds are present (for posts where CDN fails
     or returns a small thumb).

Output: data/charts/orig_{id}.jpg  (plus orig_{id}_{k}.jpg if a post has multiple real photos)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
CHARTS = DATA / "charts"
META = CHARTS / "meta.json"

# The same CDN URL repeated across many posts is a channel banner / avatar, not a chart.
BANNER_MARKER = "i5BWQeD6PQwyW5zyqSAKPBHCN"

SETUP_IDS = [5718, 5847, 6014, 6059, 6190, 6361, 7076, 7154, 7217]


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


def load_meta() -> dict:
    if not META.exists():
        raise SystemExit(f"missing {META}; run the public-page scraper first")
    return json.loads(META.read_text(encoding="utf-8"))


def real_photo_urls(post: dict) -> list[str]:
    out = []
    for u in post.get("photos") or []:
        if BANNER_MARKER in u:
            continue
        if u not in out:
            out.append(u)
    return out


def direct_download(http: httpx.Client, url: str, dest: Path) -> tuple[bool, int, int]:
    try:
        r = http.get(url, timeout=60)
        r.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        print(f"  CDN fail: {exc}")
        return False, 0, 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(r.content)
    from PIL import Image  # deferred import

    with Image.open(dest) as im:
        w, h = im.size
    return True, w, h


async def telethon_download(api_id: int, api_hash: str, session: str, post_ids: list[int]) -> dict[int, list[Path]]:
    """Fallback: fetch originals via Telethon for posts we still need."""
    try:
        from telethon import TelegramClient
    except ImportError:
        print("telethon not installed; skip fallback")
        return {}

    out: dict[int, list[Path]] = {}
    client = TelegramClient(str(ROOT / session), api_id, api_hash)
    await client.start()
    for pid in post_ids:
        try:
            msg = await client.get_messages("POPIGOGO", ids=pid)
            if isinstance(msg, list):
                msg = msg[0] if msg else None
        except Exception as exc:  # noqa: BLE001
            print(f"  telethon get {pid} fail: {exc}")
            continue
        if msg is None:
            print(f"  telethon: no message {pid}")
            continue
        paths: list[Path] = []
        if msg.photo:
            dest = CHARTS / f"orig_{pid}.jpg"
            await client.download_media(msg, file=str(dest))
            paths.append(dest)
        elif msg.media and getattr(msg.media, "photo", None):
            dest = CHARTS / f"orig_{pid}.jpg"
            await client.download_media(msg, file=str(dest))
            paths.append(dest)
        else:
            print(f"  telethon: message {pid} has no photo media")
        out[pid] = paths
    await client.disconnect()
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", nargs="*", type=int, default=SETUP_IDS, help="post ids (default: the 7 setups + 5847/7076)")
    ap.add_argument("--force", action="store_true", help="redownload even if orig file exists")
    args = ap.parse_args()

    load_env()
    meta = load_meta()
    http = httpx.Client(timeout=60, headers={"User-Agent": "Mozilla/5.0"})
    CHARTS.mkdir(parents=True, exist_ok=True)

    missing: list[int] = []
    for pid in args.ids:
        post = meta.get(str(pid))
        if not post:
            print(f"#{pid}: not in meta.json, skip")
            continue
        urls = real_photo_urls(post)
        if not urls:
            print(f"#{pid}: no real photo URL, skip")
            missing.append(pid)
            continue
        done = []
        for k, url in enumerate(urls):
            dest = CHARTS / (f"orig_{pid}.jpg" if k == 0 else f"orig_{pid}_{k}.jpg")
            if dest.exists() and not args.force:
                from PIL import Image

                with Image.open(dest) as im:
                    w, h = im.size
                print(f"#{pid}[{k}]: exists {dest.name} {w}x{h}")
                done.append(True)
                continue
            ok, w, h = direct_download(http, url, dest)
            if ok:
                print(f"#{pid}[{k}]: CDN -> {dest.name} {w}x{h}")
                done.append(True)
            else:
                print(f"#{pid}[{k}]: CDN failed")
                done.append(False)
        if not all(done):
            missing.append(pid)

    if missing:
        print(f"\n{len(missing)} post(s) still need originals: {missing}")
        api_id = os.getenv("TELEGRAM_API_ID")
        api_hash = os.getenv("TELEGRAM_API_HASH")
        session = os.getenv("TELEGRAM_SESSION", "tradelife")
        if api_id and api_hash:
            print("falling back to Telethon…")
            out = asyncio.run(telethon_download(int(api_id), api_hash, session, missing))
            for pid, paths in out.items():
                for p in paths:
                    from PIL import Image

                    with Image.open(p) as im:
                        print(f"#{pid}: telethon -> {p.name} {im.size[0]}x{im.size[1]}")
        else:
            print("no TELEGRAM_API_ID/HASH in .env; add them or re-run with creds for fallback")


if __name__ == "__main__":
    main()
