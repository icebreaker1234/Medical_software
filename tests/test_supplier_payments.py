"""Supplier payments: allocation, advances, cheques, ledger and migration of the old paid flag."""
import sqlite3
from datetime import timedelta

import pytest


def _supplier_with_dues(db):
    from app import analytics
    b = analytics.supplier_bills()
    open_ = b[(b["outstanding"] > 0.01) & (b["cheque_pending"] == 0)]
    sid = int(open_.groupby("supplier_id")["outstanding"].sum().idxmax())
    return sid, open_[open_["supplier_id"] == sid].sort_values(["received_date", "purchase_id"])


def test_payment_settles_oldest_bills_first(store_db):
    db = store_db
    from app import analytics
    sid, bills = _supplier_with_dues(db)
    first = bills.iloc[0]
    db.record_supplier_payment(sid, str(db.today()), float(first["outstanding"]), "NEFT", reference="UTR1")
    after = analytics.supplier_bills(sid).set_index("purchase_id")
    assert after.loc[first["purchase_id"], "status"] == "Paid"
    assert after.loc[bills.iloc[1]["purchase_id"], "outstanding"] == pytest.approx(bills.iloc[1]["outstanding"])


def test_extra_money_is_kept_as_advance_and_ledger_reconciles(store_db):
    db = store_db
    from app import analytics
    sid, bills = _supplier_with_dues(db)
    due = float(bills["outstanding"].sum())
    db.record_supplier_payment(sid, str(db.today()), due + 1000, "UPI", reference="UTR2")
    p = analytics.supplier_payables().set_index("supplier_id").loc[sid]
    assert p["advance"] == pytest.approx(1000, abs=0.01)
    led = analytics.supplier_ledger(sid)
    # ledger balance = what we owe - advance - cheques in transit
    assert led["balance_due"].iloc[-1] == pytest.approx(p["outstanding"] - p["advance"], abs=0.05)


def test_cheque_pending_then_bounced_reopens_bill(store_db):
    db = store_db
    from app import analytics
    sid, bills = _supplier_with_dues(db)
    bill = bills.iloc[0]
    pid = db.record_supplier_payment(sid, str(db.today()), float(bill["outstanding"]), "Cheque",
                                     allocations=[(int(bill["purchase_id"]), float(bill["outstanding"]))],
                                     cheque_no="123456", cheque_date=str(db.today() + timedelta(days=3)))
    row = analytics.supplier_bills(sid).set_index("purchase_id").loc[bill["purchase_id"]]
    assert row["status"] == "Cheque pending" and row["outstanding"] == 0
    db.set_cheque_status(pid, "Bounced", note="insufficient funds")
    row = analytics.supplier_bills(sid).set_index("purchase_id").loc[bill["purchase_id"]]
    assert row["outstanding"] == pytest.approx(bill["outstanding"])
    with pytest.raises(ValueError, match="already"):
        db.set_cheque_status(pid, "Cleared")


def test_payment_validation(store_db):
    db = store_db
    sid, bills = _supplier_with_dues(db)
    with pytest.raises(ValueError, match="Cheque number"):
        db.record_supplier_payment(sid, str(db.today()), 100, "Cheque")
    with pytest.raises(ValueError, match="future"):
        db.record_supplier_payment(sid, str(db.today() + timedelta(days=1)), 100, "Cash")
    with pytest.raises(ValueError, match="only"):
        db.record_supplier_payment(sid, str(db.today()), 10**7, "Cash",
                                   allocations=[(int(bills.iloc[0]["purchase_id"]), 10**7)])
    other = int(db.q("SELECT id FROM purchases WHERE supplier_id != ? LIMIT 1", (sid,)).id[0])
    with pytest.raises(ValueError, match="does not belong"):
        db.record_supplier_payment(sid, str(db.today()), 10, "Cash", allocations=[(other, 10)])


def test_old_paid_flag_is_migrated_to_payments(tmp_path, monkeypatch):
    from app import analytics, db
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE suppliers (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, gstin TEXT, phone TEXT,
            city TEXT, lead_time_days INTEGER DEFAULT 2, credit_days INTEGER DEFAULT 30, active INTEGER DEFAULT 1);
        CREATE TABLE purchases (id INTEGER PRIMARY KEY, supplier_id INTEGER, invoice_no TEXT, order_date TEXT,
            received_date TEXT, ordered_qty INTEGER, received_qty INTEGER, taxable REAL, tax REAL, total REAL,
            paid INTEGER DEFAULT 0);
        INSERT INTO suppliers(id, name) VALUES (1, 'Old Distributor');
        INSERT INTO purchases VALUES (1, 1, 'A1', '2026-01-01', '2026-01-02', 1, 1, 100, 5, 105, 1);
        INSERT INTO purchases VALUES (2, 1, 'A2', '2026-01-05', '2026-01-06', 1, 1, 200, 10, 210, 0);
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "PHARMACY_DB", path)
    b = analytics.supplier_bills(1).set_index("invoice_no")
    assert b.loc["A1", "status"] == "Paid" and b.loc["A2", "outstanding"] == 210
