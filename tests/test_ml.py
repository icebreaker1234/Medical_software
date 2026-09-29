import numpy as np
import pandas as pd

from src.features.build import add_features, feature_columns
from src.models.predict import forecast
from src.monitoring.drift import compute_drift, psi


def _toy(n=60):
    dates = pd.date_range("2024-01-01", periods=n)
    return pd.DataFrame({"date": dates, "category": "N02BE", "sales": np.arange(n, dtype=float)})


def test_lag_features_do_not_leak_target():
    f = add_features(_toy())
    row = f.iloc[40]
    assert row["lag_1"] == row["sales"] - 1          # yesterday, not today
    assert row["lag_7"] == row["sales"] - 7
    # rolling mean uses only past days: mean of days t-7..t-1
    assert np.isclose(row["roll_mean_7"], np.mean(np.arange(33, 40)))


def test_feature_columns_present():
    f = add_features(_toy())
    assert set(feature_columns()).issubset(f.columns)


def test_forecast_shape_and_interval():
    fc = forecast("N02BE", horizon=10)
    assert len(fc) == 10
    assert (fc["forecast"] >= 0).all()
    assert (fc["lower"] <= fc["forecast"]).all() and (fc["forecast"] <= fc["upper"]).all()
    assert pd.to_datetime(fc["date"]).is_monotonic_increasing


def test_forecast_rejects_unknown_category():
    import pytest
    with pytest.raises(ValueError):
        forecast("XXX", 5)


def test_psi_detects_shift():
    rng = np.random.default_rng(0)
    ref = rng.poisson(20, 500)
    assert psi(ref, rng.poisson(20, 300)) < 0.1
    assert psi(ref, rng.poisson(35, 300)) > 0.25


def test_compute_drift_flags_shifted_category():
    rng = np.random.default_rng(1)
    ref = pd.DataFrame({"category": ["A"] * 300 + ["B"] * 300,
                        "sales": np.r_[rng.poisson(10, 300), rng.poisson(10, 300)]})
    cur = pd.DataFrame({"category": ["A"] * 30 + ["B"] * 30,
                        "sales": np.r_[rng.poisson(10, 30), rng.poisson(25, 30)]})
    res = {d["category"]: d["drift"] for d in compute_drift(ref, cur)["details"]}
    assert res["B"] and not res["A"]
