"""Score chart readers against hand labels (experiments/fixtures/charts/).

v1 = the reader's results from the month replay (data/lab/month_replay/chart_levels.json).
v2 = the current src/chart_reader.read_levels_detail, run now.

A wrong stop is the dangerous failure (an order goes out with it); a missing stop
is a safe failure (the engine has no stop and must not enter). For captions whose
symbol does not match the chart, and for non-charts, the correct output is no levels.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

from src.chart_reader import PROMPT_VERSION, read_levels_detail

ROOT = Path(__file__).resolve().parents[3]
LABELS = ROOT / "experiments" / "fixtures" / "charts" / "popi_2026_09_chart_labels.json"
IMAGES = ROOT / "data" / "lab" / "popi_images"
V1 = ROOT / "data" / "lab" / "month_replay" / "chart_levels.json"
# Stand-in for the live Hyperliquid mark the engine passes (2026-09/10 levels).
REFERENCE = {"BTC": 80000.0, "ETH": 2500.0, "ZEC": 1350.0, "牛來": 0.11}
TIMEFRAME = {7683: "2h", 7689: "2h", 7788: "4h", 7801: "4h", 7970: "6h", 7977: "6h", 7995: "6h", 7996: "6h", 8015: "6h"}


def _close(a: Optional[float], b: Optional[float], tol: float) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) / b <= tol


def score(label: dict[str, Any], pred: dict[str, Any]) -> dict[str, Any]:
    entry, sl, tp = pred.get("entry"), pred.get("stop_loss"), pred.get("take_profit")
    has_levels = any(v is not None for v in (entry, sl, tp))
    if pred.get("error"):
        return {"bucket": "error", "dangerous": False}
    if label["kind"] != "position_tool":
        return {"bucket": "non_chart", "ok": not has_levels, "dangerous": has_levels}
    if label["chart_symbol"] != label["text_symbol"]:
        return {"bucket": "symbol_mismatch", "ok": not has_levels, "dangerous": sl is not None}
    stop_ok = _close(sl, label["stop_loss"], 0.002)
    return {
        "bucket": "chart",
        "scored": label["legible"],
        "stop": "correct" if stop_ok else ("missing" if sl is None else "wrong"),
        "entry_ok": _close(entry, label["entry"], 0.003 if label["entry_labeled"] else 0.015),
        "tp_ok": _close(tp, label["take_profit"], 0.003),
        "dangerous": sl is not None and not stop_ok,
    }


def summarize(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    s = [r[key]["score"] for r in rows]
    chart = [x for x in s if x["bucket"] == "chart" and x.get("scored")]
    return {
        "charts_scored": len(chart),
        "stop_correct": sum(x["stop"] == "correct" for x in chart),
        "stop_missing": sum(x["stop"] == "missing" for x in chart),
        "stop_wrong": sum(x["stop"] == "wrong" for x in chart),
        "entry_correct": sum(x["entry_ok"] for x in chart),
        "tp_correct": sum(x["tp_ok"] for x in chart),
        "errors": sum(x["bucket"] == "error" for x in s),
        "non_chart_clean": f"{sum(x.get('ok', False) for x in s if x['bucket'] == 'non_chart')}/"
                           f"{sum(x['bucket'] == 'non_chart' for x in s)}",
        "symbol_mismatch_rejected": f"{sum(x.get('ok', False) for x in s if x['bucket'] == 'symbol_mismatch')}/"
                                    f"{sum(x['bucket'] == 'symbol_mismatch' for x in s)}",
        "dangerous": sum(x["dangerous"] for x in s),
    }


def _v1() -> dict[int, dict[str, Any]]:
    out = {}
    for r in json.loads(V1.read_text(encoding="utf-8")):
        lv = r.get("levels") or {}
        out[r["id"]] = {"entry": lv.get("yellow_or_trigger"), "stop_loss": lv.get("stop_loss"),
                        "take_profit": lv.get("take_profit"), "error": lv.get("error")}
    return out


def _v2_one(label: dict[str, Any]) -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        d = read_levels_detail(IMAGES / f"{label['id']}.jpg", label["text_symbol"], "buy",
                               TIMEFRAME.get(label["id"], "8h"), reference_price=REFERENCE[label["text_symbol"]])
        d["sec"] = round(time.perf_counter() - t0, 1)
        return d
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {str(exc)[:160]}", "sec": round(time.perf_counter() - t0, 1)}


def run(workers: int = 4) -> dict[str, Any]:
    labels = json.loads(LABELS.read_text(encoding="utf-8"))["labels"]
    v1 = _v1()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        v2 = list(pool.map(_v2_one, labels))
    rows = []
    for label, p2 in zip(labels, v2):
        p1 = v1.get(label["id"], {"error": "not_run"})
        rows.append({"id": label["id"], "label": label,
                     "v1": {"pred": p1, "score": score(label, p1)},
                     "v2": {"pred": p2, "score": score(label, p2)}})
    return {"labels_version": json.loads(LABELS.read_text(encoding="utf-8"))["version"],
            "v2_prompt_version": PROMPT_VERSION, "reference_prices": REFERENCE,
            "summary": {"v1": summarize(rows, "v1"), "v2": summarize(rows, "v2")}, "rows": rows}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Chart reader eval vs hand labels")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args(argv)
    report = run(args.workers)
    blob = json.dumps(report, ensure_ascii=False, indent=1, default=str)
    if args.out:
        args.out.write_text(blob + "\n", encoding="utf-8")
    sys.stdout.write(json.dumps(report["summary"], ensure_ascii=False, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
