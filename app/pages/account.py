import streamlit as st

from app import accounts, tenancy
from app.accounts import AuthError

st.title("👤 My account")
t = tenancy.current()
if not t:
    st.info("Demo mode - there are no logins.")
    st.stop()

st.markdown(f"**User ID:** {t['user_id']}  \n**Role:** {t['role']}  \n**Store:** {t['store_name']}")

st.subheader("Change password")
with st.form("pw", clear_on_submit=True):
    old = st.text_input("Current password", type="password")
    new = st.text_input("New password (8+ characters, letters and numbers)", type="password")
    new2 = st.text_input("Repeat new password", type="password")
    if st.form_submit_button("Change password", type="primary"):
        try:
            if new != new2:
                raise AuthError("Passwords do not match")
            accounts.change_password(t["user_ref"], old, new)
            st.success("Password changed.")
        except AuthError as e:
            st.error(str(e))

if t["role"] == "owner":
    st.subheader("Recovery code")
    st.caption("Lost your recovery code? Make a new one - the old code stops working immediately.")
    with st.form("rc", clear_on_submit=True):
        pw = st.text_input("Confirm your password", type="password")
        if st.form_submit_button("Create new recovery code"):
            try:
                st.warning("Save this code now - it will not be shown again.")
                st.code(accounts.new_recovery_code(t["user_ref"], pw), language=None)
            except AuthError as e:
                st.error(str(e))
