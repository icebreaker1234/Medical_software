"""Login, sign-up, forgot-password and forced password-change screens."""
from __future__ import annotations

import time

import streamlit as st

from app import accounts, tenancy
from app.accounts import AuthError

IDLE_MINUTES = 30


def _start_session(tenant: dict) -> None:
    tenancy.set_tenant(tenant)
    st.session_state.last_active = time.time()
    try:
        accounts.backup_if_due(tenant["store_id"])          # daily automatic backup
    except Exception:
        pass


def logout(reason: str | None = None) -> None:
    t = tenancy.current()
    if t:
        accounts.audit("logout", t["user_id"], t["store_id"], reason)
    for k in list(st.session_state.keys()):
        del st.session_state[k]
    if reason:
        st.session_state.logout_reason = reason
    st.rerun()


def require_login() -> dict:
    """Show the login screens until a user is logged in; returns the tenant."""
    t = tenancy.current()
    if t:
        if time.time() - st.session_state.get("last_active", 0) > IDLE_MINUTES * 60:
            logout(f"Logged out after {IDLE_MINUTES} minutes without activity")
        st.session_state.last_active = time.time()
        if t.get("must_change_password"):
            _only_page(lambda: _force_password_change(t), "Set password")
        return t
    _only_page(_login_screens, "Log in")


def _only_page(fn, title: str) -> None:
    """Show a single screen with no sidebar menu (hides pages from before logout)."""
    st.navigation([st.Page(fn, title=title, icon="💊", url_path="login")], position="hidden").run()
    st.stop()


def _login_screens() -> None:
    _, mid, _ = st.columns([1, 1.4, 1])
    with mid:
        st.markdown("## 💊 Pharmacy Store")
        st.caption("Billing · Inventory · Forecasting - your store's data stays on this computer")
        if st.session_state.get("logout_reason"):
            st.info(st.session_state.pop("logout_reason"))

        pending = st.session_state.get("pending_signup")
        if pending:
            _show_recovery_code(pending)
            return

        tab_in, tab_new, tab_forgot = st.tabs(["Log in", "Create store account", "Forgot password"])
        with tab_in:
            with st.form("login"):
                uid = st.text_input("User ID")
                pw = st.text_input("Password", type="password")
                if st.form_submit_button("Log in", type="primary", width="stretch"):
                    try:
                        _start_session(accounts.login(uid, pw))
                        st.rerun()
                    except AuthError as e:
                        st.error(str(e))
        with tab_new:
            st.caption("For the store OWNER. Your store gets its own separate database. "
                       "Staff logins are created later by you, inside the app.")
            with st.form("signup"):
                store = st.text_input("Store name", placeholder="e.g. Sanjeevani Medical Store")
                city = st.text_input("City", placeholder="e.g. Jaipur")
                name = st.text_input("Your name")
                uid = st.text_input("Choose a user ID", placeholder="e.g. sanjeevani.owner or your email")
                pw = st.text_input("Password (8+ characters, letters and numbers)", type="password")
                pw2 = st.text_input("Repeat password", type="password")
                if st.form_submit_button("Create my store", type="primary", width="stretch"):
                    try:
                        if pw != pw2:
                            raise AuthError("Passwords do not match")
                        res = accounts.create_owner(uid, pw, name, store, city)
                        tenant = accounts.login(uid, pw)
                        st.session_state.pending_signup = {"code": res["recovery_code"], "tenant": tenant}
                        st.rerun()
                    except AuthError as e:
                        st.error(str(e))
        with tab_forgot:
            st.caption("**Owner:** use the recovery code you saved at sign-up.  \n"
                       "**Staff:** ask your store owner to reset your password (Team page).")
            with st.form("forgot"):
                uid = st.text_input("User ID ")
                code = st.text_input("Recovery code", placeholder="XXXX-XXXX-XXXX-XXXX")
                pw = st.text_input("New password", type="password")
                if st.form_submit_button("Reset password", width="stretch"):
                    try:
                        new_code = accounts.reset_with_recovery_code(uid, code, pw)
                        st.success("Password changed. Your old recovery code no longer works - "
                                   "save this NEW one:")
                        st.code(new_code, language=None)
                    except AuthError as e:
                        st.error(str(e))


def _show_recovery_code(pending: dict) -> None:
    st.success(f"Store **{pending['tenant']['store_name']}** created with its own database.")
    st.warning("**Save your recovery code now.** It is the only way to reset your password if you "
               "forget it. It is shown only once - write it down or keep it in a password manager.")
    st.code(pending["code"], language=None)
    ok = st.checkbox("I have saved my recovery code")
    if st.button("Open my store", type="primary", disabled=not ok, width="stretch"):
        st.session_state.pop("pending_signup")
        _start_session(pending["tenant"])
        st.rerun()


def _force_password_change(t: dict) -> None:
    _, mid, _ = st.columns([1, 1.4, 1])
    with mid:
        st.markdown("### Set your own password")
        st.caption(f"Welcome {t.get('user_name') or t['user_id']}. The owner gave you a temporary "
                   "password - choose your own before continuing.")
        with st.form("first_pw"):
            old = st.text_input("Temporary password", type="password")
            new = st.text_input("New password (8+ characters, letters and numbers)", type="password")
            new2 = st.text_input("Repeat new password", type="password")
            if st.form_submit_button("Save password", type="primary", width="stretch"):
                try:
                    if new != new2:
                        raise AuthError("Passwords do not match")
                    accounts.change_password(t["user_ref"], old, new)
                    tenancy.set_tenant({**t, "must_change_password": False})
                    st.rerun()
                except AuthError as e:
                    st.error(str(e))
        if st.button("Log out"):
            logout()
