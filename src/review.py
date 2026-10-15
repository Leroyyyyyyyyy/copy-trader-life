"""Human review of plans the engine held back (split LLM votes, "pick one of" posts,
chart reads that disagree).

    python -m src.review list
    python -m src.review show <id>
    python -m src.review approve <id> [--stop P] [--trigger P] [--tp P]
    python -m src.review reject <id>

The engine is the only writer of state.json. This tool reads it, and writes one
decision file into <data>/review_inbox/; the running engine applies it on its next
poll (risk checks run again then). An approve for an entry without a stop is
refused by the engine until a stop is given.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .util import DATA_DIR


def _cards(data_dir: Path) -> dict[str, Any]:
    path = data_dir / "state.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8")).get("reviews") or {}


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%m-%d %H:%M")


def _line(rid: str, c: dict[str, Any]) -> str:
    s = c["signal"]
    left = int((float(c["expires_at"]) - time.time()) / 60)
    return (f"{rid}  {s['channel']}#{s['message_id']}  {s['side']} {s['symbol']} {s['intent']}  "
            f"trig={s.get('trigger_price')} sl={s.get('stop_loss')} tp={s.get('take_profit')}  "
            f"{'expired' if left < 0 else f'{left}m left'}  | {'; '.join(c['reasons'])[:120]}")


def _write(data_dir: Path, cmd: dict[str, Any]) -> Path:
    inbox = data_dir / "review_inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    final = inbox / f"{int(time.time() * 1000)}-{cmd['command_id']}.json"
    tmp = final.with_suffix(".tmp")
    tmp.write_text(json.dumps(cmd, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, final)  # the engine only reads *.json, so it never sees a half-written file
    return final


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Review plans held by the engine")
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sh = sub.add_parser("show")
    sh.add_argument("id")
    ok = sub.add_parser("approve")
    ok.add_argument("id")
    ok.add_argument("--stop", type=float, dest="stop_loss")
    ok.add_argument("--trigger", type=float, dest="trigger_price")
    ok.add_argument("--tp", type=float, dest="take_profit")
    no = sub.add_parser("reject")
    no.add_argument("id")
    args = ap.parse_args(argv)

    cards = _cards(args.data_dir)
    if args.cmd == "list":
        if not cards:
            print("no plans waiting for review")
        for rid, c in sorted(cards.items(), key=lambda kv: kv[1]["created_at"]):
            print(_line(rid, c))
        return 0
    card = cards.get(args.id)
    if card is None:
        print(f"no review {args.id}", file=sys.stderr)
        return 1
    if args.cmd == "show":
        s = card["signal"]
        print(_line(args.id, card))
        print(f"created {_when(card['created_at'])} UTC, expires {_when(card['expires_at'])} UTC, mode {card['execution_mode']}")
        print("reasons:", *card["reasons"], sep="\n  ")
        if card.get("options"):
            print("options:", json.dumps(card["options"], ensure_ascii=False, indent=1))
        if card.get("pending_same_channel"):
            print("other pending plans from this channel:", *card["pending_same_channel"], sep="\n  ")
        print("post:", s["text"])
        return 0
    cmd: dict[str, Any] = {"command_id": uuid.uuid4().hex[:12], "review_id": args.id, "decision": args.cmd,
                           "at": time.time()}
    if args.cmd == "approve":
        cmd.update({k: getattr(args, k) for k in ("stop_loss", "trigger_price", "take_profit")
                    if getattr(args, k) is not None})
    path = _write(args.data_dir, cmd)
    print(f"queued {args.cmd} for {args.id} ({path.name}); the engine applies it on its next poll")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
