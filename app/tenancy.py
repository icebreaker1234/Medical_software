"""Which store (and which user / role) the current request belongs to.

In the Streamlit app the tenant lives in st.session_state - one per browser session,
so two logged-in owners can never see each other's store. Outside Streamlit (tests,
scripts) a ContextVar is used instead.

Modes
* "stores" (default on your own computer): login required, one database file per store.
* "demo" (automatic on Streamlit Cloud, or PHARMACY_MODE=demo): no login, one shared demo
  database with sample data - Streamlit Cloud wipes files on restart, so no real data there.
"""
from __future__ import annotations

import contextvars
import os
from pathlib import Path

_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar("tenant", default=None)

# pages each role may open (file names in app/pages)
ROLE_PAGES = {
    "owner": None,   # everything
    "pharmacist": {"billing", "inventory", "purchases", "suppliers", "customers", "substitutes",
                   "expiry", "forecast", "account"},
    "cashier": {"billing", "customers", "substitutes", "account"},
}


def mode() -> str:
    m = os.getenv("PHARMACY_MODE", "").lower()
    if m in ("demo", "stores"):
        return m
    return "demo" if Path("/mount/src").exists() else "stores"   # /mount/src = Streamlit Community Cloud


def _session_state():
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        if get_script_run_ctx() is None:
            return None
        import streamlit as st
        return st.session_state
    except Exception:
        return None


def set_tenant(tenant: dict | None) -> None:
    ss = _session_state()
    if ss is not None:
        if tenant is None:
            ss.pop("tenant", None)
        else:
            ss["tenant"] = tenant
    _ctx.set(tenant)


def current() -> dict | None:
    ss = _session_state()
    if ss is not None:
        return ss.get("tenant")
    return _ctx.get()


def db_path() -> str | None:
    t = current()
    return t.get("db_path") if t else None


def role() -> str:
    t = current()
    return (t or {}).get("role", "owner")


def is_owner() -> bool:
    return role() == "owner"


def can_open(page: str) -> bool:
    allowed = ROLE_PAGES.get(role())
    return allowed is None or page in allowed


def store_info() -> dict:
    """Store details for invoices / messages. Falls back to env vars in demo mode."""
    t = current() or {}
    return {
        "name": t.get("store_name") or os.getenv("STORE_NAME", "Sanjeevani Medical Store"),
        "city": t.get("city") or os.getenv("STORE_CITY", "Jaipur, Rajasthan"),
        "address": t.get("address") or "",
        "gstin": t.get("gstin") or os.getenv("STORE_GSTIN", "08ABCDE1234F1Z5"),
        "drug_licence": t.get("drug_licence") or os.getenv("STORE_DL", "RJ-JPR-20B-XXXX / 21B-XXXX"),
        "phone": t.get("phone") or "",
        "upi_id": t.get("upi_id") or None,
    }
