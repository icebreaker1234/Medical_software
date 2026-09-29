"""Paths and parameters shared by every stage."""
from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DATA_RAW = ROOT / "data" / "raw"
DATA_PROCESSED = ROOT / "data" / "processed"
MODELS_DIR = ROOT / "models"
REPORTS_DIR = ROOT / "reports"

RAW_DAILY = DATA_RAW / "salesdaily.csv"
DAILY_LONG = DATA_PROCESSED / "sales_daily_long.csv"
FEATURES = DATA_PROCESSED / "features.csv"
MODEL_FILE = MODELS_DIR / "model.joblib"
METADATA_FILE = MODELS_DIR / "metadata.json"
REFERENCE_FILE = MODELS_DIR / "reference_sales.csv"
METRICS_FILE = REPORTS_DIR / "metrics.json"
DRIFT_FILE = REPORTS_DIR / "drift.json"
PHARMACY_DB = ROOT / "data" / "pharmacy.db"

for _p in (DATA_RAW, DATA_PROCESSED, MODELS_DIR, REPORTS_DIR):
    _p.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def params() -> dict:
    with open(ROOT / "params.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def end_date() -> date:
    configured = params()["data"].get("end_date")
    return date.fromisoformat(configured) if configured else date.today() - timedelta(days=1)


def mlflow_uri() -> str:
    uri = params()["mlflow"]["tracking_uri"]
    # make sqlite paths absolute so every process uses the same DB file
    if uri.startswith("sqlite:///") and not uri.startswith("sqlite:////"):
        uri = f"sqlite:///{ROOT / uri.replace('sqlite:///', '')}"
    return uri
