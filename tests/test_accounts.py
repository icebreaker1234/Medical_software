"""Logins, roles and one-database-per-store isolation."""
import sqlite3
from pathlib import Path

import pytest

from app import accounts, tenancy
from app.accounts import AuthError


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("STORE_DATA_DIR", str(tmp_path / "store_data"))
    yield tmp_path / "store_data"
    tenancy.set_tenant(None)


def test_passwords_are_hashed_never_stored(data_dir):
    accounts.create_owner("owner.a", "Secret123", "A", "Store A")
    raw = sqlite3.connect(data_dir / "accounts.db").execute("SELECT password_hash, recovery_hash FROM users").fetchone()
    assert "Secret123" not in raw[0] and raw[0].startswith("scrypt$") and raw[1].startswith("scrypt$")
    assert accounts.verify_secret("Secret123", raw[0]) and not accounts.verify_secret("secret123", raw[0])


def test_password_rules_and_unique_user_id(data_dir):
    with pytest.raises(AuthError, match="8 characters"):
        accounts.create_owner("owner.a", "short1", "A", "Store A")
    with pytest.raises(AuthError, match="letters and numbers"):
        accounts.create_owner("owner.a", "onlyletters", "A", "Store A")
    accounts.create_owner("owner.a", "Secret123", "A", "Store A")
    with pytest.raises(AuthError, match="taken"):
        accounts.create_owner("OWNER.A", "Secret123", "B", "Store B")      # case-insensitive


def test_login_errors_do_not_reveal_which_part_was_wrong_and_lockout(data_dir):
    accounts.create_owner("owner.a", "Secret123", "A", "Store A")
    with pytest.raises(AuthError, match="Wrong user ID or password"):
        accounts.login("nobody", "Secret123")
    for _ in range(accounts.MAX_FAILED - 1):
        with pytest.raises(AuthError, match="Wrong user ID or password"):
            accounts.login("owner.a", "bad-pass1")
    with pytest.raises(AuthError, match="locked"):
        accounts.login("owner.a", "bad-pass1")
    with pytest.raises(AuthError, match="Too many"):
        accounts.login("owner.a", "Secret123")                            # even the right password


def test_each_store_has_its_own_database(data_dir):
    from app import db
    a = accounts.create_owner("owner.a", "Secret123", "A", "Store A")
    b = accounts.create_owner("owner.b", "Secret123", "B", "Store B")
    ta, tb = accounts.login("owner.a", "Secret123"), accounts.login("owner.b", "Secret123")
    assert ta["db_path"] != tb["db_path"] and Path(ta["db_path"]).exists() and Path(tb["db_path"]).exists()
    tenancy.set_tenant(ta)
    db.add_supplier("Only In A", "", "", "Jaipur", 2, 30)
    assert len(db.q("SELECT * FROM suppliers")) == 1
    tenancy.set_tenant(tb)
    assert db.q("SELECT * FROM suppliers").empty                        # B cannot see A's data
    assert a["store_id"] != b["store_id"]


def test_empty_store_can_be_used_from_scratch(data_dir):
    from app import analytics, db
    accounts.create_owner("owner.a", "Secret123", "A", "Store A")
    tenancy.set_tenant(accounts.login("owner.a", "Secret123"))
    assert db.is_empty()
    sid = db.add_supplier("Balaji Pharma", "08AAACB1234F1Z5", "9829012345", "Jaipur", 1, 30)
    pid = db.add_product(name="Paracetamol 650mg Tab", generic="Paracetamol", category="N02BE",
                         schedule="OTC", gst_rate=5, pack="15 tab", default_mrp=33)
    db.create_purchase(sid, "B-001", [{"product_id": pid, "batch_no": "P1", "expiry": "2028-01-31", "qty": 100,
                                       "free_qty": 0, "rate": 20, "mrp": 33, "gst_rate": 5}])
    inv = db.create_sale([{"product_id": pid, "qty": 2}], "Cash")
    assert inv["invoice_no"].endswith("000001")
    db.record_supplier_payment(sid, str(db.today()), 1000, "UPI", reference="UTR1")
    assert analytics.supplier_payables()["outstanding"].sum() == pytest.approx(2100 - 1000)


def test_staff_roles_and_owner_only_actions(data_dir):
    accounts.create_owner("owner.a", "Secret123", "A", "Store A")
    owner = accounts.login("owner.a", "Secret123")
    accounts.add_staff(owner["user_ref"], owner["store_id"], "cashier.1", "Ramesh", "cashier", "Temp1234")
    staff = accounts.login("cashier.1", "Temp1234")
    assert staff["role"] == "cashier" and staff["must_change_password"]
    assert staff["store_id"] == owner["store_id"]
    with pytest.raises(AuthError, match="owner"):
        accounts.add_staff(staff["user_ref"], staff["store_id"], "x.y", "X", "cashier", "Temp1234")
    tenancy.set_tenant(staff)
    assert tenancy.can_open("billing") and not tenancy.can_open("supplier_payments")
    assert not tenancy.can_open("team") and not tenancy.can_open("dashboard")
    accounts.change_password(staff["user_ref"], "Temp1234", "Mine12345")
    assert not accounts.login("cashier.1", "Mine12345")["must_change_password"]
    accounts.set_staff_active(owner["user_ref"], owner["store_id"], staff["user_ref"], False)
    with pytest.raises(AuthError, match="deactivated"):
        accounts.login("cashier.1", "Mine12345")


def test_recovery_code_resets_owner_password_once(data_dir):
    code = accounts.create_owner("owner.a", "Secret123", "A", "Store A")["recovery_code"]
    new_code = accounts.reset_with_recovery_code("owner.a", code, "NewPass123")
    assert accounts.login("owner.a", "NewPass123")
    with pytest.raises(AuthError):
        accounts.reset_with_recovery_code("owner.a", code, "Other1234")     # old code is used up
    assert new_code != code


def test_backup_is_a_working_copy(data_dir):
    from app import db
    accounts.create_owner("owner.a", "Secret123", "A", "Store A")
    t = accounts.login("owner.a", "Secret123")
    tenancy.set_tenant(t)
    db.add_supplier("Balaji Pharma", "", "", "Jaipur", 1, 30)
    b = accounts.backup_store(t["store_id"])
    assert sqlite3.connect(b).execute("SELECT name FROM suppliers").fetchone()[0] == "Balaji Pharma"
