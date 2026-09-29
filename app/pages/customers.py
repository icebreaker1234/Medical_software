import urllib.parse

import streamlit as st

from app import analytics, db
from app.ui import STORE_NAME, inr, money_cols

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
    due = bal[bal["outstanding"] > 1].sort_values("outstanding", ascending=False)
    st.metric("Total outstanding", inr(due["outstanding"].sum()), f"{len(due)} customers",
              delta_color="off")
    st.dataframe(due[["name", "phone", "outstanding", "last_visit"]], hide_index=True, width="stretch",
                 column_config=money_cols(due, ["outstanding"]))
    if len(due):
        with st.form("pay"):
            who = st.selectbox("Receive payment from", due["name"] + " · " + due["phone"])
            amt = st.number_input("Amount (₹)", 1.0, 1e6, 100.0)
            mode = st.selectbox("Mode", ["Cash", "UPI", "Card"])
            if st.form_submit_button("Record payment", type="primary"):
                cid = int(due.loc[due["phone"] == who.split(" · ")[1], "id"].iloc[0])
                db.record_payment(cid, float(amt), mode)
                st.success("Payment recorded")
                st.rerun()

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
