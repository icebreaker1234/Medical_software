import sqlite3

import pytest


def _product(db, name):
    return int(db.q("SELECT id FROM products WHERE name=?", (name,)).id[0])


def test_ledger_reconciles_with_batch_stock(store_db):
    bad = store_db.q("""SELECT COUNT(*) n FROM batches b WHERE qty !=
                        (SELECT COALESCE(SUM(qty_change),0) FROM stock_movements m WHERE m.batch_id=b.id)""")
    assert bad.n[0] == 0


def test_sale_uses_fefo_and_reduces_stock(store_db):
    db = store_db
    pid = _product(db, "Paracetamol 650mg Tab")
    before = db.fefo_batches(pid)
    first = before.iloc[0]
    inv = db.create_sale([{"product_id": pid, "qty": 1, "disc_pct": 10}], "Cash")
    assert inv["lines"][0]["batch"] == first["batch_no"]          # earliest expiry first
    after = db.fefo_batches(pid)
    assert after["qty"].sum() == before["qty"].sum() - 1
    t = inv["totals"]
    assert abs(t["taxable"] + t["tax"] - t["total"]) < 0.02      # GST back-calculation adds up


def test_prescription_rules(store_db):
    db = store_db
    h1 = _product(db, "Alprazolam 0.25mg Tab")
    with pytest.raises(ValueError, match="doctor"):
        db.create_sale([{"product_id": h1, "qty": 1}], "Cash")
    with pytest.raises(ValueError, match="patient"):
        db.create_sale([{"product_id": h1, "qty": 1}], "Cash", doctor_name="Dr X")
    inv = db.create_sale([{"product_id": h1, "qty": 1}], "Cash", doctor_name="Dr X",
                         patient_name="Test Patient")
    assert inv["invoice_no"].startswith("INV/")


def test_insufficient_stock_rolls_back(store_db):
    db = store_db
    pid = _product(db, "Paracetamol 500mg Tab")
    n_sales = db.q("SELECT COUNT(*) n FROM sales").n[0]
    with pytest.raises(ValueError, match="Insufficient"):
        db.create_sale([{"product_id": pid, "qty": 1}, {"product_id": pid + 0, "qty": 10 ** 6}], "Cash")
    assert db.q("SELECT COUNT(*) n FROM sales").n[0] == n_sales     # nothing half-saved


def test_expired_stock_is_never_sold(store_db):
    db = store_db
    pid = _product(db, "Knee Support (L)")
    assert db.fefo_batches(pid)["expiry"].min() > str(db.today())


def test_ledger_is_append_only(store_db):
    conn = store_db.connect()
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM stock_movements WHERE id=1")
    conn.close()


def test_purchase_and_returns(store_db):
    db = store_db
    pid = _product(db, "ORS Sachet 21g")
    stock0 = db.fefo_batches(pid)["qty"].sum()
    db.create_purchase(1, "TEST/1", [{"product_id": pid, "batch_no": "TST001", "expiry": "2030-01-31",
                                      "qty": 20, "free_qty": 2, "rate": 10, "mrp": 22, "gst_rate": 5}])
    assert db.fefo_batches(pid)["qty"].sum() == stock0 + 22
    bid = int(db.q("SELECT id FROM batches WHERE batch_no='TST001'").id[0])
    db.supplier_return(bid, 2, "Breakage / damage")
    assert db.fefo_batches(pid)["qty"].sum() == stock0 + 20
    with pytest.raises(ValueError):
        db.supplier_return(bid, 10_000, "Expired")


def test_whatsapp_phone_and_message():
    from app.whatsapp import bill_message, normalize_phone, wa_link
    assert normalize_phone("9876543210") == "919876543210"
    assert normalize_phone("+91 98765-43210") == "919876543210"
    assert normalize_phone("09876543210") == "919876543210"
    assert normalize_phone("12345") is None and normalize_phone("5876543210") is None
    inv = {"invoice_no": "INV/2627/000001", "ts": "2026-09-29 10:00:00",
           "lines": [{"product": "Paracetamol 650mg Tab", "qty": 2, "amount": 66.0}],
           "totals": {"gross": 66.0, "disc": 0.0, "total": 66.0}}
    msg = bill_message(inv, "Test Store", "Ramesh")
    assert "INV/2627/000001" in msg and "Rs 66.00" in msg and "Paracetamol" in msg
    assert "Paracetamol" not in bill_message(inv, "Test Store", include_items=False)
    assert wa_link("919876543210", msg).startswith("https://wa.me/919876543210?text=")
