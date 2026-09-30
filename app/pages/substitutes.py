import pandas as pd
import plotly.express as px
import streamlit as st

from app import substitutes as S
from app.sub_ui import render_panel
from app.ui import inr

st.title("🔁 Substitutes & Missed Demand")
tab_find, tab_missed, tab_log, tab_master = st.tabs(
    ["Find substitute", "Missed demand", "Substitution log", "Brand list"])

with tab_find:
    st.caption("Customer asks for a medicine you don't have? Type the brand (e.g. *Dolo 650*, "
               "*Montair LC*), a product name, or the formula (e.g. *paracetamol 650mg*).")
    with st.form("finder", clear_on_submit=False):
        c1, c2, c3 = st.columns([5, 1.2, 1.3])
        q = c1.text_input("Medicine asked for", placeholder="e.g. Dolo 650")
        qty = c2.number_input("Qty", 1, 500, 1)
        c3.write("")
        c3.write("")
        if c3.form_submit_button("Find ⏎", type="primary", width="stretch") and q.strip():
            st.session_state.finder_req = (q.strip(), int(qty))
    if st.session_state.get("finder_req"):
        fq, fqty = st.session_state.finder_req
        render_panel(fq, fqty, "out_of_stock", key="finder",
                     on_close=lambda: st.session_state.pop("finder_req", None))
    with st.expander("How does matching work?"):
        st.markdown("""
An **exact substitute** must have **all four** the same:
1. the same active ingredient(s) · 2. the same strength of *each* ingredient ·
3. the same dosage form (tablet ≠ syrup) · 4. the same release type (plain ≠ SR / ER / DR)

**Ranking:** enough stock first → batches expiring soonest (sold first, cuts expiry loss) → lowest price per unit.

**Safety:** Schedule H/H1 items and narrow-therapeutic-index drugs (e.g. levothyroxine, theophylline,
warfarin, phenytoin) need the prescriber's / customer's agreement before switching brands.
The software only suggests - the pharmacist decides, and every switch is logged.
""")

with tab_missed:
    days = st.segmented_control("Period", [7, 30, 60], default=30, format_func=lambda d: f"Last {d} days")
    days = days or 30
    s = S.unmet_summary(days)
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Requests we couldn't fill as asked", s["requests"])
    c2.metric("Saved by substituting", f"{s['substituted_pct']:.0f}%")
    c3.metric("Sales kept via substitutes", inr(s["saved_revenue"]))
    c4.metric("Units lost", s["lost_units"])
    c5.metric("Units to order for customers", s["ordered_units"])

    u = S.unmet(days)
    if u.empty:
        st.info("No missed requests logged in this period.")
    else:
        left, right = st.columns([3, 2])
        with left:
            top = S.top_unavailable(days)
            if len(top):
                fig = px.bar(top.head(10).sort_values("units"), x="units", y="formula", orientation="h",
                             color="action", title="Asked for but not given (lost + ordered units)",
                             labels={"units": "Units", "formula": "", "action": ""},
                             color_discrete_map={S.ACTIONS["new"]: "#dc2626", S.ACTIONS["reorder"]: "#ea580c",
                                                 S.ACTIONS["raise"]: "#f59e0b", S.ACTIONS["brand"]: "#2563eb",
                                                 S.ACTIONS["watch"]: "#94a3b8"},
                             hover_data=["asked_as", "requests"])
                fig.update_layout(height=380, legend=dict(orientation="h", y=-0.2), margin=dict(t=40))
                st.plotly_chart(fig, width="stretch")
        with right:
            daily = (u.assign(day=pd.to_datetime(u["ts"]).dt.date)
                      .groupby(["day", "outcome"])["qty"].sum().reset_index())
            fig = px.bar(daily, x="day", y="qty", color="outcome", title="Daily unmet requests (units)",
                         labels={"qty": "Units", "day": "", "outcome": ""},
                         color_discrete_map={"substituted": "#059669", "ordered": "#f59e0b",
                                             "lost": "#dc2626"})
            fig.update_layout(height=380, legend=dict(orientation="h", y=-0.2), margin=dict(t=40))
            st.plotly_chart(fig, width="stretch")

        st.markdown("**What to do**")
        st.dataframe(S.top_unavailable(days), hide_index=True, width="stretch",
                     column_config={"stocked": st.column_config.CheckboxColumn("We stock this formula"),
                                    "in_stock_now": "In stock now"})
        st.download_button("⬇ Download as order list (CSV)", S.top_unavailable(days).to_csv(index=False),
                           "missed_demand.csv", "text/csv")
    st.caption("Why this matters for the forecast: sales data only shows what we SOLD, not what customers "
               "WANTED. When we are out of stock, true demand is hidden (censored). Logging missed "
               "requests recovers that demand - it is added to the reorder suggestions and is the "
               "next step for retraining the demand model.")

with tab_log:
    log = S.substitution_log(90)
    st.caption("Every same-formula substitution given at the counter (last 90 days).")
    st.dataframe(log, hide_index=True, width="stretch", height=420,
                 column_config={"est_value": st.column_config.NumberColumn("Bill value", format="₹%.0f"),
                                "given_product": "Given instead"})

with tab_master:
    m = pd.DataFrame(S.brand_master())
    st.caption(f"{len(m)} brands mapped to their formula. The built-in list is **demo data** - a real store "
               "should load a verified drug master.")
    if len(m):
        st.dataframe(m[["brand", "composition", "dosage_form", "release_type"]], hide_index=True,
                     width="stretch", height=300)
    up = st.file_uploader("Add brands from a CSV", type="csv",
                          help="Columns: brand, composition, form, release  - or the Kaggle "
                               "'A-Z Medicine Dataset of India' columns: name, short_composition1, "
                               "short_composition2")
    if up is not None and st.button("Save brand list", type="primary"):
        S.CUSTOM_MASTER.parent.mkdir(parents=True, exist_ok=True)
        S.CUSTOM_MASTER.write_bytes(up.getvalue())
        st.success(f"Saved. {len(S.brand_master())} brands now available.")
