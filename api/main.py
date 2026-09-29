"""FastAPI model-serving service.

Run:  uvicorn api.main:app --reload --port 8000
Docs: http://localhost:8000/docs
"""
from __future__ import annotations

import json
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from src.config import DRIFT_FILE
from src.models.predict import forecast, load_model

app = FastAPI(title="Pharmacy Demand Forecasting API", version="1.0.0",
              description="Serves the champion demand-forecasting model from the MLOps pipeline.")

REQUEST_COUNT = {"total": 0, "errors": 0}


class ForecastRequest(BaseModel):
    category: str = Field(..., examples=["N02BE"])
    horizon: int = Field(14, ge=1, le=60, description="days to forecast")


class ForecastPoint(BaseModel):
    date: str
    forecast: float
    lower: float
    upper: float


class ForecastResponse(BaseModel):
    request_id: str
    category: str
    model_version: str | None
    interval: float
    total_forecast: float
    points: list[ForecastPoint]


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Minimal observability: count requests/errors and log latency."""
    start = time.perf_counter()
    REQUEST_COUNT["total"] += 1
    response = await call_next(request)
    if response.status_code >= 400:
        REQUEST_COUNT["errors"] += 1
    ms = (time.perf_counter() - start) * 1000
    print(json.dumps({"path": request.url.path, "status": response.status_code,
                      "latency_ms": round(ms, 1)}))
    return response


@app.get("/health")
def health():
    try:
        _, meta = load_model()
        return {"status": "ok", "model_loaded": True, "champion": meta["champion"]}
    except FileNotFoundError:
        return {"status": "degraded", "model_loaded": False}


@app.get("/model-info")
def model_info():
    try:
        _, meta = load_model()
    except FileNotFoundError as exc:
        raise HTTPException(503, str(exc)) from exc
    keys = ("model_name", "registered_version", "champion", "trained_at", "train_end",
            "data_end", "metrics", "interval")
    return {k: meta[k] for k in keys} | {"categories": list(meta["categories"])}


@app.get("/categories")
def categories():
    _, meta = load_model()
    return list(meta["categories"])


@app.post("/predict", response_model=ForecastResponse)
def predict(req: ForecastRequest):
    try:
        _, meta = load_model()
        df = forecast(req.category, req.horizon)
    except FileNotFoundError as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return ForecastResponse(
        request_id=str(uuid.uuid4()),
        category=req.category,
        model_version=str(meta.get("registered_version")),
        interval=meta["interval"],
        total_forecast=round(float(df["forecast"].sum()), 1),
        points=df.to_dict(orient="records"),
    )


@app.get("/monitoring")
def monitoring():
    drift = json.loads(DRIFT_FILE.read_text()) if DRIFT_FILE.exists() else None
    return {"requests": REQUEST_COUNT, "drift": drift}
