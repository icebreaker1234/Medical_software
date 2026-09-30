"""Local user accounts and stores.

store_data/accounts.db        users, stores, members (roles), audit log
store_data/stores/<id>.db     one separate SQLite database per store

Security
* Passwords are never stored - only a salted scrypt hash (Python standard library).
* 5 wrong passwords lock the user ID for 15 minutes (kept in the database, so a restart
  does not reset it).
* Owners get a one-time recovery code at sign-up (stored hashed) to reset a forgotten
  password without email. Staff passwords are reset by the owner.
* Roles: owner (everything), pharmacist (no money / settings pages), cashier (billing only).

Admin on the shop computer:  python -m app.accounts reset-password <user_id>
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from src.config import ROOT

ROLES = ("owner", "pharmacist", "cashier")
MAX_FAILED = 5
LOCK_MINUTES = 15
USER_ID_RE = re.compile(r"^[a-zA-Z0-9._@+-]{3,64}$")


def data_dir() -> Path:
    d = Path(os.getenv("STORE_DATA_DIR", ROOT / "store_data"))
    (d / "stores").mkdir(parents=True, exist_ok=True)
    (d / "backups").mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)                       # only this OS user can read store data
    except OSError:
        pass
    return d


def accounts_db() -> Path:
    return data_dir() / "accounts.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL UNIQUE COLLATE NOCASE, name TEXT,
    password_hash TEXT NOT NULL, recovery_hash TEXT, must_change_password INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1, failed_attempts INTEGER NOT NULL DEFAULT 0, locked_until TEXT,
    created_at TEXT NOT NULL, last_login TEXT);
CREATE TABLE IF NOT EXISTS stores (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, city TEXT, address TEXT, gstin TEXT, drug_licence TEXT,
    phone TEXT, upi_id TEXT, created_at TEXT NOT NULL, last_backup TEXT);
CREATE TABLE IF NOT EXISTS members (
    user_ref TEXT NOT NULL REFERENCES users(id), store_id TEXT NOT NULL REFERENCES stores(id),
    role TEXT NOT NULL CHECK (role IN ('owner', 'pharmacist', 'cashier')),
    active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, PRIMARY KEY (user_ref, store_id));
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, store_id TEXT, user_id TEXT, action TEXT NOT NULL, detail TEXT);
"""


class AuthError(Exception):
    """Shown to the user as-is (never reveals whether a user ID exists)."""


# ---------------------------------------------------------------- low level
def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


@contextmanager
def _db():
    conn = sqlite3.connect(accounts_db(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def hash_secret(secret: str) -> str:
    salt = os.urandom(16)
    n, r, p = 2 ** 14, 8, 1
    h = hashlib.scrypt(secret.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${base64.b64encode(salt).decode()}${base64.b64encode(h).decode()}"


def verify_secret(secret: str, stored: str | None) -> bool:
    try:
        _, n, r, p, salt, h = (stored or "").split("$")
        calc = hashlib.scrypt(secret.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r),
                              p=int(p), dklen=32)
        return hmac.compare_digest(calc, base64.b64decode(h))
    except Exception:
        return False


def check_password_strength(pw: str) -> None:
    if len(pw or "") < 8:
        raise AuthError("Password must be at least 8 characters")
    if not re.search(r"[A-Za-z]", pw) or not re.search(r"\d", pw):
        raise AuthError("Password must contain letters and numbers")


def _check_user_id(user_id: str) -> str:
    uid = (user_id or "").strip()
    if not USER_ID_RE.match(uid):
        raise AuthError("User ID: 3-64 characters - letters, numbers and . _ @ + - only")
    return uid


def audit(action: str, user_id: str | None = None, store_id: str | None = None, detail: str | None = None):
    with _db() as c:
        c.execute("INSERT INTO audit_log(ts, store_id, user_id, action, detail) VALUES (?,?,?,?,?)",
                  (str(_now()), store_id, user_id, action, detail))


def store_db_path(store_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{12}", store_id or ""):
        raise AuthError("Invalid store")
    return data_dir() / "stores" / f"{store_id}.db"


def _new_recovery_code() -> str:
    raw = secrets.token_hex(8).upper()                  # 64 bits, shown once
    return "-".join(raw[i:i + 4] for i in range(0, 16, 4))


# ---------------------------------------------------------------- sign up / stores
def create_owner(user_id: str, password: str, name: str, store_name: str, city: str = "") -> dict:
    """New owner account + new store with its own empty database. Returns the recovery code once."""
    uid = _check_user_id(user_id)
    check_password_strength(password)
    if not (store_name or "").strip():
        raise AuthError("Store name is required")
    code = _new_recovery_code()
    ref, store_id = uuid.uuid4().hex, uuid.uuid4().hex[:12]
    with _db() as c:
        if c.execute("SELECT 1 FROM users WHERE user_id=?", (uid,)).fetchone():
            raise AuthError("That user ID is taken - choose another")
        c.execute("INSERT INTO users(id, user_id, name, password_hash, recovery_hash, created_at) "
                  "VALUES (?,?,?,?,?,?)", (ref, uid, name.strip(), hash_secret(password), hash_secret(code),
                                           str(_now())))
        c.execute("INSERT INTO stores(id, name, city, created_at) VALUES (?,?,?,?)",
                  (store_id, store_name.strip(), city.strip(), str(_now())))
        c.execute("INSERT INTO members(user_ref, store_id, role, created_at) VALUES (?,?,?,?)",
                  (ref, store_id, "owner", str(_now())))
    _init_store_db(store_id)
    audit("store_created", uid, store_id, store_name)
    return {"store_id": store_id, "recovery_code": code}


def _init_store_db(store_id: str) -> None:
    """Create the store's empty database with the full schema."""
    from app import db
    path = store_db_path(store_id)
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA)
    conn.commit()
    conn.close()
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def update_store(store_id: str, **fields) -> None:
    allowed = {"name", "city", "address", "gstin", "drug_licence", "phone", "upi_id"}
    fields = {k: (v or "").strip() for k, v in fields.items() if k in allowed}
    if not fields.get("name", "x"):
        raise AuthError("Store name cannot be empty")
    with _db() as c:
        c.execute(f"UPDATE stores SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
                  (*fields.values(), store_id))


def get_store(store_id: str) -> dict:
    with _db() as c:
        r = c.execute("SELECT * FROM stores WHERE id=?", (store_id,)).fetchone()
    return dict(r) if r else {}


# ---------------------------------------------------------------- login
def login(user_id: str, password: str) -> dict:
    """Returns a tenant dict for the session. Raises AuthError with a safe message."""
    uid = (user_id or "").strip()
    generic = AuthError("Wrong user ID or password")
    with _db() as c:
        u = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        if u is None:
            verify_secret(password, hash_secret("timing-equaliser"))   # same work either way
            raise generic
        if u["locked_until"] and datetime.fromisoformat(u["locked_until"]) > _now():
            raise AuthError(f"Too many wrong attempts - try again after {u['locked_until'][11:16]}")
        if not verify_secret(password, u["password_hash"]):
            fails = u["failed_attempts"] + 1
            lock = str(_now() + timedelta(minutes=LOCK_MINUTES)) if fails >= MAX_FAILED else None
            c.execute("UPDATE users SET failed_attempts=?, locked_until=? WHERE id=?",
                      (0 if lock else fails, lock, u["id"]))
            c.execute("INSERT INTO audit_log(ts, user_id, action, detail) VALUES (?,?,?,?)",
                      (str(_now()), uid, "login_failed", "locked" if lock else None))
            c.commit()                     # keep the failed attempt even though we raise below
            if lock:
                raise AuthError(f"Too many wrong attempts - locked for {LOCK_MINUTES} minutes")
            raise generic
        if not u["active"]:
            raise AuthError("This user ID has been deactivated by the store owner")
        m = c.execute("""SELECT m.role, s.* FROM members m JOIN stores s ON s.id=m.store_id
                         WHERE m.user_ref=? AND m.active=1 ORDER BY m.created_at LIMIT 1""", (u["id"],)).fetchone()
        if m is None:
            raise AuthError("This user ID is not linked to any store")
        c.execute("UPDATE users SET failed_attempts=0, locked_until=NULL, last_login=? WHERE id=?",
                  (str(_now()), u["id"]))
    audit("login", uid, m["id"])
    return {"user_ref": u["id"], "user_id": u["user_id"], "user_name": u["name"], "role": m["role"],
            "must_change_password": bool(u["must_change_password"]), "store_id": m["id"],
            "store_name": m["name"], "city": m["city"], "address": m["address"], "gstin": m["gstin"],
            "drug_licence": m["drug_licence"], "phone": m["phone"], "upi_id": m["upi_id"],
            "db_path": str(store_db_path(m["id"]))}


def change_password(user_ref: str, old: str, new: str) -> None:
    check_password_strength(new)
    with _db() as c:
        u = c.execute("SELECT * FROM users WHERE id=?", (user_ref,)).fetchone()
        if not u or not verify_secret(old, u["password_hash"]):
            raise AuthError("Current password is wrong")
        if old == new:
            raise AuthError("New password must be different")
        c.execute("UPDATE users SET password_hash=?, must_change_password=0 WHERE id=?",
                  (hash_secret(new), user_ref))
    audit("password_changed", u["user_id"])


def reset_with_recovery_code(user_id: str, code: str, new: str) -> str:
    """Owner forgot the password. Returns a NEW recovery code (the old one is used up)."""
    check_password_strength(new)
    uid = (user_id or "").strip()
    with _db() as c:
        u = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        if not u or not verify_secret((code or "").strip().upper(), u["recovery_hash"]):
            raise AuthError("User ID or recovery code is wrong")
        new_code = _new_recovery_code()
        c.execute("UPDATE users SET password_hash=?, recovery_hash=?, failed_attempts=0, locked_until=NULL, "
                  "must_change_password=0 WHERE id=?", (hash_secret(new), hash_secret(new_code), u["id"]))
    audit("password_reset_recovery_code", uid)
    return new_code


def new_recovery_code(user_ref: str, password: str) -> str:
    """Owner confirms the password and gets a fresh recovery code (old one stops working)."""
    with _db() as c:
        u = c.execute("SELECT * FROM users WHERE id=?", (user_ref,)).fetchone()
        if not u or not verify_secret(password, u["password_hash"]):
            raise AuthError("Password is wrong")
        code = _new_recovery_code()
        c.execute("UPDATE users SET recovery_hash=? WHERE id=?", (hash_secret(code), user_ref))
    audit("recovery_code_regenerated", u["user_id"])
    return code


def verify_password(user_ref: str, password: str) -> bool:
    with _db() as c:
        u = c.execute("SELECT password_hash FROM users WHERE id=?", (user_ref,)).fetchone()
    return bool(u) and verify_secret(password, u["password_hash"])


# ---------------------------------------------------------------- staff (owner only)
def _require_owner(owner_ref: str, store_id: str, c) -> None:
    r = c.execute("SELECT role FROM members WHERE user_ref=? AND store_id=? AND active=1",
                  (owner_ref, store_id)).fetchone()
    if not r or r["role"] != "owner":
        raise AuthError("Only the store owner can manage staff")


def add_staff(owner_ref: str, store_id: str, user_id: str, name: str, role: str, temp_password: str) -> None:
    uid = _check_user_id(user_id)
    if role not in ("pharmacist", "cashier"):
        raise AuthError("Role must be pharmacist or cashier")
    check_password_strength(temp_password)
    with _db() as c:
        _require_owner(owner_ref, store_id, c)
        if c.execute("SELECT 1 FROM users WHERE user_id=?", (uid,)).fetchone():
            raise AuthError("That user ID is taken - choose another")
        ref = uuid.uuid4().hex
        c.execute("INSERT INTO users(id, user_id, name, password_hash, must_change_password, created_at) "
                  "VALUES (?,?,?,?,1,?)", (ref, uid, name.strip(), hash_secret(temp_password), str(_now())))
        c.execute("INSERT INTO members(user_ref, store_id, role, created_at) VALUES (?,?,?,?)",
                  (ref, store_id, role, str(_now())))
    audit("staff_added", uid, store_id, role)


def list_members(store_id: str) -> list[dict]:
    with _db() as c:
        rows = c.execute("""SELECT u.id AS user_ref, u.user_id, u.name, m.role, m.active AS member_active,
                                   u.active AS user_active, u.last_login, u.must_change_password
                            FROM members m JOIN users u ON u.id=m.user_ref WHERE m.store_id=?
                            ORDER BY m.role='owner' DESC, u.user_id""", (store_id,)).fetchall()
    return [dict(r) for r in rows]


def set_staff_active(owner_ref: str, store_id: str, staff_ref: str, active: bool) -> None:
    with _db() as c:
        _require_owner(owner_ref, store_id, c)
        r = c.execute("SELECT role FROM members WHERE user_ref=? AND store_id=?", (staff_ref, store_id)).fetchone()
        if not r or r["role"] == "owner":
            raise AuthError("The owner account cannot be deactivated here")
        c.execute("UPDATE members SET active=? WHERE user_ref=? AND store_id=?", (int(active), staff_ref, store_id))
        c.execute("UPDATE users SET active=? WHERE id=?", (int(active), staff_ref))
    audit("staff_activated" if active else "staff_deactivated", staff_ref, store_id)


def reset_staff_password(owner_ref: str, store_id: str, staff_ref: str, temp_password: str) -> None:
    check_password_strength(temp_password)
    with _db() as c:
        _require_owner(owner_ref, store_id, c)
        r = c.execute("SELECT role FROM members WHERE user_ref=? AND store_id=?", (staff_ref, store_id)).fetchone()
        if not r or r["role"] == "owner":
            raise AuthError("Use your recovery code to reset the owner password")
        c.execute("UPDATE users SET password_hash=?, must_change_password=1, failed_attempts=0, locked_until=NULL "
                  "WHERE id=?", (hash_secret(temp_password), staff_ref))
    audit("staff_password_reset", staff_ref, store_id)


def audit_trail(store_id: str, limit: int = 200) -> list[dict]:
    with _db() as c:
        rows = c.execute("""SELECT a.ts, a.user_id, a.action, a.detail FROM audit_log a
                            WHERE a.store_id=? OR a.user_id IN (
                               SELECT u.user_id FROM members m JOIN users u ON u.id=m.user_ref WHERE m.store_id=?)
                            ORDER BY a.id DESC LIMIT ?""", (store_id, store_id, limit)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- backups
def backup_store(store_id: str, keep: int = 14) -> Path:
    """Consistent copy of the store database (safe while the app is running)."""
    src = store_db_path(store_id)
    folder = data_dir() / "backups" / store_id
    folder.mkdir(parents=True, exist_ok=True)
    dst = folder / f"{_now():%Y-%m-%d_%H%M}.db"
    s, d = sqlite3.connect(src), sqlite3.connect(dst)
    with d:
        s.backup(d)
    s.close()
    d.close()
    for old in sorted(folder.glob("*.db"))[:-keep]:
        old.unlink()
    with _db() as c:
        c.execute("UPDATE stores SET last_backup=? WHERE id=?", (str(_now()), store_id))
    return dst


def backup_if_due(store_id: str, hours: int = 24) -> Path | None:
    last = get_store(store_id).get("last_backup")
    if last and datetime.fromisoformat(last) > _now() - timedelta(hours=hours):
        return None
    return backup_store(store_id) if store_db_path(store_id).exists() else None


# ---------------------------------------------------------------- CLI for the shop computer
def _cli() -> None:
    import argparse
    import getpass
    ap = argparse.ArgumentParser(description="Store accounts admin (run on the shop computer)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("reset-password", help="Set a new password for a user ID")
    r.add_argument("user_id")
    sub.add_parser("list", help="List stores and users")
    a = ap.parse_args()
    if a.cmd == "reset-password":
        pw = getpass.getpass("New password: ")
        check_password_strength(pw)
        with _db() as c:
            n = c.execute("UPDATE users SET password_hash=?, failed_attempts=0, locked_until=NULL, "
                          "must_change_password=1 WHERE user_id=?", (hash_secret(pw), a.user_id)).rowcount
        print("Password reset - user must change it at next login." if n else "No such user ID.")
        if n:
            audit("password_reset_cli", a.user_id)
    else:
        with _db() as c:
            for s in c.execute("SELECT * FROM stores"):
                print(f"{s['id']}  {s['name']} ({s['city'] or '-'})")
                for m in c.execute("SELECT u.user_id, m.role, m.active FROM members m JOIN users u "
                                   "ON u.id=m.user_ref WHERE m.store_id=?", (s["id"],)):
                    print(f"    {m['user_id']:<25} {m['role']:<11} {'active' if m['active'] else 'inactive'}")


if __name__ == "__main__":
    _cli()
