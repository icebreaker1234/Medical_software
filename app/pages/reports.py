from datetime import timedelta

import plotly.express as px
import streamlit as st

from app import analytics, db
from app.ui import inr, money_cols

st.title("📊 Reports & GST")
c1, c2 = st.columns(2)
start = c1.date_input("From", db.today() - timedelta(days=30))
end = c2.date_input("To", db.today())
lines = db.sales_lines(start, end)

t1, t2, t3, t4, t5 = st.tabs(["Sales summary", "Profit by medicine", "GST summary",
                              "Schedule H1 register", "Sales register"])

with t1:
    bills = lines.drop_duplicates("sale_id")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Sales", inr(lines["amount"].sum()))
    c2.metric("Bills", f"{len(bills):,}")
    c3.metric("Gross profit", inr((lines["taxable"] - lines["cost"]).sum()))
    c4.metric("Avg bill", inr(lines["amount"].sum() / max(len(bills), 1)))
    daily = lines.groupby("day", as_index=False)["amount"].sum()
    st.plotly_chart(px.line(daily, x="day", y="amount", markers=True, title="Daily sales",
                            labels={"amount": "₹", "day": ""}), width="stretch")
    cat = lines.groupby("category", as_index=False)["amount"].sum().sort_values("amount")
    st.plotly_chart(px.bar(cat, x="amount", y="category", orientation="h", title="Sales by category",
                           labels={"amount": "₹", "category": ""}), width="stretch")

with t2:
    prof = (lines.assign(profit=lines["taxable"] - lines["cost"])
                 .groupby("product", as_index=False)
                 .agg(units=("qty", "sum"), sales=("amount", "sum"), profit=("profit", "sum")))
    prof["margin_%"] = (prof["profit"] / prof["sales"] * 100).round(1)
    st.dataframe(prof.sort_values("profit", ascending=False), hide_index=True, width="stretch",
                 column_config=money_cols(prof, ["sales", "profit"]))

with t3:
    g = analytics.gst_summary(start, end)
    st.dataframe(g, hide_index=True, width="stretch",
                 column_config=money_cols(g, ["taxable", "cgst", "sgst", "invoice_value"]))
    st.caption("Outward supplies, intra-state (CGST + SGST). Reconcile with your CA before filing GSTR-1/3B.")
    st.download_button("⬇ GST summary CSV", g.to_csv(index=False), "gst_summary.csv")

with t4:
    h1 = analytics.h1_register(start, end)
    st.caption("Schedule H1 register: patient, prescriber, drug, quantity, date. "
               "Keep records as required by the Drugs Rules (demo classification - verify).")
    st.dataframe(h1, hide_index=True, width="stretch")
    st.download_button("⬇ H1 register CSV", h1.to_csv(index=False), "h1_register.csv")

with t5:
    reg = lines[["invoice_no", "ts", "product", "batch_no", "expiry", "qty", "mrp", "disc_pct",
                 "gst_rate", "taxable", "tax", "amount", "payment_mode"]].sort_values("ts", ascending=False)
    st.dataframe(reg, hide_index=True, width="stretch", height=450)
    st.download_button("⬇ Sales register CSV", reg.to_csv(index=False), "sales_register.csv")
