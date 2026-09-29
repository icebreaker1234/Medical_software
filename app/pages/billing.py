import html

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from app import db
from app.ui import STORE_DL, STORE_GSTIN, STORE_NAME, inr
from app.upi import STORE_UPI_ID, is_demo_upi, qr_data_uri, qr_png, upi_link
from app.whatsapp import bill_message, normalize_phone, wa_link

st.title("🧾 Billing (POS)")

if "cart" not in st.session_state:
    st.session_state.cart = []
if "last_invoice" not in st.session_state:
    st.session_state.last_invoice = None

products = db.products_df()
by_id = products.set_index("id")


def reset_cart(items: list[dict]):
    """Replace the cart and reset the editor widget (avoids re-applying stale edits)."""
    st.session_state.cart = items
    st.session_state.cart_view = items
    st.session_state.pop("cart_editor", None)


def add_to_cart(pid: int, qty: int = 1):
    p = by_id.loc[pid]
    items = [dict(i) for i in st.session_state.get("cart_view", st.session_state.cart)]
    for item in items:
        if item["product_id"] == pid:
            item["qty"] += qty
            reset_cart(items)
            return
    fefo = db.fefo_batches(pid)
    items.append({
        "product_id": int(pid), "medicine": p["name"], "schedule": p["schedule"],
        "qty": int(qty), "disc_pct": float(st.session_state.get("bill_disc", 0.0)),
        "mrp": float(fefo["mrp"].iloc[0]) if len(fefo) else float(p["mrp"]),
        "in_stock": int(p["stock"]), "rack": p["rack"],
        "batch (FEFO)": f"{fefo['batch_no'].iloc[0]} · exp {fefo['expiry'].iloc[0][:7]}" if len(fefo) else "-",
    })
    reset_cart(items)


def invoice_html(inv: dict, patient: str, doctor: str | None, mode: str, qr_uri: str | None = None) -> str:
    rows = "".join(
        f"<tr><td>{html.escape(l['product'])}{' <b>(' + l['schedule'] + ')</b>' if l['schedule'] != 'OTC' else ''}"
        f"</td><td>{l['hsn']}</td><td>{l['batch']}</td><td>{l['expiry'][:7]}</td><td>{l['qty']}</td>"
        f"<td>{l['mrp']:.2f}</td><td>{l['disc_pct']:.0f}%</td><td>{l['gst_rate']:.0f}%</td>"
        f"<td style='text-align:right'>{l['amount']:.2f}</td></tr>" for l in inv["lines"])
    t = inv["totals"]
    return f"""
    <div style="font-family:Arial;font-size:12px;border:1px solid #999;padding:12px;max-width:760px;
                background:#fff;color:#111">
      <div style="text-align:center"><b style="font-size:16px">{STORE_NAME}</b><br>
      Jaipur, Rajasthan · GSTIN {STORE_GSTIN} · DL No. {STORE_DL}<br><b>TAX INVOICE</b></div>
      <hr><table style="width:100%"><tr><td>Invoice: <b>{inv['invoice_no']}</b><br>Date: {inv['ts']}</td>
      <td style="text-align:right">Patient: {html.escape(patient or 'Walk-in')}<br>
      Prescriber: {html.escape(doctor or '-')}<br>Payment: {mode}</td></tr></table>
      <table style="width:100%;border-collapse:collapse" border="1" cellpadding="4">
      <tr style="background:#eee"><th>Item</th><th>HSN</th><th>Batch</th><th>Exp</th><th>Qty</th>
      <th>MRP</th><th>Disc</th><th>GST</th><th>Amount</th></tr>{rows}</table>
      <table style="width:100%;margin-top:6px"><tr><td>Taxable value: ₹{t['taxable']:.2f}<br>
      CGST: ₹{t['tax'] / 2:.2f} · SGST: ₹{t['tax'] / 2:.2f}</td>
      <td style="text-align:right">MRP total: ₹{t['gross']:.2f}<br>Discount: -₹{t['disc']:.2f}<br>
      <b style="font-size:15px">Net payable: ₹{t['total']:.2f}</b></td></tr></table>
      {f'<div style="margin-top:8px;text-align:center"><img src="{qr_uri}" width="120"><br>'
       f'<b>Scan to pay ₹{t["total"]:.2f} by UPI</b> · {STORE_UPI_ID}</div>' if qr_uri else ''}
      <div style="font-size:10px;margin-top:8px">Prices are inclusive of GST. Computer-generated invoice.
      Pharmacist: ____________</div></div>"""


tab_bill, tab_return, tab_today = st.tabs(["New bill", "Sales return", "Today's bills"])

with tab_bill:
    left, right = st.columns([3, 2])
    with left:
        with st.form("scan", clear_on_submit=True, border=True):
            c1, c2, c3 = st.columns([5, 1.2, 1.3])
            code = c1.text_input("Scan barcode or type medicine name, then press Enter",
                                 placeholder="e.g. 8901000007919 or 'para 650'")
            qty = c2.number_input("Qty", 1, 500, 1)
            c3.write("")
            c3.write("")
            submitted = c3.form_submit_button("Add ⏎", type="primary", width="stretch")
        if submitted and code.strip():
            text = code.strip().lower()
            exact = products[products["barcode"] == code.strip()]
            words = text.split()
            match = exact if len(exact) else products[products["name"].str.lower().apply(
                lambda n: all(w in n for w in words))]
            if match.empty:
                st.error(f"No medicine found for '{code}'")
            else:
                p = match.iloc[0]
                if p["stock"] < qty:
                    st.warning(f"Only {int(p['stock'])} in stock for {p['name']}.")
                    alts = products[(products["generic"] == p["generic"]) & (products["id"] != p["id"])
                                    & (products["stock"] > 0)]
                    if len(alts):
                        st.info("Same-composition alternatives in stock: " +
                                ", ".join(f"{a} ({int(s)})" for a, s in zip(alts["name"], alts["stock"])))
                    if p["stock"] == 0:
                        st.stop()
                add_to_cart(int(p["id"]), int(min(qty, p["stock"])))
                if len(match) > 1:
                    st.caption("Also matched: " + ", ".join(match["name"].iloc[1:5]))

        opts = {f"{r['name']}  ·  stock {int(r['stock'])}  ·  ₹{r['mrp']:.0f}  ·  {r['rack']}": int(r["id"])
                for _, r in products.iterrows()}
        c1, c2 = st.columns([5, 1])
        pick = c1.selectbox("…or search the catalogue", list(opts), index=None,
                            placeholder="Start typing a medicine name")
        if c2.button("Add", width="stretch", disabled=pick is None):
            add_to_cart(opts[pick])
            st.rerun()

        if st.session_state.cart:
            cart_df = pd.DataFrame(st.session_state.cart)
            edited = st.data_editor(
                cart_df, hide_index=True, width="stretch", num_rows="dynamic", key="cart_editor",
                disabled=["product_id", "medicine", "schedule", "mrp", "in_stock", "rack", "batch (FEFO)"],
                column_config={"product_id": None,
                               "qty": st.column_config.NumberColumn("Qty", min_value=1, step=1),
                               "disc_pct": st.column_config.NumberColumn("Disc %", min_value=0,
                                                                         max_value=30, step=1),
                               "mrp": st.column_config.NumberColumn("MRP", format="₹%.2f")})
            st.session_state.cart_view = edited.dropna(subset=["product_id"]).to_dict("records")
        else:
            st.session_state.cart_view = []
            st.info("Cart is empty - scan a barcode or search to add medicines.")

    with right:
        cart = st.session_state.get("cart_view", [])
        needs_rx = any(c["schedule"] in ("H", "H1", "X") for c in cart)
        needs_h1 = any(c["schedule"] == "H1" for c in cart)
        custs = db.q("SELECT id, name, phone, doctor FROM customers ORDER BY name")
        ctype = st.radio("Customer", ["Walk-in", "Registered"], horizontal=True)
        customer_id, patient, default_doc, default_phone = None, "", "", ""
        if ctype == "Registered":
            copt = {f"{r['name']} · {r['phone']}": r for _, r in custs.iterrows()}
            sel = st.selectbox("Search by name / phone", list(copt), index=None)
            if sel:
                customer_id, patient, default_doc = int(copt[sel]["id"]), copt[sel]["name"], copt[sel]["doctor"]
                default_phone = copt[sel]["phone"] or ""
        phone_raw = st.text_input("Customer mobile (to send bill on WhatsApp)", value=default_phone,
                                  placeholder="10-digit mobile, e.g. 9876543210")
        if phone_raw and not normalize_phone(phone_raw):
            st.warning("Enter a valid 10-digit Indian mobile number")
        if ctype == "Walk-in" or needs_h1:
            patient = st.text_input("Patient name" + (" (required - H1 register)" if needs_h1 else ""),
                                    value=patient)
        doctor = rx = None
        if needs_rx:
            st.warning("Prescription (Schedule H/H1) medicine in cart - verify the prescription.")
            doctor = st.text_input("Prescribing doctor (required)", value=default_doc or "")
            rx = st.text_input("Rx reference / note", value="")
        mode = st.segmented_control("Payment", ["Cash", "UPI", "Card", "Credit"], default="Cash")
        bill_disc = st.number_input("Apply discount % to all lines", 0.0, 30.0, 0.0, 1.0, key="bill_disc")
        if st.button("Apply discount", disabled=not cart):
            reset_cart([dict(c, disc_pct=bill_disc) for c in cart])
            st.rerun()

        if cart:
            gross = sum(c["mrp"] * c["qty"] for c in cart)
            disc = sum(c["mrp"] * c["qty"] * c["disc_pct"] / 100 for c in cart)
            st.metric("Net payable (estimate)", inr(gross - disc, 2), f"-{inr(disc, 2)} discount",
                      delta_color="off")
        pharmacist_ok = st.checkbox("Pharmacist has checked items, batch & expiry", value=not needs_rx)
        c1, c2 = st.columns(2)
        if c1.button("💾 Save bill", type="primary", width="stretch", shortcut="Ctrl+Enter",
                     disabled=not cart or not pharmacist_ok):
            try:
                inv = db.create_sale(
                    [{"product_id": int(c["product_id"]), "qty": int(c["qty"]),
                      "disc_pct": float(c["disc_pct"])} for c in cart],
                    payment_mode=mode or "Cash", customer_id=customer_id,
                    patient_name=patient or None, doctor_name=doctor or None, rx_ref=rx or None)
                st.session_state.last_invoice = (inv, patient, doctor, mode, normalize_phone(phone_raw))
                reset_cart([])
                st.cache_data.clear()
                st.rerun()
            except ValueError as e:
                st.error(str(e))
        if c2.button("🗑 Clear", width="stretch", shortcut="Esc"):
            reset_cart([])
            st.rerun()
        st.caption("Shortcuts: Enter = add item · Ctrl+Enter = save bill · Esc = clear")

    if st.session_state.last_invoice:
        inv, patient, doctor, mode, phone = st.session_state.last_invoice
        st.success(f"Saved {inv['invoice_no']} · {inr(inv['totals']['total'], 2)}")
        if phone:
            c1, c2 = st.columns([1, 2])
            items = c2.checkbox("Include medicine names in the message", value=True)
            msg = bill_message(inv, STORE_NAME, patient, include_items=items)
            c1.link_button(f"📲 Send bill on WhatsApp (+{phone[:2]} {phone[2:]})",
                           wa_link(phone, msg), type="primary", width="stretch")
            with st.expander("Preview WhatsApp message"):
                st.text(msg)
        else:
            st.caption("Tip: enter the customer's mobile before saving to send the bill on WhatsApp.")
        # UPI QR with the exact amount - shown for UPI and Credit (pay-later) bills
        qr_uri = None
        if mode in ("UPI", "Credit") and inv["totals"]["total"] > 0:
            link = upi_link(inv["totals"]["total"], f"Bill {inv['invoice_no']}", payee=STORE_NAME)
            qr_uri = qr_data_uri(link)
            q1, q2 = st.columns([1, 3])
            q1.image(qr_png(link), width=180)
            q2.markdown(f"**Scan to pay {inr(inv['totals']['total'], 2)}**  \nUPI ID: `{STORE_UPI_ID}`  \n"
                        "Works with GPay, PhonePe, Paytm, BHIM. Amount is pre-filled.")
            if is_demo_upi():
                q2.warning("Demo UPI ID - set STORE_UPI_ID to the store's real UPI ID before use.")
        page = invoice_html(inv, patient, doctor, mode, qr_uri)
        components.html(page, height=160 + 34 * len(inv["lines"]) + (300 if qr_uri else 140), scrolling=True)
        st.download_button("⬇ Download invoice (HTML - print to PDF)", page,
                           file_name=f"{inv['invoice_no'].replace('/', '-')}.html", mime="text/html")

with tab_return:
    inv_no = st.text_input("Invoice number", placeholder="INV/2627/000123")
    if inv_no:
        lines = db.q("""SELECT si.id, p.name, b.batch_no, si.qty, si.returned_qty, si.amount
                        FROM sales s JOIN sale_items si ON si.sale_id=s.id
                        JOIN products p ON p.id=si.product_id JOIN batches b ON b.id=si.batch_id
                        WHERE s.invoice_no=?""", (inv_no.strip(),))
        if lines.empty:
            st.error("Invoice not found")
        else:
            st.dataframe(lines, hide_index=True, width="stretch")
            lopt = {f"{r['name']} · batch {r['batch_no']} · sold {r['qty']} · returned {r['returned_qty']}":
                    int(r["id"]) for _, r in lines.iterrows()}
            with st.form("ret"):
                line = st.selectbox("Line", list(lopt))
                rq = st.number_input("Return qty", 1, 500, 1)
                reason = st.selectbox("Reason", ["Wrong item", "Doctor changed prescription",
                                                 "Excess quantity", "Damaged"])
                st.caption("Returned stock goes back to the same batch; check the strip/seal first.")
                if st.form_submit_button("Process return", type="primary"):
                    try:
                        amt = db.sale_return(lopt[line], int(rq), reason)
                        st.success(f"Refund {inr(amt, 2)} · stock added back")
                    except ValueError as e:
                        st.error(str(e))

with tab_today:
    today = db.q("""SELECT invoice_no, time(ts) AS time, payment_mode, gross, discount, total,
                           ROUND(taxable-cost,2) AS profit, doctor_name
                    FROM sales WHERE date(ts)=date('now','localtime') ORDER BY ts DESC""")
    c1, c2, c3 = st.columns(3)
    c1.metric("Bills", len(today))
    c2.metric("Sales", inr(today["total"].sum()))
    c3.metric("Cash in drawer (cash bills)", inr(today.loc[today.payment_mode == "Cash", "total"].sum()))
    st.dataframe(today, hide_index=True, width="stretch")
