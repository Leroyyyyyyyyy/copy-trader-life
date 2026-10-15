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

from src.chart_ocr import ocr_levels, read_axis_labels
from src.chart_reader import (
    OCR_PRIMARY_MIN_VOTES,
    PROMPT_VERSION,
    _post_checks,
    ocr_gate,
    ocr_primary_gate,
    read_levels_detail,
)

ROOT = Path(__file__).resolve().parents[3]
LABELS = ROOT / "experiments" / "fixtures" / "charts" / "popi_2026_09_chart_labels.json"
IMAGES = ROOT / "data" / "lab" / "popi_images"
V1 = ROOT / "data" / "lab" / "month_replay" / "chart_levels.json"
# Stand-in for the live Hyperliquid mark the engine passes (2026-09/10 levels).
REFERENCE = {"BTC": 80000.0, "ETH": 2500.0, "ZEC": 1350.0, "牛來": 0.11}
# Held-out (2026-01..08) marks are lower; a stand-in per label set keeps the scale check meaningful.
HELDOUT_REFERENCE = {"BTC": 70000.0, "ETH": 2000.0, "OIL": 92.0}
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
        "stop_exact": _close(sl, label["stop_loss"], 0.000001),
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
        "stop_exact": sum(x.get("stop_exact", False) for x in chart),
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


def _image(label: dict[str, Any]) -> Path:
    return ROOT / label["image"] if label.get("image") else IMAGES / f"{label['id']}.jpg"


def _ref(label: dict[str, Any], table: dict[str, float]) -> Optional[float]:
    return table.get(label["text_symbol"])


def _v2_one(label: dict[str, Any], table: dict[str, float] = REFERENCE) -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        d = read_levels_detail(_image(label), label["text_symbol"], label.get("side", "buy"),
                               TIMEFRAME.get(label["id"], "8h"), reference_price=_ref(label, table))
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


def add_gates(report: dict[str, Any], table: dict[str, float] = REFERENCE) -> dict[str, Any]:
    """v3 = vision reads kept only where OCR reads the same number (agree).
    v4 = OCR numbers, vision only confirms which label is which (ocr_primary).
    Both reuse the same v2 vision reads, so differences come only from the gate."""
    for row in report["rows"]:
        label = row["label"]
        side = label.get("side", "buy")
        p2 = row["v2"]["pred"]
        if p2.get("error"):
            p3: dict[str, Any] = {"error": p2["error"]}
            p4: dict[str, Any] = dict(p3)
        elif not any(p2.get(k) is not None for k in ("entry", "stop_loss", "take_profit")):
            p3 = {"entry": None, "stop_loss": None, "take_profit": None, "rejected": p2.get("rejected")}
            p4 = dict(p3)
        else:
            labels = read_axis_labels(_image(label))
            ocr = ocr_levels(labels, side)
            gated, why = ocr_gate(p2, ocr)
            why += _post_checks(gated, side, p2.get("current_price"), _ref(label, table))
            p3 = {**gated, "rejected": (p2.get("rejected") or []) + why}
            g4, why4 = ocr_primary_gate(p2, ocr_levels(labels, side, min_votes=OCR_PRIMARY_MIN_VOTES))
            why4 += _post_checks(g4, side, p2.get("current_price"), _ref(label, table))
            p4 = {**g4, "rejected": (p2.get("rejected") or []) + why4}
        row["v3"] = {"pred": p3, "score": score(label, p3)}
        row["v4"] = {"pred": p4, "score": score(label, p4)}
        for key in ("v1", "v2"):  # rescore with the current scorer
            if key in row:
                row[key]["score"] = score(label, row[key]["pred"])
    report["summary"] = {k: summarize(report["rows"], k) for k in ("v1", "v2") if all(k in r for r in report["rows"])}
    report["summary"]["v3_vision_plus_ocr"] = summarize(report["rows"], "v3")
    report["summary"]["v4_ocr_numbers_vision_roles"] = summarize(report["rows"], "v4")
    return report


def gate_existing(report_path: Path) -> dict[str, Any]:
    return add_gates(json.loads(report_path.read_text(encoding="utf-8")))


def run_heldout(labels_path: Path, workers: int = 4) -> dict[str, Any]:
    doc = json.loads(labels_path.read_text(encoding="utf-8"))
    labels = doc["labels"]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        v2 = list(pool.map(lambda lb: _v2_one(lb, HELDOUT_REFERENCE), labels))
    rows = [{"id": lb["id"], "label": lb, "v2": {"pred": p, "score": score(lb, p)}} for lb, p in zip(labels, v2)]
    report = {"labels_version": doc["version"], "v2_prompt_version": PROMPT_VERSION,
              "reference_prices": HELDOUT_REFERENCE, "rows": rows}
    return add_gates(report, HELDOUT_REFERENCE)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Chart reader eval vs hand labels")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--heldout", type=Path, default=None, help="label file to run v2+v3+v4 on (fresh vision calls)")
    ap.add_argument("--gate-from", type=Path, default=None,
                    help="apply the OCR gate to the v2 reads in an existing report (no vision calls)")
    args = ap.parse_args(argv)
    if args.heldout:
        report = run_heldout(args.heldout, args.workers)
    elif args.gate_from:
        report = gate_existing(args.gate_from)
    else:
        report = run(args.workers)
    blob = json.dumps(report, ensure_ascii=False, indent=1, default=str)
    if args.out:
        args.out.write_text(blob + "\n", encoding="utf-8")
    sys.stdout.write(json.dumps(report["summary"], ensure_ascii=False, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
