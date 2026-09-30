"""Streamlit entry point (repo root):   streamlit run streamlit_app.py"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from app import db  # noqa: E402
from app.ui import STORE_NAME, setup  # noqa: E402

st.set_page_config(page_title=STORE_NAME, page_icon="💊", layout="wide")
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
    db.init_db()
    return True


_init()

pages = {
    "Store": [
        st.Page("app/pages/dashboard.py", title="Dashboard", icon="🏠", default=True),
        st.Page("app/pages/billing.py", title="Billing (POS)", icon="🧾"),
        st.Page("app/pages/inventory.py", title="Inventory", icon="📦"),
        st.Page("app/pages/purchases.py", title="Purchases", icon="🚚"),
        st.Page("app/pages/suppliers.py", title="Suppliers", icon="🏭"),
        st.Page("app/pages/customers.py", title="Customers", icon="👥"),
        st.Page("app/pages/reports.py", title="Reports & GST", icon="📊"),
    ],
    "Intelligence": [
        st.Page("app/pages/substitutes.py", title="Substitutes & Missed Demand", icon="🔁"),
        st.Page("app/pages/expiry.py", title="Expiry & Dead Stock", icon="⏳"),
        st.Page("app/pages/forecast.py", title="Demand Forecast & Reorder", icon="📈"),
    ],
    "MLOps": [
        st.Page("app/pages/mlops.py", title="Model Monitoring", icon="🛠️"),
    ],
}
nav = st.navigation(pages)
with st.sidebar:
    st.caption(f"**{STORE_NAME}**  \nJaipur · {db.today():%d %b %Y}")
    st.caption("Demo data · Schedule classifications are illustrative")
nav.run()
