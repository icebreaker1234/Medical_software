"""Substitute finder + unmet-demand analytics.

1. resolve()   - what did the customer ask for?  (stocked product, known brand, or a formula)
2. find()      - exact same-formula products in stock, ranked; plus 'near matches'
                 (same salts but different strength / form / release) that are NOT swappable
3. unmet_*()   - analytics on requests we could not fill (for reorder + 'start stocking')

The software only SUGGESTS. The pharmacist confirms every substitution and each one is
logged in `unmet_demand` (who, when, what was asked, what was given).
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from app import db
from app.composition import (describe, load_brand_master, lookup_brand, nti_salts,
                             parse_composition, salts_key)


def custom_master_path() -> Path:
    """Each store keeps its own uploaded brand list next to its database file."""
    f = Path(db.db_file())
    return f.with_name(f.stem + "_brands.csv")


@lru_cache(maxsize=8)
def _master(path: str, mtime: float):
    return load_brand_master(Path(path))


def brand_master() -> list[dict]:
    path = custom_master_path()
    return _master(str(path), path.stat().st_mtime if path.exists() else 0.0)


def pack_units(pack: str | None) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)", pack or "")
    return float(m.group(1)) if m else 1.0


# ---------------------------------------------------------------- 1. resolve
def resolve(query: str, products: pd.DataFrame | None = None) -> dict | None:
    """Turn what was typed / asked into a composition request."""
    q = (query or "").strip()
    if not q:
        return None
    products = db.products_df() if products is None else products
    hit = products[products["barcode"] == q]
    if hit.empty:
        hit = products[products["name"].str.lower() == q.lower()]
    if hit.empty:
        words = q.lower().split()
        hit = products[products["name"].str.lower().apply(lambda n: all(w in n for w in words))]
    if len(hit):
        p = hit.iloc[0]
        d = describe(p["composition"] or "", p["dosage_form"], p["release_type"] or "IR")
        return {**d, "requested": p["name"], "product_id": int(p["id"]), "source": "stock",
                "schedule": p["schedule"], "mrp": float(p["mrp"]), "pack": p["pack"],
                "stock": int(p["stock"]), "comp_key": p["comp_key"]}
    b = lookup_brand(q, brand_master())
    if b:
        return {**b, "requested": b["brand"], "product_id": None, "source": "brand",
                "schedule": None, "mrp": None, "pack": None, "stock": 0}
    ing = parse_composition(q)
    if ing and all(i.strength for i in ing):          # a formula typed directly
        return {"composition": q, "dosage_form": None, "release_type": None, "comp_key": None,
                "salts": salts_key(ing), "ingredients": ing, "requested": q, "product_id": None,
                "source": "composition", "schedule": None, "mrp": None, "pack": None, "stock": 0}
    return None


# ---------------------------------------------------------------- 2. find
def _ingredients_prefix(ing) -> str:
    return " + ".join(map(str, ing)) + " | "


def find(req: dict, qty: int = 1, products: pd.DataFrame | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (exact substitutes ranked, near matches that are NOT interchangeable)."""
    products = db.products_df() if products is None else products
    p = products[products["comp_key"].notna()].copy()
    p = p[p["id"] != (req.get("product_id") or -1)]
    if req.get("comp_key"):
        exact_mask = p["comp_key"] == req["comp_key"]
    elif req.get("ingredients"):          # formula typed without a form: any form, flagged
        exact_mask = p["comp_key"].str.startswith(_ingredients_prefix(req["ingredients"]))
    else:
        exact_mask = pd.Series(False, index=p.index)
    p["unit_price"] = p["mrp"] / p["pack"].apply(pack_units)
    p["days_to_expiry"] = (pd.to_datetime(p["next_expiry"]) - pd.Timestamp(db.today())).dt.days

    exact = p[exact_mask & (p["stock"] > 0)].copy()
    ref_unit = (req["mrp"] / pack_units(req["pack"])) if req.get("mrp") else None
    if len(exact):
        exact["enough"] = exact["stock"] >= qty
        exact["sell_first"] = exact["days_to_expiry"] <= 120
        exact = exact.sort_values(["enough", "sell_first", "days_to_expiry", "unit_price"],
                                  ascending=[False, False, True, True])
        exact["why"] = [_why(r, i == 0, exact["unit_price"].min()) for i, (_, r) in enumerate(exact.iterrows())]
        exact["saves_per_unit"] = (ref_unit - exact["unit_price"]).round(2) if ref_unit else np.nan
        nti = nti_salts(req.get("ingredients") or ())
        exact["caution"] = [_caution(r, nti) for _, r in exact.iterrows()]

    near = pd.DataFrame()
    if req.get("salts"):
        same_salts = p["composition"].fillna("").apply(
            lambda c: salts_key(parse_composition(c)) == req["salts"])
        near = p[same_salts & ~exact_mask & (p["stock"] > 0)].copy()
        near["difference"] = [_difference(req, r) for _, r in near.iterrows()]
    return exact, near


def _why(r, first: bool, best_price: float) -> str:
    bits = []
    bits.append("enough stock" if r["enough"] else f"only {int(r['stock'])} in stock")
    if r["sell_first"]:
        bits.append(f"expires in {int(r['days_to_expiry'])} d - sell first (FEFO)")
    if abs(r["unit_price"] - best_price) < 1e-6:
        bits.append("lowest price")
    return ("★ Best choice: " if first else "") + " · ".join(bits)


def _caution(r, nti: list[str]) -> str:
    out = []
    if nti:
        out.append(f"Narrow-therapeutic-index drug ({', '.join(nti)}): brand switch only with prescriber consent")
    if r["schedule"] in ("H", "H1", "X"):
        out.append(f"Schedule {r['schedule']}: prescription required")
    if pd.notna(r["days_to_expiry"]) and r["days_to_expiry"] < 30:
        out.append("expires within 30 days - check the course length")
    return " · ".join(out)


def _difference(req: dict, r) -> str:
    mine = {i.salt: i.strength for i in (req.get("ingredients") or ())}
    theirs = {i.salt: i.strength for i in parse_composition(r["composition"])}
    diff = [f"{s} {theirs.get(s)} (asked {mine[s]})" for s in mine if mine[s] != theirs.get(s)]
    if req.get("dosage_form") and r["dosage_form"] != req["dosage_form"]:
        diff.append(f"{r['dosage_form']} (asked {req['dosage_form']})")
    if req.get("release_type") and r["release_type"] != req["release_type"]:
        diff.append(f"{r['release_type']} release (asked {req['release_type']})")
    return "Not the same: " + "; ".join(diff) if diff else "Different formulation"


def needs_consent(req: dict, row) -> bool:
    return bool(nti_salts(req.get("ingredients") or ())) or row["schedule"] in ("H", "H1", "X")


# ---------------------------------------------------------------- 3. unmet demand analytics
def unmet(days: int = 30) -> pd.DataFrame:
    return db.q("""SELECT u.*, g.name AS given_product, r.name AS requested_product
                   FROM unmet_demand u
                   LEFT JOIN products g ON g.id=u.given_product_id
                   LEFT JOIN products r ON r.id=u.requested_product_id
                   WHERE date(u.ts) > date('now','localtime', ?) ORDER BY u.ts DESC""",
                (f"-{days} days",))


def unmet_summary(days: int = 30) -> dict:
    u = unmet(days)
    n = len(u)
    by = u.groupby("outcome")["qty"].sum() if n else pd.Series(dtype=float)
    return {
        "requests": n,
        "units": int(u["qty"].sum()) if n else 0,
        "substituted_pct": float((u["outcome"] == "substituted").mean() * 100) if n else 0.0,
        "lost_units": int(by.get("lost", 0)),
        "ordered_units": int(by.get("ordered", 0)),
        "saved_revenue": float(u.loc[u["outcome"] == "substituted", "est_value"].sum()) if n else 0.0,
    }


ACTIONS = {
    "reorder": "Out of stock now - reorder",
    "raise": "Ran out earlier - keep more stock",
    "brand": "Customer wanted the brand - stock it if it repeats",
    "new": "Start stocking this formula",
    "watch": "Watch - low demand so far",
}


def top_unavailable(days: int = 30) -> pd.DataFrame:
    """What customers asked for that we could not give (lost or ordered), grouped by formula."""
    u = unmet(days)
    u = u[u["outcome"].isin(["lost", "ordered"])]
    if u.empty:
        return pd.DataFrame(columns=["formula", "asked_as", "requests", "units", "stocked", "action"])
    u["formula"] = u["composition"].fillna(u["requested"])
    g = (u.groupby("formula")
          .agg(asked_as=("requested", lambda s: ", ".join(sorted(set(s)))),
               requests=("id", "count"), units=("qty", "sum"), comp_key=("comp_key", "first"),
               ran_out=("reason", lambda s: (s == "out_of_stock").mean()))
          .reset_index().sort_values("units", ascending=False))
    stock = db.products_df().groupby("comp_key")["stock"].sum()
    g["stocked"] = g["comp_key"].isin(stock.index)
    g["in_stock_now"] = g["comp_key"].map(stock).fillna(0).astype(int)
    g["action"] = np.select(
        [g["stocked"] & (g["ran_out"] > 0.5) & (g["in_stock_now"] == 0),
         g["stocked"] & (g["ran_out"] > 0.5),
         g["stocked"],
         g["requests"] >= 5],
        [ACTIONS["reorder"], ACTIONS["raise"], ACTIONS["brand"], ACTIONS["new"]],
        ACTIONS["watch"])
    g = g.drop(columns="ran_out")
    return g.drop(columns="comp_key")


def missed_units_per_product(days: int = 30) -> pd.DataFrame:
    """Out-of-stock requests for products we DO stock -> extra daily demand for reorder."""
    return db.q("""SELECT requested_product_id AS product_id, SUM(qty) AS missed_units
                   FROM unmet_demand WHERE requested_product_id IS NOT NULL
                   AND outcome IN ('lost','ordered') AND date(ts) > date('now','localtime', ?)
                   GROUP BY requested_product_id""", (f"-{days} days",))


def substitution_log(days: int = 90) -> pd.DataFrame:
    u = unmet(days)
    u = u[u["outcome"] == "substituted"]
    return u[["ts", "requested", "composition", "given_product", "qty", "est_value", "reason",
              "pharmacist", "note"]]
