from __future__ import annotations

import os
from typing import Any, Optional

import httpx

from .gmgn import GmgnCli
from .models import Intent, Side, Signal
from .util import setup_logging

log = setup_logging()

DEXSCREENER = "https://api.dexscreener.com/latest/dex/tokens/"
GMGN_TOKEN = "https://gmgn.ai/defi/quotation/v1/tokens/sol/"
JUP_QUOTE = "https://quote-api.jup.ag/v6/quote"
JUP_SWAP = "https://quote-api.jup.ag/v6/swap"
SOL_MINT = "So11111111111111111111111111111111111111112"


class SolanaVenue:
    def __init__(self, dry_run: bool = True, poll_sec: float = 2.5):
        self.dry_run = dry_run
        self.poll_sec = poll_sec
        self.rpc = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
        self.secret = os.getenv("SOLANA_PRIVATE_KEY", "").strip()
        self.http = httpx.Client(timeout=20.0, headers={"User-Agent": "tradelife/0.1"})
        self.gmgn = GmgnCli()
        self._ready = False

    def connect(self) -> None:
        self._ready = True
        router = "gmgn-cli" if self.gmgn.available() else ("jupiter" if self.secret else "paper")
        mode = "paper" if self.dry_run else "live"
        log.info("Solana venue %s router=%s rpc=%s", mode, router, self.rpc)

    def quote_token(self, mint: str) -> dict[str, Any]:
        out: dict[str, Any] = {"mint": mint}
        try:
            r = self.http.get(DEXSCREENER + mint)
            r.raise_for_status()
            pairs = r.json().get("pairs") or []
            sol_pairs = [p for p in pairs if (p.get("chainId") == "solana")]
            pair = sol_pairs[0] if sol_pairs else (pairs[0] if pairs else None)
            if pair:
                out.update(
                    {
                        "symbol": (pair.get("baseToken") or {}).get("symbol"),
                        "price": float(pair.get("priceUsd") or 0) or None,
                        "mcap": float(pair.get("marketCap") or pair.get("fdv") or 0) or None,
                        "liq": float((pair.get("liquidity") or {}).get("usd") or 0) or None,
                        "source": "dexscreener",
                    }
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("dexscreener failed %s: %s", mint[:8], exc)
        if out.get("price") and out.get("mcap"):
            return out
        try:
            r = self.http.get(GMGN_TOKEN + mint)
            r.raise_for_status()
            data = (r.json() or {}).get("data") or {}
            token = data.get("token") or data
            out.setdefault("symbol", token.get("symbol"))
            if token.get("price"):
                out["price"] = float(token["price"])
            if token.get("market_cap") or token.get("mc"):
                out["mcap"] = float(token.get("market_cap") or token.get("mc"))
            out["source"] = out.get("source") or "gmgn"
        except Exception as exc:  # noqa: BLE001
            log.warning("gmgn quote failed %s: %s", mint[:8], exc)
        return out

    def condition_met(self, signal: Signal, quote: dict[str, Any]) -> bool:
        price = quote.get("price")
        mcap = quote.get("mcap")
        if signal.intent != Intent.WAIT_TARGET:
            return True
        if signal.trigger_mcap and mcap:
            if signal.side == Side.BUY:
                return mcap >= signal.trigger_mcap
            return mcap <= signal.trigger_mcap
        if signal.trigger_price and price:
            if signal.side == Side.BUY:
                return price <= signal.trigger_price
            return price >= signal.trigger_price
        return False

    def execute(self, signal: Signal, sol_amount: float, slippage_bps: int = 100) -> dict[str, Any]:
        if not signal.mint:
            return {"status": "skip", "reason": "no_mint"}
        quote = self.quote_token(signal.mint)
        payload = {
            "venue": "solana",
            "mint": signal.mint,
            "symbol": signal.symbol or quote.get("symbol"),
            "side": signal.side.value,
            "intent": signal.intent.value,
            "sol": sol_amount,
            "quote": quote,
        }
        if signal.side != Side.BUY:
            payload["status"] = "skip"
            payload["reason"] = "solana_sell_not_enabled"
            return payload
        if quote.get("liq") is not None and quote["liq"] < 8000:
            payload["status"] = "skip"
            payload["reason"] = f"low_liquidity:{quote['liq']}"
            return payload
        sl = 0.45
        tp = 1.0
        if signal.stop_loss and quote.get("price"):
            sl = min(0.8, abs(1 - signal.stop_loss / quote["price"]))
        if signal.take_profit and quote.get("price"):
            tp = max(0.2, abs(signal.take_profit / quote["price"] - 1))
        if self.dry_run:
            payload["status"] = "paper"
            payload["router"] = "gmgn-cli" if self.gmgn.available() else "paper"
            log.info("PAPER SOL immediate %s", payload)
            return payload
        try:
            if self.gmgn.available():
                payload["exchange"] = self.gmgn.market_buy(
                    signal.mint, sol_amount, sl_pct=sl, tp_pct=tp, dry_run=False
                )
                payload["router"] = "gmgn-cli"
            else:
                payload["exchange"] = self._jupiter_buy(signal.mint, sol_amount, slippage_bps)
                payload["router"] = "jupiter"
            payload["status"] = "ok"
        except Exception as exc:  # noqa: BLE001
            payload["status"] = "failed"
            payload["error"] = str(exc)
            log.exception("solana swap failed")
        return payload

    def _jupiter_buy(self, mint: str, sol_amount: float, slippage_bps: int) -> dict[str, Any]:
        try:
            import base64
            from solders.keypair import Keypair
            from solders.transaction import VersionedTransaction
            from solana.rpc.api import Client
            import base58
        except ImportError as exc:
            raise RuntimeError("live solana needs solders, solana, base58 installed") from exc

        kp = Keypair.from_bytes(base58.b58decode(self.secret))
        lamports = int(sol_amount * 1_000_000_000)
        params = {
            "inputMint": SOL_MINT,
            "outputMint": mint,
            "amount": str(lamports),
            "slippageBps": str(slippage_bps),
            "swapMode": "ExactIn",
        }
        q = self.http.get(JUP_QUOTE, params=params)
        q.raise_for_status()
        quote = q.json()
        body = {
            "quoteResponse": quote,
            "userPublicKey": str(kp.pubkey()),
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
        }
        s = self.http.post(JUP_SWAP, json=body)
        s.raise_for_status()
        swap_tx = s.json()["swapTransaction"]
        raw = base64.b64decode(swap_tx)
        tx = VersionedTransaction.from_bytes(raw)
        signed = VersionedTransaction(tx.message, [kp])
        client = Client(self.rpc)
        result = client.send_raw_transaction(bytes(signed))
        return {"quote": quote, "txid": str(result)}
