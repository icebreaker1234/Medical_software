import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from app import analytics
from app import substitutes as S
from app.db import MODEL_CATEGORIES
from app.ui import all_forecasts, get_forecast
from src.models.predict import load_history, load_model

NAMES = {"M01AB": "Anti-inflammatory (diclofenac type)", "M01AE": "Anti-inflammatory (ibuprofen type)",
         "N02BA": "Aspirin", "N02BE": "Paracetamol", "N05B": "Anxiolytics", "N05C": "Sleep medicines",
         "R03": "Asthma / airway", "R06": "Antihistamines"}

st.title("📈 Demand Forecast & Reorder")
try:
    _, meta = load_model()
except FileNotFoundError:
    st.error("Model not trained yet. Run `python -m src.pipeline` first.")
    st.stop()

c1, c2 = st.columns([3, 1])
cat = c1.selectbox("Drug category", MODEL_CATEGORIES, format_func=lambda c: f"{c} - {NAMES[c]}", index=3)
horizon = c2.slider("Days ahead", 7, 30, 14)
fc, source = get_forecast(cat, horizon)
hist = load_history()
hist = hist[hist["category"] == cat].tail(90)

fig = go.Figure()
fig.add_trace(go.Scatter(x=hist["date"], y=hist["sales"], name="Actual (last 90 days)",
                         line=dict(color="#64748b")))
fig.add_trace(go.Scatter(x=pd.concat([fc["date"], fc["date"][::-1]]),
                         y=pd.concat([fc["upper"], fc["lower"][::-1]]), fill="toself",
                         fillcolor="rgba(37,99,235,.15)", line=dict(width=0),
                         name=f"{int(meta['interval'] * 100)}% prediction interval"))
fig.add_trace(go.Scatter(x=fc["date"], y=fc["forecast"], name="Forecast",
                         line=dict(color="#2563eb", width=3)))
fig.update_layout(height=400, title=f"{NAMES[cat]} - daily units", legend=dict(orientation="h", y=1.12))
st.plotly_chart(fig, width="stretch")

wape = meta["per_category"][cat]["wape"]
m1, m2, m3, m4 = st.columns(4)
m1.metric(f"Expected units, next {horizon} days", f"{fc['forecast'].sum():,.0f}")
m2.metric("Range", f"{fc['lower'].sum():,.0f} - {fc['upper'].sum():,.0f}")
m3.metric("Model error (WAPE, hold-out)", f"{wape * 100:.0f}%",
          help="Weighted absolute % error on the last 90 days the model never saw during training.")
m4.metric("Served by", source, help="FastAPI model service, or in-process fallback if the API is down")
st.caption("Forecasts support purchase decisions - the pharmacist decides what to order. "
           "Daily sales are noisy; totals over a week are much more reliable than any single day.")

st.subheader("Purchase suggestions")
cover = st.slider("Order enough to cover (days after delivery)", 7, 30, 14)
with st.spinner("Forecasting all categories..."):
    fcs = all_forecasts(horizon=max(30, cover + 7))
sug = analytics.reorder_suggestions(fcs, cover_days=cover)
if sug.empty:
    st.success("Stock is sufficient for the selected cover period.")
else:
    st.dataframe(sug, hide_index=True, width="stretch", height=420, column_config={
        "sales_per_day": st.column_config.NumberColumn("Sales/day", format="%.1f"),
        "suggested_qty": st.column_config.NumberColumn("Suggested order", format="%d")})
    st.download_button("⬇ Download purchase list (CSV)", sug.to_csv(index=False),
                       "purchase_suggestions.csv", "text/csv")
new_items = S.top_unavailable(30)
new_items = new_items[new_items["action"] == S.ACTIONS["new"]]
if len(new_items):
    st.markdown("**Customers keep asking for these - we don't stock the formula**")
    st.dataframe(new_items[["formula", "asked_as", "requests", "units"]], hide_index=True, width="stretch")
with st.expander("How are suggestions calculated?"):
    st.markdown("""
* **Demand** over *supplier lead time + cover days*:
  * ML categories → category forecast × the product's share of that category (last 60 days)
  * other products → 30-day average daily sales
* **Safety stock** = half the gap between the upper prediction bound and the forecast
* **Missed demand**: units customers asked for while we were out of stock (logged at billing)
  are added back, because sales data alone hides that demand
* **Suggested qty** = demand + safety stock − current non-expired stock
""")
