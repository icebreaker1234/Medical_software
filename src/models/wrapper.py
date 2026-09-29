"""Level-normalised regressor.

Tree models cannot extrapolate a growing trend. Instead of predicting raw
units, we predict  sales / (28-day average + 1)  and multiply back. This lets
one global model learn shapes (weekday, season) that are shared across
high-volume (paracetamol) and low-volume (sedatives) categories.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from xgboost import XGBRegressor

SCALE_COL = "roll_mean_28"


class LevelNormalizedRegressor:
    def __init__(self, **xgb_params):
        self.xgb_params = xgb_params
        self.model = XGBRegressor(objective="reg:squarederror", **xgb_params)

    def _scale(self, X: pd.DataFrame) -> np.ndarray:
        return X[SCALE_COL].to_numpy(dtype=float) + 1.0

    def fit(self, X: pd.DataFrame, y) -> "LevelNormalizedRegressor":
        self.model.fit(X, np.asarray(y, float) / self._scale(X))
        return self

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        return np.clip(self.model.predict(X) * self._scale(X), 0, None)

    @property
    def feature_importances_(self):
        return self.model.feature_importances_
