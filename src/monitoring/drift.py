"""Stage 5 - data drift monitoring.

1. DATA DRIFT - compares the most recent `current_days` of sales with the SAME
   calendar window one year earlier (seasonality-aware reference; comparing
   September against the whole year would flag every monsoon as "drift"), using:
  * PSI (Population Stability Index)  - > 0.2 is commonly treated as significant shift
  * Kolmogorov-Smirnov test           - p < 0.05 means distributions differ
  * mean shift %
2. PERFORMANCE MONITORING - scores the champion model on the recent window and
   compares its WAPE with the hold-out WAPE recorded at training time.
Also writes an Evidently HTML report when Evidently is installed.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from scipy.stats import ks_2samp

from src.config import DAILY_LONG, DRIFT_FILE, REFERENCE_FILE, REPORTS_DIR, params


def psi(reference: np.ndarray, current: np.ndarray, bins: int = 5) -> float:
    """PSI with quantile bins from the reference and Laplace smoothing.

    Few bins + smoothing keep PSI stable on small windows (30 days); with 10
    bins and near-zero clipping, PSI explodes on small samples.
    """
    edges = np.unique(np.quantile(reference, np.linspace(0, 1, bins + 1)))
    if len(edges) < 3:
        return 0.0
    edges[0], edges[-1] = -np.inf, np.inf
    ref_cnt = np.histogram(reference, edges)[0] + 0.5
    cur_cnt = np.histogram(current, edges)[0] + 0.5
    ref_pct, cur_pct = ref_cnt / ref_cnt.sum(), cur_cnt / cur_cnt.sum()
    return float(np.sum((cur_pct - ref_pct) * np.log(cur_pct / ref_pct)))


def compute_drift(reference: pd.DataFrame, current: pd.DataFrame) -> dict:
    p = params()["monitoring"]
    rows = []
    for cat in sorted(reference["category"].unique()):
        ref = reference.loc[reference["category"] == cat, "sales"].to_numpy()
        cur = current.loc[current["category"] == cat, "sales"].to_numpy()
        if len(cur) < 5:
            continue
        score = psi(ref, cur)
        ks_p = float(ks_2samp(ref, cur).pvalue)
        shift = float((cur.mean() - ref.mean()) / max(ref.mean(), 1e-9) * 100)
        rows.append({
            "category": cat,
            "reference_mean": round(float(ref.mean()), 2),
            "current_mean": round(float(cur.mean()), 2),
            "mean_shift_pct": round(shift, 1),
            "psi": round(score, 3),
            "ks_pvalue": round(ks_p, 4),
            "drift": bool(score > p["psi_threshold"] or ks_p < p["ks_pvalue_threshold"]),
        })
    n_drift = sum(r["drift"] for r in rows)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "current_window_days": p["current_days"],
        "categories_checked": len(rows),
        "categories_drifted": n_drift,
        "retrain_recommended": n_drift >= max(2, len(rows) // 2),
        "details": rows,
    }


def evidently_html(reference: pd.DataFrame, current: pd.DataFrame) -> str | None:
    try:
        from evidently import Report
        from evidently.presets import DataDriftPreset
    except Exception:
        return None
    ref = reference.pivot(index="date", columns="category", values="sales").reset_index(drop=True)
    cur = current.pivot(index="date", columns="category", values="sales").reset_index(drop=True)
    snapshot = Report([DataDriftPreset()]).run(reference_data=ref, current_data=cur)
    out = REPORTS_DIR / "drift_report.html"
    snapshot.save_html(str(out))
    return str(out)


def seasonal_windows(data: pd.DataFrame, days: int):
    end = data["date"].max()
    cur = data[data["date"] > end - pd.Timedelta(days=days)]
    ref_end = end - pd.Timedelta(days=365)
    ref = data[(data["date"] > ref_end - pd.Timedelta(days=days)) & (data["date"] <= ref_end)]
    if ref.empty:                                   # less than a year of history
        ref = pd.read_csv(REFERENCE_FILE, parse_dates=["date"])
    return ref, cur


def performance(data: pd.DataFrame, days: int) -> dict | None:
    """One-step-ahead WAPE of the served model on the recent window."""
    try:
        from src.features.build import add_features, feature_columns
        from src.models.predict import load_model
        model, meta = load_model()
    except FileNotFoundError:
        return None
    feats = add_features(data).dropna(subset=feature_columns())
    recent = feats[feats["date"] > feats["date"].max() - pd.Timedelta(days=days)]
    pred = model.predict(recent[feature_columns()])
    wape = float(np.abs(pred - recent["sales"]).sum() / max(recent["sales"].abs().sum(), 1e-9))
    baseline = meta["metrics"][meta["champion"]]["wape"]
    degradation = (wape - baseline) / baseline * 100
    return {"recent_wape": round(wape, 4), "holdout_wape": round(baseline, 4),
            "degradation_pct": round(degradation, 1), "alert": bool(degradation > 20)}


def main() -> None:
    days = params()["monitoring"]["current_days"]
    data = pd.read_csv(DAILY_LONG, parse_dates=["date"])
    reference, current = seasonal_windows(data, days)
    report = compute_drift(reference, current)
    report["reference_window"] = f"{reference['date'].min().date()} -> {reference['date'].max().date()}"
    report["current_window"] = f"{current['date'].min().date()} -> {current['date'].max().date()}"
    report["performance"] = performance(data, days)
    if report["performance"] and report["performance"]["alert"]:
        report["retrain_recommended"] = True
    try:
        report["evidently_report"] = evidently_html(reference, current)
    except Exception as exc:  # Evidently is optional; never fail the pipeline on it
        report["evidently_report"] = None
        print(f"Evidently report skipped: {exc}")
    DRIFT_FILE.write_text(json.dumps(report, indent=2))
    print(f"Drift: {report['categories_drifted']}/{report['categories_checked']} categories "
          f"drifted; performance = {report['performance']}; "
          f"retrain recommended = {report['retrain_recommended']}")


if __name__ == "__main__":
    main()
