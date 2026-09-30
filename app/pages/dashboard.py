import plotly.express as px
import streamlit as st

from app import analytics, db
from app.tenancy import store_info
from app.ui import inr, kpi, lakh

st.title(f"🏠 {store_info()['name']}")
if db.is_empty():
    st.subheader("Welcome - let's set up your store")
    st.markdown("Your store has its own database and it is empty.")
    st.success("**Moving from Marg, Tally or Excel?** Export your stock, suppliers, customers and bills from the "
               "old software and drop the files here - no retyping.")
    st.page_link("app/pages/import_data.py", label="Import from my old software", icon="📥")
    st.markdown("**Or set it up by hand, in this order:**")
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.markdown("**1. Store details**  \nGSTIN, drug licence and UPI ID for your bills.")
        st.page_link("app/pages/store_settings.py", label="Store settings", icon="⚙️")
    with c2:
        st.markdown("**2. Suppliers**  \nYour distributors and their credit days.")
        st.page_link("app/pages/suppliers.py", label="Add suppliers", icon="🏭")
    with c3:
        st.markdown("**3. Medicines**  \nAdd each medicine with its composition.")
        st.page_link("app/pages/inventory.py", label="Add medicines", icon="📦")
    with c4:
        st.markdown("**4. Stock in**  \nEnter purchase bills (or scan them) to add stock.")
        st.page_link("app/pages/purchases.py", label="Purchases", icon="🚚")
    st.info("Just exploring? Load 6 months of sample data from **Store settings & backup** - "
            "you can delete it later.")
    st.stop()
k = analytics.kpis()

c1, c2, c3, c4 = st.columns(4)
vs = k["sales_vs_usual_pct"]
kpi(c1, "Today's sales", inr(k["sales"]),
    f"{vs:+.0f}% vs usual at this time" if vs is not None else "", "good")
kpi(c2, "Bills today", f"{k['bills']}", f"Avg bill {inr(k['avg_bill'])}", "info")
kpi(c3, "Est. gross profit", inr(k["profit"]),
    f"{(k['profit'] / k['sales'] * 100 if k['sales'] else 0):.1f}% margin", "good")
kpi(c4, "Inventory (at cost)", lakh(k["inventory_value"]), f"{k['sku_count']} products", "info")

c5, c6, c7, c8 = st.columns(4)
st_status = analytics.stock_status()
low = st_status[st_status["status"] != "OK"]
dead = analytics.dead_stock()
kpi(c5, "Near expiry (≤90 days)", inr(k["near_expiry_value"]), "at cost", "warn")
kpi(c6, "Expired on shelf", inr(k["expired_value"]), "segregate - not saleable",
    "bad" if k["expired_value"] else "good")
kpi(c7, "Low / out of stock", f"{len(low)}", "products below cover target",
    "warn" if len(low) else "good")
kpi(c8, "Dead & slow stock", inr(dead["stock_value_cost"].sum()), f"{len(dead)} products", "warn")

st.subheader("Needs your attention")
items = analytics.attention_list()
if not items:
    st.success("Nothing urgent today.")
for sev, msg in items:
    getattr(st, sev)(msg)

left, right = st.columns([3, 2])
with left:
    daily = db.q("""SELECT date(ts) AS day, SUM(total) AS sales, SUM(taxable-cost) AS profit,
                           COUNT(*) AS bills
                    FROM sales WHERE date(ts) > date('now','localtime','-30 days')
                    GROUP BY day ORDER BY day""")
    fig = px.bar(daily, x="day", y="sales", title="Sales - last 30 days",
                 labels={"sales": "Sales (₹)", "day": ""}, color_discrete_sequence=["#2563eb"])
    fig.add_scatter(x=daily["day"], y=daily["profit"], name="Gross profit", mode="lines+markers",
                    line=dict(color="#059669"))
    fig.update_layout(height=340, margin=dict(t=40, b=10), legend=dict(orientation="h", y=1.1))
    st.plotly_chart(fig, width="stretch")
with right:
    top = db.q("""SELECT p.name AS product, SUM(si.qty) AS units, SUM(si.amount) AS revenue
                  FROM sale_items si JOIN sales s ON s.id=si.sale_id JOIN products p ON p.id=si.product_id
                  WHERE date(s.ts) > date('now','localtime','-30 days')
                  GROUP BY p.id ORDER BY revenue DESC LIMIT 8""")
    fig = px.bar(top.sort_values("revenue"), x="revenue", y="product", orientation="h",
                 title="Top sellers - 30 days", labels={"revenue": "Revenue (₹)", "product": ""},
                 color_discrete_sequence=["#0891b2"])
    fig.update_layout(height=340, margin=dict(t=40, b=10))
    st.plotly_chart(fig, width="stretch")

a, b = st.columns(2)
with a:
    st.markdown("**Low stock** (days of cover vs supplier lead time)")
    st.dataframe(low.sort_values("days_cover")[["name", "stock", "per_day", "days_cover",
                                                "stockout_date", "status"]].head(10)
                 .rename(columns={"per_day": "sales/day"}).round(1),
                 hide_index=True, width="stretch")
with b:
    pay = db.q("""SELECT payment_mode, COUNT(*) AS bills, SUM(total) AS amount FROM sales
                  WHERE date(ts) > date('now','localtime','-30 days') GROUP BY payment_mode""")
    fig = px.pie(pay, names="payment_mode", values="amount", hole=0.55,
                 title="Payment modes - 30 days")
    fig.update_layout(height=300, margin=dict(t=40, b=10))
    st.plotly_chart(fig, width="stretch")
