"""Import connector: messy real-world exports -> clean store data."""
import pytest

from app import importer as I


@pytest.fixture()
def empty_db(tmp_path, monkeypatch):
    from app import db
    monkeypatch.setattr(db, "PHARMACY_DB", tmp_path / "empty.db")
    db.connect().close()
    return db


def _load(data: bytes, name: str, dataset=None, options=None):
    raw = next(iter(I.read_file(data, name).values()))
    df = I.frame(raw, I.find_header(raw))
    ds = dataset or I.detect_dataset(df.columns)[0]
    return I.validate(df, ds, I.auto_map(df.columns, ds), options or {}), df


def test_marg_style_stock_report(empty_db):
    db = empty_db
    data = I.sample_stock_export(25)
    checked, df = _load(data, "stock.xlsx")
    assert checked.dataset == "stock" and checked.errors == 0
    assert not df.astype(str).apply(lambda c: c.str.contains("TOTAL")).any().any()   # total line dropped
    res = I.apply(checked, "stock.xlsx", I.file_hash(data))
    p = db.products_df()
    assert "GRAND TOTAL" not in set(p["name"]) and len(p) == 8
    assert p.set_index("name").loc["Dolo 650", "comp_key"] == "paracetamol 650mg | tablet | IR"
    total_qty = sum(r["qty"] for r in checked.rows)
    assert db.q("SELECT SUM(qty) s FROM batches").s[0] == total_qty
    bad = db.q("""SELECT COUNT(*) n FROM batches b WHERE qty !=
                  (SELECT COALESCE(SUM(qty_change),0) FROM stock_movements m WHERE m.batch_id=b.id)""")
    assert bad.n[0] == 0                                          # opening stock is in the audit ledger
    again = I.apply(checked, "stock.xlsx", I.file_hash(data))    # same file twice: no duplicate stock
    assert again["skipped"] == res["created"] - 8 and db.q("SELECT SUM(qty) s FROM batches").s[0] == total_qty
    assert len(I.already_imported(I.file_hash(data))) == 2


def test_suppliers_with_cr_dr_balances(empty_db):
    from app import analytics
    csv = ("Party Name,GSTIN No,Mobile No,Station,Cr Days,Closing Balance\n"
           "Shree Balaji Pharma,08AAACB1234F1Z5,+91 98290 12345,Jaipur,30,\"25,000.00 Cr\"\n"
           "Rajasthan Medical Agencies,BADGSTIN,12345,Ajmer,21,1500 Dr\n"
           ",,,,,\n")
    checked, _ = _load(csv.encode(), "parties.csv")
    assert checked.dataset == "suppliers" and len(checked.rows) == 2
    msgs = " ".join(checked.problems["message"])
    assert "GSTIN" in msgs and "not a valid 10-digit" in msgs and "supplier owes us" in msgs
    I.apply(checked, "parties.csv", "h1")
    pay = analytics.supplier_payables().set_index("supplier")
    assert pay.loc["Shree Balaji Pharma", "outstanding"] == pytest.approx(25000)
    assert pay.loc["Rajasthan Medical Agencies", "outstanding"] == 0
    assert empty_db.q("SELECT phone FROM suppliers WHERE name='Shree Balaji Pharma'").phone[0] == "9829012345"


def test_customers_with_udhaar(empty_db):
    from app import analytics
    csv = "Customer Name,Mobile,Doctor,Outstanding\nRamesh Sharma,9876543210,Dr Mehta,450.50\nSunita,,,\n"
    checked, _ = _load(csv.encode(), "cust.csv")
    I.apply(checked, "cust.csv", "h2")
    led = analytics.udhaar_ledger()
    assert led.set_index("name").loc["Ramesh Sharma", "outstanding"] == pytest.approx(450.50)
    I.apply(checked, "cust.csv", "h2")                            # re-import: no duplicate customer
    assert len(empty_db.q("SELECT * FROM customers WHERE phone='9876543210'")) == 1


def test_sales_register_feeds_history(empty_db):
    db = empty_db
    csv = ("Bill No,Date,Party Name,Mobile,Item Name,Qty,Rate,Disc%,Amount,Mode\n"
           "S-101,05/08/2026,Ramesh,9876543210,Dolo 650,2,33,10,59.40,Cash\n"
           "S-101,05/08/2026,Ramesh,9876543210,Pan 40,1,160,0,160,Cash\n"
           "S-102,31/08/2026,Walk-in,,Dolo 650,1,33,0,33,UPI\n"
           "S-103,01/01/2099,Future,,Dolo 650,1,33,0,33,Cash\n")
    checked, _ = _load(csv.encode(), "sales.csv")
    assert checked.dataset == "sales" and checked.counts["bills"] == 2 and checked.errors == 1   # future bill
    I.apply(checked, "sales.csv", "h3")
    s = db.q("SELECT invoice_no, ts, total, payment_mode FROM sales ORDER BY ts")
    assert list(s["invoice_no"]) == ["OLD/2026-08-05/S-101", "OLD/2026-08-31/S-102"]
    assert s["ts"].iloc[0].startswith("2026-08-05")                 # 05/08 read as 5 August (Indian format)
    assert s["total"].iloc[0] == pytest.approx(219.40) and s["payment_mode"].iloc[1] == "UPI"
    assert db.q("SELECT SUM(qty) q FROM sale_items").q[0] == 4
    assert db.q("SELECT COALESCE(SUM(qty),0) q FROM batches").q[0] == 0   # history never changes stock


def test_purchase_register_marked_paid(empty_db):
    from app import analytics
    csv = ("Date,Bill No,Supplier,Item,Qty,Free,Rate,GST%,Amount\n"
           "02/09/2026,B-9,Balaji Pharma,Dolo 650,100,10,20,5,2100\n"
           "02/09/2026,B-9,Balaji Pharma,Pan 40,50,0,90,5,4725\n")
    checked, _ = _load(csv.encode(), "pur.csv")
    I.apply(checked, "pur.csv", "h4", options={"purchases_paid": True})
    b = analytics.supplier_bills()
    assert len(b) == 1 and b["total"].iloc[0] == pytest.approx(6825) and b["status"].iloc[0] == "Paid"


def test_all_or_nothing(empty_db, monkeypatch):
    db = empty_db
    data = I.sample_stock_export(10)
    checked, _ = _load(data, "stock.xlsx")

    def boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(I, "_save_stock", boom)
    with pytest.raises(RuntimeError):
        I.apply(checked, "stock.xlsx", I.file_hash(data))
    assert db.is_empty() and db.q("SELECT COUNT(*) n FROM imports").n[0] == 0


@pytest.mark.parametrize("value,expected", [
    ("12/27", "2027-12-31"), ("Dec-27", "2027-12-31"), ("DEC 2027", "2027-12-31"), ("2028-02", "2028-02-29"),
    ("31/12/2027", "2027-12-31"), ("1227", "2027-12-31"), ("13/27", None), ("", None)])
def test_expiry_formats(value, expected):
    assert I.expiry(value) == expected


def test_small_parsers():
    assert I.phone("+91-98290 12345") == "9829012345" and I.phone("12345") is None
    assert I.num("₹1,234.50") == 1234.5 and I.num("(20)") == -20
    assert I.balance("5,000 Dr", "cr") == -5000 and I.balance("5,000 Cr", "cr") == 5000
    assert I.gst_rate("12%") == 12 and I.gst_rate(0.05) == 5 and I.gst_rate("7") is None
    assert I.schedule("Sch H1") == "H1" and I.pay_mode("CREDIT") == "Credit" and I.pay_mode("GPay") == "UPI"


def test_reports_work_with_stock_but_no_sales_yet(empty_db):
    """The state right after importing a stock report: every report must still work."""
    from app import analytics
    data = I.sample_stock_export(25)
    checked, _ = _load(data, "stock.xlsx")
    I.apply(checked, "stock.xlsx", I.file_hash(data))
    st_ = analytics.stock_status()
    assert (st_["days_cover"] == float("inf")).all()
    analytics.dead_stock()
    analytics.expiry_risk()
    analytics.reorder_suggestions(None)
    analytics.kpis()
    assert analytics.expiry_buckets()["value"].sum() > 0
