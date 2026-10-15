"""Independent OCR of the coloured price labels on a TradingView chart's right axis.

Used as a second source next to the vision model: a level is only trusted when
both read the same number. Needs the `tesseract` binary (brew install tesseract);
`tesseract_available()` lets callers fail closed when it is missing.

Each label box is found by its background colour (grey/blue entry, red stop or
red current price, green/teal target or green current price), cropped, rendered
in several contrast/scale variants, read with tesseract, and the most frequent
price-shaped reading wins.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterator, Optional

from PIL import Image, ImageChops, ImageOps

PRICE_RE = re.compile(r"^\d{1,3}(,\d{3})*\.\d{2,5}$|^\d+\.\d{2,5}$")


def tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


def _colour(p: tuple[int, int, int]) -> Optional[str]:
    r, g, b = p
    if r > 170 and g < 110 and b < 120 and r - g > 80:
        return "red"
    if g > 110 and r < 90 and g - r > 50 and b > 60:
        return "green"
    if b > 170 and r < 110 and b - r > 70:
        return "blue"
    if abs(r - g) < 14 and abs(g - b) < 18 and 100 <= r <= 215:
        return "grey"
    return None


def _boxes(im: Image.Image, strip: int = 120) -> list[tuple[str, int, int, int]]:
    w, h = im.size
    x0 = max(0, w - strip)
    px = im.load()
    rows: list[Optional[str]] = []
    for y in range(h):
        counts: dict[str, int] = {}
        for x in range(x0, w - 1):
            c = _colour(px[x, y])
            if c:
                counts[c] = counts.get(c, 0) + 1
        best = max(counts.items(), key=lambda kv: kv[1]) if counts else (None, 0)
        rows.append(best[0] if best[1] >= 18 else None)
    out = []
    y = 0
    while y < h:
        c = rows[y]
        if c is None:
            y += 1
            continue
        y1 = y
        while y1 + 1 < h and (rows[y1 + 1] == c or (y1 + 2 < h and rows[y1 + 2] == c and rows[y1 + 1] is None)):
            y1 += 1
        if 7 <= y1 - y + 1 <= 40:
            mid = (y + y1) // 2
            xs = [x for x in range(x0, w - 1) if _colour(px[x, mid]) == c]
            out.append((c, y, y1, min(xs)))
        y = y1 + 1
    return out


def _variants(crop: Image.Image) -> Iterator[Image.Image]:
    r, g, b = crop.split()
    weakest = ImageChops.darker(ImageChops.darker(r, g), b)  # white text stays bright
    for k in (3, 4, 6, 8):
        a = ImageOps.autocontrast(weakest).resize((weakest.width * k, weakest.height * k), Image.BICUBIC)
        yield ImageOps.invert(a)
        yield a.point(lambda v: 0 if v > 140 else 255)
        yield a.point(lambda v: 0 if v > 190 else 255)


def _ocr(img: Image.Image, tmp: Path) -> list[float]:
    ImageOps.expand(img, border=25, fill=255).save(tmp)
    vals = []
    for psm in ("7", "8"):
        text = subprocess.run(
            ["tesseract", str(tmp), "-", "--psm", psm, "-c", "tessedit_char_whitelist=0123456789.,"],
            capture_output=True, text=True, timeout=20,
        ).stdout.strip()
        if PRICE_RE.match(text):
            vals.append(float(text.replace(",", "")))
    return vals


def read_axis_labels(image_path: str | Path) -> list[dict[str, Any]]:
    """[{colour, y, value, votes}] for every coloured label on the right axis."""
    im = Image.open(image_path).convert("RGB")
    w, h = im.size
    out = []
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d) / "label.png"
        for colour, ya, yb, xa in _boxes(im):
            crop = im.crop((max(0, xa - 3), max(0, ya - 1), w, min(h, yb + 2)))
            votes = Counter(v for variant in _variants(crop) for v in _ocr(variant, tmp))
            top = votes.most_common(1)
            out.append({"colour": colour, "y": ya, "value": top[0][0] if top else None,
                        "votes": top[0][1] if top else 0})
    return out


def ocr_levels(labels: list[dict[str, Any]], side: str, min_votes: int = 1) -> dict[str, Any]:
    """Map OCR labels to roles. The stop is the outermost red label (lowest for a
    long); the current-price label, when red, sits between it and the entry.
    Labels read with fewer than `min_votes` agreeing variants are ignored, and if
    the outermost red box is one of them there is no stop."""
    vals = [lb for lb in labels if lb["value"] is not None and lb["votes"] >= min_votes]
    reds = [lb["value"] for lb in vals if lb["colour"] == "red"]
    stop = (min(reds) if side == "buy" else max(reds)) if reds else None
    # The stop is the outermost red box by position, read or not. If that box was
    # dropped (unreadable or too few votes, e.g. cut off at the image edge), an
    # inner red label is usually the current price: no stop rather than that one.
    red_boxes = [lb for lb in labels if lb["colour"] == "red"]
    if red_boxes:
        outer = max(red_boxes, key=lambda lb: lb["y"]) if side == "buy" else min(red_boxes, key=lambda lb: lb["y"])
        if outer["value"] is None or outer["votes"] < min_votes or outer["value"] != stop:
            stop = None
    return {
        "stop_loss": stop,
        "stop_candidates": reds,
        "entry_candidates": [lb["value"] for lb in vals if lb["colour"] in ("grey", "blue")],
        "target_candidates": [lb["value"] for lb in vals if lb["colour"] == "green"],
        "labels": labels,
    }
