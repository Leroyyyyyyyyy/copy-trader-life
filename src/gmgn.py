from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any, Optional

from .util import setup_logging

log = setup_logging()

SOL_MINT = "So11111111111111111111111111111111111111112"


class GmgnCli:
    """Live Solana execution through official gmgn-cli (not the Cloudflare-blocked website)."""

    def __init__(self):
        self.bin = shutil.which("gmgn-cli")
        self.wallet = os.getenv("GMGN_WALLET", "").strip()
        self.api_key = os.getenv("GMGN_API_KEY", "").strip()
        self.allow = os.getenv("GMGN_ALLOW_AUTOMATED_TRADES", "").strip() == "1"

    def available(self) -> bool:
        return bool(self.bin and self.wallet and self.api_key)

    def _run(self, args: list[str], timeout: int = 60) -> dict[str, Any]:
        if not self.bin:
            raise RuntimeError("gmgn-cli not installed. npm install -g gmgn-cli")
        cmd = [self.bin, *args, "--raw"]
        log.info("gmgn %s", " ".join(args[:8]))
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode != 0:
            raise RuntimeError(err or out or f"gmgn-cli exit {proc.returncode}")
        try:
            return json.loads(out) if out else {"stdout": out}
        except json.JSONDecodeError:
            return {"stdout": out, "stderr": err}

    def market_buy(
        self,
        mint: str,
        sol_amount: float,
        *,
        sl_pct: float = 0.45,
        tp_pct: float = 1.0,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        lamports = str(int(sol_amount * 1_000_000_000))
        conditions = json.dumps(
            [
                {"order_type": "profit_stop", "side": "sell", "price_scale": str(int(tp_pct * 100)), "sell_ratio": "100"},
                {"order_type": "loss_stop", "side": "sell", "price_scale": str(int(sl_pct * 100)), "sell_ratio": "100"},
            ]
        )
        args = [
            "swap",
            "--chain", "sol",
            "--from", self.wallet,
            "--input-token", SOL_MINT,
            "--output-token", mint,
            "--amount", lamports,
            "--auto-slippage",
            "--anti-mev",
            "--condition-orders", conditions,
        ]
        if dry_run or not self.allow:
            return {
                "status": "paper",
                "router": "gmgn-cli",
                "cmd": " ".join(args),
                "sol": sol_amount,
                "mint": mint,
            }
        args.append("--yes")
        env_note = {"GMGN_ALLOW_AUTOMATED_TRADES": "1"}
        result = self._run(args)
        result["router"] = "gmgn-cli"
        result["status"] = "submitted"
        result.update(env_note)
        return result
