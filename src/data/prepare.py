"""Stage 2 - clean the raw wide file and convert it to long format.

Output: data/processed/sales_daily_long.csv with columns [date, category, sales]
"""
from __future__ import annotations

import pandas as pd

from src.config import DAILY_LONG, RAW_DAILY, params


def load_and_clean(path=RAW_DAILY) -> pd.DataFrame:
    cats = params()["categories"]
    raw = pd.read_csv(path)
    raw["date"] = pd.to_datetime(raw["datum"], format="mixed").dt.normalize()
    wide = raw[["date", *cats]].groupby("date", as_index=False).sum()

    # fill missing calendar days with 0 sales (store closed / no record)
    full = pd.DataFrame({"date": pd.date_range(wide["date"].min(), wide["date"].max())})
    wide = full.merge(wide, on="date", how="left").fillna(0)

    # data-quality rules: no negative sales; cap extreme outliers at the 99.9th pct
    for c in cats:
        wide[c] = wide[c].clip(lower=0)
        cap = wide[c].quantile(0.999)
        wide[c] = wide[c].clip(upper=cap)

    long = wide.melt(id_vars="date", var_name="category", value_name="sales")
    return long.sort_values(["category", "date"]).reset_index(drop=True)


def validate(df: pd.DataFrame) -> None:
    """Simple data contract - fails the pipeline if violated."""
    assert set(df.columns) == {"date", "category", "sales"}, "unexpected columns"
    assert df["sales"].ge(0).all(), "negative sales found"
    assert not df.duplicated(["date", "category"]).any(), "duplicate date/category rows"
    assert df["category"].nunique() == len(params()["categories"]), "missing categories"


def main() -> None:
    df = load_and_clean()
    validate(df)
    df.to_csv(DAILY_LONG, index=False)
    print(f"Prepared {len(df)} rows -> {DAILY_LONG}")


if __name__ == "__main__":
    main()
