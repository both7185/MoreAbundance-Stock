"""
PO → Excel summary (Streamlit)

Run:  streamlit run app.py

Tabs
  1. รวมรายการสินค้า   — items grouped across POs, one quantity column per date
  2. รวมยอดทั้งเดือน    — one row per PO (name + total), month total at the bottom
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from monthly_report import (
    build_monthly_workbook, build_rows, grand_total, month_label, month_short, pdfs_from_zip, status_text,
)
from po_extractor import PurchaseOrder, parse_po, thai_short_date
from report_builder import (
    aggregate, build_workbook, categorize, date_range_label, load_catalog, save_catalog,
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
    # Step 2: aggregated preview (category column editable)
    # --------------------------------------------------------------------------- #
    summary = aggregate(pos, date_for_po, catalog, apply_aliases)

    st.subheader(f"2. ตารางสรุป — {date_range_label(summary.dates)}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("ไฟล์", len(pos))
    c2.metric("วันที่ (คอลัมน์)", len(summary.dates))
    c3.metric("รายการสินค้า", len(summary.rows))
    grand = sum(r.unit_price * q for r in summary.rows for q in r.qty_by_date.values())
    c4.metric("ยอดรวม", f"{grand:,.2f}")

    categories = list(dict.fromkeys(catalog["category_order"] + sorted({r.category for r in summary.rows if r.category})))
    preview = []
    for i, r in enumerate(summary.rows, start=1):
        total_qty = sum(r.qty_by_date.values())
        rec = {"ลำดับ": i, "หมวดหมู่": r.category or None, "รายการสินค้า": r.name,
               "หน่วย": r.unit, "จำนวนรวม": total_qty, "ราคา/หน่วย": r.unit_price}
        for d in summary.dates:
            q = r.qty_by_date.get(d)
            rec[f"{thai_short_date(d)} จำนวน"] = "" if q is None else f"{q:,g}"
        rec["จำนวนเงินรวม"] = round(total_qty * r.unit_price, 2)
        preview.append(rec)
    preview_df = pd.DataFrame(preview)

    st.caption("แก้ไข **หมวดหมู่** ได้ในตารางนี้ (คอลัมน์อื่นเป็นค่าจาก PDF)")
    edited_preview = st.data_editor(
        preview_df,
        hide_index=True,
        width="stretch",
        height=min(38 * (len(preview_df) + 1), 600),
        disabled=[c for c in preview_df.columns if c != "หมวดหมู่"],
        column_config={
            "หมวดหมู่": st.column_config.SelectboxColumn(options=categories),
            "ราคา/หน่วย": st.column_config.NumberColumn(format="%.2f"),
            "จำนวนรวม": st.column_config.NumberColumn(format="localized"),
            **{c: st.column_config.TextColumn(alignment="right") for c in preview_df.columns if c.endswith(" จำนวน")},
            "จำนวนเงินรวม": st.column_config.NumberColumn(format="accounting"),
        },
        key="preview_" + "|".join(sorted(date_for_po)) + str(apply_aliases),
    )

    overrides = {
        row["รายการสินค้า"]: row["หมวดหมู่"]
        for _, row in edited_preview.iterrows()
        if row["หมวดหมู่"] and row["หมวดหมู่"] != categorize(row["รายการสินค้า"], catalog)
    }
    if overrides:
        if st.button(f"💾 จำหมวดหมู่ที่แก้ไว้ใน catalog.json ({len(overrides)} รายการ)"):
            catalog["items"].update(overrides)
            save_catalog(catalog)
            st.success("บันทึกแล้ว — ครั้งหน้าจะใช้หมวดหมู่นี้อัตโนมัติ")

    # --------------------------------------------------------------------------- #
    # Step 3: Excel
    # --------------------------------------------------------------------------- #
    st.subheader("3. ดาวน์โหลด Excel")
    final = aggregate(pos, date_for_po, catalog, apply_aliases, category_overrides=overrides)
    xlsx = build_workbook(final, po_label_override.strip() or None)
    fname = f"ตารางสรุปใบสั่งซื้อ {date_range_label(final.dates)}.xlsx".replace("/", "-")
    st.download_button(
        "⬇️ ดาวน์โหลดไฟล์ Excel",
        data=xlsx,
        file_name=fname,
        mime=XLSX_MIME,
        type="primary",
        key="daily_download",
    )


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


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #
st.title("🧾 สรุปและรวมรายการใบสั่งซื้อ")
tab_daily, tab_month = st.tabs(["📦 รวมรายการสินค้า", "📅 รวมยอดทั้งเดือน"])
with tab_daily:
    daily_section()
with tab_month:
    monthly_section()
