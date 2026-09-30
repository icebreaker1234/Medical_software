from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import streamlit as st

from app import analytics, db
from app.ui import STORE_NAME, inr, money_cols
from app.whatsapp import normalize_phone, wa_link

st.title("💸 Supplier Payments")
tab_pay, tab_due, tab_ledger, tab_chq, tab_hist = st.tabs(
    ["Record payment", "Payables & ageing", "Ledger (khata)", "Cheques", "Payment history"])

payables = analytics.supplier_payables()
sups = payables.set_index("supplier_id")

# ------------------------------------------------------------------ record payment
with tab_pay:
    labels = {int(r.supplier_id): f"{r.supplier}  ·  due {inr(r.outstanding)}" + (
        f"  ·  overdue {inr(r.overdue)}" if r.overdue > 0 else "") for r in payables.itertuples()}
    sid = st.selectbox("Supplier", list(labels), format_func=labels.get)
    s = sups.loc[sid]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Outstanding", inr(s.outstanding, 2), f"{int(s.open_bills)} open bills", delta_color="off")
    c2.metric("Overdue", inr(s.overdue, 2), f"credit {int(s.credit_days)} days", delta_color="off")
    c3.metric("Cheques not yet cleared", inr(s.cheque_pending, 2))
    c4.metric("Advance paid (unadjusted)", inr(s.advance, 2))

    c1, c2, c3 = st.columns([1.2, 1.2, 3])
    pay_date = c1.date_input("Payment date", value=db.today(), max_value=db.today(), format="DD/MM/YYYY")
    amount = c2.number_input("Amount (₹)", min_value=0.0, value=float(round(s.outstanding, 2)), step=100.0)
    mode = c3.segmented_control("Paid by", db.PAY_MODES, default="NEFT")
    reference = cheque_no = bank = None
    cheque_date = None
    if mode == "Cheque":
        c1, c2, c3 = st.columns(3)
        cheque_no = c1.text_input("Cheque no. *")
        cheque_date = c2.date_input("Cheque date", value=pay_date, format="DD/MM/YYYY",
                                    help="A later date = post-dated cheque (PDC)")
        bank = c3.text_input("Bank", "HDFC Bank")
    elif mode in ("UPI", "NEFT", "RTGS", "Bank transfer"):
        c1, c2 = st.columns(2)
        reference = c1.text_input("UTR / transaction ID", placeholder="e.g. UTR123456789012")
        bank = c2.text_input("From bank account", "HDFC Bank")
    note = st.text_input("Note (optional)", placeholder="e.g. paid against September bills")

    bills = analytics.supplier_bills(sid)
    open_bills = bills[bills["outstanding"] > 0.01][["purchase_id", "invoice_no", "received_date", "due_date",
                                                     "total", "outstanding", "days_overdue", "status"]]
    how = st.radio("Adjust against bills", ["Oldest bills first (automatic)", "Choose bills myself"],
                   horizontal=True)
    auto = dict(db.auto_allocate(sid, amount)) if amount > 0 else {}
    view = open_bills.assign(pay_now=open_bills["purchase_id"].map(auto).fillna(0.0))
    if how.startswith("Choose"):
        view = st.data_editor(view, hide_index=True, width="stretch", key=f"alloc_{sid}_{amount}",
                              disabled=[c for c in view.columns if c != "pay_now"],
                              column_config={"purchase_id": None,
                                             "pay_now": st.column_config.NumberColumn("Pay now (₹)", min_value=0.0,
                                                                                      format="%.2f"),
                                             **money_cols(view, ["total", "outstanding"])})
    else:
        st.dataframe(view[view["pay_now"] > 0], hide_index=True, width="stretch",
                     column_config={"purchase_id": None, **money_cols(view, ["total", "outstanding", "pay_now"])})
    allocated = float(view["pay_now"].sum())
    extra = round(amount - allocated, 2)
    st.caption(f"Adjusted against bills: **{inr(allocated, 2)}**"
               + (f" · **{inr(extra, 2)} kept as advance** with the supplier" if extra > 0.01 else ""))
    if allocated > amount + 0.01:
        st.error("You have allocated more than the payment amount.")

    if st.button("💾 Save payment", type="primary", disabled=amount <= 0 or not mode or allocated > amount + 0.01):
        try:
            pid = db.record_supplier_payment(
                sid, str(pay_date), amount, mode,
                allocations=[(int(r.purchase_id), float(r.pay_now)) for r in view.itertuples() if r.pay_now > 0],
                reference=reference, cheque_no=cheque_no, cheque_date=str(cheque_date) if cheque_date else None,
                bank=bank, note=note)
            st.session_state.last_supplier_payment = (pid, sid, amount, mode, str(pay_date), cheque_no, reference)
            st.rerun()
        except ValueError as e:
            st.error(str(e))

    if st.session_state.get("last_supplier_payment"):
        pid, psid, pamt, pmode, pdate, pchq, pref = st.session_state.last_supplier_payment
        st.success(f"Payment #{pid} saved - {inr(pamt, 2)} by {pmode}"
                   + (" (cheque pending until cleared)" if pmode == "Cheque" else ""))
        phone = normalize_phone(str(sups.loc[psid, "phone"] or ""))
        if phone:
            msg = (f"Payment advice from *{STORE_NAME}*\n\nAmount: Rs {pamt:,.2f}\nMode: {pmode}"
                   + (f"\nCheque no: {pchq}" if pchq else "") + (f"\nRef/UTR: {pref}" if pref else "")
                   + f"\nDate: {pdate}\n\nPlease update our account. Thank you!")
            st.link_button("📲 Send payment advice to supplier on WhatsApp", wa_link(phone, msg))

# ------------------------------------------------------------------ payables & ageing
with tab_due:
    allb = analytics.supplier_bills()
    open_all = allb[allb["outstanding"] > 0.01]
    next7 = open_all[(pd.to_datetime(open_all["due_date"]) >= pd.Timestamp(db.today())) &
                     (pd.to_datetime(open_all["due_date"]) <= pd.Timestamp(db.today() + timedelta(days=7)))]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total payable", inr(payables["outstanding"].sum()))
    c2.metric("Overdue", inr(payables["overdue"].sum()))
    c3.metric("Falling due in 7 days", inr(next7["outstanding"].sum()), f"{len(next7)} bills", delta_color="off")
    c4.metric("Cheques in transit", inr(payables["cheque_pending"].sum()))
    age_cols = {"not_yet_due": "Not yet due", "overdue_1_30": "Overdue 1-30 d",
                "overdue_31_60": "Overdue 31-60 d", "overdue_60_plus": "Overdue 60+ d"}
    long = payables.melt(id_vars="supplier", value_vars=list(age_cols), var_name="age", value_name="amount")
    long["age"] = long["age"].map(age_cols)
    fig = px.bar(long, x="amount", y="supplier", color="age", orientation="h", title="What we owe, by age",
                 labels={"amount": "₹ outstanding", "supplier": "", "age": ""},
                 color_discrete_map={"Not yet due": "#94a3b8", "Overdue 1-30 d": "#f59e0b",
                                     "Overdue 31-60 d": "#ea580c", "Overdue 60+ d": "#dc2626"})
    fig.update_layout(height=300, barmode="stack", legend=dict(orientation="h", y=-0.25), margin=dict(t=40))
    st.plotly_chart(fig, width="stretch")
    st.dataframe(payables[["supplier", "credit_days", "purchases", "outstanding", "overdue", *age_cols,
                           "cheque_pending", "advance", "open_bills"]].rename(columns=age_cols),
                 hide_index=True, width="stretch",
                 column_config={**money_cols(payables, ["purchases", "outstanding", "overdue", "cheque_pending",
                                                        "advance"]),
                                **{v: st.column_config.NumberColumn(v, format="₹%.0f") for v in age_cols.values()}})
    st.markdown("**Open bills** (pay the overdue ones first to keep your credit terms)")
    st.dataframe(open_all.sort_values("days_overdue", ascending=False)[
        ["supplier", "invoice_no", "received_date", "due_date", "total", "paid", "cheque_pending",
         "outstanding", "days_overdue", "status"]], hide_index=True, width="stretch", height=320,
        column_config=money_cols(open_all, ["total", "paid", "cheque_pending", "outstanding"]))

# ------------------------------------------------------------------ ledger
with tab_ledger:
    names = dict(zip(payables["supplier_id"].astype(int), payables["supplier"]))
    lid = st.selectbox("Supplier ", list(names), format_func=names.get, key="ledger_sup")
    led = analytics.supplier_ledger(lid)
    if led.empty:
        st.info("No transactions yet.")
    else:
        c1, c2 = st.columns(2)
        start = c1.date_input("From", value=db.today() - timedelta(days=90), format="DD/MM/YYYY")
        end = c2.date_input("To", value=db.today(), format="DD/MM/YYYY")
        before = led[led["date"] < str(start)]
        opening = float(before["balance_due"].iloc[-1]) if len(before) else 0.0
        view = led[(led["date"] >= str(start)) & (led["date"] <= str(end))]
        c1, c2, c3 = st.columns(3)
        c1.metric("Opening balance", inr(opening, 2))
        c2.metric("Bills in period", inr(view["bill_amount"].sum(), 2))
        c3.metric("Closing balance (we owe)", inr(float(view["balance_due"].iloc[-1]) if len(view) else opening, 2))
        st.dataframe(view, hide_index=True, width="stretch", height=420,
                     column_config={**money_cols(view, ["bill_amount", "paid_amount", "balance_due"]),
                                    "bill_amount": st.column_config.NumberColumn("Bill (Cr)", format="₹%.2f"),
                                    "paid_amount": st.column_config.NumberColumn("Paid (Dr)", format="₹%.2f")})
        st.caption("Bounced cheques are shown with zero amount - they do not reduce the balance. "
                   "Match this with the supplier's statement every month.")
        st.download_button("⬇ Download ledger (CSV)", view.to_csv(index=False),
                           f"ledger_{names[lid].replace(' ', '_')}.csv", "text/csv")

# ------------------------------------------------------------------ cheques
with tab_chq:
    chq = analytics.cheque_register()
    pending = chq[chq["status"] == "Pending"]
    c1, c2, c3 = st.columns(3)
    c1.metric("Cheques not yet cleared", inr(pending["amount"].sum()), f"{len(pending)} cheques",
              delta_color="off")
    c2.metric("Post-dated (future date)", inr(pending.loc[pending["post_dated"], "amount"].sum()))
    c3.metric("Bounced (last 90 days)", int(((chq["status"] == "Bounced") &
                                               (pd.to_datetime(chq["issued_on"]) >
                                                pd.Timestamp(db.today() - timedelta(days=90)))).sum()))
    if len(pending):
        st.markdown("**Keep this much in the bank** - cheques that can be presented soon:")
        st.dataframe(pending[["supplier", "cheque_no", "bank", "issued_on", "cheque_date", "amount", "post_dated"]],
                     hide_index=True, width="stretch", column_config=money_cols(pending, ["amount"]))
        opts = {f"#{r.cheque_no} · {r.supplier} · {inr(r.amount, 2)} · dated {r.cheque_date}": int(r.id)
                for r in pending.itertuples()}
        c1, c2, c3 = st.columns([3, 1.2, 1.5])
        pick = c1.selectbox("Update cheque", list(opts))
        on = c2.date_input("On", value=db.today(), max_value=db.today(), format="DD/MM/YYYY")
        reason = c3.text_input("Bounce reason", placeholder="e.g. insufficient funds")
        b1, b2 = st.columns(2)
        if b1.button("✅ Mark cleared", width="stretch"):
            db.set_cheque_status(opts[pick], "Cleared", str(on))
            st.rerun()
        if b2.button("❌ Mark bounced", width="stretch"):
            db.set_cheque_status(opts[pick], "Bounced", str(on), reason or "Cheque bounced")
            st.warning("Marked bounced - the bills it was paying are open again.")
            st.rerun()
    st.markdown("**Cheque register**")
    st.dataframe(chq.drop(columns=["id"]), hide_index=True, width="stretch", height=300,
                 column_config=money_cols(chq, ["amount"]))

# ------------------------------------------------------------------ history
with tab_hist:
    days = st.segmented_control("Period", [30, 90, 365], default=90, format_func=lambda d: f"Last {d} days",
                                key="ph_days") or 90
    h = analytics.supplier_payment_history(days)
    ok = h[h["status"] != "Bounced"]
    by_mode = ok.groupby("mode")["amount"].sum().reset_index()
    c1, c2 = st.columns([1, 2])
    c1.metric("Paid to suppliers", inr(ok["amount"].sum()), f"{len(ok)} payments", delta_color="off")
    if len(by_mode):
        fig = px.pie(by_mode, names="mode", values="amount", hole=0.55, title="How we paid")
        fig.update_layout(height=260, margin=dict(t=40, b=0))
        c2.plotly_chart(fig, width="stretch")
    st.dataframe(h, hide_index=True, width="stretch", height=380, column_config=money_cols(h, ["amount"]))
    st.download_button("⬇ Download payments (CSV)", h.to_csv(index=False), "supplier_payments.csv", "text/csv")
