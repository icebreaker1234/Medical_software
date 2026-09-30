import pandas as pd
import streamlit as st

from app import accounts, tenancy
from app.accounts import AuthError

st.title("🔐 Team & logins")
t = tenancy.current()
if not t or t["role"] != "owner":
    st.error("Only the store owner can manage logins.")
    st.stop()

st.caption("Every person gets their own user ID, so the audit log shows who did what. "
           "**Pharmacist**: billing, stock, purchases, customers (no profit, payments or settings). "
           "**Cashier**: billing and customers only.")

members = pd.DataFrame(accounts.list_members(t["store_id"]))
st.dataframe(members[["user_id", "name", "role", "member_active", "last_login", "must_change_password"]]
             .rename(columns={"member_active": "active", "must_change_password": "temp password"}),
             hide_index=True, width="stretch",
             column_config={"active": st.column_config.CheckboxColumn(),
                            "temp password": st.column_config.CheckboxColumn()})

left, right = st.columns(2)
with left:
    st.subheader("Add a staff login")
    with st.form("add_staff", clear_on_submit=True):
        name = st.text_input("Name")
        uid = st.text_input("User ID", placeholder="e.g. ramesh.counter1")
        role = st.selectbox("Role", ["cashier", "pharmacist"])
        pw = st.text_input("Temporary password (they must change it at first login)", type="password")
        if st.form_submit_button("Create login", type="primary"):
            try:
                accounts.add_staff(t["user_ref"], t["store_id"], uid, name, role, pw)
                st.success(f"Login created for {uid}. Tell them the temporary password in person.")
                st.rerun()
            except AuthError as e:
                st.error(str(e))

with right:
    st.subheader("Manage a staff login")
    staff = members[members["role"] != "owner"]
    if staff.empty:
        st.info("No staff logins yet.")
    else:
        opts = {f"{r.user_id} · {r.role} · {'active' if r.member_active else 'inactive'}": r.user_ref
                for r in staff.itertuples()}
        pick = st.selectbox("Staff member", list(opts))
        ref = opts[pick]
        active = bool(staff.loc[staff["user_ref"] == ref, "member_active"].iloc[0])
        if st.button("🚫 Deactivate login" if active else "✅ Re-activate login"):
            accounts.set_staff_active(t["user_ref"], t["store_id"], ref, not active)
            st.rerun()
        with st.form("reset_staff", clear_on_submit=True):
            pw = st.text_input("New temporary password", type="password")
            if st.form_submit_button("Reset their password"):
                try:
                    accounts.reset_staff_password(t["user_ref"], t["store_id"], ref, pw)
                    st.success("Password reset - they must choose a new one at next login.")
                except AuthError as e:
                    st.error(str(e))

st.subheader("Login activity")
log = pd.DataFrame(accounts.audit_trail(t["store_id"], 100))
st.dataframe(log, hide_index=True, width="stretch", height=300)
