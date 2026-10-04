"""
PO → Excel summary (Streamlit)

Run:  streamlit run app.py

Tabs
  1. รวมรายการสินค้า   — items grouped across POs, one quantity column per date
  2. รวมยอดทั้งเดือน    — one row per PO (name + total), month total at the bottom
  3. ข้อมูลสินค้า (Data) — product list (หมวดหมู่, ร้านหลัก, Sup no.) used for category + sort order
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
for _name in ("po_extractor", "products", "report_builder", "monthly_report"):
    _mod = sys.modules.get(_name)
    if _mod is not None and getattr(_mod, "__file__", None):
        _mtime = os.path.getmtime(_mod.__file__)
        if getattr(_mod, "_file_mtime", _mtime) != _mtime:
            _mod = importlib.reload(_mod)
        _mod._file_mtime = _mtime

from monthly_report import (
    build_monthly_workbook, build_rows, grand_total, month_label, month_short, pdfs_from_zip, status_text,
)
from po_extractor import PurchaseOrder, parse_po, thai_short_date
from products import (
    COL_ALIAS, COL_CAT, COL_NAME, COL_NO, COL_SUP, COL_SUPPLIER, COL_UNIT, COLUMNS, EDIT_COLUMNS,
    add_alias, build_lookup, export_excel, github_config, github_commit, import_excel,
    load_products, normalize_rows, sup_sort_key, suggest, to_csv, validate, save_products_local,
)
from report_builder import aggregate, build_workbook, date_range_label, load_catalog

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
    unique = []
    for name, data in files:
        if name in seen:  # same file name twice -> keep both, distinct keys
            seen[name] += 1
            name = f"{name} ({seen[name]})"
        else:
            seen[name] = 1
        unique.append((name, data))
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
    for kind, text in st.session_state.pop("_flash", []):
        getattr(st, kind)(text)


def persist_products(rows: list[dict], message: str) -> bool:
    """Validate -> save products.csv -> commit to GitHub when configured. Returns True if saved."""
    rows = normalize_rows(rows)
    errors, warnings = validate(rows)
    if errors:
        st.error("บันทึกไม่ได้:\n\n" + "\n".join(f"- {e}" for e in errors))
        return False
    save_products_local(rows)
    gh = github_config(st.secrets)
    if gh:
        try:
            github_commit(to_csv(rows), gh, message)
            flash("success", f"บันทึกแล้ว ({len(rows)} รายการ) และส่งขึ้น GitHub เรียบร้อย")
        except Exception as e:
            flash("error", f"บันทึกในเครื่องแล้ว แต่ส่งขึ้น GitHub ไม่สำเร็จ: {e}")
    else:
        flash("success", f"บันทึกแล้ว ({len(rows)} รายการ) ลงไฟล์ products.csv")
    for w in warnings[:10]:
        flash("warning", w)
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
        key=f"files_{use_delivery}",
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
    products = load_products()
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

    if summary.unmatched:
        unmatched_panel(summary.unmatched, products)

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


def unmatched_panel(unmatched, products: list[dict]) -> None:
    """Items not in the product list: link them to an existing product, or add them as new products."""
    st.warning(f"มี {len(unmatched)} รายการที่ไม่พบในข้อมูลสินค้า — ตอนนี้จะอยู่ท้ายตารางและไม่มีหมวดหมู่")
    with st.expander("➕ จับคู่ / เพิ่มสินค้าเหล่านี้เข้าข้อมูลสินค้า", expanded=True):
        st.caption(
            "**สะกดต่างจากชื่อเดิม** → เลือกสินค้าในช่อง *จับคู่กับสินค้าเดิม* (ระบบเติมให้เมื่อชื่อคล้ายมาก — ตรวจก่อนบันทึก)  \n"
            "**สินค้าใหม่** → ปล่อยช่องจับคู่ว่าง แล้วกรอก หมวดหมู่ / ร้านหลัก / Sup no. "
            "(ถ้าเลือกร้านหลักที่มีอยู่แล้วและเว้น Sup no. ว่าง ระบบจะใส่ Sup no. ของร้านนั้นให้)  \n"
            "แถวที่ไม่ได้กรอกอะไรจะถูกข้าม"
        )
        names = [p[COL_NAME] for p in products]
        cats = sorted({p[COL_CAT] for p in products if p[COL_CAT]})
        suppliers = sorted({p[COL_SUPPLIER] for p in products if p[COL_SUPPLIER]})
        recs = []
        for r in unmatched:
            best = suggest(r.name, products, 1)
            best_name, score = best[0] if best else ("", 0.0)
            recs.append({
                "ชื่อใน PO": r.name,
                "หน่วย": r.unit,
                "ใกล้เคียงที่สุด": f"{best_name} ({score:.0%})" if best_name else "",
                "จับคู่กับสินค้าเดิม": best_name if score >= 0.88 else "",
                COL_CAT: "", COL_SUPPLIER: "", COL_SUP: "",
            })
        df = pd.DataFrame(recs)
        edited = st.data_editor(
            df, hide_index=True, width="stretch",
            disabled=["ชื่อใน PO", "หน่วย", "ใกล้เคียงที่สุด"],
            column_config={
                "จับคู่กับสินค้าเดิม": st.column_config.SelectboxColumn(options=names),
                COL_CAT: st.column_config.SelectboxColumn(options=cats, help="สำหรับเพิ่มเป็นสินค้าใหม่"),
                COL_SUPPLIER: st.column_config.SelectboxColumn(options=suppliers, help="สำหรับเพิ่มเป็นสินค้าใหม่"),
                COL_SUP: st.column_config.TextColumn(help="เช่น M-602 (เว้นว่างได้ถ้าเลือกร้านหลักแล้ว)"),
            },
            key="unmatched_" + "|".join(r.name for r in unmatched),
        )
        if st.button("💾 บันทึกลงข้อมูลสินค้า", key="save_unmatched"):
            rows = [dict(p) for p in products]
            linked = added = 0
            by_name = {r.name: r for r in unmatched}
            for _, rec in edited.iterrows():
                item = by_name[rec["ชื่อใน PO"]]
                target = rec["จับคู่กับสินค้าเดิม"]
                if isinstance(target, str) and target:
                    for alias in {item.name, *item.po_names}:
                        if alias != target:
                            add_alias(rows, target, alias)
                    linked += 1
                    continue
                cat, sup_name, code = (rec[COL_CAT], rec[COL_SUPPLIER], rec[COL_SUP])
                if any(isinstance(v, str) and v.strip() for v in (cat, sup_name, code)):
                    rows.append({COL_NO: "", COL_CAT: cat or "", COL_NAME: item.name, COL_UNIT: item.unit,
                                 COL_SUPPLIER: sup_name or "", COL_SUP: code or "",
                                 COL_ALIAS: " | ".join(n for n in item.po_names if n != item.name)})
                    added += 1
            if not linked and not added:
                st.info("ยังไม่ได้เลือก/กรอกแถวไหน")
            elif persist_products(rows, f"PO app: จับคู่ {linked} / เพิ่มสินค้าใหม่ {added} รายการ"):
                flash("info", f"จับคู่ {linked} รายการ • เพิ่มสินค้าใหม่ {added} รายการ")
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


def products_section() -> None:
    products = load_products()
    ver = st.session_state.get("products_ver", 0)
    gh = github_config(st.secrets)

    st.caption("รายการสินค้าที่ใช้กำหนด **หมวดหมู่** และ **ลำดับการเรียง (Sup no.)** ในไฟล์ Excel รวมรายการสินค้า  \n"
               "เพิ่มแถว: คลิกแถวว่างล่างสุดของตาราง • ลบแถว: ติ๊กช่องหน้าแถวแล้วกดไอคอนถังขยะ (มุมขวาบนของตาราง) • "
               "ค้นหา: ไอคอนแว่นขยาย • แก้เสร็จแล้วกด 💾 บันทึก")
    if not gh:
        st.info("💡 ตอนนี้บันทึกลงไฟล์ products.csv ในเครื่องที่รันแอปเท่านั้น — ถ้าแอปอยู่บน Streamlit Cloud "
                "การแก้ไขจะหายเมื่อแอปรีสตาร์ท จนกว่าจะตั้งค่า GitHub token (ดู README)")

    c1, c2, c3 = st.columns(3)
    c1.metric("สินค้า", len(products))
    c2.metric("ร้านหลัก", len({p[COL_SUPPLIER] for p in products if p[COL_SUPPLIER]}))
    c3.metric("Sup no.", len({p[COL_SUP] for p in products if p[COL_SUP]}))

    df = pd.DataFrame(products, columns=COLUMNS) if products else pd.DataFrame(columns=COLUMNS)
    cats = sorted({p[COL_CAT] for p in products if p[COL_CAT]})
    edited = st.data_editor(
        df,
        num_rows="dynamic",
        hide_index=True,
        width="stretch",
        height=560,
        disabled=[COL_NO],
        column_config={
            COL_NO: st.column_config.NumberColumn(width="small", help="เรียงเลขใหม่อัตโนมัติตอนบันทึก"),
            COL_CAT: st.column_config.TextColumn(help="เช่น " + ", ".join(cats[:6])),
            COL_NAME: st.column_config.TextColumn(required=True, help="ชื่อสินค้าตามใบ PO"),
            COL_SUP: st.column_config.TextColumn(
                help="รูปแบบ A-001 • เรียง A → C → M → S → X แล้วตามเลข • เว้นว่างได้ถ้าร้านหลักมีอยู่แล้ว",
                validate=r"^$|^\s*[A-Za-z]+\s*-?\s*\d+\s*$",
            ),
            COL_ALIAS: st.column_config.TextColumn(
                width="medium", help="ชื่อสะกดแบบอื่นที่ใช้ในใบ PO คั่นด้วย | เช่น ปลาช่อน | ปลาช่อนสด"),
        },
        key=f"products_editor_{ver}",
    )
    rows = [{c: ("" if pd.isna(v) else v) for c, v in rec.items()} for rec in edited.to_dict("records")]
    changed = normalize_rows(rows) != products

    b1, b2, b3 = st.columns([1, 1, 3])
    if b1.button("💾 บันทึก", type="primary", disabled=not changed, key="save_products"):
        if persist_products(rows, "PO app: แก้ไขข้อมูลสินค้า"):
            st.rerun()
    if b2.button("↩️ ยกเลิกการแก้ไข", disabled=not changed, key="reset_products"):
        st.session_state["products_ver"] = ver + 1
        st.rerun()
    if changed:
        b3.caption("⚠️ มีการแก้ไขที่ยังไม่ได้บันทึก")

    with st.expander("รายชื่อร้านหลัก → Sup no. (ไว้ดูตอนเพิ่มสินค้าใหม่)"):
        sup = {}
        for p in products:
            if p[COL_SUPPLIER]:
                sup.setdefault((p[COL_SUP], p[COL_SUPPLIER]), 0)
                sup[(p[COL_SUP], p[COL_SUPPLIER])] += 1
        st.dataframe(
            pd.DataFrame([{"Sup no.": k[0], "ร้านหลัก": k[1], "จำนวนสินค้า": v}
                          for k, v in sorted(sup.items(), key=lambda kv: (sup_sort_key(kv[0][0]), kv[0][1]))]),
            hide_index=True, width="stretch",
        )

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
                    if persist_products(new_rows, f"PO app: นำเข้าข้อมูลสินค้าจาก {up.name}"):
                        st.rerun()


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #
st.title("🧾 สรุปและรวมรายการใบสั่งซื้อ")
show_flash()
tab_daily, tab_month, tab_data = st.tabs(["📦 รวมรายการสินค้า", "📅 รวมยอดทั้งเดือน", "🗂️ ข้อมูลสินค้า (Data)"])
with tab_daily:
    daily_section()
with tab_month:
    monthly_section()
with tab_data:
    products_section()
