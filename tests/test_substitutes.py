"""Same-formula substitutes (B) and missed-demand tracking (C)."""
import sqlite3

import pytest

from app.composition import describe, infer_from_product, load_brand_master, lookup_brand


def test_exact_key_needs_salt_strength_form_and_release():
    a = describe("Aceclofenac 100mg + Paracetamol 325mg", "tablet")["comp_key"]
    b = describe("paracetamol 325 MG + aceclofenac 100 mg", "tablet")["comp_key"]   # order/case/spacing
    assert a == b
    assert describe("Paracetamol 650mg", "tablet")["comp_key"] != describe("Paracetamol 500mg", "tablet")["comp_key"]
    assert describe("Paracetamol 650mg", "tablet")["comp_key"] != describe("Paracetamol 650mg", "syrup")["comp_key"]
    assert describe("Metformin 500mg", "tablet", "IR")["comp_key"] != describe("Metformin 500mg", "tablet", "SR")["comp_key"]
    # unit normalisation: 0.5 g == 500 mg ; 125mg/5ml == 25mg/ml
    assert describe("Amoxicillin 0.5g", "capsule")["comp_key"] == describe("Amoxicillin 500mg", "capsule")["comp_key"]
    assert "25mg/ml" in describe("Paracetamol 125mg/5ml", "syrup")["comp_key"]


def test_combination_without_strengths_is_never_guessed():
    assert infer_from_product("Some Combo Tab", "Drug A/Drug B") is None
    assert describe("Paracetamol", "tablet")["comp_key"] is None


def test_brand_lookup_is_forgiving():
    m = load_brand_master()
    assert lookup_brand("DOLO-650", m)["comp_key"] == "paracetamol 650mg | tablet | IR"
    assert lookup_brand("montair lc", m)["brand"] == "Montair LC"
    assert lookup_brand("glycomet sr", m)["release_type"] == "SR"


def test_brand_we_dont_stock_gets_same_formula_options(store_db):
    from app import substitutes as S
    req = S.resolve("Dolo 650")
    assert req["source"] == "brand"
    exact, near = S.find(req, qty=2)
    names = set(exact["name"])
    assert {"Paracetamol 650mg Tab", "Vedant Paracetamol 650 Tab"} <= names
    assert "Paracetamol 500mg Tab" not in names                 # different strength
    assert "Paracetamol 500mg Tab" in set(near["name"])         # shown only as 'not interchangeable'
    assert exact.iloc[0]["why"].startswith("★")


def test_sr_is_never_suggested_for_plain(store_db):
    from app import substitutes as S
    exact, near = S.find(S.resolve("Glycomet 500"))
    assert "Arogya Metformin SR 500 Tab" not in set(exact["name"])
    assert "Arogya Metformin SR 500 Tab" in set(near["name"])


def test_narrow_therapeutic_index_needs_consent(store_db):
    from app import substitutes as S
    req = S.resolve("Deriphyllin")                              # contains theophylline
    exact, _ = S.find(req)
    assert len(exact) and "Narrow-therapeutic-index" in exact.iloc[0]["caution"]
    assert S.needs_consent(req, exact.iloc[0])


def test_unmet_demand_logging_and_reorder(store_db):
    db = store_db
    from app import analytics
    from app import substitutes as S
    pid = int(db.q("SELECT id FROM products WHERE name='Telmisartan 40mg Tab'").id[0])
    before = S.unmet_summary(30)["requests"]
    db.log_unmet("Telmisartan 40mg Tab", 50, "out_of_stock", "lost", requested_product_id=pid)
    assert S.unmet_summary(30)["requests"] == before + 1
    with pytest.raises(ValueError):
        db.log_unmet("X", 1, "not_stocked", "substituted")      # substitution must name what was given
    sug = analytics.reorder_suggestions(None, cover_days=14)
    row = sug[sug["product"] == "Telmisartan 40mg Tab"]
    assert len(row) and int(row["missed_30d"].iloc[0]) >= 50   # missed demand raises the order


def test_top_unavailable_recommends_new_formulas(store_db):
    from app import substitutes as S
    top = S.top_unavailable(60)
    thyro = top[top["formula"].str.contains("Levothyroxine")]
    assert len(thyro) and not thyro["stocked"].iloc[0]


def test_old_database_is_migrated(tmp_path, monkeypatch):
    from app import db
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, generic TEXT,"
                 " category TEXT, manufacturer TEXT, hsn TEXT, gst_rate REAL, schedule TEXT, pack TEXT,"
                 " barcode TEXT UNIQUE, rack TEXT, reorder_level INTEGER, default_mrp REAL, chronic INTEGER,"
                 " preferred_supplier_id INTEGER)")
    conn.execute("INSERT INTO products(name, generic) VALUES ('Paracetamol 650mg Tab', 'Paracetamol')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "PHARMACY_DB", path)
    db.init_db()
    row = db.q("SELECT comp_key FROM products WHERE name='Paracetamol 650mg Tab'").iloc[0]
    assert row["comp_key"] == "paracetamol 650mg | tablet | IR"
    assert db.q("SELECT COUNT(*) n FROM unmet_demand").n[0] == 0
