"""Stage 4 - train, compare and register models.

Models compared (time-based split, last `test_days` held out):
  1. naive_seasonal  - sales 7 days ago                (baseline)
  2. moving_avg_7    - mean of the previous 7 days      (baseline)
  3. xgboost_raw     - global XGBoost over all categories, predicts units
  4. xgboost_level_normalized - XGBoost predicting sales / 28-day level (handles trend)

Everything is tracked in MLflow; the best model is registered in the MLflow
Model Registry with the alias "champion" and exported to models/ for serving.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import joblib
import matplotlib
import mlflow
import mlflow.sklearn
import numpy as np
import pandas as pd
from mlflow.models import infer_signature
from mlflow.tracking import MlflowClient
from xgboost import XGBRegressor

from src.config import (ROOT, DAILY_LONG, FEATURES, METADATA_FILE, METRICS_FILE, MODEL_FILE,
                        REFERENCE_FILE, REPORTS_DIR, mlflow_uri, params)
from src.features.build import category_codes, feature_columns
from src.models.wrapper import LevelNormalizedRegressor

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


def metrics(y_true, y_pred) -> dict:
    y_true, y_pred = np.asarray(y_true, float), np.asarray(y_pred, float)
    err = y_pred - y_true
    return {
        "mae": float(np.mean(np.abs(err))),
        "rmse": float(np.sqrt(np.mean(err ** 2))),
        # WAPE is robust for low-volume categories (no divide-by-zero per day)
        "wape": float(np.sum(np.abs(err)) / max(np.sum(np.abs(y_true)), 1e-9)),
        "bias": float(np.mean(err)),
    }


def split(df: pd.DataFrame, test_days: int):
    cutoff = df["date"].max() - pd.Timedelta(days=test_days)
    return df[df["date"] <= cutoff], df[df["date"] > cutoff], cutoff


def main() -> None:
    p = params()
    df = pd.read_csv(FEATURES, parse_dates=["date"])
    train, test, cutoff = split(df, p["train"]["test_days"])
    X_cols = feature_columns()

    mlflow.set_tracking_uri(mlflow_uri())
    mlflow.set_experiment(p["mlflow"]["experiment"])
    results: dict[str, dict] = {}

    # ---- baselines -------------------------------------------------------
    for name, col in (("naive_seasonal", "lag_7"), ("moving_avg_7", "roll_mean_7")):
        with mlflow.start_run(run_name=name):
            m = metrics(test["sales"], test[col])
            mlflow.log_params({"model_type": name, "test_days": p["train"]["test_days"]})
            mlflow.log_metrics(m)
            results[name] = m
            print(f"{name:15s} {m}")

    # ---- ML models ---------------------------------------------------------
    xgb_params = p["train"]["xgb"]
    candidates = {
        "xgboost_raw": lambda: XGBRegressor(objective="reg:squarederror",
                                            random_state=p["data"]["seed"], **xgb_params),
        "xgboost_level_normalized": lambda: LevelNormalizedRegressor(
            random_state=p["data"]["seed"], **xgb_params),
    }
    alpha = (1 - p["forecast"]["interval"]) / 2
    trained: dict[str, dict] = {}
    for name, factory in candidates.items():
        with mlflow.start_run(run_name=name) as run:
            model = factory()
            model.fit(train[X_cols], train["sales"])
            pred = np.clip(model.predict(test[X_cols]), 0, None)
            m = metrics(test["sales"], pred)
            results[name] = m
            print(f"{name:26s} {m}")
            mlflow.log_params({"model_type": name, "test_days": p["train"]["test_days"],
                               "n_features": len(X_cols), **xgb_params})
            mlflow.log_metrics(m)

            # per-category metrics + residual quantiles (used for prediction intervals)
            scored = test.assign(pred=pred, resid=test["sales"].to_numpy() - pred)
            per_cat, quantiles = {}, {}
            for cat, g in scored.groupby("category"):
                per_cat[cat] = metrics(g["sales"], g["pred"])
                mlflow.log_metric(f"wape_{cat}", per_cat[cat]["wape"])
                quantiles[cat] = {"low": float(g["resid"].quantile(alpha)),
                                  "high": float(g["resid"].quantile(1 - alpha))}

            imp = pd.Series(model.feature_importances_, index=X_cols).sort_values()
            fig, ax = plt.subplots(figsize=(6, 5))
            imp.plot.barh(ax=ax, color="#2b6cb0")
            ax.set_title(f"Feature importance - {name}")
            fig.tight_layout()
            fig_path = REPORTS_DIR / f"feature_importance_{name}.png"
            fig.savefig(fig_path, dpi=120)
            plt.close(fig)
            mlflow.log_artifact(str(fig_path))

            signature = infer_signature(test[X_cols].head(50), pred[:50])
            mlflow.sklearn.log_model(model, name="model", signature=signature,
                                     input_example=test[X_cols].head(3),
                                     code_paths=[str(ROOT / "src")],
                                     serialization_format="cloudpickle")
            trained[name] = {"model": model, "run_id": run.info.run_id,
                             "per_cat": per_cat, "quantiles": quantiles}

    # ---- choose champion & register -----------------------------------------
    best = min(results, key=lambda k: results[k]["wape"])
    print(f"Best model by WAPE: {best}")
    if best not in trained:      # a baseline won -> still serve the best ML model
        best_ml = min(trained, key=lambda k: results[k]["wape"])
        print(f"Baseline won; serving best ML model instead: {best_ml}")
    else:
        best_ml = best
    chosen = trained[best_ml]
    model, per_cat, quantiles, xgb_run_id = (chosen["model"], chosen["per_cat"],
                                              chosen["quantiles"], chosen["run_id"])
    mv = mlflow.register_model(f"runs:/{xgb_run_id}/model", p["mlflow"]["model_name"])
    MlflowClient().set_registered_model_alias(p["mlflow"]["model_name"], "champion", mv.version)
    registered_version = mv.version
    print(f"Registered {p['mlflow']['model_name']} v{mv.version} ({best_ml}) as @champion")

    # ---- export artifacts for serving (API / dashboard) -----------------------
    joblib.dump(model, MODEL_FILE)
    long = pd.read_csv(DAILY_LONG, parse_dates=["date"])
    long[long["date"] <= cutoff].to_csv(REFERENCE_FILE, index=False)   # drift reference
    metadata = {
        "model_name": p["mlflow"]["model_name"],
        "registered_version": registered_version,
        "mlflow_run_id": xgb_run_id,
        "champion": best_ml,
        "best_overall": best,
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "train_end": str(cutoff.date()),
        "data_end": str(df["date"].max().date()),
        "features": X_cols,
        "categories": category_codes(),
        "interval": p["forecast"]["interval"],
        "residual_quantiles": quantiles,
        "metrics": results,
        "per_category": per_cat,
    }
    METADATA_FILE.write_text(json.dumps(metadata, indent=2))
    METRICS_FILE.write_text(json.dumps({"models": results, "champion": best,
                                        "per_category": per_cat}, indent=2))
    print(f"Exported model -> {MODEL_FILE}")


if __name__ == "__main__":
    main()
