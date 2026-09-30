"""Streamlit entry point:   streamlit run app/main.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import streamlit as st  # noqa: E402

from app import db  # noqa: E402
from app.ui import STORE_NAME, setup  # noqa: E402

st.set_page_config(page_title=STORE_NAME, page_icon="💊", layout="wide")
setup()


@st.cache_resource
def _init():
    db.init_db()
    return True


_init()

pages = {
    "Store": [
        st.Page("pages/dashboard.py", title="Dashboard", icon="🏠", default=True),
        st.Page("pages/billing.py", title="Billing (POS)", icon="🧾"),
        st.Page("pages/inventory.py", title="Inventory", icon="📦"),
        st.Page("pages/purchases.py", title="Purchases", icon="🚚"),
        st.Page("pages/suppliers.py", title="Suppliers", icon="🏭"),
        st.Page("pages/supplier_payments.py", title="Supplier Payments", icon="💸"),
        st.Page("pages/customers.py", title="Customers", icon="👥"),
        st.Page("pages/reports.py", title="Reports & GST", icon="📊"),
    ],
    "Intelligence": [
        st.Page("pages/substitutes.py", title="Substitutes & Missed Demand", icon="🔁"),
        st.Page("pages/expiry.py", title="Expiry & Dead Stock", icon="⏳"),
        st.Page("pages/forecast.py", title="Demand Forecast & Reorder", icon="📈"),
    ],
    "MLOps": [
        st.Page("pages/mlops.py", title="Model Monitoring", icon="🛠️"),
    ],
}
nav = st.navigation(pages)
with st.sidebar:
    st.caption(f"**{STORE_NAME}**  \nJaipur · {db.today():%d %b %Y}")
    st.caption("Demo data · Schedule classifications are illustrative")
nav.run()
