"""Stage 3 - feature engineering.

All lag / rolling features are shifted so that a row only uses information
available BEFORE that day (no target leakage). The same function is reused at
serving time for recursive multi-day forecasting.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.config import DAILY_LONG, FEATURES, params


def feature_columns() -> list[str]:
    p = params()["features"]
    cols = ["cat_code", "dow", "month", "is_weekend", "doy_sin", "doy_cos"]
    cols += [f"lag_{l}" for l in p["lags"]]
    for w in p["windows"]:
        cols += [f"roll_mean_{w}", f"roll_std_{w}"]
    return cols


def category_codes() -> dict[str, int]:
    return {c: i for i, c in enumerate(params()["categories"])}


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    """df: [date, category, sales] (sales may be NaN for future rows)."""
    p = params()["features"]
    df = df.sort_values(["category", "date"]).copy()
    df["date"] = pd.to_datetime(df["date"])
    df["cat_code"] = df["category"].map(category_codes())
    df["dow"] = df["date"].dt.dayofweek
    df["month"] = df["date"].dt.month
    df["is_weekend"] = (df["dow"] >= 5).astype(int)
    doy = df["date"].dt.dayofyear
    df["doy_sin"] = np.sin(2 * np.pi * doy / 365.25)
    df["doy_cos"] = np.cos(2 * np.pi * doy / 365.25)

    g = df.groupby("category")["sales"]
    for lag in p["lags"]:
        df[f"lag_{lag}"] = g.shift(lag)
    shifted = g.shift(1)
    for w in p["windows"]:
        roll = shifted.groupby(df["category"]).rolling(w, min_periods=max(2, w // 2))
        df[f"roll_mean_{w}"] = roll.mean().reset_index(level=0, drop=True)
        df[f"roll_std_{w}"] = roll.std().reset_index(level=0, drop=True)
    return df


def main() -> None:
    df = pd.read_csv(DAILY_LONG, parse_dates=["date"])
    feats = add_features(df).dropna(subset=feature_columns())
    feats.to_csv(FEATURES, index=False)
    print(f"Built {len(feats)} feature rows x {len(feature_columns())} features -> {FEATURES}")


if __name__ == "__main__":
    main()
