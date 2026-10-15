"""Score src/llm_gate.py on posts the rule parser would act on, against hand labels.

Two sets: the 2026-09 month (the failures that motivated the gate) and older posts
2025-10..2026-08 (not looked at while writing the gate prompt). A wrong allow is the
dangerous failure: the bot trades on a post it should have ignored.
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import yaml

from src.llm_gate import GATE_VERSION, decide, review
from src.parser import parse_message

ROOT = Path(__file__).resolve().parents[3]
LABELS = ROOT / "experiments" / "fixtures" / "gate" / "popi_gate_labels.json"
SOURCES = {"month_2026_09": ROOT / "data" / "lab" / "popigogo_30d.json",
           "older_2025_10_2026_08": ROOT / "data" / "popigogo_posts.json"}


def _cases() -> list[dict[str, Any]]:
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    ch = next(c for c in cfg["channels"] if c["username"] == "POPIGOGO")
    universe = set(json.loads((ROOT / "data" / "lab" / "hl_universe.json").read_text())["mainnet"])
    labels = json.loads(LABELS.read_text(encoding="utf-8"))["labels"]
    out = []
    for set_id, path in SOURCES.items():
        posts = sorted((p for p in json.loads(path.read_text(encoding="utf-8")) if p.get("text")), key=lambda p: int(p["id"]))
        by_id = {int(p["id"]): i for i, p in enumerate(posts)}
        for lb in (x for x in labels if x["set"] == set_id):
            i = by_id[lb["id"]]
            p = posts[i]
            sig = parse_message(channel="POPIGOGO", message_id=int(p["id"]), date=p.get("date", ""), text=p["text"],
                                default_symbol=ch.get("default_symbol"), hl_universe=universe,
                                other_assets=ch.get("other_assets"))
            out.append({**lb, "text": p["text"], "has_image": bool(p.get("has_photo")),
                        "context": [q["text"] for q in posts[max(0, i - 6):i]],
                        "parsed": {"intent": sig.intent.value, "side": sig.side.value, "symbol": sig.symbol}})
    return out


def _outcome(allow: bool, should_act: bool) -> str:
    return "correct" if allow == should_act else ("wrong_allow" if allow else "wrong_block")


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary = {}
    for set_id in SOURCES:
        rs = [r for r in rows if r["set"] == set_id]
        for name, sub in (("all", rs), ("unambiguous", [r for r in rs if not r["ambiguous"]])):
            runs = [o for r in sub for o in r["outcomes"]]
            summary[f"{set_id}/{name}"] = {
                "cases": len(sub), "runs": len(runs),
                "correct": runs.count("correct"),
                "wrong_allow": runs.count("wrong_allow"),
                "wrong_block": runs.count("wrong_block"),
                "errors": runs.count("error"),
                "unstable_cases": [r["id"] for r in sub if r["unstable"]],
                "should_act_cases": sum(r["should_act"] for r in sub),
            }
    return summary


def run(workers: int = 4, repeats: int = 1) -> dict[str, Any]:
    """Each case is sent `repeats` times; the model is not deterministic even at
    temperature 0, so a case counts as unstable when its runs disagree. Per-run
    outcomes are what the engine would have done on that delivery."""
    cases = _cases()

    def one(c: dict[str, Any]) -> dict[str, Any]:
        return review(text=c["text"], context=c["context"], parsed=c["parsed"], has_image=c["has_image"]).to_dict()

    jobs = [c for c in cases for _ in range(repeats)]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        flat = list(pool.map(one, jobs))
    rows = []
    for i, c in enumerate(cases):
        ds = flat[i * repeats:(i + 1) * repeats]
        outs = ["error" if d["error"] else _outcome(d["allow"], c["should_act"]) for d in ds]
        rows.append({**{k: c[k] for k in ("set", "id", "should_act", "ambiguous", "note", "parsed")},
                     "text": c["text"][:200], "decisions": ds, "outcomes": outs,
                     "outcome": max(set(outs), key=outs.count), "unstable": len(set(d["allow"] for d in ds)) > 1})
    summary = _summary(rows)
    tokens = sum((d.get("usage") or {}).get("completion_tokens", 0) for d in flat)
    return {"gate_version": GATE_VERSION, "repeats": repeats, "completion_tokens": tokens,
            "summary": summary, "rows": rows}


def rescore(path: Path) -> dict[str, Any]:
    """Re-apply the current `decide` rules to the model replies stored in a report (no API calls)."""
    report = json.loads(path.read_text(encoding="utf-8"))
    for r in report["rows"]:
        for d in r["decisions"]:
            if not d["error"]:
                d["allow"], *_ , why = decide(d, r["parsed"])
                d["rule_reasons"] = why
        r["outcomes"] = ["error" if d["error"] else _outcome(d["allow"], r["should_act"]) for d in r["decisions"]]
        r["outcome"] = max(set(r["outcomes"]), key=r["outcomes"].count)
        r["unstable"] = len(set(d["allow"] for d in r["decisions"])) > 1
    report["summary"] = _summary(report["rows"])
    report["gate_version"] = GATE_VERSION + "+rescored"
    return report


def main(argv: list[str] | None = None) -> int:
    import os

    from src.lab.models.provider import _read_env_file

    for k, v in _read_env_file(ROOT / ".env").items():  # DEEPSEEK_* only
        os.environ.setdefault(k, v)
    ap = argparse.ArgumentParser(description="LLM gate eval on rule-parser actions")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--rescore", type=Path, default=None, help="re-apply current rules to a saved report")
    args = ap.parse_args(argv)
    report = rescore(args.rescore) if args.rescore else run(repeats=args.repeats)
    if args.out:
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    sys.stdout.write(json.dumps(report["summary"], ensure_ascii=False, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
