import streamlit as st

from app import analytics, db
from app.ui import inr, money_cols

st.title("📦 Inventory")
tab_stock, tab_batch, tab_adjust, tab_ledger, tab_new = st.tabs(
    ["Stock by medicine", "Batch-wise stock", "Stock audit / adjustment", "Stock ledger (audit)",
     "Add medicine"])

with tab_stock:
    st_df = analytics.stock_status()
    c1, c2, c3 = st.columns([3, 2, 2])
    search = c1.text_input("Search", placeholder="name or composition")
    cats = c2.multiselect("Category", sorted(st_df["category"].unique()))
    status = c3.multiselect("Status", ["OUT OF STOCK", "CRITICAL", "LOW", "OK"])
    view = st_df
    if search:
        view = view[view["name"].str.contains(search, case=False) |
                    view["generic"].str.contains(search, case=False)]
    if cats:
        view = view[view["category"].isin(cats)]
    if status:
        view = view[view["status"].isin(status)]
    c1, c2, c3 = st.columns(3)
    c1.metric("Products", len(view))
    c2.metric("Stock value (cost)", inr(view["stock_value_cost"].sum()))
    c3.metric("Below cover target", int((view["status"] != "OK").sum()))
    cols = ["name", "generic", "category", "schedule", "rack", "stock", "expired_qty", "per_day",
            "days_cover", "stockout_date", "next_expiry", "stock_value_cost", "status"]
    st.dataframe(view[cols].round(1), hide_index=True, width="stretch", height=480,
                 column_config={**money_cols(view, ["stock_value_cost"]),
                                "per_day": st.column_config.NumberColumn("Sales/day", format="%.1f"),
                                "days_cover": st.column_config.NumberColumn("Days cover", format="%.0f")})

with tab_batch:
    b = db.batches_df()
    show_expired = st.toggle("Show expired batches only")
    if show_expired:
        b = b[b["days_to_expiry"] <= 0]
    st.dataframe(b, hide_index=True, width="stretch", height=480,
                 column_config={**money_cols(b, ["value_cost"]),
                                "days_to_expiry": st.column_config.ProgressColumn(
                                    "Days to expiry", min_value=0, max_value=730, format="%d")})

with tab_adjust:
    st.caption("Use after a physical stock count. Every adjustment is written to the audit ledger "
               "with reason and user - it cannot be edited or deleted.")
    b = db.batches_df()
    opts = {f"{r['product']} · {r['batch_no']} · exp {r['expiry']} · qty {r['qty']}": int(r["batch_id"])
            for _, r in b.iterrows()}
    with st.form("adj"):
        sel = st.selectbox("Batch", list(opts))
        change = st.number_input("Quantity change (+ found / - missing)", -1000, 1000, 0)
        reason = st.selectbox("Reason", ["Physical count mismatch", "Breakage / damage",
                                         "Expired - moved to expiry bin", "Found in other rack"])
        user = st.text_input("Done by", "manager")
        if st.form_submit_button("Post adjustment", type="primary") and change != 0:
            try:
                db.adjust_stock(opts[sel], int(change), reason, user)
                st.success("Adjustment posted")
            except ValueError as e:
                st.error(str(e))

with tab_ledger:
    prods = db.products_df()
    sel = st.selectbox("Medicine", prods["name"], index=None, placeholder="Choose a medicine")
    if sel:
        led = db.q("""SELECT m.ts, b.batch_no, m.type, m.qty_change, m.ref, m.note, m.user
                      FROM stock_movements m JOIN batches b ON b.id=m.batch_id
                      JOIN products p ON p.id=m.product_id WHERE p.name=? ORDER BY m.ts, m.id""", (sel,))
        led["running_stock"] = led["qty_change"].cumsum()
        st.dataframe(led.iloc[::-1], hide_index=True, width="stretch", height=420)
        st.caption("Running stock is rebuilt from the ledger - it always reconciles with batch stock.")

with tab_new:
    sups = db.q("SELECT id, name FROM suppliers")
    with st.form("newprod"):
        c1, c2 = st.columns(2)
        name = c1.text_input("Brand / product name")
        generic = c2.text_input("Composition (generic)")
        cat = c1.text_input("Category / ATC", "N02BE")
        sch = c2.selectbox("Schedule", ["OTC", "H", "H1", "X"])
        gst = c1.selectbox("GST %", [5, 12, 18, 0])
        hsn = c2.text_input("HSN", "3004")
        pack = c1.text_input("Pack", "10 tab")
        mrp = c2.number_input("MRP (₹)", 0.0, 100000.0, 50.0)
        barcode = c1.text_input("Barcode (optional)")
        rack = c2.text_input("Rack", "R1-S1")
        sup = c1.selectbox("Preferred supplier", sups["name"])
        chronic = c2.checkbox("Chronic / regular-refill medicine")
        if st.form_submit_button("Add medicine", type="primary") and name:
            try:
                db.add_product(name=name, generic=generic, category=cat, schedule=sch, gst_rate=gst,
                               hsn=hsn, pack=pack, default_mrp=mrp, barcode=barcode or None,
                               rack=rack, chronic=int(chronic),
                               preferred_supplier_id=int(sups.loc[sups.name == sup, "id"].iloc[0]))
                st.success(f"Added {name}. Stock is added through Purchases.")
            except Exception as e:
                st.error(f"Could not add: {e}")
