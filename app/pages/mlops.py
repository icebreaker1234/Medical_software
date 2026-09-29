import json
import os

import pandas as pd
import plotly.express as px
import streamlit as st

from app.ui import API_URL
from src.config import DRIFT_FILE, REPORTS_DIR, mlflow_uri
from src.models.predict import load_model

st.title("🛠️ Model Monitoring (MLOps)")

try:
    _, meta = load_model()
except FileNotFoundError:
    st.error("Model not trained yet. Run `python -m src.pipeline`.")
    st.stop()

c1, c2, c3, c4 = st.columns(4)
LABEL = {"xgboost_level_normalized": "XGBoost (level)", "xgboost_raw": "XGBoost (raw)"}
c1.metric("Champion model", LABEL.get(meta["champion"], meta["champion"]), help=meta["champion"])
c2.metric("Registry version", f"v{meta['registered_version']} @champion")
c3.metric("Trained on", meta["trained_at"][:10], help=f"{meta['trained_at']} UTC")
c4.metric("Data up to", meta["data_end"])

# ---- API health ----------------------------------------------------------------
try:
    import httpx
    h = httpx.get(f"{API_URL}/health", timeout=2).json()
    mon = httpx.get(f"{API_URL}/monitoring", timeout=2).json()["requests"]
    st.success(f"Model API at {API_URL} is **{h['status']}** · requests served: {mon['total']} "
               f"· errors: {mon['errors']}")
except Exception:
    st.warning(f"Model API not reachable at {API_URL} - dashboard is using the in-process model. "
               "Start it with `uvicorn api.main:app --port 8000`.")

tab_cmp, tab_cat, tab_drift, tab_runs = st.tabs(["Model comparison", "Per-category accuracy",
                                                 "Drift & performance", "MLflow runs"])
with tab_cmp:
    m = pd.DataFrame(meta["metrics"]).T.reset_index().rename(columns={"index": "model"})
    m["wape_%"] = (m["wape"] * 100).round(1)
    m["role"] = m["model"].eq(meta["champion"]).map({True: "champion", False: "challenger / baseline"})
    fig = px.bar(m.sort_values("wape"), x="model", y="wape_%", text="wape_%", color="role",
                 title="Hold-out error by model (lower is better)",
                 color_discrete_map={"champion": "#059669", "challenger / baseline": "#94a3b8"},
                 labels={"wape_%": "WAPE %", "model": "", "role": ""})
    fig.update_layout(barmode="overlay", xaxis={"categoryorder": "total ascending"})
    st.plotly_chart(fig, width="stretch")
    st.dataframe(m.drop(columns="role").round(3), hide_index=True, width="stretch")
    img = REPORTS_DIR / f"feature_importance_{meta['champion']}.png"
    if img.exists():
        st.image(str(img), caption="Feature importance of the champion model", width=520)

with tab_cat:
    pc = pd.DataFrame(meta["per_category"]).T.reset_index().rename(columns={"index": "category"})
    pc["wape_%"] = (pc["wape"] * 100).round(1)
    st.plotly_chart(px.bar(pc, x="category", y="wape_%", title="WAPE by category",
                           labels={"wape_%": "WAPE %"}), width="stretch")
    st.caption("Low-volume categories (e.g. N05C sleep medicines) are harder to forecast day by day - "
               "their percentage error is naturally higher.")

with tab_drift:
    if not DRIFT_FILE.exists():
        st.info("Run `python -m src.monitoring.drift` to create the drift report.")
    else:
        d = json.loads(DRIFT_FILE.read_text())
        a, b, c = st.columns(3)
        a.metric("Categories drifted", f"{d['categories_drifted']} / {d['categories_checked']}")
        perf = d.get("performance") or {}
        b.metric("Recent WAPE vs hold-out", f"{perf.get('recent_wape', 0) * 100:.1f}%",
                 f"{perf.get('degradation_pct', 0):+.1f}%", delta_color="inverse")
        c.metric("Retrain recommended", "YES" if d["retrain_recommended"] else "no")
        st.caption(f"Current window {d.get('current_window')} vs same season last year "
                   f"{d.get('reference_window')} · generated {d['generated_at']}")
        dd = pd.DataFrame(d["details"])
        st.dataframe(dd, hide_index=True, width="stretch", column_config={
            "drift": st.column_config.CheckboxColumn("Drift?")})
        html = d.get("evidently_report")
        if html and os.path.exists(html):
            with open(html, encoding="utf-8") as f:
                st.download_button("⬇ Evidently drift report (HTML)", f.read(), "drift_report.html")
        st.caption("Retraining is triggered when at least half the categories drift or the recent error "
                   "is > 20% worse than at training time. In CI this runs on a schedule.")

with tab_runs:
    try:
        import mlflow
        mlflow.set_tracking_uri(mlflow_uri())
        runs = mlflow.search_runs(experiment_names=[meta.get("experiment", "pharmacy-demand-forecasting")],
                                  order_by=["start_time DESC"], max_results=50)
        cols = [c for c in ["start_time", "tags.mlflow.runName", "metrics.wape", "metrics.mae",
                            "metrics.rmse", "params.model_type", "run_id"] if c in runs.columns]
        st.dataframe(runs[cols], hide_index=True, width="stretch")
        st.caption("Open the full MLflow UI with `mlflow ui --backend-store-uri sqlite:///mlflow.db`")
    except Exception as e:
        st.info(f"MLflow runs unavailable: {e}")
