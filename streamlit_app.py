"""Streamlit entry point (repo root):   streamlit run streamlit_app.py

On your own computer: owners log in and each store has its own database file.
On Streamlit Community Cloud (or PHARMACY_MODE=demo): no login, shared sample data.
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("TZ", "Asia/Kolkata")          # Indian store: dates follow IST
if hasattr(time, "tzset"):
    time.tzset()

import streamlit as st  # noqa: E402

from app import db, tenancy  # noqa: E402
from app.ui import setup  # noqa: E402

st.set_page_config(page_title="Pharmacy Store", page_icon="💊", layout="wide")
setup()


@st.cache_resource
def _init():
    # On a fresh server (e.g. Streamlit Cloud) the model is not in git - train it once
    from src.config import MODEL_FILE
    if not MODEL_FILE.exists():
        with st.spinner("First start: training the model (about 30 seconds)..."):
            from src.data import generate, prepare
            from src.features import build
            from src.models import train
            from src.monitoring import drift
            for step in (generate, prepare, build, train, drift):
                step.main()
    if tenancy.mode() == "demo":
        db.init_db()                                  # shared sample store
    return True


_init()

if tenancy.mode() == "stores":
    from app.auth_ui import logout, require_login
    tenant = require_login()
else:
    tenancy.set_tenant(None)
    tenant = None

SPEC = {   # group -> (page key = file name, title, icon)
    "Store": [("dashboard", "Dashboard", "🏠"), ("billing", "Billing (POS)", "🧾"),
              ("inventory", "Inventory", "📦"), ("purchases", "Purchases", "🚚"),
              ("suppliers", "Suppliers", "🏭"), ("supplier_payments", "Supplier Payments", "💸"),
              ("customers", "Customers", "👥"), ("reports", "Reports & GST", "📊"),
              ("import_data", "Import data", "📥")],
    "Intelligence": [("substitutes", "Substitutes & Missed Demand", "🔁"),
                     ("expiry", "Expiry & Dead Stock", "⏳"),
                     ("forecast", "Demand Forecast & Reorder", "📈")],
    "MLOps": [("mlops", "Model Monitoring", "🛠️")],
}
if tenant:
    SPEC["Account"] = [("team", "Team & logins", "🔐"), ("store_settings", "Store settings & backup", "⚙️"),
                       ("account", "My account", "👤")]

allowed = {g: [x for x in items if tenancy.can_open(x[0])] for g, items in SPEC.items()}
allowed = {g: items for g, items in allowed.items() if items}
first = next(iter(allowed.values()))[0][0]             # owner -> Dashboard, staff -> Billing
pages = {g: [st.Page(f"app/pages/{k}.py", title=t, icon=i, default=(k == first)) for k, t, i in items]
         for g, items in allowed.items()}
nav = st.navigation(pages, expanded=True)

info = tenancy.store_info()
with st.sidebar:
    st.caption(f"**{info['name']}**  \n{info['city'] or ''} · {db.today():%d %b %Y}")
    if tenant:
        st.caption(f"👤 {tenant.get('user_name') or tenant['user_id']} · {tenant['role']}")
        if st.button("Log out", width="stretch"):
            logout()
    else:
        st.caption("Demo mode - sample data, no login")
nav.run()
