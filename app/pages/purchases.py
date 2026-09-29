import hashlib
import io
import random
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from app import db
from app.ocr import OCRUnavailable, parse_bill, run_ocr, sample_invoice
from app.ui import inr, money_cols

st.title("🚚 Purchases")
tab_new, tab_scan, tab_hist, tab_ret = st.tabs(["New purchase (GRN)", "📷 Scan bill (OCR)",
                                                "Purchase history", "Expiry / breakage returns & claims"])

products = db.products_df()
suppliers = db.q("SELECT id, name FROM suppliers WHERE active=1")

with tab_new:
    c1, c2, c3 = st.columns(3)
    sup = c1.selectbox("Supplier", suppliers["name"])
    inv_no = c2.text_input("Supplier invoice no.")
    order_date = c3.date_input("Order date", db.today() - timedelta(days=1))
    st.caption("Enter each line from the distributor bill. Free (scheme) quantity lowers the effective cost.")
    template = pd.DataFrame([{"medicine": None, "batch_no": "", "expiry": db.today() + timedelta(days=540),
                              "qty": 10, "free_qty": 0, "rate": 0.0, "mrp": 0.0, "gst_rate": 5.0}])
    lines = st.data_editor(
        template, num_rows="dynamic", hide_index=True, width="stretch", key="grn",
        column_config={
            "medicine": st.column_config.SelectboxColumn("Medicine", options=list(products["name"]),
                                                         required=True, width="large"),
            "expiry": st.column_config.DateColumn("Expiry", min_value=db.today()),
            "rate": st.column_config.NumberColumn("Purchase rate (ex-GST)", format="₹%.2f"),
            "mrp": st.column_config.NumberColumn("MRP", format="₹%.2f"),
            "gst_rate": st.column_config.SelectboxColumn("GST %", options=[0.0, 5.0, 12.0, 18.0]),
        })
    valid = lines.dropna(subset=["medicine"])
    valid = valid[(valid["qty"] > 0) & (valid["rate"] > 0) & (valid["batch_no"].str.len() > 0)]
    if len(valid):
        taxable = (valid["qty"] * valid["rate"]).sum()
        tax = (valid["qty"] * valid["rate"] * valid["gst_rate"] / 100).sum()
        c1, c2, c3 = st.columns(3)
        c1.metric("Taxable", inr(taxable, 2))
        c2.metric("GST (input credit)", inr(tax, 2))
        c3.metric("Bill total", inr(taxable + tax, 2))
        margin = (valid["mrp"] / (1 + valid["gst_rate"] / 100) - valid["rate"]) / (
            valid["mrp"] / (1 + valid["gst_rate"] / 100))
        if (margin < 0.1).any():
            st.warning("Some lines give less than 10% margin on MRP - check the rate / scheme.")
    if st.button("Save purchase & add stock", type="primary", disabled=not len(valid) or not inv_no):
        name_to_id = dict(zip(products["name"], products["id"]))
        try:
            pid = db.create_purchase(
                int(suppliers.loc[suppliers.name == sup, "id"].iloc[0]), inv_no,
                [{"product_id": int(name_to_id[r["medicine"]]), "batch_no": r["batch_no"].strip().upper(),
                  "expiry": str(pd.to_datetime(r["expiry"]).date()), "qty": int(r["qty"]),
                  "free_qty": int(r["free_qty"]), "rate": float(r["rate"]), "mrp": float(r["mrp"]),
                  "gst_rate": float(r["gst_rate"])} for _, r in valid.iterrows()],
                order_date=str(order_date))
            st.success(f"Purchase #{pid} saved and stock updated.")
            st.session_state.pop("grn", None)
        except ValueError as e:
            st.error(str(e))


@st.cache_data(show_spinner=False)
def _ocr_cached(img_bytes: bytes) -> str:
    from PIL import Image
    return run_ocr(Image.open(io.BytesIO(img_bytes)))


def _demo_bill() -> bytes:
    """A realistic distributor bill made from random catalogue items (for demos)."""
    rnd = random.Random()
    rows = products[products["mrp"] > 0].sample(rnd.randint(3, 6), random_state=rnd.randint(0, 9999))
    items = []
    for _, r in rows.iterrows():
        rate = round(r["mrp"] / (1 + r["gst_rate"] / 100) * rnd.uniform(0.72, 0.8), 2)
        exp = db.today() + timedelta(days=rnd.randint(300, 900))
        items.append({"name": r["name"], "batch": f"{r['name'][:2].upper()}{rnd.randint(1000, 9999)}X",
                      "expiry": f"{exp:%m/%y}", "qty": rnd.choice([10, 20, 30, 50]),
                      "free": rnd.choice([0, 0, 1, 2]), "mrp": float(r["mrp"]), "rate": rate,
                      "gst": float(r["gst_rate"])})
    img = sample_invoice(items, rnd.choice(list(suppliers["name"])),
                         f"SB/26/{rnd.randint(10000, 99999)}", db.today() - timedelta(days=1))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


with tab_scan:
    st.caption("Photograph or upload the distributor's bill. The app reads it with OCR, matches each line "
               "to your medicine list and fills the purchase entry. **Check every line before saving** - "
               "OCR can misread batch numbers and prices.")
    c1, c2 = st.columns([3, 1])
    up = c1.file_uploader("Bill photo / scan (JPG or PNG)", type=["jpg", "jpeg", "png"])
    c2.write("")
    if c2.button("Try a sample bill", width="stretch"):
        st.session_state.ocr_img = _demo_bill()
        st.session_state.pop("ocr_grid", None)
    if up is not None:
        data = up.getvalue()
        if hashlib.md5(data).hexdigest() != st.session_state.get("ocr_img_hash"):
            st.session_state.ocr_img = data
            st.session_state.ocr_img_hash = hashlib.md5(data).hexdigest()
            st.session_state.pop("ocr_grid", None)
    img_bytes = st.session_state.get("ocr_img")

    text = None
    if img_bytes:
        try:
            with st.spinner("Reading the bill..."):
                text = _ocr_cached(img_bytes)
        except OCRUnavailable as e:
            st.error(str(e))
    if img_bytes and text is not None:
        left, right = st.columns([2, 3])
        left.image(img_bytes, caption="Bill image", width="stretch")
        with right:
            bill = parse_bill(text, list(products["name"]), list(suppliers["name"]))
            sup_names = list(suppliers["name"])
            sup = st.selectbox("Supplier", sup_names,
                               index=sup_names.index(bill.supplier) if bill.supplier in sup_names else 0,
                               key=f"ocr_sup_{hash(img_bytes)}")
            inv_no = st.text_input("Supplier invoice no.", value=bill.invoice_no or "",
                                   key=f"ocr_inv_{hash(img_bytes)}")
            found = len(bill.lines)
            matched = sum(1 for l in bill.lines if l.medicine)
            st.metric("Lines read", f"{found}", f"{matched} matched to your medicine list", delta_color="off")
            with st.expander("Raw OCR text"):
                st.text(text)

        if not bill.lines:
            st.warning("No item lines found. Try a sharper, straight photo in good light, or enter the bill manually.")
        else:
            grid = pd.DataFrame([{
                "check": "OK" if (l.medicine and l.match_score >= 0.8 and l.rate > 0) else "⚠ check",
                "medicine": l.medicine, "read_as": l.product_text, "match_%": int(l.match_score * 100),
                "batch_no": l.batch_no, "expiry": pd.to_datetime(l.expiry).date() if l.expiry else None,
                "qty": l.qty, "free_qty": l.free_qty, "rate": l.rate, "mrp": l.mrp, "gst_rate": l.gst_rate,
            } for l in bill.lines])
            st.markdown("**Review and correct, then save**")
            edited = st.data_editor(
                grid, hide_index=True, width="stretch", num_rows="dynamic", key=f"ocr_grid_{hash(img_bytes)}",
                disabled=["check", "read_as", "match_%"],
                column_config={
                    "medicine": st.column_config.SelectboxColumn("Medicine", options=list(products["name"]),
                                                                 width="large"),
                    "read_as": st.column_config.TextColumn("OCR read"),
                    "match_%": st.column_config.ProgressColumn("Match", min_value=0, max_value=100, format="%d%%"),
                    "expiry": st.column_config.DateColumn("Expiry", min_value=db.today()),
                    "rate": st.column_config.NumberColumn("Rate (ex-GST)", format="₹%.2f"),
                    "mrp": st.column_config.NumberColumn("MRP", format="₹%.2f"),
                    "gst_rate": st.column_config.SelectboxColumn("GST %", options=[0.0, 5.0, 12.0, 18.0]),
                })
            problems = []
            for i, r in edited.iterrows():
                if not r["medicine"]:
                    problems.append(f"Row {i + 1}: choose the medicine")
                if not str(r["batch_no"] or "").strip():
                    problems.append(f"Row {i + 1}: batch number missing")
                if pd.isna(r["expiry"]) or pd.to_datetime(r["expiry"]).date() <= db.today():
                    problems.append(f"Row {i + 1}: expiry missing or past")
                if not r["qty"] or r["qty"] <= 0 or not r["rate"] or r["rate"] <= 0:
                    problems.append(f"Row {i + 1}: qty and rate must be > 0")
                elif r["mrp"] and r["rate"] >= r["mrp"]:
                    problems.append(f"Row {i + 1}: rate is not below MRP - check the numbers")
            total = float((edited["qty"] * edited["rate"] * (1 + edited["gst_rate"] / 100)).sum())
            st.metric("Bill total (calculated)", inr(total, 2))
            for pmsg in problems[:6]:
                st.warning(pmsg)
            confirmed = st.checkbox("I have checked every line against the paper bill")
            if st.button("Save purchase & add stock", type="primary",
                         disabled=bool(problems) or not confirmed or not inv_no, key="ocr_save"):
                name_to_id = dict(zip(products["name"], products["id"]))
                try:
                    pid = db.create_purchase(
                        int(suppliers.loc[suppliers.name == sup, "id"].iloc[0]), inv_no,
                        [{"product_id": int(name_to_id[r["medicine"]]),
                          "batch_no": str(r["batch_no"]).strip().upper(),
                          "expiry": str(pd.to_datetime(r["expiry"]).date()), "qty": int(r["qty"]),
                          "free_qty": int(r["free_qty"] or 0), "rate": float(r["rate"]),
                          "mrp": float(r["mrp"]), "gst_rate": float(r["gst_rate"])}
                         for _, r in edited.iterrows()],
                        order_date=bill.bill_date)
                    st.success(f"Purchase #{pid} saved from scanned bill - stock updated.")
                    st.session_state.pop("ocr_img", None)
                except ValueError as e:
                    st.error(str(e))

with tab_hist:
    h = db.q("""SELECT pu.id, pu.received_date, s.name AS supplier, pu.invoice_no, pu.ordered_qty,
                       pu.received_qty, pu.taxable, pu.tax, pu.total,
                       CASE WHEN pu.paid=1 THEN 'Paid' ELSE 'Due' END AS payment
                FROM purchases pu JOIN suppliers s ON s.id=pu.supplier_id
                ORDER BY pu.received_date DESC, pu.id DESC""")
    c1, c2 = st.columns(2)
    c1.metric("Purchases - last 30 days",
              inr(h.loc[pd.to_datetime(h.received_date) > pd.Timestamp(db.today() - timedelta(days=30)),
                        "total"].sum()))
    c2.metric("Payable to suppliers", inr(h.loc[h.payment == "Due", "total"].sum()))
    st.dataframe(h, hide_index=True, width="stretch", height=450,
                 column_config=money_cols(h, ["taxable", "tax", "total"]))

with tab_ret:
    st.caption("Return expired / near-expiry / damaged stock to the supplier and track the credit note. "
               "Whether a supplier accepts returns depends on your trade terms.")
    b = db.batches_df()
    b = b[b["days_to_expiry"] <= 120]
    opts = {f"{r['product']} · {r['batch_no']} · exp {r['expiry']} · qty {r['qty']} · {r['supplier']}":
            int(r["batch_id"]) for _, r in b.iterrows()}
    with st.form("sret"):
        sel = st.selectbox("Batch (expired or expiring within 120 days)", list(opts))
        qty = st.number_input("Qty to return", 1, 10000, 1)
        reason = st.selectbox("Reason", ["Expired", "Near expiry", "Breakage / damage", "Wrong supply"])
        if st.form_submit_button("Create return", type="primary") and sel:
            try:
                rid = db.supplier_return(opts[sel], int(qty), reason)
                st.success(f"Return #{rid} created - credit note pending")
            except ValueError as e:
                st.error(str(e))
    claims = db.q("""SELECT r.id, r.ts, s.name AS supplier, p.name AS product, b.batch_no, r.qty,
                            r.value, r.reason, r.credit_note_status, r.credit_note_no,
                            CAST(julianday('now','localtime') - julianday(r.ts) AS INT) AS days_pending
                     FROM supplier_returns r JOIN suppliers s ON s.id=r.supplier_id
                     JOIN batches b ON b.id=r.batch_id JOIN products p ON p.id=b.product_id
                     ORDER BY r.ts DESC""")
    pending = claims[claims.credit_note_status == "Pending"]
    st.metric("Claims pending with suppliers", inr(pending["value"].sum()), f"{len(pending)} returns",
              delta_color="off")
    st.dataframe(claims, hide_index=True, width="stretch", column_config=money_cols(claims, ["value"]))
    if len(pending):
        with st.form("cn"):
            rsel = st.selectbox("Mark credit note received", pending["id"].astype(str) + " · " +
                                pending["supplier"] + " · " + pending["product"])
            cn = st.text_input("Credit note no.")
            if st.form_submit_button("Update") and cn:
                db.settle_credit_note(int(rsel.split(" · ")[0]), cn)
                st.success("Updated")
                st.rerun()
