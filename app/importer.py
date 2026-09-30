"""Import connector: load a store's data from its old software (Marg, Tally, GoFrugal, Excel...).

Flow:  read_file -> find_header -> detect_dataset -> auto_map -> validate (preview) -> apply (save)

Nothing is written until apply(). apply() runs in ONE transaction: either everything in the
file is saved or nothing is. Re-importing the same file is blocked (file hash), masters are
matched by name so they are not duplicated, and batches / bills that already exist are skipped.

Column names are matched against synonyms seen in common Indian pharmacy software exports.
Real exports vary by version and report settings - the owner can fix the mapping on screen.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from app import db
from app.composition import describe, infer_from_product, parse_composition, salts_key

# ---------------------------------------------------------------- what we can import
# field -> (label, required, synonyms)
F = lambda label, req, *syn: (label, req, syn)  # noqa: E731

DATASETS: dict[str, dict] = {
    "stock": {
        "label": "Items & current stock (stock report with batch / expiry)",
        "hint": "Marg: Stock Statement / Batch-wise stock · Tally: Stock Summary (batch-wise) · any item list",
        "fields": {
            "item": F("Item / medicine name", True, "item name", "item", "product", "product name", "itemname",
                      "medicine", "medicine name", "description", "particulars", "name of item", "item description"),
            "batch": F("Batch no.", False, "batch", "batch no", "batchno", "batch number", "batch no.", "b.no", "bno"),
            "expiry": F("Expiry", False, "expiry", "exp", "exp date", "expiry date", "exp.dt", "expdt", "exp dt",
                        "expiry dt", "exp."),
            "qty": F("Quantity in stock", False, "qty", "quantity", "stock", "closing stock", "cl stock", "cl. stock",
                     "cl.stock", "closing qty", "balance qty", "bal qty", "stock qty", "current stock", "qty in stock"),
            "mrp": F("MRP", False, "mrp", "m.r.p", "m.r.p.", "mrp rs", "max retail price"),
            "rate": F("Purchase rate (cost)", False, "purchase rate", "p.rate", "prate", "pur rate", "pur. rate",
                      "cost", "cost rate", "purchase price", "landing cost", "net rate", "rate"),
            "composition": F("Composition / salt", False, "salt", "composition", "generic", "generic name", "content",
                             "molecule", "salt name", "contents"),
            "company": F("Company / manufacturer", False, "company", "mfg", "manufacturer", "mfr", "mkt by",
                         "company name", "mfg by", "marketed by"),
            "pack": F("Pack", False, "pack", "packing", "pack size", "unit", "uom"),
            "hsn": F("HSN", False, "hsn", "hsn code", "hsn/sac", "hsn sac"),
            "gst": F("GST %", False, "gst", "gst%", "gst %", "gst rate", "tax", "tax%", "tax %", "igst", "igst%"),
            "schedule": F("Schedule (H / H1 / X)", False, "schedule", "sch", "sch.", "drug schedule", "schedule type"),
            "rack": F("Rack / location", False, "rack", "location", "shelf", "rack no", "bin"),
            "barcode": F("Barcode", False, "barcode", "bar code", "ean", "ean code"),
        },
    },
    "suppliers": {
        "label": "Suppliers / distributors (party list)",
        "hint": "Marg: Party / Supplier master · Tally: Sundry Creditors list",
        "fields": {
            "name": F("Supplier name", True, "supplier", "supplier name", "party", "party name", "name", "ledger",
                      "ledger name", "distributor", "account name"),
            "gstin": F("GSTIN", False, "gstin", "gst no", "gstin no", "gst number", "gstin/uin", "gst in"),
            "phone": F("Phone", False, "phone", "mobile", "mobile no", "phone no", "contact", "contact no", "mob"),
            "city": F("City", False, "city", "town", "station", "place", "area"),
            "credit_days": F("Credit days", False, "credit days", "cr days", "credit period", "due days", "days"),
            "balance": F("Amount we owe (opening balance)", False, "balance", "closing balance", "outstanding",
                         "cl balance", "cl. balance", "opening balance", "due", "payable", "amount due"),
        },
    },
    "customers": {
        "label": "Customers / patients",
        "hint": "Marg: Customer / Party list · any contact list",
        "fields": {
            "name": F("Customer name", True, "customer", "customer name", "patient", "patient name", "name",
                      "party", "party name"),
            "phone": F("Mobile", False, "mobile", "phone", "mobile no", "phone no", "contact", "contact no", "mob"),
            "address": F("Address", False, "address", "addr", "area", "city"),
            "doctor": F("Doctor", False, "doctor", "dr", "doctor name", "dr name", "prescriber"),
            "balance": F("Udhaar / amount due", False, "balance", "outstanding", "due", "closing balance",
                         "receivable", "amount due", "udhaar"),
        },
    },
    "purchases": {
        "label": "Purchase history (item-wise purchase register)",
        "hint": "Marg: Purchase Register (item-wise) · Tally: Purchase Register with item details",
        "fields": {
            "date": F("Bill date", True, "date", "bill date", "invoice date", "inv date", "pur date", "vch date"),
            "invoice": F("Bill / invoice no.", True, "bill no", "invoice no", "inv no", "bill number", "voucher no",
                         "vch no", "invoice", "bill"),
            "supplier": F("Supplier", True, "supplier", "supplier name", "party", "party name", "distributor"),
            "item": F("Item", True, "item name", "item", "product", "product name", "medicine", "particulars",
                      "description"),
            "qty": F("Quantity", True, "qty", "quantity", "pur qty", "billed qty"),
            "free": F("Free qty", False, "free", "free qty", "sch qty", "scheme", "bonus"),
            "rate": F("Rate", False, "rate", "purchase rate", "p.rate", "pur rate", "cost"),
            "amount": F("Amount", False, "amount", "net amount", "value", "total", "net amt", "amt"),
            "gst": F("GST %", False, "gst", "gst%", "gst %", "tax%", "tax %", "igst%"),
            "batch": F("Batch", False, "batch", "batch no", "batchno"),
            "expiry": F("Expiry", False, "expiry", "exp", "exp date", "expiry date"),
            "mrp": F("MRP", False, "mrp", "m.r.p"),
        },
    },
    "sales": {
        "label": "Sales history (item-wise sales register)",
        "hint": "Marg: Sale Register / Bill-wise item details · Tally: Sales Register with items",
        "fields": {
            "date": F("Bill date", True, "date", "bill date", "invoice date", "inv date", "sale date", "vch date"),
            "invoice": F("Bill no.", True, "bill no", "invoice no", "inv no", "bill number", "voucher no", "vch no",
                         "bill", "invoice"),
            "item": F("Item", True, "item name", "item", "product", "product name", "medicine", "particulars",
                      "description"),
            "qty": F("Quantity", True, "qty", "quantity", "sold qty", "sale qty"),
            "rate": F("Rate / MRP", False, "rate", "mrp", "sale rate", "price", "s.rate"),
            "amount": F("Amount", False, "amount", "net amount", "value", "total", "net amt", "amt"),
            "discount": F("Discount %", False, "disc", "disc%", "discount", "discount %", "dis%"),
            "customer": F("Customer name", False, "customer", "customer name", "patient", "patient name", "party",
                          "party name", "name"),
            "phone": F("Customer mobile", False, "mobile", "phone", "mobile no", "contact"),
            "mode": F("Payment mode", False, "mode", "payment mode", "pay mode", "payment", "cash/credit", "type"),
            "doctor": F("Doctor", False, "doctor", "dr", "doctor name", "prescriber"),
        },
    },
}
ORDER = ["suppliers", "stock", "customers", "purchases", "sales"]   # recommended import order


def norm(s) -> str:
    return re.sub(r"[^a-z0-9%]+", "", str(s).lower())


def _syn(dataset: str, fld: str) -> set[str]:
    label, _, syn = DATASETS[dataset]["fields"][fld]
    return {norm(x) for x in (*syn, label)}


# ---------------------------------------------------------------- reading files
def read_file(data: bytes, filename: str) -> dict[str, pd.DataFrame]:
    """All sheets as raw grids (no header yet). CSV -> one sheet."""
    name = filename.lower()
    if name.endswith((".csv", ".txt")):
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        sep = "\t" if text.count("\t") > text.count(",") else ","
        return {"data": pd.read_csv(io.StringIO(text), header=None, sep=sep, dtype=str,
                                    keep_default_na=False, on_bad_lines="skip", engine="python")}
    if name.endswith((".xlsx", ".xlsm")):
        return pd.read_excel(io.BytesIO(data), sheet_name=None, header=None, engine="openpyxl")
    if name.endswith(".xls"):
        return pd.read_excel(io.BytesIO(data), sheet_name=None, header=None, engine="xlrd")
    raise ValueError("Please upload a CSV or Excel file (.csv, .xlsx, .xls)")


def find_header(raw: pd.DataFrame, max_scan: int = 40) -> int:
    """Exports often start with the store name, report title and date range - skip those."""
    every = set().union(*(_syn(d, f) for d in DATASETS for f in DATASETS[d]["fields"]))
    best, best_row = -1, 0
    for i in range(min(max_scan, len(raw))):
        cells = [norm(v) for v in raw.iloc[i].tolist() if str(v).strip() not in ("", "nan", "None")]
        score = sum(1 for c in cells if c in every)
        if score > best:
            best, best_row = score, i
    return best_row


def frame(raw: pd.DataFrame, header_row: int) -> pd.DataFrame:
    df = raw.iloc[header_row + 1:].copy()
    cols, seen = [], {}
    for v in raw.iloc[header_row].tolist():
        c = str(v).strip() if str(v).strip() not in ("", "nan", "None") else "column"
        seen[c] = seen.get(c, 0) + 1
        cols.append(c if seen[c] == 1 else f"{c} ({seen[c]})")
    df.columns = cols
    df = df.replace({"": np.nan, "nan": np.nan, "None": np.nan}).dropna(how="all")
    total_re = re.compile(r"^\s*(?:grand\s*|sub\s*|net\s*)?total\b", re.I)
    is_total = df.apply(lambda r: any(isinstance(v, str) and total_re.match(v) for v in r.tolist()[:4]), axis=1)
    df = df[~is_total] if len(df) else df
    return df.reset_index(drop=True)


def detect_dataset(columns) -> tuple[str, dict[str, float]]:
    cols = {norm(c) for c in columns}
    scores = {}
    for d, spec in DATASETS.items():
        hit = sum(1 for f in spec["fields"] if _syn(d, f) & cols)
        req = [f for f, (_, r, _) in spec["fields"].items() if r]
        req_hit = sum(1 for f in req if _syn(d, f) & cols)
        scores[d] = hit / len(spec["fields"]) + (1.0 if req_hit == len(req) else 0) + 0.2 * req_hit
    return max(scores, key=scores.get), scores


def auto_map(columns, dataset: str) -> dict[str, str | None]:
    """Best column for each field (exact synonym match first, then 'contains')."""
    out, used = {}, set()
    cols = list(columns)
    for f in DATASETS[dataset]["fields"]:
        syn = _syn(dataset, f)
        pick = next((c for c in cols if norm(c) in syn and c not in used), None)
        if pick is None:
            pick = next((c for c in cols if c not in used and any(s and len(s) > 3 and s in norm(c) for s in syn)),
                        None)
        out[f] = pick
        if pick:
            used.add(pick)
    return out


# ---------------------------------------------------------------- value parsing
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                      "nov", "dec"], 1)}


def _blank(v) -> bool:
    return v is None or (isinstance(v, float) and np.isnan(v)) or str(v).strip() in ("", "nan", "None", "-")


def text(v) -> str | None:
    if _blank(v):
        return None
    s = str(v).strip()
    return s[:-2] if re.fullmatch(r"\d+\.0", s) else s          # Excel turns 12345 into 12345.0


def num(v) -> float | None:
    if _blank(v):
        return None
    if isinstance(v, (int, float, np.number)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("₹", "").replace("Rs.", "").replace("Rs", "").replace("%", "")
    s = re.sub(r"\s*(dr|cr)\.?$", "", s, flags=re.I).strip()
    neg = s.startswith("(") and s.endswith(")")
    try:
        x = float(s.strip("()"))
    except ValueError:
        return None
    return -x if neg else x


def balance(v, owed_suffix: str) -> float | None:
    """'12,345.00 Cr' -> +12345 if Cr is the side that means 'owed'; the other side -> negative."""
    x = num(v)
    if x is None:
        return None
    m = re.search(r"(dr|cr)\.?\s*$", str(v).strip(), flags=re.I)
    if m and m.group(1).lower() != owed_suffix:
        return -abs(x)
    return x


def _month_end(y: int, m: int) -> date:
    if y < 100:
        y += 2000
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return nxt - timedelta(days=1)


def _excel_serial(x: float) -> date:
    return (datetime(1899, 12, 30) + timedelta(days=float(x))).date()


def expiry(v) -> str | None:
    """12/27, 12/2027, Dec-27, DEC 2027, 2027-12-31, 31/12/2027, Excel dates -> ISO date."""
    if _blank(v):
        return None
    if isinstance(v, (datetime, pd.Timestamp)):
        return str(_month_end(v.year, v.month)) if v.day == 1 else str(v.date())
    if isinstance(v, date):
        return str(v)
    if isinstance(v, (int, float, np.number)) and 20000 < float(v) < 80000:
        d = _excel_serial(v)
        return str(d)
    s = str(v).strip().lower()
    m = re.fullmatch(r"(\d{1,2})\s*[/\-.]\s*(\d{2}|\d{4})", s)                       # 12/27 or 12/2027
    if m and 1 <= int(m.group(1)) <= 12:
        return str(_month_end(int(m.group(2)), int(m.group(1))))
    m = re.fullmatch(r"([a-z]{3})[a-z]*\s*[/\-.' ]?\s*(\d{2}|\d{4})", s)                # dec-27, december 2027
    if m and m.group(1) in MONTHS:
        return str(_month_end(int(m.group(2)), MONTHS[m.group(1)]))
    m = re.fullmatch(r"(\d{4})[/\-.](\d{1,2})(?:[/\-.](\d{1,2}))?(?:\s.*)?", s)          # 2027-12[-31]
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        return str(date(y, mo, int(m.group(3)))) if m.group(3) else str(_month_end(y, mo))
    m = re.fullmatch(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2}|\d{4})", s)                 # 31/12/2027
    if m:
        y = int(m.group(3)) + (2000 if len(m.group(3)) == 2 else 0)
        return str(date(y, int(m.group(2)), int(m.group(1))))
    m = re.fullmatch(r"(\d{2})(\d{2}|\d{4})", s)                                           # 1227 / 122027
    if m and 1 <= int(m.group(1)) <= 12:
        return str(_month_end(int(m.group(2)), int(m.group(1))))
    return None


def bill_date(v) -> str | None:
    if _blank(v):
        return None
    if isinstance(v, (datetime, pd.Timestamp)):
        return str(v.date())
    if isinstance(v, date):
        return str(v)
    if isinstance(v, (int, float, np.number)) and 20000 < float(v) < 80000:
        return str(_excel_serial(v))
    try:
        return str(pd.to_datetime(str(v).strip(), dayfirst=True).date())          # Indian dd/mm/yyyy
    except (ValueError, TypeError):
        return None


def phone(v) -> str | None:
    d = re.sub(r"\D", "", text(v) or "")
    if len(d) == 12 and d.startswith("91"):
        d = d[2:]
    if len(d) == 11 and d.startswith("0"):
        d = d[1:]
    return d if re.fullmatch(r"[6-9]\d{9}", d) else None


GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")


def gst_rate(v) -> float | None:
    x = num(v)
    if x is None:
        return None
    if 0 < x < 1:
        x *= 100
    return x if x in (0, 0.25, 3, 5, 12, 18, 28) else None


def schedule(v) -> str | None:
    s = norm(text(v) or "")
    s = s.replace("schedule", "").replace("sch", "")
    return {"h1": "H1", "h": "H", "x": "X", "otc": "OTC", "g": "OTC", "nrx": "H", "rx": "H", "": None}.get(s)


def pay_mode(v) -> str:
    s = norm(text(v) or "")
    if s.startswith(("cr", "udh", "due", "ac")):
        return "Credit"
    if any(k in s for k in ("upi", "gpay", "phonepe", "paytm", "online", "bhim")):
        return "UPI"
    if "card" in s or s in ("cc", "dc"):
        return "Card"
    return "Cash"


def pack_units(p: str | None) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)", p or "")
    return float(m.group(1)) if m else 1.0


# ---------------------------------------------------------------- validation (preview)
@dataclass
class Checked:
    dataset: str
    rows: list[dict]                               # clean rows ready to save
    problems: pd.DataFrame                         # row, level (error / warning), message
    counts: dict = field(default_factory=dict)

    @property
    def errors(self) -> int:
        return int((self.problems["level"] == "error").sum()) if len(self.problems) else 0

    @property
    def warnings(self) -> int:
        return int((self.problems["level"] == "warning").sum()) if len(self.problems) else 0


def validate(df: pd.DataFrame, dataset: str, mapping: dict, options: dict | None = None) -> Checked:
    options = options or {}
    spec = DATASETS[dataset]["fields"]
    missing = [spec[f][0] for f, (_, req, _) in spec.items() if req and not mapping.get(f)]
    if missing:
        raise ValueError("Choose a column for: " + ", ".join(missing))
    get = {f: (lambda r, c=c: r.get(c)) if c else (lambda r: None) for f, c in mapping.items()}
    rows, probs = [], []
    today = db.today()

    def bad(i, level, msg):
        probs.append({"row": i, "level": level, "message": msg})

    for i, r in enumerate(df.to_dict("records"), start=1):
        g = {f: get[f](r) for f in spec}
        if dataset == "stock":
            item = text(g["item"])
            if not item:
                bad(i, "error", "Item name is empty")
                continue
            qty = num(g["qty"]) or 0
            if qty < 0:
                bad(i, "error", f"{item}: negative stock ({qty:g})")
                continue
            exp = expiry(g["expiry"])
            mrp, rate, gst = num(g["mrp"]), num(g["rate"]), gst_rate(g["gst"])
            sch = schedule(g["schedule"]) or options.get("default_schedule", "H")
            if g["gst"] is not None and not _blank(g["gst"]) and gst is None:
                bad(i, "warning", f"{item}: GST '{g['gst']}' not a valid GST rate - using {options.get('default_gst', 5)}%")
            gst = gst if gst is not None else options.get("default_gst", 5)
            if qty > 0 and not exp:
                bad(i, "error", f"{item}: stock {qty:g} but no valid expiry ('{g['expiry']}') - batches need an expiry")
                continue
            if qty > 0 and not mrp:
                bad(i, "error", f"{item}: stock {qty:g} but no MRP")
                continue
            if exp and exp <= str(today) and qty > 0:
                bad(i, "warning", f"{item} batch {text(g['batch']) or '-'}: already expired ({exp}) - imported so "
                                  "it shows in Expiry for return / disposal")
            if qty > 0 and not rate:
                rate = round(mrp / (1 + gst / 100) * 0.75, 2)
                bad(i, "warning", f"{item}: no purchase rate - estimated at {rate} (75% of MRP before GST)")
            if schedule(g["schedule"]) is None:
                pass                                         # counted below
            rows.append({"item": item, "batch": text(g["batch"]) or "OPENING", "expiry": exp, "qty": int(round(qty)),
                         "mrp": mrp, "rate": rate, "composition": text(g["composition"]),
                         "company": text(g["company"]), "pack": text(g["pack"]), "hsn": text(g["hsn"]),
                         "gst": gst, "schedule": sch, "schedule_given": schedule(g["schedule"]) is not None,
                         "rack": text(g["rack"]), "barcode": text(g["barcode"])})
        elif dataset == "suppliers":
            name = text(g["name"])
            if not name:
                bad(i, "error", "Supplier name is empty")
                continue
            gstin = (text(g["gstin"]) or "").upper().replace(" ", "") or None
            if gstin and not GSTIN_RE.match(gstin):
                bad(i, "warning", f"{name}: GSTIN '{gstin}' looks invalid - saved anyway, please check")
            ph = phone(g["phone"])
            if g["phone"] and not _blank(g["phone"]) and not ph:
                bad(i, "warning", f"{name}: phone '{g['phone']}' is not a valid 10-digit mobile - left blank")
            bal = balance(g["balance"], "cr")          # supplier's Cr balance = we owe them
            if bal is not None and bal < 0:
                bad(i, "warning", f"{name}: balance is Dr (supplier owes us {abs(bal):,.2f}) - not imported, "
                                  "adjust manually")
                bal = None
            cd = num(g["credit_days"])
            rows.append({"name": name, "gstin": gstin, "phone": ph, "city": text(g["city"]),
                         "credit_days": int(cd) if cd is not None and 0 <= cd <= 365 else None,
                         "balance": bal if bal and bal > 0 else None})
        elif dataset == "customers":
            name = text(g["name"])
            if not name:
                bad(i, "error", "Customer name is empty")
                continue
            ph = phone(g["phone"])
            if g["phone"] and not _blank(g["phone"]) and not ph:
                bad(i, "warning", f"{name}: mobile '{g['phone']}' not valid - left blank")
            bal = balance(g["balance"], "dr")          # customer's Dr balance = they owe us
            if bal is not None and bal < 0:
                bad(i, "warning", f"{name}: balance is Cr (we owe the customer {abs(bal):,.2f}) - not imported")
                bal = None
            rows.append({"name": name, "phone": ph, "address": text(g["address"]), "doctor": text(g["doctor"]),
                         "balance": bal if bal and bal > 0 else None})
        else:                                           # purchases / sales history
            d, inv, item, qty = bill_date(g["date"]), text(g["invoice"]), text(g["item"]), num(g["qty"])
            if not (d and inv and item) or not qty:
                bad(i, "error", f"Missing/invalid date, bill no., item or quantity "
                                f"({g['date']} / {g['invoice']} / {g['item']} / {g['qty']})")
                continue
            if d > str(today):
                bad(i, "error", f"Bill {inv}: date {d} is in the future")
                continue
            if qty < 0:
                bad(i, "warning", f"Bill {inv} {item}: negative qty (return) - skipped")
                continue
            rate, amount = num(g["rate"]), num(g["amount"])
            if not rate and not amount:
                bad(i, "error", f"Bill {inv} {item}: needs a rate or an amount")
                continue
            row = {"date": d, "invoice": inv, "item": item, "qty": qty, "rate": rate, "amount": amount}
            if dataset == "purchases":
                sup = text(g["supplier"])
                if not sup:
                    bad(i, "error", f"Bill {inv}: supplier is empty")
                    continue
                row.update(supplier=sup, free=num(g["free"]) or 0, gst=gst_rate(g["gst"]),
                           batch=text(g["batch"]), expiry=expiry(g["expiry"]), mrp=num(g["mrp"]))
            else:
                row.update(discount=num(g["discount"]) or 0, customer=text(g["customer"]), phone=phone(g["phone"]),
                           mode=pay_mode(g["mode"]), doctor=text(g["doctor"]))
            rows.append(row)

    if dataset == "stock":
        no_sch = sum(1 for r in rows if not r["schedule_given"])
        if no_sch:
            probs.append({"row": None, "level": "warning",
                          "message": f"{no_sch} items have no schedule column/value - set to "
                                     f"'{options.get('default_schedule', 'H')}'. Review H1 / X drugs in Inventory."})
    counts = {"rows_in_file": len(df), "ready": len(rows)}
    if dataset in ("purchases", "sales") and rows:
        counts["bills"] = len({(r["invoice"], r["date"]) for r in rows})
    if dataset == "stock" and rows:
        counts["items"] = len({r["item"].lower() for r in rows})
        counts["batches_with_stock"] = sum(1 for r in rows if r["qty"] > 0)
    return Checked(dataset, rows, pd.DataFrame(probs, columns=["row", "level", "message"]), counts)


# ---------------------------------------------------------------- saving
def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def already_imported(h: str) -> pd.DataFrame:
    return db.q("SELECT id, ts, file_name, dataset, created FROM imports WHERE file_hash=?", (h,))


def history() -> pd.DataFrame:
    return db.q("SELECT id, ts, file_name, dataset, rows_in, created, updated, skipped, user FROM imports "
                "ORDER BY id DESC")


class _Ctx:
    """Lookups kept in memory during one import (case-insensitive names)."""

    def __init__(self, conn):
        self.conn = conn
        self.products = {r["name"].lower(): dict(r) for r in conn.execute(
            "SELECT id, name, gst_rate, default_mrp, pack, schedule FROM products")}
        self.suppliers = {r["name"].lower(): r["id"] for r in conn.execute("SELECT id, name FROM suppliers")}
        self.cust_phone = {r["phone"]: r["id"] for r in conn.execute(
            "SELECT id, phone FROM customers WHERE phone IS NOT NULL")}
        self.cust_name = {r["name"].lower(): r["id"] for r in conn.execute("SELECT id, name FROM customers")}
        self.barcodes = {r[0] for r in conn.execute("SELECT barcode FROM products WHERE barcode IS NOT NULL")}
        self.preexisting = set(self.products)            # product names that were there before this import
        self.touched: set[str] = set()
        self.hist_batch: dict[int, int] = {}
        self.stats = {"created": 0, "updated": 0, "skipped": 0}
        self.notes: list[str] = []

    def product(self, name: str, **kw) -> int:
        key = name.lower()
        if key in self.products:
            return self.products[key]["id"]
        comp_text = kw.get("composition")
        d = None
        if comp_text and parse_composition(comp_text) and all(i.strength for i in parse_composition(comp_text)):
            from app.composition import detect_form, detect_release
            d = describe(comp_text, detect_form(name) or "tablet", detect_release(name))
        else:
            d = infer_from_product(name, comp_text)
        generic = (salts_key(parse_composition(comp_text)).title() if comp_text else None) or comp_text
        barcode = kw.get("barcode")
        if barcode and barcode in self.barcodes:
            self.notes.append(f"Barcode {barcode} already used - not set for {name}")
            barcode = None
        gst = kw.get("gst") if kw.get("gst") is not None else 5
        pid = self.conn.execute(
            "INSERT INTO products(name, generic, category, manufacturer, hsn, gst_rate, schedule, pack, barcode, rack,"
            " default_mrp, composition, dosage_form, release_type, comp_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, generic, kw.get("category") or "Imported", kw.get("company"), kw.get("hsn") or "3004", gst,
             kw.get("schedule") or "H", kw.get("pack"), barcode, kw.get("rack"), kw.get("mrp"),
             d["composition"] if d else comp_text, d["dosage_form"] if d else None,
             d["release_type"] if d else "IR", d["comp_key"] if d else None)).lastrowid
        if barcode:
            self.barcodes.add(barcode)
        self.products[key] = {"id": pid, "name": name, "gst_rate": gst, "default_mrp": kw.get("mrp"),
                              "pack": kw.get("pack"), "schedule": kw.get("schedule") or "H"}
        self.stats["created"] += 1
        return pid

    def supplier(self, name: str, **kw) -> int:
        key = name.lower()
        if key in self.suppliers:
            return self.suppliers[key]
        sid = self.conn.execute("INSERT INTO suppliers(name, gstin, phone, city, lead_time_days, credit_days) "
                                "VALUES (?,?,?,?,?,?)", (name, kw.get("gstin"), kw.get("phone"), kw.get("city"),
                                                         2, kw.get("credit_days") or 30)).lastrowid
        self.suppliers[key] = sid
        self.stats["created"] += 1
        return sid

    def customer(self, name: str | None, ph: str | None, **kw) -> int | None:
        if ph and ph in self.cust_phone:
            return self.cust_phone[ph]
        if not ph and name and name.lower() in self.cust_name:
            return self.cust_name[name.lower()]
        if not name:
            return None
        cid = self.conn.execute("INSERT INTO customers(name, phone, address, doctor, created_on) VALUES (?,?,?,?,?)",
                                (name, ph, kw.get("address"), kw.get("doctor"), str(db.today()))).lastrowid
        if ph:
            self.cust_phone[ph] = cid
        self.cust_name.setdefault(name.lower(), cid)
        self.stats["created"] += 1
        return cid

    def history_batch(self, pid: int, mrp: float, rate: float) -> int:
        """Zero-quantity batch that old bills point to (current stock comes from the stock report)."""
        if pid not in self.hist_batch:
            row = self.conn.execute("SELECT id FROM batches WHERE product_id=? AND batch_no='IMPORTED-HISTORY'",
                                    (pid,)).fetchone()
            self.hist_batch[pid] = row[0] if row else self.conn.execute(
                "INSERT INTO batches(product_id, batch_no, expiry, mrp, purchase_rate, qty, received_on) "
                "VALUES (?,?,?,?,?,0,?)", (pid, "IMPORTED-HISTORY", "2099-12-31", mrp or 0, rate or 0,
                                           str(db.today()))).lastrowid
        return self.hist_batch[pid]


def apply(checked: Checked, file_name: str, data_hash: str, user: str = "owner", options: dict | None = None) -> dict:
    """Save validated rows - one transaction, all or nothing."""
    options = options or {}
    if not checked.rows:
        raise ValueError("Nothing to import - fix the errors first")
    ts = db.now_ts()
    with db.tx() as conn:
        imp = conn.execute("INSERT INTO imports(ts, file_name, file_hash, dataset, rows_in, user) VALUES (?,?,?,?,?,?)",
                           (ts, file_name, data_hash, checked.dataset, len(checked.rows), user)).lastrowid
        c = _Ctx(conn)
        {"stock": _save_stock, "suppliers": _save_suppliers, "customers": _save_customers,
         "purchases": _save_purchases, "sales": _save_sales}[checked.dataset](c, checked.rows, imp, ts, options)
        conn.execute("UPDATE imports SET created=?, updated=?, skipped=?, summary=? WHERE id=?",
                     (c.stats["created"], c.stats["updated"], c.stats["skipped"],
                      json.dumps(c.notes[:200]), imp))
    return {"import_id": imp, **c.stats, "notes": c.notes}


def _save_stock(c: _Ctx, rows, imp, ts, options):
    merged: dict[tuple, dict] = {}
    for r in rows:                                              # same item + batch twice -> add quantities
        k = (r["item"].lower(), r["batch"].lower())
        if k in merged:
            merged[k]["qty"] += r["qty"]
        else:
            merged[k] = dict(r)
    for r in merged.values():
        key = r["item"].lower()
        pid = c.product(r["item"], composition=r["composition"], company=r["company"], pack=r["pack"],
                        hsn=r["hsn"], gst=r["gst"], schedule=r["schedule"], rack=r["rack"], barcode=r["barcode"],
                        mrp=r["mrp"])
        if key in c.preexisting and key not in c.touched:      # existing product: fill blanks only, count once
            c.touched.add(key)
            c.conn.execute("""UPDATE products SET
                                 manufacturer=COALESCE(manufacturer, ?), pack=COALESCE(pack, ?),
                                 rack=COALESCE(rack, ?), default_mrp=COALESCE(default_mrp, ?) WHERE id=?""",
                           (r["company"], r["pack"], r["rack"], r["mrp"], pid))
            c.stats["updated"] += 1
        if r["qty"] <= 0:
            continue
        if c.conn.execute("SELECT 1 FROM batches WHERE product_id=? AND batch_no=?", (pid, r["batch"])).fetchone():
            c.stats["skipped"] += 1
            c.notes.append(f"{r['item']} batch {r['batch']} already exists - not added again")
            continue
        bid = c.conn.execute("INSERT INTO batches(product_id, batch_no, expiry, mrp, purchase_rate, qty, received_on)"
                             " VALUES (?,?,?,?,?,?,?)", (pid, r["batch"], r["expiry"], r["mrp"], r["rate"], r["qty"],
                                                         str(db.today()))).lastrowid
        c.conn.execute("INSERT INTO stock_movements(ts, batch_id, product_id, qty_change, type, ref, note, user) "
                       "VALUES (?,?,?,?,?,?,?,?)", (ts, bid, pid, r["qty"], "OPENING", f"IMPORT#{imp}",
                                                    "Opening stock from import", "import"))
        c.stats["created"] += 1


def _save_suppliers(c: _Ctx, rows, imp, ts, options):
    for r in rows:
        key = r["name"].lower()
        if key in c.suppliers:
            c.conn.execute("""UPDATE suppliers SET gstin=COALESCE(gstin, ?), phone=COALESCE(phone, ?),
                              city=COALESCE(city, ?), credit_days=COALESCE(?, credit_days) WHERE id=?""",
                           (r["gstin"], r["phone"], r["city"], r["credit_days"], c.suppliers[key]))
            c.stats["updated"] += 1
            sid = c.suppliers[key]
        else:
            sid = c.supplier(r["name"], gstin=r["gstin"], phone=r["phone"], city=r["city"],
                             credit_days=r["credit_days"])
        if r["balance"]:
            inv = f"OPENING-BALANCE-{imp}"
            c.conn.execute("INSERT INTO purchases(supplier_id, invoice_no, order_date, received_date, ordered_qty, "
                           "received_qty, taxable, tax, total, paid) VALUES (?,?,?,?,0,0,?,0,?,0)",
                           (sid, inv, str(db.today()), str(db.today()), r["balance"], r["balance"]))
            c.notes.append(f"{r['name']}: opening balance {r['balance']:,.2f} added as payable")


def _save_customers(c: _Ctx, rows, imp, ts, options):
    for r in rows:
        existing = (r["phone"] and r["phone"] in c.cust_phone) or (
            not r["phone"] and r["name"].lower() in c.cust_name)
        cid = c.customer(r["name"], r["phone"], address=r["address"], doctor=r["doctor"])
        if existing:
            c.conn.execute("UPDATE customers SET address=COALESCE(address, ?), doctor=COALESCE(doctor, ?) WHERE id=?",
                           (r["address"], r["doctor"], cid))
            c.stats["updated"] += 1
        if r["balance"]:
            c.conn.execute("INSERT INTO sales(invoice_no, ts, customer_id, patient_name, payment_mode, gross, discount,"
                           " taxable, cgst, sgst, total, cost) VALUES (?,?,?,?,?,?,0,?,0,0,?,?)",
                           (f"OB/{imp}/{cid}", f"{db.today()} 00:00:00", cid, "Opening balance (imported)", "Credit",
                            r["balance"], r["balance"], r["balance"], r["balance"]))
            c.notes.append(f"{r['name']}: udhaar {r['balance']:,.2f} added")


def _group(rows):
    bills: dict[tuple, list] = {}
    for r in rows:
        bills.setdefault((r["invoice"], r["date"], r.get("supplier") or ""), []).append(r)
    return bills


def _save_purchases(c: _Ctx, rows, imp, ts, options):
    mark_paid = options.get("purchases_paid", True)
    paid_by_supplier: dict[int, list] = {}
    for (inv, d, sup), lines in _group(rows).items():
        sid = c.supplier(sup)
        if c.conn.execute("SELECT 1 FROM purchases WHERE supplier_id=? AND invoice_no=?", (sid, inv)).fetchone():
            c.stats["skipped"] += 1
            c.notes.append(f"Purchase bill {inv} ({sup}) already exists - skipped")
            continue
        taxable = tax = 0.0
        items = []
        for r in lines:
            gst = r["gst"] if r["gst"] is not None else 5
            rate = r["rate"] or (r["amount"] / (1 + gst / 100) / r["qty"])
            pid = c.product(r["item"], gst=gst, mrp=r["mrp"])
            amt = r["amount"] or round(r["qty"] * rate * (1 + gst / 100), 2)
            taxable += r["qty"] * rate
            tax += r["qty"] * rate * gst / 100
            items.append((pid, r, rate, gst, amt))
        pid_ = c.conn.execute("INSERT INTO purchases(supplier_id, invoice_no, order_date, received_date, ordered_qty,"
                              " received_qty, taxable, tax, total, paid) VALUES (?,?,?,?,?,?,?,?,?,?)",
                              (sid, inv, d, d, sum(int(r["qty"]) for r in lines),
                               sum(int(r["qty"] + r["free"]) for r in lines), round(taxable, 2), round(tax, 2),
                               round(taxable + tax, 2), int(mark_paid))).lastrowid
        for pid, r, rate, gst, amt in items:
            bid = c.history_batch(pid, r["mrp"] or 0, rate)
            c.conn.execute("INSERT INTO purchase_items(purchase_id, product_id, batch_id, qty, free_qty, rate, gst_rate,"
                           " amount) VALUES (?,?,?,?,?,?,?,?)", (pid_, pid, bid, int(r["qty"]), int(r["free"]),
                                                                 round(rate, 2), gst, amt))
        if mark_paid:
            paid_by_supplier.setdefault(sid, []).append((pid_, round(taxable + tax, 2)))
        c.stats["created"] += 1
    for sid, bills in paid_by_supplier.items():            # old bills are settled: one adjustment per supplier
        pay = c.conn.execute("INSERT INTO supplier_payments(supplier_id, pay_date, amount, mode, status, note, "
                             "entered_by, ts) VALUES (?,?,?,?,?,?,?,?)",
                             (sid, str(db.today()), round(sum(a for _, a in bills), 2), "Adjustment", "Cleared",
                              f"Imported purchase history (import #{imp}) - marked as already paid", "import", ts)
                             ).lastrowid
        c.conn.executemany("INSERT INTO supplier_payment_allocations(payment_id, purchase_id, amount) VALUES (?,?,?)",
                           [(pay, p, a) for p, a in bills])


def _save_sales(c: _Ctx, rows, imp, ts, options):
    for (inv, d, _), lines in _group(rows).items():
        invoice_no = f"OLD/{d}/{inv}"          # bill numbers restart every financial year
        if c.conn.execute("SELECT 1 FROM sales WHERE invoice_no=?", (invoice_no,)).fetchone():
            c.stats["skipped"] += 1
            continue
        first = lines[0]
        cid = c.customer(first["customer"], first["phone"], doctor=first["doctor"]) \
            if (first["customer"] or first["phone"]) else None
        tot = dict(gross=0.0, disc=0.0, taxable=0.0, tax=0.0, total=0.0, cost=0.0)
        items = []
        for r in lines:
            pid = c.product(r["item"])
            p = c.products[r["item"].lower()]
            gst = p["gst_rate"] or 5
            mrp = r["rate"] or (r["amount"] / r["qty"] / (1 - r["discount"] / 100) if r["discount"] < 100
                                else r["amount"] / r["qty"])
            gross = mrp * r["qty"]
            net = r["amount"] if r["amount"] is not None else round(gross * (1 - r["discount"] / 100), 2)
            taxable, tax = db.split_gst(net, gst)
            cost_row = c.conn.execute("SELECT purchase_rate FROM batches WHERE product_id=? AND purchase_rate>0 "
                                      "ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
            unit_cost = cost_row[0] if cost_row else mrp / (1 + gst / 100) * 0.75
            cost = round(unit_cost * r["qty"], 2)
            bid = c.history_batch(pid, mrp, unit_cost)
            items.append((pid, bid, r["qty"], mrp, r["discount"], gst, taxable, tax, net, cost))
            for k, v in (("gross", gross), ("disc", gross - net), ("taxable", taxable), ("tax", tax), ("total", net),
                         ("cost", cost)):
                tot[k] += v
        sale = c.conn.execute("INSERT INTO sales(invoice_no, ts, customer_id, patient_name, doctor_name, payment_mode,"
                              " gross, discount, taxable, cgst, sgst, total, cost) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              (invoice_no, f"{d} 12:00:00", cid, first["customer"], first["doctor"], first["mode"],
                               round(tot["gross"], 2), round(tot["disc"], 2), round(tot["taxable"], 2),
                               round(tot["tax"] / 2, 2), round(tot["tax"] / 2, 2), round(tot["total"], 2),
                               round(tot["cost"], 2))).lastrowid
        c.conn.executemany("INSERT INTO sale_items(sale_id, product_id, batch_id, qty, mrp, disc_pct, gst_rate, "
                           "taxable, tax, amount, cost) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           [(sale, *it) for it in items])
        c.stats["created"] += 1


# ---------------------------------------------------------------- templates and a sample export
def template_csv(dataset: str) -> str:
    return ",".join(label for label, _, _ in DATASETS[dataset]["fields"].values()) + "\n"


def sample_stock_export(n: int = 25) -> bytes:
    """A stock report laid out like typical desktop pharmacy software exports (title rows,
    batch-wise lines, MM/YY expiry, totals row). For demos and tests."""
    import random
    rnd = random.Random(11)
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Stock"
    ws.append(["SHREE RAM MEDICOS"])
    ws.append(["Station Road, Ajmer  Ph: 0145-2620000"])
    ws.append(["BATCH WISE STOCK STATEMENT AS ON " + db.today().strftime("%d/%m/%Y")])
    ws.append([])
    ws.append(["S.No", "Item Name", "Company", "Packing", "Batch No", "Exp.", "Cl. Stock", "P.Rate", "M.R.P",
               "GST%", "Rack", "Salt"])
    items = [("Dolo 650", "Micro Labs", "15 TAB", "Paracetamol 650mg", 33.0), ("Pan 40", "Alkem", "15 TAB",
             "Pantoprazole 40mg", 160.0), ("Azithral 500", "Alembic", "5 TAB", "Azithromycin 500mg", 120.0),
             ("Montair LC", "Cipla", "10 TAB", "Montelukast 10mg + Levocetirizine 5mg", 190.0),
             ("Telma 40", "Glenmark", "15 TAB", "Telmisartan 40mg", 250.0),
             ("Glycomet 500", "USV", "20 TAB", "Metformin 500mg", 35.0),
             ("Thyronorm 50", "Abbott", "120 TAB", "Levothyroxine 50mcg", 180.0),
             ("Asthalin Inhaler", "Cipla", "200 MD", "Salbutamol 100mcg", 160.0)]
    k = 0
    for i in range(n):
        name, co, pack, salt, mrp = items[i % len(items)]
        k += 1
        mm = rnd.randint(1, 12)
        yy = rnd.choice([26, 27, 27, 28])
        ws.append([k, name, co, pack, f"{name[:2].upper()}{rnd.randint(1000, 9999)}", f"{mm:02d}/{yy}",
                   rnd.choice([0, 12, 30, 45, 60, 100]), round(mrp / 1.05 * 0.72, 2), mrp, "5", f"R{rnd.randint(1, 6)}",
                   salt])
    ws.append([])
    ws.append(["", "GRAND TOTAL", "", "", "", "", "", "", "", "", "", ""])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
