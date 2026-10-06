"""
PO → Excel summary (Streamlit)

Run:  streamlit run app.py

Tabs
  1. รวมรายการสินค้า   — items grouped across POs, one quantity column per date
  2. รวมยอดทั้งเดือน    — one row per PO (name + total), month total at the bottom
  3. ข้อมูลสินค้า (Data) — product list (หมวดหมู่, ร้านหลัก, Sup no.) used for category + sort order
  4. รหัส Sup            — what each code position means + shops and their Sup no.
"""
from __future__ import annotations

import importlib
import os
import sys

import pandas as pd
import streamlit as st

# Streamlit Community Cloud re-reads app.py on every run but keeps the other modules in memory,
# so after a `git push` the old report_builder.py / products.py ... would keep running until a reboot.
# Reload any of our modules whose file changed since it was loaded (dependencies first).
for _name in ("po_extractor", "datastore", "products", "sup_codes", "report_builder", "monthly_report"):
    _mod = sys.modules.get(_name)
    if _mod is not None and getattr(_mod, "__file__", None):
        _mtime = os.path.getmtime(_mod.__file__)
        if getattr(_mod, "_file_mtime", _mtime) != _mtime:
            _mod = importlib.reload(_mod)
        _mod._file_mtime = _mtime

import datastore
from monthly_report import (
    build_monthly_workbook, build_rows, grand_total, month_label, month_short, pdfs_from_zip, status_text,
)
from po_extractor import PurchaseOrder, parse_po, thai_short_date
from products import (
    COL_ALIAS, COL_CAT, COL_NAME, COL_NO, COL_SUP, COL_SUPPLIER, COL_UNIT, COLUMNS,
    add_alias, build_lookup, export_excel, github_config, import_excel, suggest_category,
    load_products, normalize_rows, set_letter_order, suggest, thai_sort_key, to_csv, validate,
    save_products_local,
)
from report_builder import aggregate, build_workbook, date_range_label, load_catalog
from sup_codes import (
    code_meaning, export_sup_excel, group_label, import_sup_excel, letter_label, letter_order,
    load_sup, next_code, normalize_sup, save_sup_local, shop_map, to_json, validate_sup,
)

st.set_page_config(page_title="สรุปใบสั่งซื้อ", page_icon="🧾", layout="wide")

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@st.cache_data(show_spinner=False)
def _parse_cached(data: bytes, name: str) -> PurchaseOrder:
    return parse_po(data, name)


def collect_pdfs(uploads) -> list[tuple[str, bytes]]:
    """Uploaded PDFs + PDFs inside uploaded .zip files, with unique names."""
    files: list[tuple[str, bytes]] = []
    for up in uploads or []:
        if up.name.lower().endswith(".zip"):
            try:
                files.extend(pdfs_from_zip(up.getvalue()))
            except Exception as e:
                st.error(f"เปิดไฟล์ zip {up.name} ไม่ได้: {e}")
        else:
            files.append((up.name, up.getvalue()))
    seen: dict[str, int] = {}
    seen_data: dict[bytes, str] = {}
    unique, skipped = [], []
    for name, data in files:
        if data in seen_data:  # exactly the same file twice (e.g. alone and inside a zip) -> count once
            skipped.append(f"{name} (ซ้ำกับ {seen_data[data]})")
            continue
        seen_data[data] = name
        if name in seen:  # same file name, different content -> keep both, distinct keys
            seen[name] += 1
            stem, dot, ext = name.rpartition(".")
            name = f"{stem} ({seen[name]}).{ext}" if dot else f"{name} ({seen[name]})"
        else:
            seen[name] = 1
        unique.append((name, data))
    if skipped:
        st.warning("ข้ามไฟล์ที่อัปโหลดซ้ำ (เนื้อหาเหมือนกันทุกอย่าง): " + ", ".join(skipped))
    return unique


def parse_all(files: list[tuple[str, bytes]]) -> list[PurchaseOrder]:
    pos: list[PurchaseOrder] = []
    bar = st.progress(0.0, text="กำลังอ่านไฟล์ PDF...") if len(files) > 3 else None
    for i, (name, data) in enumerate(files, start=1):
        try:
            pos.append(_parse_cached(data, name))
        except Exception as e:  # corrupt / scanned / password-protected PDF
            st.error(f"อ่านไฟล์ {name} ไม่ได้: {e}")
        if bar:
            bar.progress(i / len(files), text=f"กำลังอ่านไฟล์ PDF... {i}/{len(files)}")
    if bar:
        bar.empty()
    return pos


def flash(kind: str, text: str) -> None:
    """Show a message after st.rerun()."""
    st.session_state.setdefault("_flash", []).append((kind, text))


def show_flash() -> None:
    # Always one container at the same spot, so the tabs below keep the selected tab after st.rerun()
    box = st.container()
    for kind, text in st.session_state.pop("_flash", []):
        getattr(box, kind)(text)


def _commit(filename: str, content: str, message: str) -> str | None:
    """
    Commit a data file to the GitHub *data* branch when secrets are set (see datastore.py).
    Returns an error text, '' if done, None if GitHub is not set up.
    """
    gh = github_config(st.secrets)
    if not gh:
        return None
    try:
        datastore.push(gh, filename, content, message)
        return ""
    except Exception as e:
        return str(e)


@st.cache_data(ttl=60, show_spinner=False)
def _sync_data(repo: str, data_branch: str, path: str) -> tuple[list[str], str]:
    """Download the data files from the data branch (at most once a minute). Args = cache key."""
    gh = github_config(st.secrets)
    if not gh:
        return [], ""
    try:
        return datastore.pull(gh), ""
    except Exception as e:
        return [], str(e)


def sync_data() -> None:
    gh = github_config(st.secrets)
    if gh:
        _, err = _sync_data(gh["repo"], datastore.data_branch(gh), gh["path"])
        if err:
            st.warning(f"ดึงข้อมูลสินค้าล่าสุดจาก GitHub ไม่ได้ — ใช้ข้อมูลที่มีในเครื่องไปก่อน ({err[:150]})")


def _report_save(what: str, results: list[str | None]) -> None:
    if any(r for r in results):
        flash("error", f"บันทึก{what}ไว้ชั่วคราวแล้ว แต่ส่งขึ้น GitHub ไม่สำเร็จ — กดบันทึกอีกครั้ง "
                       "ไม่อย่างนั้นการแก้ไขจะหายเมื่อแอปรีสตาร์ท: " + "; ".join(r for r in results if r))
    elif all(r == "" for r in results):
        flash("success", f"บันทึก{what}แล้ว และส่งขึ้น GitHub เรียบร้อย")
    else:
        flash("success", f"บันทึก{what}แล้ว")


def current_sup() -> dict:
    sup = load_sup()
    set_letter_order(letter_order(sup))   # position-1 order = Excel sort order
    return sup


def current_products(sup: dict | None = None) -> list[dict]:
    """Products with Sup no. taken from their ร้านหลัก."""
    return normalize_rows(load_products(), shop_map(sup or current_sup()))


def persist_products(rows: list[dict], message: str, sup: dict | None = None) -> bool:
    """Validate -> save products.csv -> commit to GitHub when configured. Returns True if saved."""
    rows = normalize_rows(rows, shop_map(sup or current_sup()))
    errors, warnings = validate(rows)
    if errors:
        st.error("บันทึกไม่ได้:\n\n" + "\n".join(f"- {e}" for e in errors))
        return False
    save_products_local(rows)
    _report_save(f"ข้อมูลสินค้า ({len(rows)} รายการ)", [_commit("products.csv", to_csv(rows), message)])
    for w in warnings[:10]:
        flash("warning", w)
    st.session_state["products_ver"] = st.session_state.get("products_ver", 0) + 1
    return True


def persist_sup(data: dict, message: str, products: list[dict] | None = None) -> bool:
    """
    Save sup_codes.json. Products follow their shop: a changed shop code (or renamed shop, passed in
    `products`) is written to products.csv too.
    """
    data = normalize_sup(data)
    products = products if products is not None else load_products()
    errors, warnings = validate_sup(data, products, previous=load_sup())
    if errors:
        st.error("บันทึกไม่ได้:\n\n" + "\n".join(f"- {e}" for e in errors))
        return False
    save_sup_local(data)
    results = [_commit("sup_codes.json", to_json(data), message)]
    new_products = normalize_rows(products, shop_map(data))
    if new_products != load_products():
        save_products_local(new_products)
        results.append(_commit("products.csv", to_csv(new_products), message + " (อัปเดต Sup no. ของสินค้า)"))
    _report_save("รหัส Sup", results)
    for w in warnings[:10]:
        flash("warning", w)
    st.session_state["sup_ver"] = st.session_state.get("sup_ver", 0) + 1
    st.session_state["products_ver"] = st.session_state.get("products_ver", 0) + 1
    return True


catalog = load_catalog()

# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.header("ตั้งค่า — รวมรายการสินค้า")
    date_basis = st.radio(
        "ใช้วันที่ใดเป็นคอลัมน์",
        ["ใช้ในวันที่ (วันส่งของ)", "วันที่ (หัวใบสั่ง)"],
        help="ไฟล์ตัวอย่างทั้ง 2 ใบมี 'วันที่' หัวกระดาษเป็น 2 ต.ค. เหมือนกัน "
             "แต่ template แยกคอลัมน์ตาม 'ใช้ในวันที่' (6 และ 7 ต.ค.)",
    )
    apply_aliases = st.toggle(
        "ปรับชื่อสินค้า/หน่วยตาม catalog.json", value=True,
        help="เช่น ซีอิ๊วขาวตราง่วนเชียง → ซีอิ๊วขาวง่วนเชียง, ปี๊บ → ปิ๊บ (ให้ตรงกับ template)",
    )
    po_label_override = st.text_input("ใบสั่งที่ (เว้นว่าง = ใช้ค่าจาก PDF)", "")


def daily_section() -> None:
    st.caption("อัปโหลดใบสั่งซื้อ (PDF หรือ .zip) กี่ไฟล์ก็ได้ → ตรวจสอบ → ดาวน์โหลด Excel ที่มีสูตรคำนวณ")
    uploads = st.file_uploader("ไฟล์ใบสั่งซื้อ (PDF / ZIP)", type=["pdf", "zip"],
                               accept_multiple_files=True, key="daily_upload")
    if not uploads:
        st.info("เลือกไฟล์ PDF ใบสั่งซื้ออย่างน้อย 1 ไฟล์")
        return
    pos = parse_all(collect_pdfs(uploads))
    if not pos:
        return

    use_delivery = date_basis.startswith("ใช้ใน")

    # --------------------------------------------------------------------------- #
    # Step 1: per-file check (date column is editable in case a header is unreadable)
    # --------------------------------------------------------------------------- #
    st.subheader("1. ตรวจสอบไฟล์ที่อ่านได้")
    files_df = pd.DataFrame([{
        "ไฟล์": p.filename,
        "ใบสั่งที่": p.po_number,
        "วันที่ (หัวใบสั่ง)": p.po_date,
        "ใช้ในวันที่": p.delivery_date,
        "วันที่ที่ใช้ในรายงาน": (p.delivery_date if use_delivery else p.po_date) or p.po_date or p.delivery_date,
        "จำนวนรายการ": len(p.items),
        "ยอดตามใบสั่ง": p.stated_total,
        "ยอดที่คำนวณได้": p.computed_total,
        "สถานะ": "✅ ตรงกัน" if not p.warnings else "⚠️ ตรวจสอบ",
    } for p in pos]).sort_values("วันที่ที่ใช้ในรายงาน", kind="stable")

    edited_files = st.data_editor(
        files_df,
        hide_index=True,
        width="stretch",
        disabled=[c for c in files_df.columns if c != "วันที่ที่ใช้ในรายงาน"],
        column_config={
            "วันที่ที่ใช้ในรายงาน": st.column_config.DateColumn(format="DD/MM/YYYY", required=True),
            "วันที่ (หัวใบสั่ง)": st.column_config.DateColumn(format="DD/MM/YYYY"),
            "ใช้ในวันที่": st.column_config.DateColumn(format="DD/MM/YYYY"),
            "ยอดตามใบสั่ง": st.column_config.NumberColumn(format="accounting"),
            "ยอดที่คำนวณได้": st.column_config.NumberColumn(format="accounting"),
        },
        # new key when the set of files changes, so a date typed for one file never moves to another
        key=f"files_{use_delivery}_" + "|".join(sorted(p.filename for p in pos)),
    )

    for p in pos:
        if p.warnings:
            with st.expander(f"⚠️ {p.filename}"):
                for w in p.warnings:
                    st.write("•", w)

    date_for_po = {}
    for _, row in edited_files.iterrows():
        d = row["วันที่ที่ใช้ในรายงาน"]
        if pd.isna(d):
            st.error(f"กรุณาระบุวันที่ให้ไฟล์ {row['ไฟล์']}")
            return
        date_for_po[row["ไฟล์"]] = pd.Timestamp(d).date()

    # --------------------------------------------------------------------------- #
    # Step 2: preview — category + order come from the product list (🗂️ tab)
    # --------------------------------------------------------------------------- #
    sup = current_sup()
    products = current_products(sup)
    summary = aggregate(pos, date_for_po, catalog, build_lookup(products), apply_aliases)

    st.subheader(f"2. ตารางสรุป — {date_range_label(summary.dates)}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("ไฟล์", len(pos))
    c2.metric("วันที่ (คอลัมน์)", len(summary.dates))
    c3.metric("รายการสินค้า", len(summary.rows))
    grand = sum(r.unit_price * q for r in summary.rows for q in r.qty_by_date.values())
    c4.metric("ยอดรวม", f"{grand:,.2f}")

    preview = []
    for i, r in enumerate(summary.rows, start=1):
        total_qty = sum(r.qty_by_date.values())
        rec = {"ลำดับ": i, "หมวดหมู่": r.category, "Sup no.": r.sup_no if r.matched else "⚠️ ไม่พบ",
               "รายการสินค้า": r.name, "หน่วย": r.unit, "จำนวนรวม": total_qty, "ราคา/หน่วย": r.unit_price}
        for d in summary.dates:
            q = r.qty_by_date.get(d)
            rec[f"{thai_short_date(d)} จำนวน"] = "" if q is None else f"{q:,g}"
        rec["จำนวนเงินรวม"] = round(total_qty * r.unit_price, 2)
        preview.append(rec)
    preview_df = pd.DataFrame(preview)

    st.caption("เรียงตาม **Sup no.** (A → C → M → S → X แล้วตามเลข) • หมวดหมู่และ Sup no. มาจากแท็บ 🗂️ ข้อมูลสินค้า • "
               "คอลัมน์ Sup no. แสดงบนเว็บเท่านั้น ไม่อยู่ใน Excel")
    st.dataframe(
        preview_df,
        hide_index=True,
        width="stretch",
        height=min(36 * (len(preview_df) + 1), 600),
        column_config={
            "ราคา/หน่วย": st.column_config.NumberColumn(format="%.2f"),
            "จำนวนรวม": st.column_config.NumberColumn(format="localized"),
            **{c: st.column_config.TextColumn(alignment="right") for c in preview_df.columns if c.endswith(" จำนวน")},
            "จำนวนเงินรวม": st.column_config.NumberColumn(format="accounting"),
        },
    )

    if summary.unmatched or summary.missing_category:
        unmatched_panel(summary.unmatched, summary.missing_category, products, sup)

    # --------------------------------------------------------------------------- #
    # Step 3: Excel
    # --------------------------------------------------------------------------- #
    st.subheader("3. ดาวน์โหลด Excel")
    xlsx = build_workbook(summary, po_label_override.strip() or None)
    fname = f"ตารางสรุปใบสั่งซื้อ {date_range_label(summary.dates)}.xlsx".replace("/", "-")
    st.download_button(
        "⬇️ ดาวน์โหลดไฟล์ Excel",
        data=xlsx,
        file_name=fname,
        mime=XLSX_MIME,
        type="primary",
        key="daily_download",
    )


def unmatched_panel(unmatched, missing, products: list[dict], sup: dict) -> None:
    """
    Fix the product list from the PO screen:
      * items not in the product list -> link to an existing product, or add as new products
      * products already in the list but without หมวดหมู่ -> fill it in (suggested from the same shop)
    """
    # the same item at two prices / units is still one product -> one row here
    po_names_of: dict[str, set] = {}
    first_rows = []
    for r in unmatched:
        if r.name not in po_names_of:
            po_names_of[r.name] = set()
            first_rows.append(r)
        po_names_of[r.name].update(r.po_names)
    unmatched = first_rows

    if unmatched:
        st.warning(f"มี {len(unmatched)} รายการที่ไม่พบในข้อมูลสินค้า — ตอนนี้จะอยู่ท้ายตารางและไม่มีหมวดหมู่")
    if missing:
        st.warning(f"มี {len(missing)} รายการที่อยู่ในข้อมูลสินค้าแล้ว แต่ยังไม่มีหมวดหมู่ — ช่องหมวดหมู่ใน Excel จะว่าง")

    names = [p[COL_NAME] for p in products]
    cats = sorted({p[COL_CAT] for p in products if p[COL_CAT]}, key=thai_sort_key)
    shops = [s["name"] for s in sup["shops"]]
    codes = shop_map(sup)
    letters = [d["code"] for d in sup["letters"]]
    groups = [d["code"] for d in sup["groups"]]
    shop_fmt = lambda n: f"{n} ({codes[n]})" if n in codes else n

    def val(v) -> str:
        return v.strip() if isinstance(v, str) else ""

    plan, new_shops, problems, cat_plan = [], {}, [], []
    with st.expander("➕ จับคู่ / เพิ่มสินค้าเหล่านี้เข้าข้อมูลสินค้า", expanded=True):
        # ------------------------------------------------------------------ #
        # A. items not in the product list
        # ------------------------------------------------------------------ #
        if unmatched:
            st.markdown("**ก. สินค้าที่ยังไม่มีในข้อมูลสินค้า**")
            st.caption(
                "**สะกดต่างจากชื่อเดิม** → เลือกในช่อง *จับคู่กับสินค้าเดิม* (ระบบเติมให้เมื่อชื่อคล้ายมาก — ตรวจก่อนบันทึก)  \n"
                "**สินค้าใหม่ ร้านเดิม** → เลือก *ร้านหลัก* แล้ว Sup no. จะตามร้านนั้น "
                "(ถ้าไม่เลือกหมวดหมู่ ระบบใช้หมวดหมู่ที่ร้านนั้นใช้บ่อยที่สุด)  \n"
                "**สินค้าใหม่ ร้านใหม่** → พิมพ์ *ชื่อร้านใหม่* แล้วเลือก *Sup ตัวที่ 1* และ *Sup ตัวที่ 2* — "
                "เลข 2 หลักท้ายระบบเรียงต่อให้อัตโนมัติ  \n"
                "แถวที่ไม่ได้กรอกอะไรจะถูกข้าม • ดูผลลัพธ์ในตารางด้านล่างก่อนกดบันทึก"
            )
            recs = []
            for r in unmatched:
                best = suggest(r.name, products, 1)
                best_name, score = best[0] if best else ("", 0.0)
                recs.append({
                    "ชื่อใน PO": r.name,
                    "หน่วย": r.unit,
                    "ใกล้เคียงที่สุด": f"{best_name} ({score:.0%})" if best_name else "",
                    "จับคู่กับสินค้าเดิม": best_name if score >= 0.88 else "",
                    COL_CAT: "", COL_SUPPLIER: "", "ชื่อร้านใหม่": "", "Sup ตัวที่ 1": "", "Sup ตัวที่ 2": "",
                })
            edited = st.data_editor(
                pd.DataFrame(recs), hide_index=True, width="stretch",
                disabled=["ชื่อใน PO", "หน่วย", "ใกล้เคียงที่สุด"],
                column_config={
                    "จับคู่กับสินค้าเดิม": st.column_config.SelectboxColumn(options=[""] + names),
                    COL_CAT: st.column_config.SelectboxColumn(options=[""] + cats),
                    COL_SUPPLIER: st.column_config.SelectboxColumn(
                        "ร้านหลัก (ร้านเดิม)", options=[""] + shops, format_func=shop_fmt),
                    "ชื่อร้านใหม่": st.column_config.TextColumn(help="ถ้าเป็นร้านที่ยังไม่มีในรายชื่อ"),
                    "Sup ตัวที่ 1": st.column_config.SelectboxColumn(
                        options=[""] + letters, format_func=lambda c: letter_label(sup, c) if c else "",
                        help="ตำแหน่งที่ 1 ของ Sup no."),
                    "Sup ตัวที่ 2": st.column_config.SelectboxColumn(
                        options=[""] + groups, format_func=lambda c: group_label(sup, c) if c else "",
                        help="ตำแหน่งที่ 2 ของ Sup no."),
                },
                key="unmatched_" + "|".join(r.name for r in unmatched),
            )

            by_name = {r.name: r for r in unmatched}
            for _, rec in edited.iterrows():
                item = by_name[rec["ชื่อใน PO"]]
                target, cat, shop = val(rec["จับคู่กับสินค้าเดิม"]), val(rec[COL_CAT]), val(rec[COL_SUPPLIER])
                new_name, l1, l2 = val(rec["ชื่อร้านใหม่"]), val(rec["Sup ตัวที่ 1"]), val(rec["Sup ตัวที่ 2"])
                if target:
                    plan.append(("link", item, target, None, None))
                    continue
                if not any([cat, shop, new_name, l1, l2]):
                    continue
                if shop:
                    plan.append(("add", item, cat or suggest_category(products, shop, codes[shop]), shop, codes[shop]))
                elif new_name:
                    if new_name in codes:                       # typed an existing shop name
                        plan.append(("add", item, cat or suggest_category(products, new_name), new_name, codes[new_name]))
                    elif new_name in new_shops:                 # same new shop used by several rows
                        plan.append(("add", item, cat, new_name, new_shops[new_name]))
                    elif l1 and l2:
                        code = next_code(sup, l1, l2, also_used=list(new_shops.values()))
                        new_shops[new_name] = code
                        plan.append(("add", item, cat, new_name, code))
                    else:
                        problems.append(f"'{item.name}': เลือก Sup ตัวที่ 1 และ 2 ให้ร้านใหม่ '{new_name}'")
                elif l1 or l2:
                    problems.append(f"'{item.name}': ใส่ชื่อร้านใหม่ (รหัสเป็นของร้าน)")
                else:
                    plan.append(("add", item, cat, "", ""))

        # ------------------------------------------------------------------ #
        # B. already in the product list, but หมวดหมู่ is empty
        # ------------------------------------------------------------------ #
        if missing:
            if unmatched:
                st.divider()
            st.markdown("**ข. สินค้าที่มีในข้อมูลแล้ว แต่ยังไม่มีหมวดหมู่**")
            st.caption("ระบบเติม *หมวดหมู่* ให้จากสินค้าอื่นของร้านเดียวกัน (เช่น A-002 แมคโคร → แมคโคร) — "
                       "ตรวจ แก้ได้ แล้วกดบันทึก • เปลี่ยน *ร้านหลัก* ได้ถ้าร้านไม่ถูก (Sup no. จะตามร้าน)")
            by_product = {p[COL_NAME]: p for p in products}
            mrecs = []
            for r in missing:
                prod = by_product.get(r.product_name, {})
                shop = prod.get(COL_SUPPLIER, "")
                mrecs.append({
                    COL_NAME: r.product_name,
                    "หน่วย": r.unit,
                    COL_SUPPLIER: shop,
                    COL_SUP: r.sup_no,
                    COL_CAT: suggest_category(products, shop, r.sup_no),
                })
            m_edited = st.data_editor(
                pd.DataFrame(mrecs), hide_index=True, width="stretch",
                disabled=[COL_NAME, "หน่วย", COL_SUP],
                column_config={
                    COL_SUPPLIER: st.column_config.SelectboxColumn(
                        options=list(dict.fromkeys([""] + shops + [m[COL_SUPPLIER] for m in mrecs])), format_func=shop_fmt),
                    COL_SUP: st.column_config.TextColumn(help="ตามร้านหลัก (อัปเดตหลังบันทึก)"),
                    COL_CAT: st.column_config.SelectboxColumn(options=[""] + cats, help="ระบบเติมให้จากร้านเดียวกัน"),
                },
                key="missingcat_" + "|".join(r.product_name for r in missing),
            )
            for _, rec in m_edited.iterrows():
                cat, shop = val(rec[COL_CAT]), val(rec[COL_SUPPLIER])
                old_shop = by_product.get(rec[COL_NAME], {}).get(COL_SUPPLIER, "")
                if cat or shop != old_shop:
                    cat_plan.append((rec[COL_NAME], cat, shop, codes.get(shop, rec[COL_SUP])))

        # ------------------------------------------------------------------ #
        # preview + save
        # ------------------------------------------------------------------ #
        if plan or cat_plan:
            st.markdown("**ผลลัพธ์ที่จะบันทึก**")
            preview = [{
                "ชื่อใน PO": p[1].name,
                "จะทำอะไร": f"จับคู่กับ '{p[2]}'" if p[0] == "link" else
                            ("เพิ่มสินค้าใหม่" + (f" + ร้านใหม่ '{p[3]}'" if p[3] in new_shops else "")),
                COL_CAT: "" if p[0] == "link" else (p[2] or "⚠️ ว่าง"),
                COL_SUPPLIER: "" if p[0] == "link" else p[3],
                COL_SUP: "" if p[0] == "link" else (p[4] or "⚠️ ไม่มี (จะอยู่ท้ายตาราง)"),
                "ความหมาย": "" if p[0] == "link" else code_meaning(sup, p[4] or ""),
            } for p in plan]
            preview += [{
                "ชื่อใน PO": name,
                "จะทำอะไร": "ใส่หมวดหมู่" + (" + เปลี่ยนร้าน" if shop != by_product.get(name, {}).get(COL_SUPPLIER, "") else ""),
                COL_CAT: cat or "⚠️ ว่าง",
                COL_SUPPLIER: shop,
                COL_SUP: code or "⚠️ ไม่มี (จะอยู่ท้ายตาราง)",
                "ความหมาย": code_meaning(sup, code or ""),
            } for name, cat, shop, code in cat_plan]
            st.dataframe(pd.DataFrame(preview), hide_index=True, width="stretch")
        for msg in problems:
            st.error(msg)

        if st.button("💾 บันทึกลงข้อมูลสินค้า", key="save_unmatched",
                     disabled=not (plan or cat_plan) or bool(problems)):
            rows = [dict(p) for p in products]
            for kind, item, a1, shop, code in plan:
                if kind == "link":
                    for alias in {item.name, *po_names_of[item.name]}:
                        if alias != a1:
                            add_alias(rows, a1, alias)
                else:
                    rows.append({COL_NO: "", COL_CAT: a1, COL_NAME: item.name, COL_UNIT: item.unit,
                                 COL_SUPPLIER: shop, COL_SUP: code or "",
                                 COL_ALIAS: " | ".join(n for n in sorted(po_names_of[item.name]) if n != item.name)})
            for name, cat, shop, code in cat_plan:
                for row in rows:
                    if row[COL_NAME] == name:
                        if cat:
                            row[COL_CAT] = cat
                        if shop:
                            row[COL_SUPPLIER], row[COL_SUP] = shop, code
            ok = True
            if new_shops:
                new_sup = dict(sup, shops=sup["shops"] + [{"name": n, "code": c} for n, c in new_shops.items()])
                ok = persist_sup(new_sup, f"PO app: เพิ่มร้านใหม่ {', '.join(new_shops)}")
                sup = new_sup
            if ok and persist_products(rows, f"PO app: จับคู่/เพิ่มสินค้า/ใส่หมวดหมู่ {len(plan) + len(cat_plan)} รายการ", sup):
                links = sum(1 for p in plan if p[0] == "link")
                parts = [f"จับคู่ {links} รายการ", f"เพิ่มสินค้าใหม่ {len(plan) - links} รายการ"]
                if cat_plan:
                    parts.append(f"ใส่หมวดหมู่ {len(cat_plan)} รายการ")
                if new_shops:
                    parts.append(f"ร้านใหม่ {len(new_shops)} ร้าน")
                flash("info", " • ".join(parts))
                st.rerun()


def monthly_section() -> None:
    st.caption("อัปโหลดใบสั่งซื้อทั้งเดือน (เลือกหลายไฟล์ หรืออัปโหลดเป็น .zip ทั้งโฟลเดอร์) "
               "→ ได้ Excel 2 คอลัมน์: ชื่อใบสั่งซื้อ และยอดรวม พร้อมยอดรวมทั้งเดือน")
    uploads = st.file_uploader("ไฟล์ใบสั่งซื้อทั้งเดือน (PDF / ZIP)", type=["pdf", "zip"],
                               accept_multiple_files=True, key="monthly_upload")
    use_printed = st.toggle(
        "ใช้ยอด 'เป็นเงิน' ตามที่พิมพ์ในใบสั่ง", value=True,
        help="ปิด = ใช้ผลรวม จำนวน × ราคา ของทุกรายการแทน "
             "(บางใบสั่งปัดยอดรวมเป็นบาทเต็ม เช่น 107,524.60 → 107,525)",
        key="monthly_printed",
    )
    st.caption("เรียงตามวันที่ในชื่อไฟล์ • วันเดียวกัน: ไม่มีวงเล็บ → (มื้อเย็น) → (กล่องโฟม) • "
               "ไม่สนใจคำว่า (อัพเดทใหม่)")
    if not uploads:
        st.info("เลือกไฟล์ PDF ของทั้งเดือน หรือไฟล์ .zip ของโฟลเดอร์")
        return
    pos = parse_all(collect_pdfs(uploads))
    if not pos:
        return

    rows = build_rows(pos, use_printed_total=use_printed)
    total = grand_total(rows)

    c1, c2, c3 = st.columns(3)
    c1.metric("ใบสั่งซื้อ", len(rows))
    c2.metric("เดือน", month_label(rows).replace("เดือน", "") or "-")
    c3.metric("ยอดรวมทั้งเดือน", f"{total:,.2f}")

    # checks
    names = [r.name for r in rows]
    dups = sorted({n for n in names if names.count(n) > 1})
    if dups:
        st.warning("ชื่อใบสั่งซื้อซ้ำกัน (อาจอัปโหลดทั้งฉบับเดิมและฉบับอัพเดทใหม่): " + ", ".join(dups))
    no_date = [r.filename for r in rows if r.day is None]
    if no_date:
        st.warning("หาวันที่ไม่ได้ทั้งจากชื่อไฟล์และใน PDF — จัดไว้ท้ายตาราง: " + ", ".join(no_date))

    df = pd.DataFrame([{
        "ลำดับ": i,
        "ใบสั่งซื้อ": r.name,
        "ยอดรวม": r.amount,
        "ยอดตามใบสั่ง": r.printed_total,
        "ผลรวมรายการ": r.computed_total,
        "สถานะ": status_text(r),
        "ไฟล์": r.filename,
    } for i, r in enumerate(rows, start=1)])
    st.caption("คอลัมน์ **ใบสั่งซื้อ**, **ยอดรวม**, **ผลรวมรายการ** และ **สถานะ** จะอยู่ใน Excel")
    st.dataframe(
        df, hide_index=True, width="stretch", height=min(36 * (len(df) + 1), 640),
        column_config={
            "ยอดรวม": st.column_config.NumberColumn(format="accounting"),
            "ยอดตามใบสั่ง": st.column_config.NumberColumn(format="accounting"),
            "ผลรวมรายการ": st.column_config.NumberColumn(format="accounting"),
            "สถานะ": st.column_config.TextColumn(help="เทียบผลรวม จำนวน × ราคา ของทุกรายการ กับยอด 'เป็นเงิน' ที่พิมพ์ในใบสั่ง"),
        },
    )

    st.download_button(
        "⬇️ ดาวน์โหลด Excel ยอดรวมทั้งเดือน",
        data=build_monthly_workbook(rows),
        file_name=f"สรุปยอดรวมใบสั่งซื้อ {month_short(rows)}.xlsx".replace("/", "-").replace(" .xlsx", ".xlsx"),
        mime=XLSX_MIME,
        type="primary",
        key="monthly_download",
    )


SORT_OPTIONS = {
    "ลำดับในรายการ": None,
    "หมวดหมู่ (ก-ฮ)": lambda r: (thai_sort_key(r[COL_CAT]), r[COL_CAT] == ""),
    "Sup no.": lambda r: (r[COL_SUP] == "", r[COL_SUP]),
    "ร้านหลัก (ก-ฮ)": lambda r: thai_sort_key(r[COL_SUPPLIER]),
    "รายการ (ก-ฮ)": lambda r: thai_sort_key(r[COL_NAME]),
}


def products_section() -> None:
    sup = current_sup()
    products = current_products(sup)
    ver = st.session_state.get("products_ver", 0)
    gh = github_config(st.secrets)
    codes = shop_map(sup)

    st.caption("รายการสินค้าที่ใช้กำหนด **หมวดหมู่** และ **ลำดับการเรียง (Sup no.)** ในไฟล์ Excel รวมรายการสินค้า  \n"
               "เพิ่มแถว: ปุ่ม **+** มุมขวาบนของตาราง • ลบแถว: ติ๊กช่องหน้าแถวแล้วกดไอคอนถังขยะ • "
               "ค้นหา: ไอคอนแว่นขยาย • แก้เสร็จแล้วกด 💾 บันทึก  \n"
               "**Sup no. ตามร้านหลักอัตโนมัติ** — เปลี่ยนรหัสหรือเพิ่มร้านใหม่ที่แท็บ 🏷️ รหัส Sup")
    if not gh:
        st.info("💡 ตอนนี้บันทึกลงไฟล์ในเครื่องที่รันแอปเท่านั้น — ถ้าแอปอยู่บน Streamlit Cloud "
                "การแก้ไขจะหายเมื่อแอปรีสตาร์ท จนกว่าจะตั้งค่า GitHub token (ดู README)")

    c1, c2, c3, c4 = st.columns([1, 1, 1, 2])
    c1.metric("สินค้า", len(products))
    c2.metric("หมวดหมู่", len({p[COL_CAT] for p in products if p[COL_CAT]}))
    c3.metric("ร้านหลัก", len({p[COL_SUPPLIER] for p in products if p[COL_SUPPLIER]}))
    dirty = st.session_state.get("products_dirty", False)
    sort_by = c4.selectbox(
        "เรียงตาราง", list(SORT_OPTIONS), key="products_sort", disabled=dirty,
        help="เรียงเพื่อดูเท่านั้น — ลำดับที่บันทึกในรายการไม่เปลี่ยน "
             "(ลำดับในรายการใช้เรียงสินค้าที่มี Sup no. เดียวกันในไฟล์ Excel)"
             + ("\n\nบันทึกหรือยกเลิกการแก้ไขก่อนเปลี่ยนการเรียง" if dirty else ""),
    )
    view = list(products)
    if SORT_OPTIONS[sort_by]:
        view = sorted(view, key=SORT_OPTIONS[sort_by])

    df = pd.DataFrame(view, columns=COLUMNS) if view else pd.DataFrame(columns=COLUMNS)
    df[COL_NO] = pd.to_numeric(df[COL_NO], errors="coerce")
    shop_options = list(dict.fromkeys([s["name"] for s in sup["shops"]] +
                                      [p[COL_SUPPLIER] for p in products if p[COL_SUPPLIER]]))
    cats = sorted({p[COL_CAT] for p in products if p[COL_CAT]}, key=thai_sort_key)
    edited = st.data_editor(
        df,
        num_rows="dynamic",
        hide_index=True,
        width="stretch",
        height=560,
        disabled=[COL_NO, COL_SUP],
        column_config={
            COL_NO: st.column_config.NumberColumn(width="small", help="ลำดับในรายการ (สินค้าใหม่ต่อท้าย)"),
            COL_CAT: st.column_config.TextColumn(help="เช่น " + ", ".join(cats[:6])),
            COL_NAME: st.column_config.TextColumn(required=True, help="ชื่อสินค้าตามใบ PO"),
            COL_SUPPLIER: st.column_config.SelectboxColumn(
                options=shop_options, format_func=lambda n: f"{n} ({codes[n]})" if n in codes else n,
                help="เลือกร้าน — Sup no. จะตามร้านนี้ (เพิ่มร้านใหม่ที่แท็บ 🏷️ รหัส Sup)"),
            COL_SUP: st.column_config.TextColumn(help="มาจากร้านหลักอัตโนมัติ"),
            COL_ALIAS: st.column_config.TextColumn(
                width="medium", help="ชื่อสะกดแบบอื่นที่ใช้ในใบ PO คั่นด้วย | เช่น ปลาช่อน | ปลาช่อนสด"),
        },
        key=f"products_editor_{ver}_{sort_by}",
    )
    rows = [{c: ("" if pd.isna(v) else v) for c, v in rec.items()} for rec in edited.to_dict("records")]
    # keep the saved list order whatever the view sort: existing rows by ลำดับ, new rows at the end
    rows.sort(key=lambda r: (r[COL_NO] == "", float(r[COL_NO]) if r[COL_NO] != "" else 0))
    for r in rows:
        if r[COL_NO] != "":
            r[COL_NO] = str(int(float(r[COL_NO])))
    changed = normalize_rows(rows, codes) != products
    if changed != dirty:
        st.session_state["products_dirty"] = changed
        st.rerun()

    b1, b2, b3 = st.columns([1, 1, 3])
    if b1.button("💾 บันทึก", type="primary", disabled=not changed, key="save_products"):
        if persist_products(rows, "PO app: แก้ไขข้อมูลสินค้า", sup):
            st.session_state["products_dirty"] = False
            st.rerun()
    if b2.button("↩️ ยกเลิกการแก้ไข", disabled=not changed, key="reset_products"):
        st.session_state["products_ver"] = ver + 1
        st.session_state["products_dirty"] = False
        st.rerun()
    if changed:
        b3.caption("⚠️ มีการแก้ไขที่ยังไม่ได้บันทึก")

    with st.expander("⬇️ ดาวน์โหลด / ⬆️ นำเข้าจาก Excel"):
        st.download_button("⬇️ ดาวน์โหลดข้อมูลสินค้าเป็น Excel", data=export_excel(products),
                           file_name="ข้อมูลสินค้า (Data).xlsx", mime=XLSX_MIME, key="export_products")
        st.divider()
        up = st.file_uploader("นำเข้าไฟล์ Excel (เช่น DATA P BOTH.xlsx) — จะ **แทนที่** ข้อมูลทั้งหมด",
                              type=["xlsx"], key=f"import_products_{ver}")
        if up is not None:
            try:
                new_rows, notes = import_excel(up.getvalue())
            except Exception as e:
                st.error(f"อ่านไฟล์ไม่ได้: {e}")
            else:
                st.write(f"พบ {len(new_rows)} รายการ (ตอนนี้มี {len(products)} รายการ)")
                for n in notes:
                    st.warning(n)
                if st.button(f"แทนที่ข้อมูลทั้งหมดด้วย {len(new_rows)} รายการนี้", key="confirm_import"):
                    if persist_products(new_rows, f"PO app: นำเข้าข้อมูลสินค้าจาก {up.name}", sup):
                        st.rerun()


def sup_section() -> None:
    sup = current_sup()
    products = load_products()
    ver = st.session_state.get("sup_ver", 0)
    used = {}
    for p in products:
        if p[COL_SUPPLIER]:
            used[p[COL_SUPPLIER]] = used.get(p[COL_SUPPLIER], 0) + 1

    st.caption("รูปแบบ Sup no. เช่น **M-211** = ตำแหน่งที่ 1 **M** (Market) • ตำแหน่งที่ 2 **2** (ของสด) • "
               "ตำแหน่งที่ 3-4 **11** (เลขเรียงต่อกันในกลุ่มเดียวกัน)  \n"
               "สินค้าแต่ละตัวได้ Sup no. ตาม **ร้านหลัก** — แก้รหัสของร้านที่นี่แล้วสินค้าทุกตัวของร้านนั้นเปลี่ยนตาม")

    # ---------------- add a shop ----------------
    st.subheader("➕ เพิ่มร้านใหม่")
    letters = [d["code"] for d in sup["letters"]]
    groups = [d["code"] for d in sup["groups"]]
    a1, a2, a3, a4 = st.columns([3, 2, 2, 2])
    new_name = a1.text_input("ชื่อร้าน", key=f"new_shop_name_{ver}", placeholder="เช่น ร้านใบมะกรูด")
    l1 = a2.selectbox("Sup ตัวที่ 1", letters, format_func=lambda c: letter_label(sup, c), key=f"new_shop_l1_{ver}",
                      index=letters.index("M") if "M" in letters else 0)
    l2 = a3.selectbox("Sup ตัวที่ 2", groups, format_func=lambda c: group_label(sup, c), key=f"new_shop_l2_{ver}")
    try:
        preview = next_code(sup, l1, l2) if l1 and l2 else ""
    except ValueError as e:
        preview = ""
        st.error(str(e))
    a4.text_input("Sup no. ที่จะได้", value=preview, disabled=True, key=f"new_shop_code_{ver}_{l1}_{l2}",
                  help="เลข 2 หลักท้าย = เลขสูงสุดที่ใช้แล้วในกลุ่มตัวที่ 2 เดียวกัน + 1")
    if st.button("➕ เพิ่มร้าน", key="add_shop", disabled=not (new_name.strip() and preview)):
        if new_name.strip() in shop_map(sup):
            st.error(f"มีร้าน '{new_name.strip()}' อยู่แล้ว ({shop_map(sup)[new_name.strip()]})")
        elif persist_sup(dict(sup, shops=sup["shops"] + [{"name": new_name.strip(), "code": preview}]),
                         f"PO app: เพิ่มร้าน {new_name.strip()} ({preview})"):
            flash("info", f"เพิ่ม '{new_name.strip()}' = {preview} แล้ว — เลือกร้านนี้ให้สินค้าได้ที่แท็บ 🗂️ ข้อมูลสินค้า")
            st.rerun()

    # ---------------- shops ----------------
    st.subheader("🏪 รายชื่อร้านและ Sup no.")
    shops_df = pd.DataFrame([{
        "ร้าน": s["name"], "Sup no.": s["code"], "ความหมาย": code_meaning(sup, s["code"]),
        "จำนวนสินค้า": used.get(s["name"], 0),
    } for s in sup["shops"]], columns=["ร้าน", "Sup no.", "ความหมาย", "จำนวนสินค้า"])
    ed_shops = st.data_editor(
        shops_df, num_rows="dynamic", hide_index=True, width="stretch", height=420,
        disabled=["ความหมาย", "จำนวนสินค้า"],
        column_config={
            "ร้าน": st.column_config.TextColumn(required=True, help="เปลี่ยนชื่อร้านได้ — สินค้าของร้านจะเปลี่ยนตาม"),
            "Sup no.": st.column_config.TextColumn(
                required=True, validate=r"^\s*[A-Za-z]+\s*-?\s*\d+\s*$",
                help="รูปแบบ A-001 — แก้แล้วสินค้าทุกตัวของร้านนี้เปลี่ยนตาม"),
            "จำนวนสินค้า": st.column_config.NumberColumn(help="ร้านที่ยังมีสินค้าใช้อยู่ลบไม่ได้"),
        },
        key=f"sup_shops_{ver}",
    )

    # ---------------- code meanings ----------------
    st.subheader("🔤 ความหมายของรหัส")
    m1, m2 = st.columns(2)
    ed_letters = m1.data_editor(
        pd.DataFrame([{"ตัวอักษร": d["code"], "ความหมาย": d["meaning"]} for d in sup["letters"]],
                     columns=["ตัวอักษร", "ความหมาย"]),
        num_rows="dynamic", hide_index=True, width="stretch",
        column_config={"ตัวอักษร": st.column_config.TextColumn(
            "Sup ตัวที่ 1", required=True, max_chars=1, validate=r"^[A-Za-z]$")},
        key=f"sup_letters_{ver}",
    )
    m1.caption("ลำดับแถวในตารางนี้ = ลำดับการเรียงในไฟล์ Excel (บนลงล่าง)")
    ed_groups = m2.data_editor(
        pd.DataFrame([{"ตัวเลข": d["code"], "ความหมาย": d["meaning"]} for d in sup["groups"]],
                     columns=["ตัวเลข", "ความหมาย"]),
        num_rows="dynamic", hide_index=True, width="stretch",
        column_config={"ตัวเลข": st.column_config.TextColumn(
            "Sup ตัวที่ 2", required=True, max_chars=1, validate=r"^\d$")},
        key=f"sup_groups_{ver}",
    )

    def txt(v) -> str:
        return "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v).strip()

    new_data = normalize_sup({
        "letters": [{"code": txt(r["ตัวอักษร"]), "meaning": txt(r["ความหมาย"])} for _, r in ed_letters.iterrows()],
        "groups": [{"code": txt(r["ตัวเลข"]), "meaning": txt(r["ความหมาย"])} for _, r in ed_groups.iterrows()],
        "shops": [{"name": txt(r["ร้าน"]), "code": txt(r["Sup no."])} for _, r in ed_shops.iterrows()],
    })
    # renamed shops: same row (index) as before, different name -> products follow
    renames = {}
    for idx, r in ed_shops.iterrows():
        if idx in shops_df.index:
            old, new = shops_df.at[idx, "ร้าน"], txt(r["ร้าน"])
            if new and old != new:
                renames[old] = new
    changed = new_data != sup

    b1, b2, b3 = st.columns([1, 1, 3])
    if b1.button("💾 บันทึกรหัส Sup", type="primary", disabled=not changed, key="save_sup"):
        prods = [dict(p, **{COL_SUPPLIER: renames.get(p[COL_SUPPLIER], p[COL_SUPPLIER])}) for p in products]
        if persist_sup(new_data, "PO app: แก้ไขรหัส Sup", prods):
            if renames:
                flash("info", "เปลี่ยนชื่อร้านในข้อมูลสินค้าด้วย: " + ", ".join(f"{o} → {n}" for o, n in renames.items()))
            st.rerun()
    if b2.button("↩️ ยกเลิกการแก้ไข", disabled=not changed, key="reset_sup"):
        st.session_state["sup_ver"] = ver + 1
        st.rerun()
    if changed:
        b3.caption("⚠️ มีการแก้ไขที่ยังไม่ได้บันทึก")

    with st.expander("⬇️ ดาวน์โหลด / ⬆️ นำเข้าจาก Excel"):
        st.download_button("⬇️ ดาวน์โหลดรหัส Sup เป็น Excel", data=export_sup_excel(sup),
                           file_name="Sup no.xlsx", mime=XLSX_MIME, key="export_sup")
        st.divider()
        up = st.file_uploader("นำเข้าไฟล์ Sup no.xlsx — จะ **แทนที่** ความหมายรหัสและรายชื่อร้านทั้งหมด",
                              type=["xlsx"], key=f"import_sup_{ver}")
        if up is not None:
            try:
                new_sup, notes = import_sup_excel(up.getvalue())
            except Exception as e:
                st.error(f"อ่านไฟล์ไม่ได้: {e}")
            else:
                st.write(f"พบ {len(new_sup['shops'])} ร้าน • ตัวที่ 1: {len(new_sup['letters'])} แบบ • "
                         f"ตัวที่ 2: {len(new_sup['groups'])} แบบ")
                for n in notes:
                    st.warning(n)
                if st.button("แทนที่ด้วยข้อมูลจากไฟล์นี้", key="confirm_import_sup"):
                    if persist_sup(new_sup, f"PO app: นำเข้ารหัส Sup จาก {up.name}"):
                        st.rerun()


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #
st.title("🧾 สรุปและรวมรายการใบสั่งซื้อ")
sync_data()   # products.csv / sup_codes.json from the GitHub data branch
show_flash()
tab_daily, tab_month, tab_data, tab_sup = st.tabs(
    ["📦 รวมรายการสินค้า", "📅 รวมยอดทั้งเดือน", "🗂️ ข้อมูลสินค้า (Data)", "🏷️ รหัส Sup"])
with tab_daily:
    daily_section()
with tab_month:
    monthly_section()
with tab_data:
    products_section()
with tab_sup:
    sup_section()
