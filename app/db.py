"""Pharmacy store database (SQLite) - schema, demo seed data and business logic.

Design rules
* Stock lives at BATCH level (batch no + expiry + MRP + purchase rate).
* Every stock change writes an immutable row to `stock_movements` (audit ledger).
* Sales pick batches FEFO (First-Expiry-First-Out) and never sell expired stock.
* Prices are MRP-inclusive of GST (Indian retail practice); GST is back-calculated
  and split CGST/SGST (intra-state sale).
* Schedule H / H1 / X data is ILLUSTRATIVE for the demo - verify against the
  current Drugs Rules notifications before real use.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from src.config import DAILY_LONG, PHARMACY_DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS suppliers (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, gstin TEXT, phone TEXT, city TEXT,
    lead_time_days INTEGER DEFAULT 2, credit_days INTEGER DEFAULT 30, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, generic TEXT, category TEXT,
    manufacturer TEXT, hsn TEXT DEFAULT '3004', gst_rate REAL DEFAULT 5,
    schedule TEXT DEFAULT 'OTC', pack TEXT, barcode TEXT UNIQUE, rack TEXT,
    reorder_level INTEGER DEFAULT 10, default_mrp REAL, chronic INTEGER DEFAULT 0,
    preferred_supplier_id INTEGER REFERENCES suppliers(id));
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id),
    batch_no TEXT NOT NULL, expiry TEXT NOT NULL, mrp REAL NOT NULL,
    purchase_rate REAL NOT NULL, qty INTEGER NOT NULL DEFAULT 0 CHECK (qty >= 0),
    supplier_id INTEGER REFERENCES suppliers(id), received_on TEXT,
    UNIQUE(product_id, batch_no));
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT UNIQUE, address TEXT,
    doctor TEXT, created_on TEXT);
CREATE TABLE IF NOT EXISTS purchases (
    id INTEGER PRIMARY KEY, supplier_id INTEGER REFERENCES suppliers(id), invoice_no TEXT,
    order_date TEXT, received_date TEXT, ordered_qty INTEGER, received_qty INTEGER,
    taxable REAL, tax REAL, total REAL, paid INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS purchase_items (
    id INTEGER PRIMARY KEY, purchase_id INTEGER REFERENCES purchases(id),
    product_id INTEGER REFERENCES products(id), batch_id INTEGER REFERENCES batches(id),
    qty INTEGER, free_qty INTEGER DEFAULT 0, rate REAL, gst_rate REAL, amount REAL);
CREATE TABLE IF NOT EXISTS sales (
    id INTEGER PRIMARY KEY, invoice_no TEXT UNIQUE, ts TEXT, customer_id INTEGER
    REFERENCES customers(id), patient_name TEXT, doctor_name TEXT, rx_ref TEXT,
    payment_mode TEXT, gross REAL, discount REAL, taxable REAL, cgst REAL, sgst REAL,
    total REAL, cost REAL);
CREATE TABLE IF NOT EXISTS sale_items (
    id INTEGER PRIMARY KEY, sale_id INTEGER REFERENCES sales(id),
    product_id INTEGER REFERENCES products(id), batch_id INTEGER REFERENCES batches(id),
    qty INTEGER, mrp REAL, disc_pct REAL, gst_rate REAL, taxable REAL, tax REAL,
    amount REAL, cost REAL, returned_qty INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS sale_returns (
    id INTEGER PRIMARY KEY, sale_item_id INTEGER REFERENCES sale_items(id), ts TEXT,
    qty INTEGER, amount REAL, reason TEXT);
CREATE TABLE IF NOT EXISTS supplier_returns (
    id INTEGER PRIMARY KEY, supplier_id INTEGER REFERENCES suppliers(id),
    batch_id INTEGER REFERENCES batches(id), ts TEXT, qty INTEGER, value REAL,
    reason TEXT, credit_note_status TEXT DEFAULT 'Pending', credit_note_no TEXT);
CREATE TABLE IF NOT EXISTS customer_payments (
    id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id), ts TEXT,
    amount REAL, mode TEXT);
CREATE TABLE IF NOT EXISTS stock_movements (
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, batch_id INTEGER REFERENCES batches(id),
    product_id INTEGER REFERENCES products(id), qty_change INTEGER NOT NULL,
    type TEXT NOT NULL, ref TEXT, note TEXT, user TEXT DEFAULT 'system');
CREATE INDEX IF NOT EXISTS ix_sales_ts ON sales(ts);
CREATE INDEX IF NOT EXISTS ix_si_sale ON sale_items(sale_id);
CREATE INDEX IF NOT EXISTS ix_si_prod ON sale_items(product_id);
CREATE INDEX IF NOT EXISTS ix_batch_prod ON batches(product_id);
CREATE INDEX IF NOT EXISTS ix_mov_batch ON stock_movements(batch_id);
-- the audit ledger is append-only
CREATE TRIGGER IF NOT EXISTS no_update_movements BEFORE UPDATE ON stock_movements
BEGIN SELECT RAISE(ABORT, 'stock_movements is append-only'); END;
CREATE TRIGGER IF NOT EXISTS no_delete_movements BEFORE DELETE ON stock_movements
BEGIN SELECT RAISE(ABORT, 'stock_movements is append-only'); END;
"""

# ------------------------------------------------------------------ catalogue
# name, generic, category, schedule, gst, pack, mrp, chronic, weight-in-category
CATALOGUE = [
    ("Diclofenac 50mg Tab", "Diclofenac sodium", "M01AB", "H", 5, "10 tab", 22, 0, 4),
    ("Aceclofenac 100mg Tab", "Aceclofenac", "M01AB", "H", 5, "10 tab", 68, 0, 3),
    ("Aceclofenac + Paracetamol Tab", "Aceclofenac/Paracetamol", "M01AB", "H", 5, "10 tab", 75, 0, 5),
    ("Diclofenac Gel 30g", "Diclofenac diethylamine", "M01AB", "OTC", 5, "30 g", 110, 0, 2),
    ("Ibuprofen 400mg Tab", "Ibuprofen", "M01AE", "H", 5, "15 tab", 18, 0, 5),
    ("Ibuprofen + Paracetamol Tab", "Ibuprofen/Paracetamol", "M01AE", "H", 5, "15 tab", 32, 0, 4),
    ("Naproxen 250mg Tab", "Naproxen", "M01AE", "H", 5, "10 tab", 45, 0, 1),
    ("Aspirin 75mg Tab", "Acetylsalicylic acid", "N02BA", "OTC", 5, "14 tab", 6, 1, 6),
    ("Aspirin 150mg Tab", "Acetylsalicylic acid", "N02BA", "OTC", 5, "14 tab", 8, 1, 3),
    ("Paracetamol 500mg Tab", "Paracetamol", "N02BE", "OTC", 5, "15 tab", 15, 0, 6),
    ("Paracetamol 650mg Tab", "Paracetamol", "N02BE", "OTC", 5, "15 tab", 33, 0, 9),
    ("Paracetamol Syrup 60ml", "Paracetamol", "N02BE", "OTC", 5, "60 ml", 40, 0, 3),
    ("Alprazolam 0.25mg Tab", "Alprazolam", "N05B", "H1", 5, "10 tab", 25, 0, 4),
    ("Alprazolam 0.5mg Tab", "Alprazolam", "N05B", "H1", 5, "10 tab", 38, 0, 3),
    ("Clonazepam 0.5mg Tab", "Clonazepam", "N05B", "H", 5, "10 tab", 55, 0, 3),
    ("Hydroxyzine 25mg Tab", "Hydroxyzine", "N05B", "H", 5, "15 tab", 70, 0, 2),
    ("Zolpidem 10mg Tab", "Zolpidem", "N05C", "H1", 5, "10 tab", 95, 0, 3),
    ("Melatonin 3mg Tab", "Melatonin", "N05C", "H", 5, "10 tab", 120, 0, 1),
    ("Salbutamol Inhaler 100mcg", "Salbutamol", "R03", "H", 5, "200 md", 160, 1, 3),
    ("Montelukast + Levocetirizine Tab", "Montelukast/Levocetirizine", "R03", "H", 5, "10 tab", 180, 0, 5),
    ("Budesonide + Formoterol Inhaler", "Budesonide/Formoterol", "R03", "H", 5, "120 md", 420, 1, 2),
    ("Etofylline + Theophylline Tab", "Etofylline/Theophylline", "R03", "H", 5, "30 tab", 25, 1, 2),
    ("Cetirizine 10mg Tab", "Cetirizine", "R06", "OTC", 5, "10 tab", 20, 0, 5),
    ("Levocetirizine 5mg Tab", "Levocetirizine", "R06", "H", 5, "10 tab", 45, 0, 4),
    ("Fexofenadine 120mg Tab", "Fexofenadine", "R06", "H", 5, "10 tab", 190, 0, 2),
    ("Chlorpheniramine Syrup 100ml", "Chlorpheniramine", "R06", "OTC", 5, "100 ml", 65, 0, 1),
    # ---- not covered by the 8-category ML model (simple moving-average forecast)
    ("Amoxicillin 500mg Cap", "Amoxicillin", "J01 Antibiotics", "H", 5, "10 cap", 85, 0, 2.5),
    ("Azithromycin 500mg Tab", "Azithromycin", "J01 Antibiotics", "H", 5, "3 tab", 72, 0, 2.0),
    ("Cefpodoxime 200mg Tab", "Cefpodoxime", "J01 Antibiotics", "H1", 5, "10 tab", 210, 0, 1.2),
    ("Pantoprazole 40mg Tab", "Pantoprazole", "A02 Acid disorders", "H", 5, "15 tab", 150, 0, 4.0),
    ("Antacid Suspension 170ml", "Magaldrate/Simethicone", "A02 Acid disorders", "OTC", 5, "170 ml", 120, 0, 2.0),
    ("Metformin 500mg Tab", "Metformin", "A10 Diabetes", "H", 5, "20 tab", 30, 1, 4.0),
    ("Glimepiride 1mg Tab", "Glimepiride", "A10 Diabetes", "H", 5, "10 tab", 60, 1, 2.0),
    ("Amlodipine 5mg Tab", "Amlodipine", "C Cardiovascular", "H", 5, "15 tab", 40, 1, 3.5),
    ("Telmisartan 40mg Tab", "Telmisartan", "C Cardiovascular", "H", 5, "15 tab", 95, 1, 3.0),
    ("Atorvastatin 10mg Tab", "Atorvastatin", "C Cardiovascular", "H", 5, "15 tab", 110, 1, 2.5),
    ("Vitamin D3 60000 IU Cap", "Cholecalciferol", "A11 Vitamins", "OTC", 5, "4 cap", 130, 0, 2.0),
    ("Vitamin B-Complex Cap", "B-complex", "A11 Vitamins", "OTC", 5, "15 cap", 40, 0, 2.5),
    ("ORS Sachet 21g", "Oral rehydration salts", "A07 ORS", "OTC", 5, "1 sachet", 22, 0, 4.0),
    ("Hand Sanitizer 500ml", "Isopropyl alcohol", "Non-drug", "OTC", 18, "500 ml", 250, 0, 0.6),
    ("Digital Thermometer", "Device", "Non-drug", "OTC", 18, "1 pc", 299, 0, 0.3),
    # deliberately slow / dead items for the dead-stock demo
    ("Herbal Cough Syrup 100ml", "Herbal", "R05 Cough", "OTC", 5, "100 ml", 95, 0, 0.0),
    ("Knee Support (L)", "Orthopaedic aid", "Non-drug", "OTC", 12, "1 pc", 450, 0, 0.0),
    ("Calcium + D3 Tab", "Calcium carbonate/D3", "A11 Vitamins", "OTC", 5, "15 tab", 115, 0, 0.15),
]
MODEL_CATEGORIES = ["M01AB", "M01AE", "N02BA", "N02BE", "N05B", "N05C", "R03", "R06"]

SUPPLIERS = [  # name, city, lead time mean, fill-rate, credit days
    ("Shree Balaji Pharma Distributors", "Jaipur", 1, 0.97, 30),
    ("Rajasthan Medical Agencies", "Jaipur", 2, 0.93, 21),
    ("Arogya Healthcare LLP", "Jaipur", 4, 0.86, 45),
    ("Navjeevan Drug House", "Ajmer", 3, 0.90, 30),
]
FIRST = ["Ramesh", "Sunita", "Amit", "Priya", "Suresh", "Kavita", "Vikram", "Neha", "Rajesh",
         "Pooja", "Mahesh", "Anita", "Deepak", "Rekha", "Sanjay", "Meena", "Arjun", "Geeta",
         "Manoj", "Sarita", "Ashok", "Kiran", "Naveen", "Lata", "Harish", "Usha", "Gopal",
         "Nisha", "Rakesh", "Seema", "Dinesh", "Asha", "Mukesh", "Ritu", "Pankaj", "Shobha"]
LAST = ["Sharma", "Agarwal", "Jain", "Gupta", "Meena", "Choudhary", "Singh", "Verma", "Saini",
        "Khandelwal", "Mathur", "Joshi"]
DOCTORS = ["Dr. A. Mehta (MBBS, MD)", "Dr. S. Rathore (MBBS)", "Dr. P. Bansal (MD Med)",
           "Dr. K. Purohit (MS Ortho)", "Dr. R. Kothari (MD Psych)", "Dr. V. Tak (MBBS, DCH)"]
PAYMENT_MODES = ["Cash", "UPI", "Card", "Credit"]


# ------------------------------------------------------------------ helpers
def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(PHARMACY_DB, timeout=10, detect_types=0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def tx():
    """Transaction: commit on success, roll back everything on any error."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def q(sql: str, params: tuple | dict = ()) -> pd.DataFrame:
    conn = connect()
    try:
        return pd.read_sql_query(sql, conn, params=params)
    finally:
        conn.close()


def today() -> date:
    return date.today()


def now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def split_gst(amount_incl: float, gst_rate: float) -> tuple[float, float]:
    """MRP-inclusive amount -> (taxable value, total GST)."""
    taxable = amount_incl / (1 + gst_rate / 100)
    return round(taxable, 2), round(amount_incl - taxable, 2)


def financial_year(d: date) -> str:
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start % 100:02d}{(start + 1) % 100:02d}"


def next_invoice_no(conn, d: date) -> str:
    prefix = f"INV/{financial_year(d)}/"
    row = conn.execute("SELECT invoice_no FROM sales WHERE invoice_no LIKE ? "
                       "ORDER BY id DESC LIMIT 1", (prefix + "%",)).fetchone()
    n = int(row[0].split("/")[-1]) + 1 if row else 1
    return f"{prefix}{n:06d}"


# ------------------------------------------------------------------ seeding
def init_db(force: bool = False) -> None:
    if force and PHARMACY_DB.exists():
        PHARMACY_DB.unlink()
        for ext in ("-wal", "-shm"):
            p = PHARMACY_DB.with_name(PHARMACY_DB.name + ext)
            if p.exists():
                p.unlink()
    conn = connect()
    conn.executescript(SCHEMA)
    seeded = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] > 0
    conn.close()
    if not seeded:
        seed()


def seed(days: int = 180, seed_value: int = 7) -> None:
    """Simulate `days` of store operations so every screen has realistic data."""
    rng = np.random.default_rng(seed_value)
    end = today()
    start = end - timedelta(days=days)
    conn = connect()
    cur = conn.cursor()

    # suppliers
    for i, (name, city, lead, fill, credit) in enumerate(SUPPLIERS, 1):
        cur.execute("INSERT INTO suppliers(id,name,gstin,phone,city,lead_time_days,credit_days)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (i, name, f"08AAB{'CDEF'[i-1]}{1234+i}Q1Z{i}", f"98290{10000+i*137}",
                     city, lead, credit))
    sup_profile = {i: (lead, fill) for i, (_, _, lead, fill, _) in enumerate(SUPPLIERS, 1)}

    # products
    prod = []
    for i, (name, generic, cat, sch, gst, pack, mrp, chronic, w) in enumerate(CATALOGUE, 1):
        pref = int(rng.integers(1, len(SUPPLIERS) + 1))
        cur.execute("INSERT INTO products(id,name,generic,category,manufacturer,hsn,gst_rate,"
                    "schedule,pack,barcode,rack,reorder_level,default_mrp,chronic,"
                    "preferred_supplier_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (i, name, generic, cat, rng.choice(["Arogya Pharma", "Sanjeevani Labs",
                     "Vedant Remedies", "Kiran Lifesciences"]),
                     "3004" if gst == 5 else "9025" if "Thermo" in name else "3808",
                     gst, sch, pack, f"890{1000000000 + i * 7919}", f"R{1 + i % 6}-S{1 + i % 4}",
                     10, mrp, chronic, pref))
        prod.append({"id": i, "name": name, "cat": cat, "mrp": mrp, "gst": gst, "w": w,
                     "chronic": chronic, "pref": pref, "schedule": sch})

    # customers
    customers = []
    for i in range(1, 41):
        nm = f"{FIRST[(i*7) % len(FIRST)]} {LAST[(i*5) % len(LAST)]}"
        cur.execute("INSERT INTO customers(id,name,phone,address,doctor,created_on) "
                    "VALUES (?,?,?,?,?,?)",
                    (i, nm, f"9{rng.integers(100000000, 999999999)}", "Jaipur",
                     DOCTORS[i % len(DOCTORS)], str(start)))
        customers.append(i)
    chronic_ids = [p["id"] for p in prod if p["chronic"]]
    chronic_plan = {c: list(rng.choice(chronic_ids, size=int(rng.integers(1, 3)), replace=False))
                    for c in customers[:18]}
    refill_day = {c: int(rng.integers(0, 30)) for c in chronic_plan}

    # daily demand per product -----------------------------------------------
    cat_series = pd.read_csv(DAILY_LONG, parse_dates=["date"]) if DAILY_LONG.exists() else None
    dates = [start + timedelta(days=d) for d in range(days + 1)]
    demand = {p["id"]: np.zeros(len(dates), dtype=int) for p in prod}
    by_cat: dict[str, list] = {}
    for p in prod:
        by_cat.setdefault(p["cat"], []).append(p)
    for cat, plist in by_cat.items():
        weights = np.array([p["w"] for p in plist], float)
        for di, d in enumerate(dates):
            if cat in MODEL_CATEGORIES and cat_series is not None:
                ref = pd.Timestamp(d if d < end else d - timedelta(days=7))
                row = cat_series[(cat_series["category"] == cat) & (cat_series["date"] == ref)]
                units = int(row["sales"].iloc[0]) if len(row) else 0
                if weights.sum() > 0 and units > 0:
                    split = rng.multinomial(units, weights / weights.sum())
                    for p, u in zip(plist, split):
                        demand[p["id"]][di] += u
            else:
                for p in plist:
                    demand[p["id"]][di] += rng.poisson(p["w"] * (0.8 if d.weekday() == 6 else 1))
    # today is a partial day: only the share of the business day already elapsed
    now_h = datetime.now().hour + datetime.now().minute / 60
    elapsed = float(np.clip((now_h - 9) / 13, 0.0, 1.0))
    for p in prod:
        demand[p["id"]][-1] = int(round(demand[p["id"]][-1] * elapsed))
    # dead-stock items: occasional early sales only
    for p in prod:
        if p["w"] == 0:
            demand[p["id"]][:] = 0
            for k in rng.choice(40, size=3, replace=False):
                demand[p["id"]][k] = 1

    # simulate purchases + FEFO sales ------------------------------------------
    batches: dict[int, list] = {p["id"]: [] for p in prod}      # [batch_id, qty, expiry, mrp, rate]
    batch_rows, purchase_rows, pitem_rows, moves = [], [], [], []
    sale_rows, sitem_rows = [], []
    bid = pid = sid = siid = 0
    inv_counter: dict[str, int] = {}

    def avg_demand(pidx):
        return max(demand[pidx][:60].mean(), 0.2)

    def purchase(p, d: date, qty: int, expiry: date | None = None):
        nonlocal bid, pid
        sup = p["pref"] if rng.random() < 0.7 else int(rng.integers(1, len(SUPPLIERS) + 1))
        lead, fill = sup_profile[sup]
        lead_days = max(0, int(round(rng.normal(lead, 0.8))))
        order_date = d - timedelta(days=lead_days)
        received = qty if rng.random() < fill else int(qty * rng.uniform(0.5, 0.9))
        received = max(received, 1)
        rate = round(p["mrp"] / (1 + p["gst"] / 100) * rng.uniform(0.74, 0.80), 2)
        exp = expiry or (d + timedelta(days=int(rng.integers(240, 720))))
        exp = date(exp.year, exp.month, 1) + timedelta(days=27)          # month-end style
        bid += 1
        pid += 1
        batch_no = f"{p['name'][:2].upper()}{d.strftime('%y%m')}{bid:04d}"
        batch_rows.append([bid, p["id"], batch_no, str(exp), p["mrp"], rate, received, sup, str(d)])
        taxable = round(received * rate, 2)
        tax = round(taxable * p["gst"] / 100, 2)
        purchase_rows.append([pid, sup, f"SB/{d.strftime('%y')}/{pid:05d}", str(order_date), str(d),
                              qty, received, taxable, tax, round(taxable + tax, 2),
                              1 if d < end - timedelta(days=30) else 0])
        pitem_rows.append([pid, p["id"], bid, received, 0, rate, p["gst"], round(taxable + tax, 2)])
        moves.append([f"{d} 09:00:00", bid, p["id"], received, "PURCHASE", f"PUR#{pid}", None])
        batches[p["id"]].append([bid, received, exp, p["mrp"], rate])

    for p in prod:                                   # opening stock
        opening = int(avg_demand(p["id"]) * rng.uniform(25, 45)) + 5
        purchase(p, start - timedelta(days=1), opening)

    for di, d in enumerate(dates):
        lines = []                                   # (customer or None, product, qty)
        for p in prod:
            u = int(demand[p["id"]][di])
            while u > 0:
                take = min(u, int(rng.choice([1, 1, 1, 2, 2, 3])))
                lines.append((None, p, take))
                u -= take
        for c, plist in chronic_plan.items():        # monthly refills of regular patients
            if (di - refill_day[c]) % 30 == 0 and di >= refill_day[c]:
                for pid_ in plist:
                    lines.append((c, prod[pid_ - 1], int(rng.integers(1, 3))))
        order = rng.permutation(len(lines))
        lines = [lines[i] for i in order]
        # group lines into invoices
        i = 0
        while i < len(lines):
            size = int(rng.choice([1, 1, 2, 2, 3, 4]))
            chunk = lines[i:i + size]
            i += size
            cust = next((c for c, _, _ in chunk if c), None)
            if cust is None and rng.random() < 0.25:
                cust = int(rng.choice(customers))
            hh = int(rng.choice(range(9, 22), p=np.array([4, 6, 7, 6, 5, 4, 4, 5, 7, 9, 10, 9, 6]) / 82))
            if d == end:                              # keep today's bills in the past
                hh = int(rng.integers(9, max(datetime.now().hour, 10)))
            ts = f"{d} {hh:02d}:{int(rng.integers(0, 60)):02d}:{int(rng.integers(0, 60)):02d}"
            needs_rx = any(p["schedule"] in ("H", "H1", "X") for _, p, _ in chunk)
            doctor = str(rng.choice(DOCTORS)) if needs_rx else None
            mode = str(rng.choice(PAYMENT_MODES, p=[0.42, 0.46, 0.08, 0.04])) if cust else \
                str(rng.choice(PAYMENT_MODES[:3], p=[0.45, 0.47, 0.08]))
            disc = float(rng.choice([0, 5, 10, 10, 15]))
            items, gross = [], 0.0
            for _, p, qty in chunk:
                # FEFO allocation over non-expired batches
                avail = sorted([b for b in batches[p["id"]] if b[1] > 0 and b[2] > d],
                               key=lambda b: b[2])
                if sum(b[1] for b in avail) < qty:
                    purchase(p, d, int(avg_demand(p["id"]) * 30) + qty + 5)
                    avail = sorted([b for b in batches[p["id"]] if b[1] > 0 and b[2] > d],
                                   key=lambda b: b[2])
                need = qty
                for b in avail:
                    if need == 0:
                        break
                    t = min(need, b[1])
                    b[1] -= t
                    need -= t
                    items.append((p, b, t))
            if not items:
                continue
            sid += 1
            fy = financial_year(d)
            inv_counter[fy] = inv_counter.get(fy, 0) + 1
            invoice = f"INV/{fy}/{inv_counter[fy]:06d}"
            tot = {"gross": 0, "disc": 0, "taxable": 0, "tax": 0, "total": 0, "cost": 0}
            for p, b, t in items:
                siid += 1
                g = b[3] * t
                dsc = round(g * disc / 100, 2)
                net = round(g - dsc, 2)
                taxable, tax = split_gst(net, p["gst"])
                cost = round(b[4] * t, 2)
                sitem_rows.append([siid, sid, p["id"], b[0], t, b[3], disc, p["gst"], taxable,
                                   tax, net, cost])
                moves.append([ts, b[0], p["id"], -t, "SALE", invoice, None])
                for k, v in (("gross", g), ("disc", dsc), ("taxable", taxable), ("tax", tax),
                             ("total", net), ("cost", cost)):
                    tot[k] += v
            patient = None
            if needs_rx and cust is None:
                patient = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
            sale_rows.append([sid, invoice, ts, cust, patient, doctor,
                              f"RX-{sid:06d}" if needs_rx else None, mode,
                              round(tot["gross"], 2), round(tot["disc"], 2), round(tot["taxable"], 2),
                              round(tot["tax"] / 2, 2), round(tot["tax"] / 2, 2),
                              round(tot["total"], 2), round(tot["cost"], 2)])
        # daily reorder check (pharmacist tops up fast movers)
        for p in prod:
            stock = sum(b[1] for b in batches[p["id"]] if b[2] > d)
            if p["w"] > 0 and stock < avg_demand(p["id"]) * 5 and di < len(dates) - 1:
                purchase(p, d, int(avg_demand(p["id"]) * rng.uniform(20, 35)) + 3)

    # inject expiry-risk scenarios: slow items holding big short-dated batches
    risky = [("Naproxen 250mg Tab", 20, 60), ("Fexofenadine 120mg Tab", 45, 40),
             ("Melatonin 3mg Tab", 75, 35), ("Chlorpheniramine Syrup 100ml", 25, 50),
             ("Herbal Cough Syrup 100ml", 55, 40), ("Calcium + D3 Tab", 110, 30)]
    by_name = {p["name"]: p for p in prod}
    for name, days_left, qty in risky:
        purchase(by_name[name], end - timedelta(days=300), qty, expiry=end + timedelta(days=days_left))
        batch_rows[-1][3] = str(end + timedelta(days=days_left))
    # already-expired stock still on the shelf
    for name, qty in (("Knee Support (L)", 6), ("Glimepiride 1mg Tab", 12)):
        purchase(by_name[name], end - timedelta(days=400), qty, expiry=end - timedelta(days=10))
        batch_rows[-1][3] = str(end - timedelta(days=10))

    # write final batch quantities
    final_qty = {b[0]: b[1] for plist in batches.values() for b in plist}
    for r in batch_rows:
        r[6] = final_qty[r[0]]

    cur.executemany("INSERT INTO batches(id,product_id,batch_no,expiry,mrp,purchase_rate,qty,"
                    "supplier_id,received_on) VALUES (?,?,?,?,?,?,?,?,?)", batch_rows)
    cur.executemany("INSERT INTO purchases(id,supplier_id,invoice_no,order_date,received_date,"
                    "ordered_qty,received_qty,taxable,tax,total,paid) VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?)", purchase_rows)
    cur.executemany("INSERT INTO purchase_items(purchase_id,product_id,batch_id,qty,free_qty,"
                    "rate,gst_rate,amount) VALUES (?,?,?,?,?,?,?,?)", pitem_rows)
    cur.executemany("INSERT INTO sales(id,invoice_no,ts,customer_id,patient_name,doctor_name,"
                    "rx_ref,payment_mode,gross,discount,taxable,cgst,sgst,total,cost) VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", sale_rows)
    cur.executemany("INSERT INTO sale_items(id,sale_id,product_id,batch_id,qty,mrp,disc_pct,"
                    "gst_rate,taxable,tax,amount,cost) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    sitem_rows)
    moves.sort(key=lambda m: m[0])
    cur.executemany("INSERT INTO stock_movements(ts,batch_id,product_id,qty_change,type,ref,note)"
                    " VALUES (?,?,?,?,?,?,?)", moves)
    # a few credit-customer payments and pending expiry claims
    credit = cur.execute("SELECT customer_id, SUM(total) FROM sales WHERE payment_mode='Credit'"
                         " GROUP BY customer_id").fetchall()
    for c, amt in credit:
        cur.execute("INSERT INTO customer_payments(customer_id,ts,amount,mode) VALUES (?,?,?,?)",
                    (c, f"{end - timedelta(days=5)} 18:00:00", round(amt * 0.6, 0), "UPI"))
    conn.commit()
    conn.close()


# ------------------------------------------------------------------ reads
def products_df() -> pd.DataFrame:
    return q("""
        SELECT p.id, p.name, p.generic, p.category, p.schedule, p.gst_rate, p.pack, p.barcode,
               p.rack, p.reorder_level, p.default_mrp AS mrp, p.chronic,
               s.name AS preferred_supplier, s.lead_time_days,
               COALESCE(SUM(CASE WHEN b.expiry > date('now','localtime') THEN b.qty END),0) AS stock,
               COALESCE(SUM(CASE WHEN b.expiry <= date('now','localtime') THEN b.qty END),0) AS expired_qty,
               COALESCE(SUM(b.qty*b.purchase_rate),0) AS stock_value_cost,
               MIN(CASE WHEN b.qty>0 AND b.expiry > date('now','localtime') THEN b.expiry END) AS next_expiry
        FROM products p
        LEFT JOIN batches b ON b.product_id = p.id
        LEFT JOIN suppliers s ON s.id = p.preferred_supplier_id
        GROUP BY p.id ORDER BY p.name""")


def batches_df(only_in_stock: bool = True) -> pd.DataFrame:
    df = q(f"""
        SELECT b.id AS batch_id, p.name AS product, p.category, b.batch_no, b.expiry, b.mrp,
               b.purchase_rate, b.qty, ROUND(b.qty*b.purchase_rate,2) AS value_cost,
               s.name AS supplier, b.received_on, p.rack, p.schedule
        FROM batches b JOIN products p ON p.id=b.product_id
        LEFT JOIN suppliers s ON s.id=b.supplier_id
        {"WHERE b.qty > 0" if only_in_stock else ""} ORDER BY p.name, b.expiry""")
    df["days_to_expiry"] = (pd.to_datetime(df["expiry"]) - pd.Timestamp(today())).dt.days
    return df


def fefo_batches(product_id: int) -> pd.DataFrame:
    return q("""SELECT id, batch_no, expiry, mrp, qty FROM batches
                WHERE product_id=? AND qty>0 AND expiry > date('now','localtime')
                ORDER BY expiry, id""", (product_id,))


def sales_lines(start: date, end: date) -> pd.DataFrame:
    return q("""
        SELECT s.id AS sale_id, s.invoice_no, s.ts, date(s.ts) AS day, s.payment_mode,
               s.customer_id, s.doctor_name, si.id AS sale_item_id, si.product_id,
               p.name AS product, p.category, p.schedule, si.qty, si.mrp, si.disc_pct,
               si.gst_rate, si.taxable, si.tax, si.amount, si.cost, si.returned_qty,
               b.batch_no, b.expiry
        FROM sales s JOIN sale_items si ON si.sale_id=s.id
        JOIN products p ON p.id=si.product_id JOIN batches b ON b.id=si.batch_id
        WHERE date(s.ts) BETWEEN ? AND ?""", (str(start), str(end)))


def velocity(days: int = 30) -> pd.DataFrame:
    """Average units sold per day per product over the last `days` days."""
    df = q("""SELECT si.product_id, SUM(si.qty - si.returned_qty) AS units,
                     MAX(date(s.ts)) AS last_sale
              FROM sale_items si JOIN sales s ON s.id=si.sale_id
              WHERE date(s.ts) > date('now','localtime', ?) GROUP BY si.product_id""",
           (f"-{days} days",))
    df["per_day"] = df["units"] / days
    return df


def last_sale_dates() -> pd.DataFrame:
    return q("""SELECT si.product_id, MAX(date(s.ts)) AS last_sale
                FROM sale_items si JOIN sales s ON s.id=si.sale_id GROUP BY si.product_id""")


# ------------------------------------------------------------------ writes
def create_sale(cart: list[dict], payment_mode: str, customer_id: int | None = None,
                patient_name: str | None = None, doctor_name: str | None = None,
                rx_ref: str | None = None, user: str = "counter") -> dict:
    """cart: [{product_id, qty, disc_pct}]  -> allocates FEFO batches atomically."""
    if not cart:
        raise ValueError("Cart is empty")
    if payment_mode == "Credit" and not customer_id:
        raise ValueError("Credit sale needs a registered customer")
    with tx() as conn:
        prods = {r["id"]: r for r in conn.execute(
            f"SELECT * FROM products WHERE id IN ({','.join('?' * len(cart))})",
            [c["product_id"] for c in cart])}
        if any(prods[c["product_id"]]["schedule"] in ("H", "H1", "X") for c in cart):
            if not doctor_name:
                raise ValueError("Prescription drug in cart: doctor name / Rx is required")
        if any(prods[c["product_id"]]["schedule"] == "X" for c in cart):
            raise ValueError("Schedule X items need the separate Schedule X register workflow")
        if any(prods[c["product_id"]]["schedule"] == "H1" for c in cart) and not (
                patient_name or customer_id):
            raise ValueError("Schedule H1 drug: patient name is required for the H1 register")
        ts = now_ts()
        invoice = next_invoice_no(conn, today())
        cur = conn.execute("INSERT INTO sales(invoice_no,ts,customer_id,patient_name,doctor_name,"
                           "rx_ref,payment_mode) VALUES (?,?,?,?,?,?,?)",
                           (invoice, ts, customer_id, patient_name, doctor_name, rx_ref, payment_mode))
        sale_id = cur.lastrowid
        tot = dict(gross=0.0, disc=0.0, taxable=0.0, tax=0.0, total=0.0, cost=0.0)
        lines = []
        for c in cart:
            p = prods[c["product_id"]]
            need = int(c["qty"])
            batches = conn.execute("SELECT * FROM batches WHERE product_id=? AND qty>0 AND "
                                   "expiry > date('now','localtime') ORDER BY expiry, id",
                                   (p["id"],)).fetchall()
            if sum(b["qty"] for b in batches) < need:
                raise ValueError(f"Insufficient non-expired stock for {p['name']}")
            for b in batches:
                if need == 0:
                    break
                t = min(need, b["qty"])
                need -= t
                g = b["mrp"] * t
                dsc = round(g * float(c.get("disc_pct", 0)) / 100, 2)
                net = round(g - dsc, 2)
                taxable, tax = split_gst(net, p["gst_rate"])
                cost = round(b["purchase_rate"] * t, 2)
                # optimistic concurrency guard: never let a batch go negative
                upd = conn.execute("UPDATE batches SET qty = qty - ? WHERE id=? AND qty >= ?",
                                   (t, b["id"], t))
                if upd.rowcount != 1:
                    raise ValueError("Stock changed during billing - please retry")
                conn.execute("INSERT INTO sale_items(sale_id,product_id,batch_id,qty,mrp,disc_pct,"
                             "gst_rate,taxable,tax,amount,cost) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                             (sale_id, p["id"], b["id"], t, b["mrp"], c.get("disc_pct", 0),
                              p["gst_rate"], taxable, tax, net, cost))
                conn.execute("INSERT INTO stock_movements(ts,batch_id,product_id,qty_change,type,"
                             "ref,user) VALUES (?,?,?,?,?,?,?)",
                             (ts, b["id"], p["id"], -t, "SALE", invoice, user))
                for k, v in (("gross", g), ("disc", dsc), ("taxable", taxable), ("tax", tax),
                             ("total", net), ("cost", cost)):
                    tot[k] += v
                lines.append({"product": p["name"], "batch": b["batch_no"], "expiry": b["expiry"],
                              "qty": t, "mrp": b["mrp"], "disc_pct": c.get("disc_pct", 0),
                              "gst_rate": p["gst_rate"], "hsn": p["hsn"], "taxable": taxable,
                              "tax": tax, "amount": net, "schedule": p["schedule"]})
        conn.execute("UPDATE sales SET gross=?,discount=?,taxable=?,cgst=?,sgst=?,total=?,cost=? "
                     "WHERE id=?", (round(tot["gross"], 2), round(tot["disc"], 2),
                                    round(tot["taxable"], 2), round(tot["tax"] / 2, 2),
                                    round(tot["tax"] / 2, 2), round(tot["total"], 2),
                                    round(tot["cost"], 2), sale_id))
    return {"sale_id": sale_id, "invoice_no": invoice, "ts": ts, "lines": lines,
            "totals": {k: round(v, 2) for k, v in tot.items()}}


def create_purchase(supplier_id: int, invoice_no: str, lines: list[dict],
                    order_date: str | None = None, user: str = "manager") -> int:
    """lines: [{product_id, batch_no, expiry, qty, free_qty, rate, mrp, gst_rate}]"""
    if not lines:
        raise ValueError("No purchase lines")
    ts = now_ts()
    with tx() as conn:
        taxable = sum(l["qty"] * l["rate"] for l in lines)
        tax = sum(l["qty"] * l["rate"] * l["gst_rate"] / 100 for l in lines)
        received = sum(l["qty"] + l.get("free_qty", 0) for l in lines)
        cur = conn.execute("INSERT INTO purchases(supplier_id,invoice_no,order_date,received_date,"
                           "ordered_qty,received_qty,taxable,tax,total) VALUES (?,?,?,?,?,?,?,?,?)",
                           (supplier_id, invoice_no, order_date or str(today()), str(today()),
                            received, received, round(taxable, 2), round(tax, 2),
                            round(taxable + tax, 2)))
        pid = cur.lastrowid
        for l in lines:
            if date.fromisoformat(str(l["expiry"])) <= today():
                raise ValueError(f"Batch {l['batch_no']} is already expired")
            units = int(l["qty"]) + int(l.get("free_qty", 0))
            # free goods lower the effective cost per unit
            eff_rate = round(l["qty"] * l["rate"] / units, 2)
            row = conn.execute("SELECT id FROM batches WHERE product_id=? AND batch_no=?",
                               (l["product_id"], l["batch_no"])).fetchone()
            if row:
                bid = row["id"]
                conn.execute("UPDATE batches SET qty=qty+? WHERE id=?", (units, bid))
            else:
                bid = conn.execute("INSERT INTO batches(product_id,batch_no,expiry,mrp,purchase_rate,"
                                   "qty,supplier_id,received_on) VALUES (?,?,?,?,?,?,?,?)",
                                   (l["product_id"], l["batch_no"], str(l["expiry"]), l["mrp"],
                                    eff_rate, units, supplier_id, str(today()))).lastrowid
            amt = round(l["qty"] * l["rate"] * (1 + l["gst_rate"] / 100), 2)
            conn.execute("INSERT INTO purchase_items(purchase_id,product_id,batch_id,qty,free_qty,"
                         "rate,gst_rate,amount) VALUES (?,?,?,?,?,?,?,?)",
                         (pid, l["product_id"], bid, l["qty"], l.get("free_qty", 0), l["rate"],
                          l["gst_rate"], amt))
            conn.execute("INSERT INTO stock_movements(ts,batch_id,product_id,qty_change,type,ref,"
                         "user) VALUES (?,?,?,?,?,?,?)",
                         (ts, bid, l["product_id"], units, "PURCHASE", f"PUR#{pid} {invoice_no}", user))
    return pid


def sale_return(sale_item_id: int, qty: int, reason: str, user: str = "counter") -> float:
    with tx() as conn:
        it = conn.execute("SELECT * FROM sale_items WHERE id=?", (sale_item_id,)).fetchone()
        if not it:
            raise ValueError("Invoice line not found")
        if qty <= 0 or qty > it["qty"] - it["returned_qty"]:
            raise ValueError("Return qty exceeds quantity sold")
        amount = round(it["amount"] / it["qty"] * qty, 2)
        conn.execute("UPDATE sale_items SET returned_qty = returned_qty + ? WHERE id=?",
                     (qty, sale_item_id))
        conn.execute("UPDATE batches SET qty = qty + ? WHERE id=?", (qty, it["batch_id"]))
        conn.execute("INSERT INTO sale_returns(sale_item_id,ts,qty,amount,reason) VALUES (?,?,?,?,?)",
                     (sale_item_id, now_ts(), qty, amount, reason))
        conn.execute("INSERT INTO stock_movements(ts,batch_id,product_id,qty_change,type,ref,note,user)"
                     " VALUES (?,?,?,?,?,?,?,?)", (now_ts(), it["batch_id"], it["product_id"], qty,
                                                   "SALE_RETURN", f"SI#{sale_item_id}", reason, user))
    return amount


def supplier_return(batch_id: int, qty: int, reason: str, user: str = "manager") -> int:
    with tx() as conn:
        b = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not b or qty <= 0 or qty > b["qty"]:
            raise ValueError("Invalid return quantity")
        conn.execute("UPDATE batches SET qty = qty - ? WHERE id=?", (qty, batch_id))
        rid = conn.execute("INSERT INTO supplier_returns(supplier_id,batch_id,ts,qty,value,reason) "
                           "VALUES (?,?,?,?,?,?)", (b["supplier_id"], batch_id, now_ts(), qty,
                                                    round(qty * b["purchase_rate"], 2), reason)).lastrowid
        conn.execute("INSERT INTO stock_movements(ts,batch_id,product_id,qty_change,type,ref,note,user)"
                     " VALUES (?,?,?,?,?,?,?,?)", (now_ts(), batch_id, b["product_id"], -qty,
                                                   "SUPPLIER_RETURN", f"SR#{rid}", reason, user))
    return rid


def settle_credit_note(return_id: int, credit_note_no: str) -> None:
    with tx() as conn:
        conn.execute("UPDATE supplier_returns SET credit_note_status='Received', credit_note_no=? "
                     "WHERE id=?", (credit_note_no, return_id))


def adjust_stock(batch_id: int, qty_change: int, reason: str, user: str = "manager") -> None:
    with tx() as conn:
        b = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not b or b["qty"] + qty_change < 0:
            raise ValueError("Adjustment would make stock negative")
        conn.execute("UPDATE batches SET qty = qty + ? WHERE id=?", (qty_change, batch_id))
        conn.execute("INSERT INTO stock_movements(ts,batch_id,product_id,qty_change,type,ref,note,user)"
                     " VALUES (?,?,?,?,?,?,?,?)", (now_ts(), batch_id, b["product_id"], qty_change,
                                                   "ADJUSTMENT", "STOCK-AUDIT", reason, user))


def add_customer(name: str, phone: str, address: str = "", doctor: str = "") -> int:
    with tx() as conn:
        return conn.execute("INSERT INTO customers(name,phone,address,doctor,created_on) "
                            "VALUES (?,?,?,?,?)", (name, phone, address, doctor,
                                                   str(today()))).lastrowid


def add_supplier(name, gstin, phone, city, lead_time_days, credit_days) -> int:
    with tx() as conn:
        return conn.execute("INSERT INTO suppliers(name,gstin,phone,city,lead_time_days,credit_days)"
                            " VALUES (?,?,?,?,?,?)", (name, gstin, phone, city, lead_time_days,
                                                      credit_days)).lastrowid


def add_product(**kw) -> int:
    cols = ",".join(kw)
    with tx() as conn:
        return conn.execute(f"INSERT INTO products({cols}) VALUES ({','.join('?' * len(kw))})",
                            tuple(kw.values())).lastrowid


def record_payment(customer_id: int, amount: float, mode: str) -> None:
    with tx() as conn:
        conn.execute("INSERT INTO customer_payments(customer_id,ts,amount,mode) VALUES (?,?,?,?)",
                     (customer_id, now_ts(), amount, mode))
