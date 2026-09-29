"""Shared UI helpers: Indian number format, KPI cards, forecast client."""
from __future__ import annotations

import os

import pandas as pd
import streamlit as st

from app.fmt import inr, lakh  # noqa: F401  (re-exported for pages)

API_URL = os.getenv("API_URL", "http://localhost:8000")
STORE_NAME = os.getenv("STORE_NAME", "Sanjeevani Medical Store")
STORE_GSTIN = os.getenv("STORE_GSTIN", "08ABCDE1234F1Z5")
STORE_DL = os.getenv("STORE_DL", "RJ-JPR-20B-XXXX / 21B-XXXX")


CSS = """
<style>
.block-container {padding-top: 1.6rem;}
.kpi {border:1px solid rgba(128,128,128,.25); border-radius:12px; padding:14px 16px;
      background: rgba(127,127,127,.04);}
.kpi .label {font-size:.82rem; opacity:.75; text-transform:uppercase; letter-spacing:.04em;}
.kpi .value {font-size:1.9rem; font-weight:700; line-height:1.2; margin-top:4px;}
.kpi .sub {font-size:.8rem; opacity:.7;}
.kpi.warn {border-left:5px solid #d97706;} .kpi.bad {border-left:5px solid #dc2626;}
.kpi.good {border-left:5px solid #059669;} .kpi.info {border-left:5px solid #2563eb;}
</style>
"""


def setup():
    st.markdown(CSS, unsafe_allow_html=True)


def kpi(col, label: str, value: str, sub: str = "", tone: str = "info"):
    col.markdown(f'<div class="kpi {tone}"><div class="label">{label}</div>'
                 f'<div class="value">{value}</div><div class="sub">{sub}</div></div>',
                 unsafe_allow_html=True)


@st.cache_data(ttl=600, show_spinner=False)
def get_forecast(category: str, horizon: int = 14) -> tuple[pd.DataFrame, str]:
    """Call the FastAPI model service; fall back to in-process model if the API is down."""
    try:
        import httpx
        r = httpx.post(f"{API_URL}/predict", json={"category": category, "horizon": horizon},
                       timeout=3)
        r.raise_for_status()
        return pd.DataFrame(r.json()["points"]), "API"
    except Exception:
        from src.models.predict import forecast
        return forecast(category, horizon), "local model"


def all_forecasts(horizon: int = 21) -> dict[str, pd.DataFrame]:
    from app.db import MODEL_CATEGORIES
    out = {}
    for c in MODEL_CATEGORIES:
        try:
            out[c] = get_forecast(c, horizon)[0]
        except Exception:
            pass
    return out


def money_cols(df: pd.DataFrame, cols: list[str]) -> dict:
    return {c: st.column_config.NumberColumn(c.replace("_", " ").title(), format="₹%.0f")
            for c in cols if c in df.columns}
