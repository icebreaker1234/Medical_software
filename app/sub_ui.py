"""Streamlit panel: 'customer asked for X, we don't have it -> same-formula options'."""
from __future__ import annotations

from typing import Callable

import streamlit as st

from app import db
from app import substitutes as S

REASON_TEXT = {"out_of_stock": "is out of stock", "not_stocked": "is not stocked here",
               "unknown": "was not found"}


def render_panel(query: str, qty: int, reason: str, key: str,
                 on_give: Callable[[int, int], None] | None = None,
                 on_close: Callable[[], None] | None = None) -> None:
    """Show exact substitutes for `query`; log the outcome to unmet_demand.

    on_give(product_id, qty) is called when the pharmacist gives a substitute (billing).
    Without on_give (finder page) the panel is read-only apart from logging.
    """
    products = db.products_df()
    req = S.resolve(query, products)
    with st.container(border=True):
        if req is None:
            st.markdown(f"**'{query}'** was not found in stock or in the brand list.")
            st.caption("Type the formula instead (e.g. *paracetamol 650mg*), or log it so it "
                       "shows up in the missed-demand report.")
            c1, c2 = st.columns(2)
            if c1.button("❌ Log as missed request", key=f"{key}_unk"):
                db.log_unmet(query, qty, "unknown", "lost")
                st.toast("Logged as missed request")
                _close(on_close)
            if c2.button("Close", key=f"{key}_x0"):
                _close(on_close)
            return

        reason = reason if req["source"] != "brand" else "not_stocked"
        form = " · ".join(x for x in (req.get("dosage_form"), req.get("release_type")) if x)
        st.markdown(f"🔁 Customer asked for **{req['requested']}** × {qty} — it {REASON_TEXT[reason]}.")
        st.caption(f"Formula: **{req['composition']}**" + (f" · {form}" if form else "")
                   + ("  ·  *from brand list*" if req["source"] == "brand" else ""))

        exact, near = S.find(req, qty, products)
        if exact.empty:
            st.warning("No medicine with the exact same formula is in stock.")
        else:
            show = exact.head(5).assign(expires_in=lambda d: d["days_to_expiry"].astype("Int64"))
            cols = ["name", "stock", "mrp", "unit_price", "expires_in", "why"]
            cfg = {"name": "Same-formula medicine", "mrp": st.column_config.NumberColumn("MRP", format="₹%.2f"),
                   "unit_price": st.column_config.NumberColumn("₹ / unit", format="₹%.2f"),
                   "expires_in": st.column_config.NumberColumn("Expires in (days)"),
                   "why": st.column_config.TextColumn("Why suggested", width="large")}
            if exact["saves_per_unit"].notna().any():
                cols.insert(4, "saves_per_unit")
                cfg["saves_per_unit"] = st.column_config.NumberColumn("Customer saves / unit", format="₹%.2f")
            st.dataframe(show[cols], hide_index=True, width="stretch", column_config=cfg)
            if not on_give and show["caution"].str.len().gt(0).any():
                for n, c in zip(show["name"], show["caution"]):
                    if c:
                        st.caption(f"⚠️ **{n}**: {c}")

            if on_give:
                names = list(show["name"])
                c1, c2 = st.columns([3, 1])
                pick = c1.selectbox("Give instead", names, key=f"{key}_pick")
                row = show[show["name"] == pick].iloc[0]
                give_qty = c2.number_input("Qty", 1, int(row["stock"]), min(qty, int(row["stock"])),
                                           key=f"{key}_q")
                if row["caution"]:
                    st.warning(row["caution"])
                consent = True
                if S.needs_consent(req, row):
                    consent = st.checkbox("Prescriber / customer has agreed to this brand change",
                                          key=f"{key}_ok")
                if st.button(f"✅ Give {pick} instead", type="primary", disabled=not consent,
                             key=f"{key}_give"):
                    db.log_unmet(req["requested"], int(give_qty), reason, "substituted",
                                 requested_product_id=req.get("product_id"),
                                 composition=req["composition"], comp_key=req.get("comp_key"),
                                 given_product_id=int(row["id"]),
                                 note="consent recorded" if S.needs_consent(req, row) else None,
                                 est_value=round(float(row["mrp"]) * int(give_qty), 2))
                    on_give(int(row["id"]), int(give_qty))
                    _close(on_close)

        if len(near):
            with st.expander(f"⚠️ {len(near)} similar but NOT interchangeable (different strength / form / release)"):
                st.dataframe(near[["name", "composition", "dosage_form", "release_type", "stock", "difference"]],
                             hide_index=True, width="stretch")
                st.caption("These need a new prescription or the prescriber's instruction - do not swap.")

        c1, c2, c3 = st.columns(3)
        common = dict(requested_product_id=req.get("product_id"), composition=req["composition"],
                      comp_key=req.get("comp_key"))
        if c1.button("❌ Customer declined (lost sale)", key=f"{key}_lost", width="stretch"):
            db.log_unmet(req["requested"], qty, reason, "lost", **common)
            st.toast("Logged as lost sale - it will show in Missed demand")
            _close(on_close)
        if c2.button("📦 Order for customer", key=f"{key}_ord", width="stretch"):
            db.log_unmet(req["requested"], qty, reason, "ordered", **common)
            st.toast("Added to the missed-demand order list")
            _close(on_close)
        if c3.button("Close", key=f"{key}_x", width="stretch"):
            _close(on_close)
        st.caption("Software suggests only - the pharmacist confirms. Every substitution is logged.")


def _close(on_close):
    if on_close:
        on_close()
    st.rerun()

