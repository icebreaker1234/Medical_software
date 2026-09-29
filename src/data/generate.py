"""Stage 1 - produce data/raw/salesdaily.csv.

If params.data.source == "kaggle" and the Kaggle file exists, it is used as-is.
Otherwise a synthetic dataset is generated with the SAME schema as the Kaggle
"Pharma sales data" daily file (datum + 8 ATC category columns), with realistic
Indian patterns: weekly cycle, monsoon/winter fever & allergy seasons, festival
dips, slow growth and count noise.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.config import RAW_DAILY, end_date, params

# average units/day per category for one mid-size store (illustrative)
BASE_LEVEL = {
    "M01AB": 9.0, "M01AE": 7.5, "N02BA": 5.0, "N02BE": 48.0,
    "N05B": 12.0, "N05C": 2.0, "R03": 10.0, "R06": 8.0,
}
# day-of-week multipliers Mon..Sun (Sunday is quieter)
DOW = np.array([1.08, 1.02, 1.0, 1.0, 1.04, 1.06, 0.80])


def seasonal_multiplier(cat: str, doy: np.ndarray) -> np.ndarray:
    """Smooth yearly seasonality per category."""
    def bump(center, width, height):
        dist = np.minimum(np.abs(doy - center), 365 - np.abs(doy - center))
        return height * np.exp(-0.5 * (dist / width) ** 2)

    if cat == "N02BE":   # paracetamol: monsoon fever season (Aug-Oct) + winter
        return 1 + bump(250, 30, 0.55) + bump(15, 25, 0.25)
    if cat == "R06":     # antihistamines: spring pollen + monsoon
        return 1 + bump(80, 25, 0.45) + bump(220, 30, 0.30)
    if cat == "R03":     # respiratory: winter smog / cold
        return 1 + bump(345, 35, 0.50)
    if cat in ("M01AB", "M01AE"):  # pain: mild winter rise
        return 1 + bump(10, 40, 0.15)
    return np.ones_like(doy, dtype=float)


def generate_synthetic() -> pd.DataFrame:
    p = params()
    rng = np.random.default_rng(p["data"]["seed"])
    dates = pd.date_range(p["data"]["start_date"], end_date(), freq="D")
    n = len(dates)
    doy = dates.dayofyear.to_numpy()
    dow = DOW[dates.dayofweek.to_numpy()]
    trend = 1 + 0.08 * np.arange(n) / 365          # ~8% growth per year
    # festival days (approximate Diwali / Holi windows) -> store partly closed
    festival = np.ones(n)
    for d in ("10-24", "11-12", "11-01", "03-08", "03-25", "03-14"):
        festival[dates.strftime("%m-%d") == d] = 0.55

    out = {"datum": dates}
    for cat in p["categories"]:
        lam = BASE_LEVEL[cat] * trend * dow * seasonal_multiplier(cat, doy) * festival
        # negative-binomial style over-dispersion via gamma-Poisson mixture
        shape = 8.0
        lam_noisy = rng.gamma(shape, lam / shape)
        out[cat] = rng.poisson(lam_noisy).astype(float)

    df = pd.DataFrame(out)
    df["Year"] = df["datum"].dt.year
    df["Month"] = df["datum"].dt.month
    df["Weekday Name"] = df["datum"].dt.day_name()
    return df


def main() -> None:
    p = params()
    if p["data"]["source"] == "kaggle":
        src = p["data"]["kaggle_path"]
        df = pd.read_csv(src)
        print(f"Using Kaggle data from {src}: {len(df)} rows")
        if str(src) != str(RAW_DAILY):
            df.to_csv(RAW_DAILY, index=False)
        return
    df = generate_synthetic()
    df.to_csv(RAW_DAILY, index=False)
    print(f"Synthetic data written to {RAW_DAILY}: {len(df)} days, "
          f"{df['datum'].min().date()} -> {df['datum'].max().date()}")


if __name__ == "__main__":
    main()
