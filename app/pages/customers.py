import urllib.parse

import streamlit as st

from app import analytics, db
from app.ui import STORE_NAME, inr, money_cols
from app.upi import STORE_UPI_ID, qr_png, upi_link
from app.whatsapp import normalize_phone, udhaar_message, wa_link

st.title("👥 Customers")
tab_list, tab_refill, tab_credit, tab_add = st.tabs(
    ["Customers", "Refill reminders", "Credit (udhaar) ledger", "Add customer"])

bal = analytics.customer_balances()

with tab_list:
    s = st.text_input("Search name / phone")
    view = bal[bal["name"].str.contains(s, case=False) | bal["phone"].str.contains(s)] if s else bal
    st.dataframe(view, hide_index=True, width="stretch", height=350,
                 column_config=money_cols(view, ["lifetime_value", "outstanding"]))
    pick = st.selectbox("Purchase history of", view["name"] + " · " + view["phone"], index=None)
    if pick:
        phone = pick.split(" · ")[1]
        hist = db.q("""SELECT s.ts, s.invoice_no, p.name AS medicine, si.qty, si.amount, s.doctor_name
                       FROM sales s JOIN sale_items si ON si.sale_id=s.id JOIN products p ON p.id=si.product_id
                       JOIN customers c ON c.id=s.customer_id WHERE c.phone=? ORDER BY s.ts DESC""", (phone,))
        st.dataframe(hist, hide_index=True, width="stretch")

with tab_refill:
    st.caption("Regular patients on chronic medicines, due within 7 days (from their own buying interval).")
    rf = analytics.refill_due()
    if rf.empty:
        st.info("No refills due.")
    else:
        def wa(r):
            msg = (f"Namaste {r['customer'].split()[0]} ji, your {r['medicine']} may be due for refill. "
                   f"Reply to reserve it at {STORE_NAME}.")
            return f"https://wa.me/91{r['phone']}?text={urllib.parse.quote(msg)}"
        rf["whatsapp"] = rf.apply(wa, axis=1)
        st.dataframe(rf, hide_index=True, width="stretch", column_config={
            "whatsapp": st.column_config.LinkColumn("Remind", display_text="Send WhatsApp")})
        st.caption("Reminders are opt-in messages the pharmacist sends manually - "
                   "get the customer's consent before messaging.")

with tab_credit:
    led = analytics.udhaar_ledger()
    if led.empty:
        st.success("No pending credit (udhaar).")
    else:
        c1, c2, c3 = st.columns(3)
        c1.metric("Total outstanding", inr(led["outstanding"].sum()))
        c2.metric("Customers with dues", len(led))
        c3.metric("Pending > 30 days", inr(led.loc[led["days_pending"] > 30, "outstanding"].sum()))
        ages = st.multiselect("Ageing", ["0-15 days", "16-30 days", "31-60 days", "60+ days"],
                              placeholder="All ageing buckets")
        view = led[led["ageing"].isin(ages)] if ages else led

        def reminder(r):
            phone = normalize_phone(r["phone"])
            if not phone:
                return None
            msg = udhaar_message(r["name"], r["outstanding"], STORE_NAME, STORE_UPI_ID, r["last_credit_bill"])
            return wa_link(phone, msg)
        view = view.assign(remind=view.apply(reminder, axis=1))
        st.dataframe(
            view[["name", "phone", "outstanding", "credit_bills", "last_credit_bill", "last_payment_date",
                  "days_pending", "ageing", "remind"]],
            hide_index=True, width="stretch",
            column_config={**money_cols(view, ["outstanding"]),
                           "remind": st.column_config.LinkColumn("Reminder", display_text="📲 WhatsApp")})
        st.caption("Click 📲 WhatsApp to open a polite reminder with the pending amount and your UPI ID. "
                   "The pharmacist reviews and sends it.")

        st.markdown("**Receive payment**")
        who = st.selectbox("Customer", led["name"] + " · " + led["phone"])
        cid = int(led.loc[led["phone"] == who.split(" · ")[1], "id"].iloc[0])
        due_amt = float(led.loc[led["id"] == cid, "outstanding"].iloc[0])
        with st.form(f"pay_{cid}"):
            amt = st.number_input("Amount (₹)", 1.0, 1e6, round(due_amt, 2))
            mode = st.selectbox("Mode", ["UPI", "Cash", "Card"])
            show_qr = st.checkbox("Show UPI QR for this amount", value=True)
            if st.form_submit_button("Record payment", type="primary"):
                db.record_payment(cid, float(amt), mode)
                st.success(f"Payment of {inr(amt, 2)} recorded")
                st.rerun()
        if show_qr:
            link = upi_link(float(amt), f"Udhaar {who.split(' · ')[0]}", payee=STORE_NAME)
            q1, q2 = st.columns([1, 3])
            q1.image(qr_png(link), width=170)
            q2.markdown(f"Customer scans to pay **{inr(amt, 2)}** to `{STORE_UPI_ID}`, "
                        "then click *Record payment* once it is received.")

with tab_add:
    with st.form("cust"):
        c1, c2 = st.columns(2)
        name = c1.text_input("Name")
        phone = c2.text_input("Mobile")
        addr = c1.text_input("Address")
        doc = c2.text_input("Regular doctor")
        consent = st.checkbox("Customer consents to storing details and receiving refill reminders")
        if st.form_submit_button("Add", type="primary") and name and phone:
            if not consent:
                st.error("Consent is required to store customer details.")
            else:
                try:
                    db.add_customer(name, phone, addr, doc)
                    st.success("Customer added")
                except Exception as e:
                    st.error(f"Could not add (duplicate phone?): {e}")
