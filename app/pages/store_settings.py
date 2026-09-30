from datetime import datetime
from pathlib import Path

import streamlit as st

from app import accounts, db, tenancy
from app.accounts import AuthError
from app.upi import valid_upi_id

st.title("⚙️ Store settings & backup")
t = tenancy.current()
if not t or t["role"] != "owner":
    st.error("Only the store owner can change store settings.")
    st.stop()

s = accounts.get_store(t["store_id"])
st.subheader("Store details (printed on bills)")
with st.form("store"):
    c1, c2 = st.columns(2)
    name = c1.text_input("Store name", s.get("name") or "")
    city = c2.text_input("City", s.get("city") or "")
    address = st.text_input("Address", s.get("address") or "")
    gstin = c1.text_input("GSTIN", s.get("gstin") or "", max_chars=15)
    dl = c2.text_input("Drug licence no(s).", s.get("drug_licence") or "")
    phone = c1.text_input("Store phone", s.get("phone") or "")
    upi = c2.text_input("Store UPI ID (for payment QR on bills)", s.get("upi_id") or "",
                        placeholder="e.g. yourstore@okaxis")
    if st.form_submit_button("Save", type="primary"):
        try:
            if upi and not valid_upi_id(upi):
                raise AuthError("UPI ID looks wrong - it should look like name@bank")
            if gstin and len(gstin.strip()) != 15:
                raise AuthError("GSTIN must be 15 characters")
            accounts.update_store(t["store_id"], name=name, city=city, address=address, gstin=gstin.upper(),
                                  drug_licence=dl, phone=phone, upi_id=upi)
            tenancy.set_tenant({**t, "store_name": name.strip(), "city": city.strip(), "address": address.strip(),
                                "gstin": gstin.strip().upper(), "drug_licence": dl.strip(),
                                "phone": phone.strip(), "upi_id": upi.strip() or None})
            accounts.audit("store_updated", t["user_id"], t["store_id"])
            st.success("Saved")
            st.rerun()
        except AuthError as e:
            st.error(str(e))

st.subheader("Backup")
path = Path(t["db_path"])
st.caption(f"Your store's data is one database file on this computer "
           f"({path.stat().st_size / 1e6:.1f} MB). An automatic backup is made once a day when "
           f"someone logs in; the last 14 are kept. Last backup: **{s.get('last_backup') or 'never'}**. "
           "Also copy a backup to a pen drive or Google Drive every week.")
c1, c2 = st.columns(2)
if c1.button("💾 Back up now", width="stretch"):
    b = accounts.backup_store(t["store_id"])
    accounts.audit("backup", t["user_id"], t["store_id"], b.name)
    st.success(f"Backup saved: {b.name}")
    st.rerun()
backups = sorted((accounts.data_dir() / "backups" / t["store_id"]).glob("*.db"))
if backups:
    latest = backups[-1]
    c2.download_button("⬇ Download latest backup", latest.read_bytes(),
                       file_name=f"{name or 'store'}_{latest.stem}.db".replace(" ", "_"),
                       mime="application/octet-stream", width="stretch")

st.subheader("Data")
if db.is_empty():
    st.info("Your store has no products yet. Add them in **Inventory → Add medicine** and stock them "
            "through **Purchases**, or load sample data to explore the app first.")
    if st.button("Load sample data (for trying the app)"):
        with st.spinner("Creating 6 months of sample sales, purchases and payments..."):
            db.seed()
        accounts.audit("demo_data_loaded", t["user_id"], t["store_id"])
        st.success("Sample data loaded.")
        st.rerun()
else:
    with st.expander("⚠️ Delete ALL store data (cannot be undone)"):
        st.caption("A backup is taken automatically first. Logins and store details are kept.")
        with st.form("wipe"):
            confirm = st.text_input(f"Type the store name exactly: {s.get('name')}")
            pw = st.text_input("Your password", type="password")
            if st.form_submit_button("Delete all data", type="primary"):
                if confirm != s.get("name") or not accounts.verify_password(t["user_ref"], pw):
                    st.error("Store name or password did not match - nothing deleted.")
                else:
                    b = accounts.backup_store(t["store_id"])
                    db.wipe_store()
                    accounts.audit("store_data_deleted", t["user_id"], t["store_id"], f"backup {b.name}")
                    st.success(f"All data deleted. Backup kept: {b.name}")
                    st.rerun()
st.caption(f"Store ID: {t['store_id']} · created {s.get('created_at', '')[:10]} · "
           f"today {datetime.now():%d %b %Y}")
