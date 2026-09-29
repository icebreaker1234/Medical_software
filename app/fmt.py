"""Number formatting (no Streamlit dependency, so analytics can use it)."""
from __future__ import annotations

import pandas as pd


def inr(x: float, decimals: int = 0) -> str:
    """Format in the Indian system: 1,57,055"""
    if x is None or pd.isna(x):
        return "-"
    neg = x < 0
    x = abs(float(x))
    s = f"{x:.{decimals}f}"
    whole, _, frac = s.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts) + "," + tail
    return ("-" if neg else "") + "₹" + whole + (f".{frac}" if frac else "")


def lakh(x: float) -> str:
    return f"₹{x / 1e5:.2f} L" if abs(x) >= 1e5 else inr(x)
