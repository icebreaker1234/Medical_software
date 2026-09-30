import pandas as pd
import streamlit as st

from app import accounts, importer as I, tenancy

st.title("📥 Import from your old software")
st.caption("Drop a file exported from Marg, Tally, GoFrugal, Busy or any Excel sheet. You will see exactly what "
           "will be loaded before anything is saved.")

if st.session_state.get("import_done"):
    d, res = st.session_state.pop("import_done")
    st.success(f"Imported **{I.DATASETS[d]['label']}**: {res['created']:,} created, {res['updated']:,} updated, "
               f"{res['skipped']:,} skipped (already there). A backup was taken before the import.")
    for n in res["notes"][:10]:
        st.caption("• " + n)
    nxt = I.ORDER[I.ORDER.index(d) + 1] if d != I.ORDER[-1] else None
    if nxt:
        st.info(f"Next: import **{I.DATASETS[nxt]['label']}**.")

with st.expander("Which files, and in what order?", expanded=False):
    st.markdown("""
Import in this order (each step can use the previous one):

| # | File | Typical report in your old software |
|---|---|---|
| 1 | **Suppliers** | Supplier / party list (with GSTIN, phone, balance) |
| 2 | **Items & stock** | Batch-wise stock statement (item, batch, expiry, qty, MRP, purchase rate) |
| 3 | **Customers** | Customer list (with mobile, doctor, outstanding) |
| 4 | **Purchase history** | Item-wise purchase register |
| 5 | **Sales history** | Item-wise sales register - this feeds the forecasts, refill reminders and dead-stock reports |

**How to export:** open the report in your old software, choose the date range (for history: last 6-12 months),
and use its **Export → Excel** (or CSV) option. Menu names differ between versions - any Excel/CSV with a
header row works. Title lines above the header (store name, report name, dates) and total lines are skipped
automatically.
""")
    c = st.columns(len(I.ORDER))
    for col, d in zip(c, I.ORDER):
        col.download_button(f"Template: {d}", I.template_csv(d), f"template_{d}.csv", "text/csv", width="stretch")

# ------------------------------------------------------------------ 1. file
c1, c2 = st.columns([4, 1])
up = c1.file_uploader("Drop the exported file here", type=["xlsx", "xls", "xlsm", "csv", "txt"])
c2.write("")
c2.write("")
if c2.button("Try a sample stock report", width="stretch"):
    st.session_state.import_file = ("sample_batch_stock.xlsx", I.sample_stock_export())
if up is not None:
    st.session_state.import_file = (up.name, up.getvalue())
if "import_file" not in st.session_state:
    hist = I.history()
    if len(hist):
        st.subheader("Previous imports")
        st.dataframe(hist, hide_index=True, width="stretch")
    st.stop()

name, data = st.session_state.import_file
digest = I.file_hash(data)


@st.cache_data(show_spinner="Reading file...", max_entries=5)
def _read(h: str, fname: str, raw: bytes):
    return I.read_file(raw, fname)


try:
    sheets = _read(digest, name, data)
except Exception as e:                                   # corrupt / password-protected / wrong type
    st.error(f"Could not read **{name}**: {e}")
    st.stop()

st.markdown(f"**File:** {name} · {len(data) / 1024:,.0f} KB")
sheet_names = [s for s, df in sheets.items() if len(df)]
if not sheet_names:
    st.error("The file has no data.")
    st.stop()
sheet = st.selectbox("Sheet", sheet_names, index=0) if len(sheet_names) > 1 else sheet_names[0]
raw = sheets[sheet]

auto_header = I.find_header(raw)
c1, c2 = st.columns([1, 3])
header_row = c1.number_input("Header is on row", 1, max(1, min(60, len(raw))), auto_header + 1,
                             help="Found automatically - change it if the column names look wrong") - 1
df = I.frame(raw, header_row)
with c2.expander(f"Top of the file ({len(df):,} data rows found)"):
    st.dataframe(df.head(8), width="stretch", hide_index=True)

prev = I.already_imported(digest)
if len(prev):
    st.warning(f"This exact file was already imported on {prev['ts'].iloc[0]} (import #{prev['id'].iloc[0]}). "
               "Importing again will not duplicate items, batches or bills that already exist.")

# ------------------------------------------------------------------ 2. what is it + column mapping
detected, scores = I.detect_dataset(df.columns)
labels = {d: I.DATASETS[d]["label"] for d in I.ORDER}
dataset = st.selectbox("What is in this file?", I.ORDER, index=I.ORDER.index(detected), format_func=labels.get,
                       help="Detected from the column names")
st.caption(I.DATASETS[dataset]["hint"])

st.markdown("**Match the columns** (filled in automatically - fix any that are wrong)")
auto = I.auto_map(df.columns, dataset)
NONE = "— not in file —"
mapping = {}
fields = I.DATASETS[dataset]["fields"]
cols = st.columns(3)
for n, (f, (label, req, _)) in enumerate(fields.items()):
    options = [NONE] + list(df.columns)
    pick = cols[n % 3].selectbox(f"{label}{' *' if req else ''}", options,
                                 index=options.index(auto[f]) if auto.get(f) in options else 0,
                                 key=f"map_{digest[:8]}_{dataset}_{f}")
    mapping[f] = None if pick == NONE else pick

options = {}
if dataset == "stock":
    c1, c2 = st.columns(2)
    options["default_schedule"] = c1.selectbox(
        "Schedule for items without one", ["H", "OTC"],
        help="H is safer: billing asks for the prescriber. Mark OTC items later in Inventory.")
    options["default_gst"] = c2.selectbox("GST % for items without one", [5, 12, 18, 0])
elif dataset == "purchases":
    options["purchases_paid"] = st.checkbox(
        "These old bills are already paid", value=True,
        help="Import what you still owe through the Suppliers file (balance column) instead.")

# ------------------------------------------------------------------ 3. check (preview)
try:
    checked = I.validate(df, dataset, mapping, options)
except ValueError as e:
    st.error(str(e))
    st.stop()

st.subheader("Check before saving")
m = st.columns(4)
m[0].metric("Rows in file", f"{checked.counts['rows_in_file']:,}")
m[1].metric("Ready to import", f"{checked.counts['ready']:,}")
m[2].metric("Errors (row skipped)", checked.errors)
m[3].metric("Warnings (imported)", checked.warnings)
extra = {k: v for k, v in checked.counts.items() if k not in ("rows_in_file", "ready")}
if extra:
    st.caption(" · ".join(f"{k.replace('_', ' ')}: **{v:,}**" for k, v in extra.items()))

if len(checked.problems):
    lv = st.segmented_control("Show", ["error", "warning"], default="error" if checked.errors else "warning",
                              key="lv") or "error"
    probs = checked.problems[checked.problems["level"] == lv]
    st.dataframe(probs, hide_index=True, width="stretch", height=min(35 * (len(probs) + 1), 300))
    st.download_button("⬇ Download all problems (CSV)", checked.problems.to_csv(index=False),
                       "import_problems.csv", "text/csv")
with st.expander("Preview the cleaned data", expanded=not checked.errors):
    st.dataframe(pd.DataFrame(checked.rows).head(50), hide_index=True, width="stretch")

# ------------------------------------------------------------------ 4. save
ok = st.checkbox(f"I have checked the preview - import {checked.counts['ready']:,} rows")
if st.button("📥 Import now", type="primary", disabled=not ok or not checked.rows):
    t = tenancy.current()
    try:
        if t:
            accounts.backup_store(t["store_id"])            # safety copy before changing data
        with st.spinner("Saving..."):
            res = I.apply(checked, name, digest, user=(t or {}).get("user_id", "owner"), options=options)
        if t:
            accounts.audit("data_imported", t["user_id"], t["store_id"], f"{dataset}: {name}")
        st.session_state.pop("import_file", None)
        st.session_state.import_done = (dataset, res)
        st.cache_data.clear()
        st.rerun()
    except Exception as e:
        st.error(f"Import failed - nothing was saved. ({e})")
