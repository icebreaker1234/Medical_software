import plotly.express as px
import streamlit as st

from app import analytics, db
from app.ui import money_cols

st.title("🏭 Suppliers")
perf = analytics.supplier_performance()

if perf.empty or perf["invoices"].fillna(0).sum() == 0:
    st.info("No purchase history yet - the scorecard appears once you record bills in **Purchases**. "
            "Start by adding your suppliers below.")
else:
    st.subheader("Supplier scorecard")
    st.caption("Reliability = 60% fill rate + 40% delivery speed (weights are a business choice, "
               "shown so the score is explainable). Based on your own purchase history.")
    view = perf[["name", "city", "invoices", "purchase_value", "avg_lead_days", "fill_rate",
                 "short_supply_rate", "credit_days", "payable", "returns_value", "pending_claims",
                 "reliability_score"]]
    st.dataframe(view, hide_index=True, width="stretch", column_config={
        **money_cols(view, ["purchase_value", "payable", "returns_value", "pending_claims"]),
        "avg_lead_days": st.column_config.NumberColumn("Avg delivery (days)", format="%.1f"),
        "fill_rate": st.column_config.NumberColumn("Fill rate", format="percent"),
        "short_supply_rate": st.column_config.NumberColumn("Short-supplied bills", format="percent"),
        "reliability_score": st.column_config.ProgressColumn("Reliability", min_value=0, max_value=100,
                                                             format="%d")})

    c1, c2 = st.columns(2)
    fig = px.scatter(perf, x="avg_lead_days", y="fill_rate", size="purchase_value", text="name",
                     title="Speed vs completeness (bubble = purchase value)",
                     labels={"avg_lead_days": "Avg delivery days", "fill_rate": "Fill rate"})
    fig.update_traces(textposition="top center")
    fig.update_layout(height=380)
    c1.plotly_chart(fig, width="stretch")

    prices = analytics.price_comparison()
    multi = prices.groupby("product").filter(lambda g: g["supplier"].nunique() > 1)
    with c2:
        st.markdown("**Same medicine, different suppliers - effective purchase rate (180 days)**")
        if multi.empty:
            st.info("No medicine bought from more than one supplier yet.")
        else:
            pv = multi.pivot_table(index="product", columns="supplier", values="avg_rate")
            pv["cheapest"] = pv.idxmin(axis=1)
            pv["gap_%"] = ((pv.drop(columns="cheapest").max(axis=1) / pv.drop(columns="cheapest").min(axis=1) - 1)
                           * 100).round(1)
            st.dataframe(pv.sort_values("gap_%", ascending=False), width="stretch", height=340)


st.subheader("Add supplier")
with st.form("sup"):
    c1, c2, c3 = st.columns(3)
    name = c1.text_input("Name")
    gstin = c2.text_input("GSTIN")
    phone = c3.text_input("Phone")
    city = c1.text_input("City", "Jaipur")
    lead = c2.number_input("Usual lead time (days)", 0, 30, 2)
    credit = c3.number_input("Credit days", 0, 120, 30)
    if st.form_submit_button("Add supplier") and name:
        try:
            db.add_supplier(name, gstin, phone, city, int(lead), int(credit))
            st.success("Supplier added")
        except Exception as e:
            st.error(str(e))
