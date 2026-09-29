import plotly.express as px
import streamlit as st

from app import analytics
from app.ui import inr, kpi, money_cols

st.title("⏳ Expiry & Dead Stock")
st.caption("Where is my money locked, and what can still be recovered?")

buckets = analytics.expiry_buckets()
risk = analytics.expiry_risk(180)
dead = analytics.dead_stock()

c1, c2, c3, c4 = st.columns(4)
val = dict(zip(buckets["bucket"].astype(str), buckets["value"]))
kpi(c1, "Expired on shelf", inr(val.get("Expired", 0)), "cannot be sold", "bad")
kpi(c2, "Expiring ≤ 90 days", inr(val.get("0-30 days", 0) + val.get("31-60 days", 0) +
                                   val.get("61-90 days", 0)), "at cost", "warn")
kpi(c3, "Likely loss (projected)", inr(risk["estimated_loss"].sum() if len(risk) else 0),
    "stock that won't sell before expiry", "warn")
kpi(c4, "Dead + slow stock", inr(dead["stock_value_cost"].sum()), f"{len(dead)} products", "info")

left, right = st.columns([2, 3])
with left:
    near = buckets[buckets["bucket"].astype(str) != "> 180 days"]
    fig = px.bar(near, x="bucket", y="value", text_auto=".3s",
                 title="Stock value expiring within 6 months (at cost)",
                 labels={"value": "₹ at cost", "bucket": ""},
                 color="bucket", color_discrete_sequence=["#dc2626", "#ea580c", "#f59e0b", "#facc15",
                                                          "#84cc16", "#10b981"])
    fig.update_layout(showlegend=False, height=360)
    st.plotly_chart(fig, width="stretch")
with right:
    st.markdown("**Expiry-risk engine** - projected unsold units if sold FEFO at the current 30-day rate")
    if risk.empty:
        st.success("No batch expiring in the next 180 days is at risk.")
    else:
        st.dataframe(risk, hide_index=True, width="stretch", height=330, column_config={
            **money_cols(risk, ["estimated_loss"]),
            "risk": st.column_config.TextColumn("Risk"),
            "suggested_action": st.column_config.TextColumn("Consider", width="large")})

with st.expander("How is expiry risk calculated?"):
    st.markdown("""
For every batch expiring within 180 days:

1. **Sell-through capacity** = product's average daily sales (last 30 days) × days left to expiry
2. **Stock ahead of it** = this batch + all earlier-expiring batches of the same product (FEFO sells those first)
3. **Projected unsold** = stock ahead − sell-through capacity, capped at the batch quantity
4. **Risk** = HIGH if ≥ 50 % of the batch is projected unsold, MEDIUM if ≥ 20 %
5. **Estimated loss** = projected unsold × purchase rate

This is a transparent rule, not a black-box model - the owner can verify every number.
""")

st.subheader("Dead & slow-moving stock")
c1, c2 = st.columns(2)
days = c1.slider("Dead = no sale for at least (days)", 30, 180, 90, 15)
cover = c2.slider("Slow = stock covers more than (days)", 60, 365, 120, 15)
dead = analytics.dead_stock(days, cover)
st.dataframe(dead, hide_index=True, width="stretch", column_config={
    **money_cols(dead, ["stock_value_cost"]),
    "per_day": st.column_config.NumberColumn("Sales/day", format="%.2f"),
    "days_cover": st.column_config.NumberColumn("Days cover", format="%.0f")})
st.caption("Typical actions: stop reordering, return to supplier where terms allow, move to the front "
           "counter, or transfer to another branch.")
