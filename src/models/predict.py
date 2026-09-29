"""Recursive multi-day forecasting with prediction intervals.

Used by both the FastAPI service and the Streamlit dashboard.
Interval = point forecast + empirical residual quantiles from the hold-out
period. Day-to-day noise dominates the error, but recursive forecasts also
accumulate error, so the band is widened gently: sqrt(1 + (step-1)/7).
This is a simple, explainable heuristic - not a statistical guarantee.
"""
from __future__ import annotations

import json
from functools import lru_cache

import joblib
import numpy as np
import pandas as pd

from src.config import DAILY_LONG, METADATA_FILE, MODEL_FILE
from src.features.build import add_features, feature_columns


@lru_cache(maxsize=1)
def load_model():
    if not MODEL_FILE.exists():
        raise FileNotFoundError("Model not trained yet. Run: python -m src.pipeline")
    return joblib.load(MODEL_FILE), json.loads(METADATA_FILE.read_text())


@lru_cache(maxsize=1)
def _history_cached(mtime: float) -> pd.DataFrame:
    return pd.read_csv(DAILY_LONG, parse_dates=["date"])


def load_history() -> pd.DataFrame:
    """Cached read that refreshes automatically when the pipeline rewrites the file."""
    return _history_cached(DAILY_LONG.stat().st_mtime).copy()


def forecast(category: str, horizon: int = 14, history: pd.DataFrame | None = None) -> pd.DataFrame:
    model, meta = load_model()
    if category not in meta["categories"]:
        raise ValueError(f"Unknown category '{category}'. Valid: {list(meta['categories'])}")
    if not 1 <= horizon <= 60:
        raise ValueError("horizon must be between 1 and 60 days")

    hist = history if history is not None else load_history()
    hist = hist[hist["category"] == category][["date", "category", "sales"]].copy()
    hist = hist.sort_values("date").tail(70)           # longest lag (28) + window (28) + margin
    q = meta["residual_quantiles"][category]
    cols = feature_columns()

    rows = []
    for step in range(1, horizon + 1):
        next_day = hist["date"].max() + pd.Timedelta(days=1)
        frame = pd.concat([hist, pd.DataFrame({"date": [next_day], "category": [category],
                                               "sales": [np.nan]})], ignore_index=True)
        feats = add_features(frame).iloc[[-1]][cols]
        yhat = float(max(model.predict(feats)[0], 0.0))
        widen = np.sqrt(1 + (step - 1) / 7)
        rows.append({
            "date": next_day.date().isoformat(),
            "forecast": round(yhat, 2),
            "lower": round(max(yhat + q["low"] * widen, 0.0), 2),
            "upper": round(yhat + q["high"] * widen, 2),
        })
        hist = pd.concat([hist, pd.DataFrame({"date": [next_day], "category": [category],
                                              "sales": [yhat]})], ignore_index=True)
    return pd.DataFrame(rows)
