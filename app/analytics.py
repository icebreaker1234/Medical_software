"""Business intelligence on top of the store DB.

Deterministic rules and simple statistics wherever they are sufficient; the ML
model is only used for demand forecasting of the 8 modelled categories.
"""
from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd

from app import db
from app.fmt import inr


def kpis(day=None) -> dict:
    day = day or db.today()
    s = db.q("""SELECT COUNT(*) bills, COALESCE(SUM(total),0) sales,
                       COALESCE(SUM(taxable-cost),0) profit, COALESCE(SUM(discount),0) discount
                FROM sales WHERE date(ts)=?""", (str(day),)).iloc[0]
    # compare with the average of the last 4 same weekdays, up to the same time of day
    cutoff = db.now_ts()[11:] if day == db.today() else "23:59:59"
    prev_days = [str(day - timedelta(days=7 * k)) for k in range(1, 5)]
    prev = db.q(f"SELECT COALESCE(SUM(total),0)/4.0 v FROM sales WHERE date(ts) IN "
                f"({','.join('?' * 4)}) AND time(ts)<=?", (*prev_days, cutoff)).v[0]
    stock = db.batches_df()
    live = stock[stock["days_to_expiry"] > 0]
    return {
        "sales": float(s.sales), "bills": int(s.bills), "profit": float(s.profit),
        "avg_bill": float(s.sales / s.bills) if s.bills else 0.0,
        "sales_vs_usual_pct": float((s.sales - prev) / prev * 100) if prev else None,
        "inventory_value": float(live["value_cost"].sum()),
        "near_expiry_value": float(live.loc[live["days_to_expiry"] <= 90, "value_cost"].sum()),
        "expired_value": float(stock.loc[stock["days_to_expiry"] <= 0, "value_cost"].sum()),
        "sku_count": int(live["product"].nunique()),
    }


def stock_status(cover_target_days: int = 7) -> pd.DataFrame:
    """Per product: stock, velocity, days of cover, estimated stock-out date."""
    p = db.products_df()
    v = db.velocity(30)[["product_id", "per_day"]]
    df = p.merge(v, left_on="id", right_on="product_id", how="left").fillna({"per_day": 0})
    df["days_cover"] = np.where(df["per_day"] > 0, df["stock"] / df["per_day"], np.inf)
    df["stockout_date"] = [
        (db.today() + timedelta(days=int(d))).isoformat() if np.isfinite(d) else "-"
        for d in df["days_cover"]]
    lead = df["lead_time_days"].fillna(2)
    df["status"] = np.select(
        [df["stock"] == 0, df["days_cover"] <= lead, df["days_cover"] <= lead + cover_target_days],
        ["OUT OF STOCK", "CRITICAL", "LOW"], "OK")
    return df


def expiry_buckets() -> pd.DataFrame:
    b = db.batches_df()
    bins = [-10_000, 0, 30, 60, 90, 180, 100_000]
    labels = ["Expired", "0-30 days", "31-60 days", "61-90 days", "91-180 days", "> 180 days"]
    b["bucket"] = pd.cut(b["days_to_expiry"], bins=bins, labels=labels)
    return (b.groupby("bucket", observed=False)
             .agg(batches=("batch_id", "count"), units=("qty", "sum"), value=("value_cost", "sum"))
             .reset_index())


def expiry_risk(horizon_days: int = 180) -> pd.DataFrame:
    """Projected unsold units per batch before expiry, assuming FEFO selling at the
    current 30-day velocity. Stock ahead of a batch (earlier expiry) is sold first."""
    b = db.batches_df()
    b = b[(b["days_to_expiry"] > 0) & (b["days_to_expiry"] <= horizon_days)]
    allb = db.batches_df()
    allb = allb[allb["days_to_expiry"] > 0]
    prod = db.products_df()[["id", "name"]].rename(columns={"id": "product_id", "name": "product"})
    v = db.velocity(30)[["product_id", "per_day"]].merge(prod, on="product_id")
    vel = dict(zip(v["product"], v["per_day"]))
    rows = []
    for _, r in b.iterrows():
        same = allb[(allb["product"] == r["product"]) & (allb["expiry"] <= r["expiry"])]
        cum_qty = same["qty"].sum()                     # this batch + batches sold before it
        sellable = vel.get(r["product"], 0.0) * r["days_to_expiry"]
        unsold = float(np.clip(cum_qty - sellable, 0, r["qty"]))
        share = unsold / r["qty"] if r["qty"] else 0
        risk = "HIGH" if share >= 0.5 else "MEDIUM" if share >= 0.2 else "LOW"
        if risk == "HIGH" and r["days_to_expiry"] <= 90:
            action = "Raise expiry return with supplier now (check return terms); stop reordering"
        elif risk == "HIGH":
            action = "Stop reordering; offer to regular customers / transfer to another branch"
        elif risk == "MEDIUM":
            action = "Keep in front (FEFO); reduce next order quantity"
        else:
            action = "No action - will sell through"
        rows.append({**r[["product", "batch_no", "expiry", "days_to_expiry", "qty",
                          "purchase_rate", "supplier"]].to_dict(),
                     "sales_per_day": round(vel.get(r["product"], 0.0), 2),
                     "projected_unsold": round(unsold, 1),
                     "estimated_loss": round(unsold * r["purchase_rate"], 0),
                     "risk": risk, "suggested_action": action})
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    return out.sort_values(["risk", "estimated_loss"], key=lambda s: s.map(order) if s.name == "risk"
                           else -s).reset_index(drop=True)


def dead_stock(no_sale_days: int = 90, slow_cover_days: int = 120) -> pd.DataFrame:
    """Dead = stock on hand but no sale for N days. Slow = > slow_cover_days of cover."""
    st = stock_status()
    last = db.last_sale_dates()
    df = st.merge(last, left_on="id", right_on="product_id", how="left", suffixes=("", "_ls"))
    df = df[df["stock"] > 0].copy()
    df["days_since_sale"] = (pd.Timestamp(db.today()) -
                             pd.to_datetime(df["last_sale"])).dt.days.fillna(9999).astype(int)
    df["class"] = np.select([df["days_since_sale"] >= no_sale_days,
                             df["days_cover"] >= slow_cover_days], ["DEAD", "SLOW"], "")
    df = df[df["class"] != ""]
    df["reason"] = np.where(
        df["class"] == "DEAD", "No sale in " + df["days_since_sale"].astype(str) + " days",
        "Stock covers " + df["days_cover"].replace(np.inf, 9999).round(0).astype(int).astype(str)
        + " days of sales")
    return df[["name", "category", "class", "stock", "stock_value_cost", "per_day", "days_cover",
               "last_sale", "days_since_sale", "reason"]].sort_values(
        "stock_value_cost", ascending=False)


def supplier_performance() -> pd.DataFrame:
    p = db.q("""SELECT s.id, s.name, s.city, s.credit_days, COUNT(pu.id) AS invoices,
                       SUM(pu.total) AS purchase_value,
                       AVG(julianday(pu.received_date) - julianday(pu.order_date)) AS avg_lead_days,
                       SUM(pu.received_qty)*1.0 / SUM(pu.ordered_qty) AS fill_rate,
                       SUM(CASE WHEN pu.received_qty < pu.ordered_qty THEN 1 ELSE 0 END)*1.0
                           / COUNT(pu.id) AS short_supply_rate,
                       SUM(CASE WHEN pu.paid=0 THEN pu.total ELSE 0 END) AS payable
                FROM suppliers s LEFT JOIN purchases pu ON pu.supplier_id = s.id
                GROUP BY s.id""")
    r = db.q("""SELECT supplier_id AS id, SUM(value) AS returns_value,
                       SUM(CASE WHEN credit_note_status='Pending' THEN value ELSE 0 END) AS pending_claims
                FROM supplier_returns GROUP BY supplier_id""")
    df = p.merge(r, on="id", how="left").fillna({"returns_value": 0, "pending_claims": 0})
    # transparent score: fast + complete deliveries rank higher (weights are a business choice)
    lead_score = 1 - (df["avg_lead_days"].clip(0, 7) / 7)
    df["reliability_score"] = (100 * (0.6 * df["fill_rate"].fillna(0) + 0.4 * lead_score)).round(0)
    return df.sort_values("reliability_score", ascending=False)


def price_comparison() -> pd.DataFrame:
    """Effective purchase rate of each product by supplier (last 180 days)."""
    return db.q("""SELECT p.name AS product, s.name AS supplier, ROUND(AVG(b.purchase_rate),2) AS avg_rate,
                          COUNT(*) AS batches
                   FROM batches b JOIN products p ON p.id=b.product_id
                   JOIN suppliers s ON s.id=b.supplier_id
                   WHERE b.received_on > date('now','localtime','-180 days')
                   GROUP BY p.id, s.id""")


def gst_summary(start, end) -> pd.DataFrame:
    return db.q("""SELECT si.gst_rate AS gst_rate, ROUND(SUM(si.taxable),2) AS taxable,
                          ROUND(SUM(si.tax)/2,2) AS cgst, ROUND(SUM(si.tax)/2,2) AS sgst,
                          ROUND(SUM(si.amount),2) AS invoice_value
                   FROM sale_items si JOIN sales s ON s.id=si.sale_id
                   WHERE date(s.ts) BETWEEN ? AND ? GROUP BY si.gst_rate""", (str(start), str(end)))


def h1_register(start, end) -> pd.DataFrame:
    return db.q("""SELECT s.ts AS date_time, s.invoice_no, COALESCE(c.name, s.patient_name) AS patient,
                          s.doctor_name AS prescriber, p.name AS drug, si.qty, b.batch_no
                   FROM sales s JOIN sale_items si ON si.sale_id=s.id
                   JOIN products p ON p.id=si.product_id JOIN batches b ON b.id=si.batch_id
                   LEFT JOIN customers c ON c.id=s.customer_id
                   WHERE p.schedule='H1' AND date(s.ts) BETWEEN ? AND ? ORDER BY s.ts""",
                (str(start), str(end)))


def customer_balances() -> pd.DataFrame:
    return db.q("""SELECT c.id, c.name, c.phone, c.doctor,
                          COALESCE((SELECT SUM(total) FROM sales WHERE customer_id=c.id),0) AS lifetime_value,
                          COALESCE((SELECT COUNT(*) FROM sales WHERE customer_id=c.id),0) AS visits,
                          (SELECT MAX(date(ts)) FROM sales WHERE customer_id=c.id) AS last_visit,
                          COALESCE((SELECT SUM(total) FROM sales WHERE customer_id=c.id
                                    AND payment_mode='Credit'),0)
                          - COALESCE((SELECT SUM(amount) FROM customer_payments
                                      WHERE customer_id=c.id),0) AS outstanding
                   FROM customers c ORDER BY lifetime_value DESC""")


def refill_due(window_days: int = 7) -> pd.DataFrame:
    """Regular patients whose chronic medicine is due for refill (from their buying interval)."""
    df = db.q("""SELECT c.name AS customer, c.phone, p.name AS medicine, date(s.ts) AS day
                 FROM sales s JOIN sale_items si ON si.sale_id=s.id
                 JOIN products p ON p.id=si.product_id JOIN customers c ON c.id=s.customer_id
                 WHERE p.chronic=1""")
    if df.empty:
        return df
    df["day"] = pd.to_datetime(df["day"])
    out = []
    for (cust, phone, med), g in df.groupby(["customer", "phone", "medicine"]):
        days = g["day"].drop_duplicates().sort_values()
        gaps = days.diff().dt.days.dropna()
        gaps = gaps[gaps >= 7]                  # ignore same-week top-ups
        if len(gaps) < 2:
            continue
        interval = int(gaps.median())
        due = days.iloc[-1] + pd.Timedelta(days=interval)
        delta = (due - pd.Timestamp(db.today())).days
        if -interval <= delta <= window_days:   # due soon, or overdue by < one cycle
            out.append({"customer": cust, "phone": phone, "medicine": med,
                        "last_bought": days.iloc[-1].date(), "usual_interval_days": interval,
                        "due_on": due.date(), "status": "OVERDUE" if delta < 0 else "DUE SOON"})
    return pd.DataFrame(out).sort_values("due_on") if out else pd.DataFrame()


def reorder_suggestions(category_forecasts: dict[str, pd.DataFrame] | None = None,
                        cover_days: int = 14) -> pd.DataFrame:
    """Suggested order qty = demand over (lead time + cover days) + safety stock - stock on hand.

    For products in the 8 ML categories, product demand = category forecast x the
    product's recent share of that category. Others use the 30-day moving average.
    """
    st = stock_status()
    share = db.q("""SELECT si.product_id, p.category, SUM(si.qty) AS units
                    FROM sale_items si JOIN sales s ON s.id=si.sale_id JOIN products p ON p.id=si.product_id
                    WHERE date(s.ts) > date('now','localtime','-60 days') GROUP BY si.product_id""")
    share["share"] = share["units"] / share.groupby("category")["units"].transform("sum")
    st = st.merge(share[["product_id", "share"]], left_on="id", right_on="product_id",
                  how="left", suffixes=("", "_s")).fillna({"share": 0})
    rows = []
    for _, r in st.iterrows():
        lead = int(r["lead_time_days"] or 2)
        horizon = lead + cover_days
        fc = (category_forecasts or {}).get(r["category"])
        if fc is not None and len(fc) >= 1:
            daily = fc["forecast"].to_numpy() * r["share"]
            demand = float(daily[:horizon].sum() + daily[-1] * max(0, horizon - len(daily)))
            upper = float((fc["upper"].to_numpy() * r["share"])[:horizon].sum())
            method = "ML forecast"
        else:
            demand = r["per_day"] * horizon
            upper = demand * 1.3
            method = "30-day average"
        safety = max(upper - demand, 0) * 0.5
        qty = int(np.ceil(max(demand + safety - r["stock"], 0)))
        if qty > 0 and demand > 0.5:
            rows.append({"product": r["name"], "category": r["category"], "stock": int(r["stock"]),
                         "sales_per_day": round(r["per_day"], 2),
                         "lead_time_days": lead, "demand_lead+cover": round(demand, 1), "safety_stock": round(safety, 1),
                         "suggested_qty": qty, "supplier": r["preferred_supplier"],
                         "est_stockout": r["stockout_date"], "method": method})
    df = pd.DataFrame(rows)
    return df.sort_values("est_stockout") if not df.empty else df


def attention_list(limit: int = 8) -> list[tuple[str, str]]:
    """(severity, message) items for the owner's home screen."""
    items: list[tuple[str, str]] = []
    st = stock_status()
    crit = st[st["status"].isin(["OUT OF STOCK", "CRITICAL"]) & (st["per_day"] > 0.3)]
    if len(crit):
        names = ", ".join(crit.sort_values("days_cover")["name"].head(3))
        items.append(("error", f"{len(crit)} fast-moving medicines may stock out before the next "
                               f"delivery - e.g. {names}"))
    er = expiry_risk(120)
    if not er.empty:
        high = er[er["risk"] == "HIGH"]
        if len(high):
            items.append(("warning", f"{inr(high['estimated_loss'].sum())} of stock is unlikely to sell "
                                     f"before expiry ({len(high)} batches) - consider supplier returns"))
    k = kpis()
    if k["expired_value"] > 0:
        items.append(("error", f"{inr(k['expired_value'])} of EXPIRED stock is still on the shelf - "
                               "segregate it; it cannot be sold"))
    ds = dead_stock()
    if len(ds):
        items.append(("info", f"{inr(ds['stock_value_cost'].sum())} locked in dead/slow stock "
                              f"({len(ds)} products)"))
    rf = refill_due()
    if len(rf):
        items.append(("info", f"{len(rf)} regular patients are due for a refill this week"))
    claims = db.q("SELECT COALESCE(SUM(value),0) v, COUNT(*) n FROM supplier_returns "
                  "WHERE credit_note_status='Pending'").iloc[0]
    if claims.n:
        items.append(("warning", f"{inr(claims.v)} in expiry/breakage claims pending with suppliers "
                                 f"({int(claims.n)} returns)"))
    return items[:limit]
