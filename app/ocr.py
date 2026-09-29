"""Purchase-bill OCR: photo of a distributor invoice -> draft purchase lines.

Pipeline
  1. quality check - blur (Laplacian variance), contrast, resolution
  2. enhancement   - deskew, shadow removal, denoise, unsharp mask, Richardson-Lucy
                     deblur, adaptive (Sauvola) threshold; several pipelines are
                     tried and the one whose numbers cross-check best is kept
  2b. OCR          - Tesseract (free, runs locally / on the server)
  3. parse        - regex + rules pull out invoice no, date, and per line:
                    batch, expiry (MM/YY), qty, free qty, MRP, rate, GST%
  4. match        - fuzzy-match each line to the medicine catalogue (difflib)
  5. human review - the pharmacist checks/edits every line before stock is added

OCR is never trusted blindly: every field lands in an editable grid with a
match-confidence score, and nothing is saved until a person confirms.
"""
from __future__ import annotations

import calendar
import difflib
import re
from dataclasses import dataclass, field
from datetime import date

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

GST_RATES = {0.0, 5.0, 12.0, 18.0, 28.0}
EXPIRY_RE = re.compile(r"\b(0?[1-9]|1[0-2])\s*[/\-.]\s*(\d{4}|\d{2})\b")
DATE_RE = re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})\b")
INVOICE_RE = re.compile(r"\b(?:[il1]nvoice|inv|bill)\b\.?\s*(?:no|number|#)?\.?\s*[:#\-]*\s*"
                        r"([A-Z0-9][A-Z0-9/\-]*\d[A-Z0-9/\-]*)", re.IGNORECASE)
NUM_RE = re.compile(r"^\d+(?:[.,]\d+)?%?$")


class OCRUnavailable(RuntimeError):
    pass


@dataclass
class ParsedLine:
    raw: str
    product_text: str
    medicine: str | None
    match_score: float
    batch_no: str = ""
    expiry: str | None = None
    qty: int = 0
    free_qty: int = 0
    mrp: float = 0.0
    rate: float = 0.0
    gst_rate: float = 5.0
    amount: float | None = None
    amount_check: bool = False          # qty x rate x (1+GST) matches the printed line amount


@dataclass
class ParsedBill:
    invoice_no: str | None = None
    bill_date: str | None = None
    supplier: str | None = None
    lines: list[ParsedLine] = field(default_factory=list)
    raw_text: str = ""


# ----------------------------------------------------------------- 1. image quality + enhancement
def _gray(img: Image.Image) -> np.ndarray:
    return np.asarray(ImageOps.exif_transpose(img).convert("L")).astype(np.float32)


def _to_img(a: np.ndarray) -> Image.Image:
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))


def _resize_w(a: np.ndarray, width: int) -> np.ndarray:
    h, w = a.shape
    if w == width:
        return a
    return np.asarray(_to_img(a).resize((width, max(1, int(h * width / w))), Image.LANCZOS)).astype(np.float32)


def _otsu(a: np.ndarray) -> float:
    hist = np.bincount(np.clip(a, 0, 255).astype(np.uint8).ravel(), minlength=256).astype(float)
    p = hist / hist.sum()
    w, mu = np.cumsum(p), np.cumsum(p * np.arange(256))
    with np.errstate(divide="ignore", invalid="ignore"):
        between = (mu[-1] * w - mu) ** 2 / (w * (1 - w))
    return float(np.nanargmax(between))


def image_quality(img: Image.Image) -> dict:
    """Blur (variance of the Laplacian), brightness and contrast, measured at a fixed width.

    Sharp printed text gives a high Laplacian variance; blur smooths edges and drives it down.
    """
    from scipy import ndimage as ndi
    a = _resize_w(_gray(img), 1000)
    blur = float(ndi.laplace(a / 255.0).var() * 1000)
    brightness, contrast = float(a.mean()), float(a.std())
    if img.width < 700:
        verdict = "low resolution"
    elif blur < 1.5:
        verdict = "very blurry"
    elif blur < 6:
        verdict = "blurry"
    elif contrast < 35:
        verdict = "low contrast"
    else:
        verdict = "good"
    return {"blur_score": round(blur, 1), "brightness": round(brightness), "contrast": round(contrast),
            "width": img.width, "height": img.height, "verdict": verdict}


def deskew(a: np.ndarray, max_angle: float = 6.0, step: float = 0.5) -> tuple[np.ndarray, float]:
    """Straighten tilted photos: pick the angle that makes text rows line up
    (maximum variance of the horizontal ink projection)."""
    from scipy import ndimage as ndi
    small = _resize_w(a, min(800, a.shape[1]))
    ink = (small < _otsu(small)).astype(np.float32)
    best, angle = -1.0, 0.0
    for t in np.arange(-max_angle, max_angle + 1e-6, step):
        score = float(np.var(ndi.rotate(ink, t, reshape=False, order=0).sum(axis=1)))
        if score > best:
            best, angle = score, float(t)
    if abs(angle) < 0.25:
        return a, 0.0
    return ndi.rotate(a, angle, reshape=True, order=1, cval=255.0), angle


def flatten_lighting(a: np.ndarray) -> np.ndarray:
    """Remove shadows / uneven light: divide by a smooth estimate of the paper background."""
    from scipy import ndimage as ndi
    bg = ndi.gaussian_filter(ndi.grey_closing(a, size=(15, 15)), max(a.shape) / 30)
    return np.clip(a / np.maximum(bg, 1.0) * 235.0, 0, 255)


def unsharp(a: np.ndarray, sigma: float, amount: float) -> np.ndarray:
    """Unsharp mask: add back the difference between the image and a blurred copy."""
    from scipy import ndimage as ndi
    return np.clip(a + amount * (a - ndi.gaussian_filter(a, sigma)), 0, 255)


def deblur_rl(a: np.ndarray, sigma: float, iters: int = 10) -> np.ndarray:
    """Richardson-Lucy deconvolution with a Gaussian blur model (reverses focus / motion softness)."""
    from scipy import ndimage as ndi
    img = np.clip(a, 1, 255) / 255.0
    u = img.copy()
    for _ in range(iters):
        est = ndi.gaussian_filter(u, sigma)
        u = np.clip(u * ndi.gaussian_filter(img / np.maximum(est, 1e-3), sigma), 0, 1.5)
    return np.clip(u * 255.0, 0, 255)


def sauvola(a: np.ndarray, window: int = 41, k: float = 0.2) -> np.ndarray:
    """Local (adaptive) threshold - each pixel is compared with its own neighbourhood,
    so shadows and gradients don't wipe out text the way one global threshold does."""
    from scipy import ndimage as ndi
    m = ndi.uniform_filter(a, window)
    sd = np.sqrt(np.maximum(ndi.uniform_filter(a * a, window) - m * m, 0))
    return (a > m * (1 + k * (sd / 128.0 - 1))) * 255.0


def preprocess(img: Image.Image) -> Image.Image:
    """Basic pipeline (good for clean scans): grayscale, upscale, contrast, global Otsu threshold."""
    img = ImageOps.exif_transpose(img).convert("L")
    if img.width < 1600:
        scale = 1600 / img.width
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    a = np.asarray(ImageOps.autocontrast(img, cutoff=1).filter(ImageFilter.SHARPEN)).astype(np.float32)
    return _to_img((a > _otsu(a)) * 255.0)


def enhance(img: Image.Image, variant: str, quality: dict | None = None) -> tuple[Image.Image, dict]:
    """Return an enhanced image for OCR plus a note of what was applied."""
    from scipy import ndimage as ndi
    if variant == "basic":
        return preprocess(img), {"steps": ["grayscale", "upscale", "auto-contrast", "Otsu threshold"]}
    q = quality or image_quality(img)
    a = flatten_lighting(_gray(img))                  # first, so shadows don't confuse deskew
    a, angle = deskew(a)
    steps = ["shadow removal"] + ([f"deskew {angle:+.1f}°"] if angle else [])
    width = int(min(max(a.shape[1], 2000), 2800))     # small photos up, huge phone photos down (speed)
    a = _resize_w(a, width)
    a = ndi.median_filter(a, 3)                       # remove sensor noise / JPEG speckle
    steps += [f"resize to {width}px wide", "median denoise"]
    if variant == "sharpen":
        a = unsharp(a, 2.0, 1.5)
        steps.append("unsharp mask")
    elif variant == "deblur":
        sigma = 3.5 if q["blur_score"] < 1.5 else 2.5
        a = unsharp(deblur_rl(a, sigma), 1.0, 0.5)
        steps.append(f"Richardson-Lucy deblur (sigma {sigma})")
    elif variant == "deblur_strong":
        a = unsharp(deblur_rl(a, 4.5, iters=20), 1.0, 0.5)
        steps.append("strong Richardson-Lucy deblur (sigma 4.5, 20 iterations)")
    elif variant == "adaptive":
        a = sauvola(unsharp(a, 2.5, 2.0))
        steps += ["strong unsharp", "Sauvola adaptive threshold"]
    else:
        raise ValueError(variant)
    return _to_img(a), {"steps": steps, "deskew_deg": angle}


# ----------------------------------------------------------------- 2. OCR
@dataclass
class OCRResult:
    text: str
    variant: str
    confidence: float
    quality: dict
    image: Image.Image
    steps: list[str]
    tried: list[dict]
    bill: "ParsedBill | None" = None      # lines merged from all pipelines (best version of each)


def _tesseract(img: Image.Image) -> tuple[str, float]:
    """Text (line by line) and mean word confidence (0-100) from one Tesseract pass."""
    try:
        import pytesseract
        d = pytesseract.image_to_data(img, config="--oem 1 --psm 6", output_type=pytesseract.Output.DICT)
    except Exception as exc:  # binary missing or not on PATH
        name = type(exc).__name__
        if "TesseractNotFound" in name or isinstance(exc, (ImportError, FileNotFoundError)):
            raise OCRUnavailable("Tesseract OCR is not installed. Mac: `brew install tesseract`; "
                                 "Ubuntu/Streamlit Cloud: add `tesseract-ocr` to packages.txt") from exc
        raise
    lines: dict[tuple, list[str]] = {}
    confs = []
    for i, word in enumerate(d["text"]):
        if not word.strip():
            continue
        key = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
        lines.setdefault(key, []).append(word)
        c = float(d["conf"][i])
        if c >= 0:
            confs.append(c)
    text = "\n".join(" ".join(w) for _, w in sorted(lines.items()))
    return text, (float(np.mean(confs)) if confs else 0.0)


VARIANTS = ("basic", "sharpen", "deblur", "deblur_strong", "adaptive")


def ocr_image(img: Image.Image, mode: str = "auto", catalogue: list[str] | None = None,
              suppliers: list[str] | None = None) -> OCRResult:
    """Run OCR, trying several enhancement pipelines in auto mode and keeping the best.

    Best = most item lines whose amount cross-checks, then most lines read, then
    Tesseract's own confidence. Pipelines run in parallel threads (Tesseract is a
    separate process, so this uses several CPU cores).
    """
    from concurrent.futures import ThreadPoolExecutor
    q = image_quality(img)
    if mode == "auto":
        order = (["basic", "sharpen"] if q["verdict"] == "good"
                 else ["sharpen", "deblur", "deblur_strong", "adaptive", "basic"])
    elif mode in VARIANTS:
        order = [mode]
    else:
        raise ValueError(mode)

    def attempt(v):
        eimg, info = enhance(img, v, q)
        text, conf = _tesseract(eimg)
        bill = parse_bill(text, catalogue or [], suppliers)
        checked = sum(l.amount_check for l in bill.lines)
        return {"variant": v, "text": text, "confidence": round(conf, 1), "lines": len(bill.lines),
                "checked": checked, "image": eimg, "steps": info["steps"], "bill": bill}

    with ThreadPoolExecutor(max_workers=min(5, len(order))) as pool:
        results = list(pool.map(attempt, order))
    best = max(results, key=lambda r: (r["checked"], r["lines"], r["confidence"]))
    tried = [{k: r[k] for k in ("variant", "lines", "checked", "confidence")} for r in results]
    merged = merge_bills([best["bill"]] + [r["bill"] for r in results if r is not best])
    return OCRResult(text=best["text"], variant=best["variant"], confidence=best["confidence"],
                     quality=q, image=best["image"], steps=best["steps"], tried=tried, bill=merged)


def merge_bills(bills: list["ParsedBill"]) -> "ParsedBill":
    """Line-level fusion: different filters read different rows correctly on a blurry
    photo, so keep the best reading of each medicine across all pipelines
    (amount cross-check passed > matched to catalogue > higher match score)."""
    out = ParsedBill(raw_text=bills[0].raw_text if bills else "")
    for b in bills:
        out.invoice_no = out.invoice_no or b.invoice_no
        out.bill_date = out.bill_date or b.bill_date
        out.supplier = out.supplier or b.supplier
    chosen: dict[str, ParsedLine] = {}
    order: list[str] = []
    for b in bills:
        for l in b.lines:
            key = l.medicine or f"?{l.product_text.lower()[:20]}"
            rank = (l.amount_check, l.medicine is not None, l.match_score)
            if key not in chosen:
                order.append(key)
                chosen[key] = l
            elif rank > (chosen[key].amount_check, chosen[key].medicine is not None, chosen[key].match_score):
                chosen[key] = l
    # drop unmatched fragments when a matched, cross-checked version of the bill exists
    out.lines = [chosen[k] for k in order if chosen[k].medicine or chosen[k].amount_check]
    return out


def run_ocr(img: Image.Image, mode: str = "auto", catalogue: list[str] | None = None) -> str:
    return ocr_image(img, mode, catalogue).text


# ----------------------------------------------------------------- 3-4. parse + match
def _num(tok: str) -> float | None:
    tok = tok.replace(",", ".").rstrip("%")
    try:
        return float(tok)
    except ValueError:
        return None


def _month_end(month: int, year: int) -> str:
    year = year + 2000 if year < 100 else year
    return date(year, month, calendar.monthrange(year, month)[1]).isoformat()


def match_product(text: str, catalogue: list[str]) -> tuple[str | None, float]:
    """Fuzzy match OCR text to a catalogue name. Returns (name, score 0-1)."""
    clean = re.sub(r"[^a-z0-9+ ]", " ", text.lower())
    clean = re.sub(r"\s+", " ", clean).strip()
    if not clean:
        return None, 0.0
    best, score = None, 0.0
    for name in catalogue:
        n = re.sub(r"[^a-z0-9+ ]", " ", name.lower())
        s = difflib.SequenceMatcher(None, clean, n).ratio()
        # bonus when the key words (brand/molecule + strength) all appear
        words = [w for w in clean.split() if len(w) > 2]
        if words and all(w in n for w in words):
            s = max(s, 0.9)
        if s > score:
            best, score = name, s
    return (best, round(score, 2)) if score >= 0.55 else (None, round(score, 2))


_OCR_DIGIT_FIX = str.maketrans({"O": "0", "o": "0", "D": "0", "Q": "0", "l": "1", "I": "1", "|": "1",
                                "i": "1", "S": "5", "s": "5", "B": "8", "Z": "2", "z": "2", "g": "9"})


def _clean_num_token(tok: str) -> tuple[float, bool] | None:
    """OCR token -> (value, had_decimal_point). Fixes O->0, l->1, S->5 ... in mostly-numeric tokens."""
    tok = tok.strip(",;:'\"`~()[]{}")
    if not tok:
        return None
    digits = sum(c.isdigit() for c in tok)
    if digits == 0 or digits / len(tok) < 0.5:
        return None
    fixed = re.sub(r"[^0-9.,%]", "", tok.translate(_OCR_DIGIT_FIX))
    if not fixed or not NUM_RE.match(fixed):
        return None
    val = _num(fixed)
    return (val, "." in fixed or "," in fixed) if val is not None else None


def _interpret_numbers(nums: list[tuple[float, bool]]) -> dict:
    """Assign qty / free / MRP / rate / GST / amount to the numbers after the expiry.

    Blurry photos often lose decimal points (131.50 -> 13150), so every price
    without a decimal point is also tried divided by 100. The interpretation whose
    qty x rate x (1 + GST%) best matches the printed line amount wins - the bill
    cross-checks itself.
    """
    vals = [v for v, _ in nums]
    decs = [d for _, d in nums]
    layouts = {6: [("qty", "free", "mrp", "rate", "gst", "amount")],
               5: [("qty", "mrp", "rate", "gst", "amount"), ("qty", "free", "mrp", "rate", "amount")],
               4: [("qty", "mrp", "rate", "amount")]}
    best, best_err = None, float("inf")
    for n in (6, 5, 4):
        if len(vals) < n:
            continue
        for layout in layouts[n]:
            for start in range(0, len(vals) - n + 1):
                seg, dseg = vals[start:start + n], decs[start:start + n]
                price_idx = [i for i, k in enumerate(layout) if k in ("mrp", "rate", "amount")
                             and not dseg[i] and seg[i] >= 100]
                for mask in range(1 << len(price_idx)):
                    f = dict(zip(layout, seg))
                    for b, i in enumerate(price_idx):
                        if mask >> b & 1:
                            f[layout[i]] = seg[i] / 100
                    f.setdefault("free", 0)
                    f.setdefault("gst", 5.0)
                    q, r, a, mrp_, g = f["qty"], f["rate"], f["amount"], f["mrp"], f["gst"]
                    if q <= 0 or not float(q).is_integer() or r <= 0 or a <= 0 or r > mrp_:
                        continue
                    if g not in GST_RATES or f["free"] >= q:
                        continue
                    err = min(abs(q * r * (1 + g / 100) - a), abs(q * r - a)) / a
                    err += 0.001 * (start + (6 - n))            # prefer complete, left-aligned reads
                    if err < best_err:
                        best, best_err = f, err
    if best is not None and best_err < 0.03:
        return {"qty": int(best["qty"]), "free": int(best["free"]), "mrp": round(best["mrp"], 2),
                "rate": round(best["rate"], 2), "gst": float(best["gst"]),
                "amount": round(best["amount"], 2), "ok": True}
    # fallback: simple rules (no reliable cross-check)
    ints = [v for v, d in nums if not d]
    decimals = [v for v, d in nums if d]
    qty = int(ints[0]) if ints else 0
    free = int(nums[1][0]) if len(nums) > 1 and not nums[0][1] and not nums[1][1] \
        and nums[1][0] < max(qty, 1) else 0
    gst = next((v for v in ints[1:] if v in GST_RATES and v != 0), 5.0)
    amount = decimals[-1] if len(decimals) >= 3 else None
    prices = decimals[:-1] if amount is not None else decimals
    return {"qty": qty, "free": free, "mrp": max(prices) if prices else 0.0,
            "rate": min(prices) if len(prices) > 1 else 0.0, "gst": float(gst),
            "amount": amount, "ok": False}


_BATCH_RE = re.compile(r"[A-Za-z0-9\-]{3,15}")
# expiry with the slash misread by OCR on blurry photos: 06/28 -> 06728, 0628, 06128, 06|28
_EXPIRY_BROKEN_RE = re.compile(r"^(0[1-9]|1[0-2])[/7lI|1\\]?(\d{2})[.,]?$")


def _is_batch(tok: str) -> bool:
    return bool(_BATCH_RE.fullmatch(tok) and re.search(r"\d", tok) and re.search(r"[A-Za-z]", tok))


def _find_expiry(raw: str):
    """Return (before_tokens, after_tokens, month, year) or None."""
    m = EXPIRY_RE.search(raw)
    if m and not DATE_RE.search(raw[max(0, m.start() - 3):m.end()]):    # a full date is not an expiry
        return raw[:m.start()].split(), raw[m.end():].split(), int(m.group(1)), int(m.group(2))
    toks = raw.split()
    for i in range(1, len(toks)):                 # fallback: broken expiry right after a batch number
        mb = _EXPIRY_BROKEN_RE.match(toks[i])
        if mb and _is_batch(toks[i - 1]):
            return toks[:i], toks[i + 1:], int(mb.group(1)), int(mb.group(2))
    return None


def parse_line(raw: str, catalogue: list[str]) -> ParsedLine | None:
    found = _find_expiry(raw)
    if not found:
        return None
    before, after, exp_month, exp_year = found
    nums = [n for n in (_clean_num_token(t) for t in after) if n is not None]
    nums_after = [v for v, _ in nums]
    # batch = last alphanumeric token before the expiry that contains a digit
    batch, cut = "", len(before)
    for i in range(len(before) - 1, -1, -1):
        tok = before[i]
        if _is_batch(tok):
            batch, cut = tok.upper(), i
            break
    name_tokens = before[:cut]
    if name_tokens and re.fullmatch(r"\d{1,3}[.)]?", name_tokens[0]):    # leading serial no.
        name_tokens = name_tokens[1:]
    product_text = " ".join(name_tokens)
    medicine, score = match_product(product_text, catalogue)

    fields = _interpret_numbers(nums)
    qty, free, mrp, rate, gst, amount, ok = (fields[k] for k in
                                             ("qty", "free", "mrp", "rate", "gst", "amount", "ok"))
    if qty <= 0 and medicine is None:
        return None
    return ParsedLine(raw=raw, product_text=product_text, medicine=medicine, match_score=score,
                      batch_no=batch, expiry=_month_end(exp_month, exp_year),
                      qty=qty, free_qty=free, mrp=mrp, rate=rate, gst_rate=float(gst), amount=amount,
                      amount_check=ok)


def parse_bill(text: str, catalogue: list[str], suppliers: list[str] | None = None) -> ParsedBill:
    bill = ParsedBill(raw_text=text)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for l in lines[:12]:
        l = l.replace("$", "S")                        # common OCR slip in invoice numbers
        if not bill.invoice_no and (m := INVOICE_RE.search(l)):
            bill.invoice_no = m.group(1).upper()
        if not bill.bill_date and (m := DATE_RE.search(l)) and not EXPIRY_RE.fullmatch(m.group(0)):
            d, mo, y = (int(g) for g in m.groups())
            y = y + 2000 if y < 100 else y
            try:
                bill.bill_date = date(y, mo, d).isoformat()
            except ValueError:
                pass
    if suppliers:
        head = " ".join(lines[:6]).lower()
        scored = [(difflib.SequenceMatcher(None, s.lower(), head).find_longest_match(
            0, len(s), 0, len(head)).size / len(s), s) for s in suppliers]
        score, name = max(scored)
        bill.supplier = name if score >= 0.6 else None
    for l in lines:
        if INVOICE_RE.search(l) or re.search(r"\bdate\b", l, re.IGNORECASE):
            continue                                   # header rows are not items
        pl = parse_line(l, catalogue)
        if pl:
            bill.lines.append(pl)
    return bill


# ----------------------------------------------------------------- demo invoice
def _font(size: int, bold: bool = False):
    for path in (f"/usr/share/fonts/truetype/dejavu/DejaVuSans{'-Bold' if bold else ''}.ttf",
                 "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else
                 "/System/Library/Fonts/Supplemental/Arial.ttf",
                 "arial.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def sample_invoice(items: list[dict], supplier: str, invoice_no: str, bill_date: date) -> Image.Image:
    """Render a clean distributor-style invoice (for demos and tests).

    items: [{name, batch, expiry: 'MM/YY', qty, free, mrp, rate, gst}]
    """
    W, H = 1700, 520 + 60 * len(items)
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    f, fb, fh = _font(26), _font(26, True), _font(40, True)
    d.text((60, 40), supplier, font=fh, fill="black")
    d.text((60, 100), "Wholesale Pharmaceutical Distributor - Jaipur - GSTIN 08AABCD1234Q1Z1", font=f, fill="black")
    d.text((60, 160), f"Invoice No: {invoice_no}", font=fb, fill="black")
    d.text((1100, 160), f"Date: {bill_date:%d/%m/%Y}", font=fb, fill="black")
    cols = [("Sr", 60), ("Product", 120), ("Batch", 700), ("Exp", 880), ("Qty", 1000), ("Free", 1080),
            ("MRP", 1170), ("Rate", 1300), ("GST%", 1430), ("Amount", 1530)]
    y = 240
    d.line((50, y - 10, W - 50, y - 10), fill="black", width=2)
    for name, x in cols:
        d.text((x, y), name, font=fb, fill="black")
    y += 50
    d.line((50, y - 8, W - 50, y - 8), fill="black", width=2)
    total = 0.0
    for i, it in enumerate(items, 1):
        amount = it["qty"] * it["rate"] * (1 + it["gst"] / 100)
        total += amount
        vals = [str(i), it["name"], it["batch"], it["expiry"], str(it["qty"]), str(it["free"]),
                f"{it['mrp']:.2f}", f"{it['rate']:.2f}", f"{it['gst']:g}", f"{amount:.2f}"]
        for (_, x), v in zip(cols, vals):
            d.text((x, y), v, font=f, fill="black")
        y += 60
    d.line((50, y, W - 50, y), fill="black", width=2)
    d.text((1150, y + 30), f"Grand Total: {total:.2f}", font=fb, fill="black")
    return img


def simulate_phone_photo(img: Image.Image, blur: float = 1.8, seed: int = 0) -> Image.Image:
    """Degrade a clean bill like a hand-held phone photo (for demos/tests):
    slight tilt, defocus blur, uneven light, sensor noise and JPEG compression."""
    import io
    rng = np.random.default_rng(seed)
    im = img.convert("L").rotate(float(rng.uniform(-2.5, 2.5)), expand=True, fillcolor=255,
                                 resample=Image.BICUBIC)
    im = im.filter(ImageFilter.GaussianBlur(blur))
    a = np.asarray(im).astype(np.float32)
    h, w = a.shape
    a = a * (0.6 + 0.4 * np.linspace(0, 1, w))[None, :] * 0.9 + 15      # shadow from one side
    a = a + rng.normal(0, 6, a.shape)
    buf = io.BytesIO()
    Image.fromarray(np.clip(a, 0, 255).astype(np.uint8)).save(buf, "JPEG", quality=60)
    return Image.open(io.BytesIO(buf.getvalue()))
