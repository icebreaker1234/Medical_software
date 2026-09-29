"""Purchase-bill OCR: photo of a distributor invoice -> draft purchase lines.

Pipeline
  1. preprocess   - grayscale, upscale small photos, auto-contrast, Otsu threshold
  2. OCR          - Tesseract (free, runs locally / on the server)
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


@dataclass
class ParsedBill:
    invoice_no: str | None = None
    bill_date: str | None = None
    supplier: str | None = None
    lines: list[ParsedLine] = field(default_factory=list)
    raw_text: str = ""


# ----------------------------------------------------------------- 1. preprocess
def preprocess(img: Image.Image) -> Image.Image:
    img = ImageOps.exif_transpose(img).convert("L")          # phone photos: fix rotation, grayscale
    if img.width < 1600:                                       # Tesseract likes ~300 dpi text
        scale = 1600 / img.width
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    img = ImageOps.autocontrast(img, cutoff=1).filter(ImageFilter.SHARPEN)
    arr = np.asarray(img)
    hist = np.bincount(arr.ravel(), minlength=256).astype(float)   # Otsu threshold
    total, sum_all = arr.size, np.dot(np.arange(256), hist)
    w_b = sum_b = 0.0
    best, thresh = 0.0, 128
    for t in range(256):
        w_b += hist[t]
        if w_b == 0 or w_b == total:
            continue
        sum_b += t * hist[t]
        m_b, m_f = sum_b / w_b, (sum_all - sum_b) / (total - w_b)
        between = w_b * (total - w_b) * (m_b - m_f) ** 2
        if between > best:
            best, thresh = between, t
    return Image.fromarray(((arr > thresh) * 255).astype(np.uint8))


# ----------------------------------------------------------------- 2. OCR
def run_ocr(img: Image.Image) -> str:
    try:
        import pytesseract
        return pytesseract.image_to_string(preprocess(img), config="--oem 3 --psm 6")
    except Exception as exc:  # binary missing or not on PATH
        name = type(exc).__name__
        if "TesseractNotFound" in name or isinstance(exc, (ImportError, FileNotFoundError)):
            raise OCRUnavailable("Tesseract OCR is not installed. Mac: `brew install tesseract`; "
                                 "Ubuntu/Streamlit Cloud: add `tesseract-ocr` to packages.txt") from exc
        raise


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


def parse_line(raw: str, catalogue: list[str]) -> ParsedLine | None:
    m = EXPIRY_RE.search(raw)
    if not m or DATE_RE.search(raw[max(0, m.start() - 3):m.end()]):   # a full date, not an expiry
        return None
    before, after = raw[:m.start()].split(), raw[m.end():].split()
    # common OCR confusions inside numbers: O->0, l/I->1
    after = [re.sub(r"[Oo]", "0", t) if re.fullmatch(r"[0-9Oo.,%]+", t) else t for t in after]
    after = [re.sub(r"[lI]", "1", t) if re.fullmatch(r"[0-9lI.,%]+", t) else t for t in after]
    nums = [(_num(t), ("." in t or "," in t)) for t in after if NUM_RE.match(t)]
    nums = [(v, dec) for v, dec in nums if v is not None]
    nums_after = [v for v, _ in nums]
    # batch = last alphanumeric token before the expiry that contains a digit
    batch, cut = "", len(before)
    for i in range(len(before) - 1, -1, -1):
        tok = before[i]
        if re.fullmatch(r"[A-Za-z0-9\-]{3,15}", tok) and re.search(r"\d", tok) and re.search(r"[A-Za-z]", tok):
            batch, cut = tok.upper(), i
            break
    name_tokens = before[:cut]
    if name_tokens and re.fullmatch(r"\d{1,3}[.)]?", name_tokens[0]):    # leading serial no.
        name_tokens = name_tokens[1:]
    product_text = " ".join(name_tokens)
    medicine, score = match_product(product_text, catalogue)

    ints = [v for v, dec in nums if not dec]
    decimals = [v for v, dec in nums if dec]
    qty = int(ints[0]) if ints else 0
    free = 0
    if len(nums) > 1 and not nums[0][1] and not nums[1][1] and nums[1][0] < max(qty, 1):
        free = int(nums[1][0])
    gst = next((v for v in ints[1:] if v in GST_RATES and v != 0), 5.0)
    amount = decimals[-1] if len(decimals) >= 3 else None      # MRP, rate, amount
    prices = decimals[:-1] if amount is not None else decimals
    mrp = max(prices) if prices else 0.0
    rate = min(prices) if len(prices) > 1 else 0.0
    if qty <= 0 and medicine is None:
        return None
    return ParsedLine(raw=raw, product_text=product_text, medicine=medicine, match_score=score,
                      batch_no=batch, expiry=_month_end(int(m.group(1)), int(m.group(2))),
                      qty=qty, free_qty=free, mrp=mrp, rate=rate, gst_rate=float(gst), amount=amount)


def parse_bill(text: str, catalogue: list[str], suppliers: list[str] | None = None) -> ParsedBill:
    bill = ParsedBill(raw_text=text)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for l in lines[:12]:
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
