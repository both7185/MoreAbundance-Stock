"""
หน้า 📦 สต็อกสินค้า (separate from the PO pages). Data logic lives in stock.py.

* Main table: name / หมวด / หน่วย / ขั้นต่ำ are edited in the table (💾 บันทึก); tick ประวัติ to see an
  item's in/out history (and fix its expiry lots) under the table.
* ➕➖ เข้า-ออกสินค้า: search an item, then เข้า / ออก / จำนวนในสต็อก / วันหมดอายุ / วันที่บันทึก → ยอดใหม่.
* 🆕 เพิ่มสินค้าใหม่: name, รหัส, หมวด only — quantities come from เข้า-ออกสินค้า.
"""
from __future__ import annotations

from datetime import timedelta

import pandas as pd
import streamlit as st

import datastore
import stock
from products import github_config
from stock import AVAILABLE, CATEGORIES, LOW, OUT, fmt_date, fmt_num

COLORS = {AVAILABLE: ("#1b7f3b", "#e3f4e8"), LOW: ("#c25e00", "#fdeedd"), OUT: ("#c62828", "#fde4e4")}
STATUS_TH = {AVAILABLE: "Available", LOW: "Low stock", OUT: "Out of stock"}

C_NAME, C_CODE, C_CAT, C_QTY, C_UNIT, C_MIN, C_STATUS, C_EXP, C_HIST = (
    "ชื่อสินค้า", "รหัส", "หมวด", "จำนวนในสต็อก", "หน่วย", "ขั้นต่ำ", "สถานะ", "วันหมดอายุ", "ประวัติ")


def _flash(kind: str, text: str) -> None:
    """Shown by app.show_flash() after st.rerun()."""
    st.session_state.setdefault("_flash", []).append((kind, text))


def _bump() -> None:
    st.session_state["stock_ver"] = st.session_state.get("stock_ver", 0) + 1


def save(change, message: str) -> bool:
    """
    Apply `change(data) -> error text` to the latest stock.json and save it (local file + GitHub data
    branch when configured). The latest version is downloaded first, so two people saving one after
    the other do not overwrite each other's entries.
    """
    gh = github_config(st.secrets)
    data = None
    if gh:
        try:
            latest = datastore.fetch(gh, stock.STOCK_FILE)
            data = stock.parse(latest) if latest else None
        except Exception as e:
            st.error(f"ดึงข้อมูลสต็อกล่าสุดจาก GitHub ไม่ได้ — ยังไม่ได้บันทึก ลองใหม่อีกครั้ง ({str(e)[:150]})")
            return False
    if data is None:
        data = stock.load()
    err = change(data)
    if err:
        st.error(err)
        return False
    stock.save_local(data)
    if gh:
        try:
            datastore.push(gh, stock.STOCK_FILE, stock.to_json(data), message)
        except Exception as e:
            _flash("error", "บันทึกไว้ชั่วคราวแล้ว แต่ส่งขึ้น GitHub ไม่สำเร็จ — บันทึกอีกครั้ง "
                            f"ไม่อย่างนั้นข้อมูลจะหายเมื่อแอปรีสตาร์ท: {str(e)[:200]}")
            _bump()
            return True
    _flash("success", message.replace("Stock: ", "บันทึกแล้ว — ", 1))
    _bump()
    return True


def lots_text(item: dict) -> str:
    """All expiry dates of an item, earliest first: '26/03/2027 (20) · 01/05/2027 (12)'."""
    today = stock.today().isoformat()
    parts = []
    for lot in item["lots"]:
        mark = "⚠️ " if lot["exp"] < today else ""
        parts.append(f"{mark}{fmt_date(lot['exp'])} ({fmt_num(lot['qty'])})")
    return " · ".join(parts)


def item_label(item: dict) -> str:
    return f"{item['code']} · {item['name']}"


# --------------------------------------------------------------------------- #
# Dialogs
# --------------------------------------------------------------------------- #
@st.dialog("➕➖ เข้า-ออกสินค้า", width="large")
def movement_dialog(data: dict) -> None:
    items = {it["code"]: it for it in data["items"]}
    code = st.selectbox(
        "ค้นหาสินค้า", list(items), index=None, format_func=lambda c: item_label(items[c]),
        placeholder="พิมพ์ชื่อสินค้าหรือรหัส เช่น น้ำปลา หรือ I-001", key="mv_search",
    )
    if code is None:
        st.caption("พิมพ์ชื่อหรือรหัสในช่องด้านบน แล้วเลือกสินค้าที่ต้องการ")
        return
    item = items[code]
    s = stock.status(item)
    color = COLORS[s][0]
    unit = item["unit"]
    st.markdown(
        f"**{item['name']}** ({item['code']} · {item['category']}) — ตอนนี้มี "
        f"<span style='color:{color};font-weight:600'>{fmt_num(item['qty'])} {unit}</span> "
        f"· ขั้นต่ำ {fmt_num(item['min'])} · <span style='color:{color}'>{STATUS_TH[s]}</span>",
        unsafe_allow_html=True,
    )
    if item["lots"]:
        st.caption("วันหมดอายุ: " + lots_text(item))

    c_in, c_out, c_count, c_exp, c_date, c_after = st.columns([1, 1, 1.1, 1.2, 1.2, 1.1])
    count = c_count.number_input("จำนวนในสต็อก", min_value=0.0, value=float(item["qty"]), step=1.0,
                                 format="%g", key=f"mv_count_{code}",
                                 help="ยอดตามสต็อกตอนนี้ — ถ้านับจริงได้ไม่ตรง แก้ตัวเลขนี้เป็นยอดที่นับได้")
    qty_in = c_in.number_input("เข้า (+)", min_value=0.0, value=0.0, step=1.0, format="%g", key=f"mv_in_{code}")
    out_locked = count <= 0
    qty_out = c_out.number_input("ออก (−)", min_value=0.0, value=0.0, step=1.0, format="%g", key=f"mv_out_{code}",
                                 disabled=out_locked,
                                 help="สินค้าหมดสต็อก (0) — เอาออกไม่ได้" if out_locked else None)
    if out_locked:
        qty_out = 0.0
    exp = c_exp.date_input("วันหมดอายุ (ไม่บังคับ)", value=None, format="DD/MM/YYYY", key=f"mv_exp_{code}",
                           help="วันหมดอายุของล็อตที่รับเข้า — ไม่รู้ก็เว้นว่างได้")
    today = stock.today()
    on = c_date.date_input("วันที่บันทึก", value=today, format="DD/MM/YYYY", key=f"mv_date_{code}",
                           min_value=today - timedelta(days=stock.HISTORY_DAYS), max_value=today,
                           help=f"เก็บประวัติย้อนหลัง {stock.HISTORY_DAYS} วัน")
    after = stock.num(count + qty_in - qty_out)
    after_color = "#c62828" if after < 0 else COLORS[stock.status({**item, "qty": after})][0]
    c_after.markdown(
        f"<div style='font-size:0.875rem'>ยอดหลังบันทึก</div>"
        f"<div style='font-size:1.6rem;font-weight:700;color:{after_color};line-height:2.2rem'>"
        f"{fmt_num(after)} <span style='font-size:0.9rem'>{unit}</span></div>",
        unsafe_allow_html=True,
    )
    if after < 0:
        st.error(f"เอาออกได้ไม่เกิน {fmt_num(count + qty_in)} {unit}")

    recount = stock.num(count) != item["qty"]
    if c_after.button("💾 บันทึก", type="primary", key=f"mv_save_{code}", disabled=after < 0, width="stretch"):
        def change(d: dict) -> str:
            return stock.record(d, code, qty_in, qty_out, count if recount else None, exp, on)
        parts = [f"+{fmt_num(qty_in)}" if qty_in else "", f"−{fmt_num(qty_out)}" if qty_out else "",
                 f"นับใหม่ {fmt_num(count)}" if recount else ""]
        msg = f"Stock: {item['name']} ({code}) {' '.join(p for p in parts if p)} → {fmt_num(after)} {unit}"
        if save(change, msg.replace("  ", " ")):
            st.session_state.pop("mv_search", None)
            st.rerun()


@st.dialog("🆕 เพิ่มสินค้าใหม่")
def add_dialog(data: dict) -> None:
    st.caption("ใส่แค่ชื่อ รหัส และหมวด — จำนวน ขั้นต่ำ วันหมดอายุ ไปกรอกตอนเข้า-ออกสินค้า / ในตารางหลัก")
    name = st.text_input("ชื่อสินค้า", key="add_name")
    cat = st.selectbox("หมวด", list(CATEGORIES), index=None, placeholder="เลือกหมวด",
                       format_func=lambda c: f"{c} ({CATEGORIES[c]})", key="add_cat")
    code = st.text_input("รหัส", value=stock.next_code(data, cat) if cat else "", key=f"add_code_{cat}",
                         disabled=cat is None, help="ตั้งให้อัตโนมัติ = ตัวอักษรหมวด + เลขถัดไป (แก้ได้)")
    unit = st.text_input("หน่วย (ไม่บังคับ)", key="add_unit", placeholder="เช่น กก., ขวด, แพ็ค")
    if st.button("💾 เพิ่มสินค้า", type="primary", disabled=not (name.strip() and cat and code.strip())):
        if save(lambda d: stock.add_item(d, name, code, cat, unit),
                f"Stock: เพิ่มสินค้าใหม่ {name.strip()} ({code.strip().upper()})"):
            for k in ("add_name", "add_cat", "add_unit"):
                st.session_state.pop(k, None)
            st.rerun()


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #
def summary_cards(items: list[dict]) -> None:
    counts = {s: sum(stock.status(it) == s for it in items) for s in COLORS}
    cards = [("ทั้งหมด", len(items), "#333", "#999")] + [
        (STATUS_TH[s], counts[s], COLORS[s][0], COLORS[s][0]) for s in COLORS]
    html = "".join(
        f"<div style='flex:1;min-width:130px;border:1px solid #ddd;border-left:5px solid {edge};"
        f"border-radius:10px;padding:10px 16px'>"
        f"<span style='font-size:1.9rem;font-weight:700;color:{color}'>{n}</span> "
        f"<span style='font-size:0.85rem;letter-spacing:0.05em;color:{color}'>{label.upper()}</span></div>"
        for label, n, color, edge in cards)
    st.markdown(f"<div style='display:flex;gap:12px;flex-wrap:wrap;margin:4px 0 12px'>{html}</div>",
                unsafe_allow_html=True)


def render(show_flash=None) -> None:
    st.title("📦 สต็อกสินค้า")
    if show_flash:
        show_flash()
    st.caption("ดูยอดคงเหลือของสินค้าทั้งหมด • แก้ ชื่อสินค้า / หมวด / หน่วย / ขั้นต่ำ ในตารางได้เลยแล้วกด 💾 บันทึก "
               "(ย้ายหมวด = ได้รหัสใหม่เป็นเลขถัดไปของหมวดนั้น) • ติ๊กช่อง **ประวัติ** เพื่อดูการเข้า-ออกของสินค้านั้น")
    if not github_config(st.secrets):
        st.info("💡 ตอนนี้บันทึกลงไฟล์ในเครื่องที่รันแอปเท่านั้น — ถ้าแอปอยู่บน Streamlit Cloud "
                "ข้อมูลจะหายเมื่อแอปรีสตาร์ท จนกว่าจะตั้งค่า GitHub token (ดู README)")
    data = stock.load()
    items = data["items"]
    ver = st.session_state.get("stock_ver", 0)
    dirty = st.session_state.get("stock_dirty", False)

    b1, b2, _ = st.columns([1.2, 1.2, 4])
    if b1.button("➕➖ เข้า-ออกสินค้า", type="primary", width="stretch", disabled=dirty):
        st.session_state.pop("mv_search", None)
        movement_dialog(data)
    if b2.button("🆕 เพิ่มสินค้าใหม่", width="stretch", disabled=dirty):
        add_dialog(data)

    f1, f2 = st.columns([3, 1])
    query = f1.text_input("ค้นหา", placeholder="ค้นหาชื่อสินค้าหรือรหัส...", label_visibility="collapsed",
                          key="stock_query", disabled=dirty)
    cat_filter = f2.selectbox("หมวด", ["ทุกหมวด"] + list(CATEGORIES), label_visibility="collapsed",
                              key="stock_cat", disabled=dirty)
    if dirty:
        st.caption("⚠️ บันทึกหรือยกเลิกการแก้ไขในตารางก่อน จึงจะค้นหา / เข้า-ออกสินค้าได้")
    summary_cards(items)

    q = query.strip().lower()
    view = [it for it in items
            if (cat_filter == "ทุกหมวด" or it["category"] == cat_filter)
            and (not q or q in it["name"].lower() or q in it["code"].lower())]
    if not view:
        st.info("ไม่พบสินค้า" if items else "ยังไม่มีสินค้า — กด 🆕 เพิ่มสินค้าใหม่")
        return

    last = stock.last_movement(data)
    df = pd.DataFrame([{
        C_NAME: it["name"], C_CODE: it["code"], C_CAT: it["category"], C_QTY: it["qty"], C_UNIT: it["unit"],
        C_MIN: it["min"], C_STATUS: STATUS_TH[stock.status(it)], C_EXP: lots_text(it), C_HIST: False,
    } for it in view])
    status_by_row = [stock.status(it) for it in view]

    def row_colors(row: pd.Series) -> list[str]:
        fg, bg = COLORS[status_by_row[row.name]]
        return [f"color:{fg};font-weight:600" if c == C_QTY else
                f"color:{fg};background-color:{bg};font-weight:600" if c == C_STATUS else "" for c in row.index]

    styled = df.style.apply(row_colors, axis=1).format({C_QTY: fmt_num, C_MIN: fmt_num})
    edited = st.data_editor(
        styled,
        hide_index=True,
        width="stretch",
        height=min(38 + 35 * len(df), 640),
        disabled=[C_CODE, C_QTY, C_STATUS, C_EXP],
        column_config={
            C_NAME: st.column_config.TextColumn(required=True, width="medium"),
            C_CODE: st.column_config.TextColumn(width="small"),
            C_CAT: st.column_config.SelectboxColumn(options=list(CATEGORIES), required=True,
                                                    help="ย้ายหมวด = ได้รหัสใหม่ (ตัวอักษรหมวด + เลขถัดไป) ตอนบันทึก"),
            C_QTY: st.column_config.NumberColumn(width=115, help="เปลี่ยนยอดที่ปุ่ม ➕➖ เข้า-ออกสินค้า"),
            C_UNIT: st.column_config.TextColumn(width="small"),
            C_MIN: st.column_config.NumberColumn(min_value=0, width="small", format="localized",
                                                 help="ต่ำกว่าขั้นต่ำ = Low stock • 0 = Out of stock"),
            C_STATUS: st.column_config.TextColumn(width=110),
            C_EXP: st.column_config.TextColumn(width="medium", help="ทุกล็อตที่ยังเหลือ (จำนวน) • ⚠️ = หมดอายุแล้ว"),
            C_HIST: st.column_config.CheckboxColumn(width="small", help="ติ๊กเพื่อดูประวัติเข้า-ออก (ใต้ตาราง)"),
        },
        key=f"stock_editor_{ver}_{cat_filter}_{q}",
    )

    # ---- edits of name / หมวด / หน่วย / ขั้นต่ำ
    changes = []
    for it, (_, row) in zip(view, edited.iterrows()):
        new = (str(row[C_NAME] or "").strip(), row[C_CAT], str(row[C_UNIT] or "").strip(),
               stock.num(0 if pd.isna(row[C_MIN]) else row[C_MIN]))
        if new != (it["name"], it["category"], it["unit"], it["min"]):
            changes.append((it["code"], *new))
    if bool(changes) != dirty:
        st.session_state["stock_dirty"] = bool(changes)
        st.rerun()
    if changes:
        s1, s2, s3 = st.columns([1, 1, 3])
        if s1.button("💾 บันทึก", type="primary", key="stock_save"):
            moved = []

            def change(d: dict) -> str:
                for code, name, cat, unit, min_level in changes:
                    err, new_code = stock.update_item(d, code, name, cat, unit, min_level)
                    if err:
                        return f"{code}: {err}"
                    if new_code != code:
                        moved.append(f"{name}: {code} → {new_code}")
                return ""
            if save(change, f"Stock: แก้ไขข้อมูลสินค้า {len(changes)} รายการ"):
                for m in moved:
                    _flash("info", f"ย้ายหมวดแล้ว รหัสใหม่ — {m}")
                st.session_state["stock_dirty"] = False
                st.rerun()
        if s2.button("↩️ ยกเลิกการแก้ไข", key="stock_reset"):
            st.session_state["stock_dirty"] = False
            _bump()
            st.rerun()
        s3.caption(f"⚠️ แก้ไข {len(changes)} รายการ ยังไม่ได้บันทึก")

    # ---- history of the ticked items
    for it, ticked in zip(view, edited[C_HIST]):
        if ticked:
            history_panel(data, it, last.get(it["code"]))


def history_panel(data: dict, item: dict, last: str | None) -> None:
    unit = item["unit"]
    with st.container(border=True):
        st.markdown(f"#### 📜 ประวัติ — {item['name']} ({item['code']})")
        rows = stock.history(data, item["code"])
        if not rows:
            st.caption(f"ยังไม่มีการเข้า-ออกใน {stock.HISTORY_DAYS} วันที่ผ่านมา")
        else:
            st.caption(f"{len(rows)} รายการ ย้อนหลัง {stock.HISTORY_DAYS} วัน • ล่าสุด {fmt_date(last)}")
            st.dataframe(pd.DataFrame([{
                "วันที่": fmt_date(m["date"]),
                "เข้า (+)": fmt_num(m["in"]) if m["in"] else "",
                "ออก (−)": fmt_num(m["out"]) if m["out"] else "",
                "ยอดก่อน": fmt_num(m["before"]),
                "นับใหม่": fmt_num(m["count"]) if m["count"] != m["before"] else "",
                "ยอดหลัง": f"{fmt_num(m['after'])} {unit}".strip(),
                "วันหมดอายุ": fmt_date(m["exp"]),
                "บันทึกเมื่อ": m.get("saved_at", ""),
            } for m in rows]), hide_index=True, width="stretch")

        with st.expander("🗓️ แก้ล็อตวันหมดอายุ (ถ้ากรอกวันผิด)"):
            st.caption(f"ยอดในสต็อก {fmt_num(item['qty'])} {unit} — รวมทุกล็อตต้องไม่เกินยอดนี้ "
                       "(ส่วนที่ไม่ได้อยู่ในล็อต = ไม่รู้วันหมดอายุ) • ลบแถว: ติ๊กหน้าแถวแล้วกดถังขยะ")
            lots_df = pd.DataFrame({"วันหมดอายุ": pd.to_datetime([l["exp"] for l in item["lots"]]).date,
                                    "จำนวน": [float(l["qty"]) for l in item["lots"]]})
            ver = st.session_state.get("stock_ver", 0)
            lots = st.data_editor(
                lots_df, num_rows="dynamic", hide_index=True, key=f"lots_{item['code']}_{ver}",
                column_config={
                    "วันหมดอายุ": st.column_config.DateColumn(format="DD/MM/YYYY", required=True),
                    "จำนวน": st.column_config.NumberColumn(min_value=0, required=True, format="localized"),
                },
            )
            new_lots = [(d, q) for d, q in zip(lots["วันหมดอายุ"], lots["จำนวน"])
                        if not pd.isna(d) and not pd.isna(q)]
            current = [(pd.Timestamp(l["exp"]).date(), float(l["qty"])) for l in item["lots"]]
            if st.button("💾 บันทึกล็อต", key=f"lots_save_{item['code']}", disabled=new_lots == current):
                lots_arg = [(pd.Timestamp(d).date(), q) for d, q in new_lots]
                if save(lambda d: stock.set_lots(d, item["code"], lots_arg),
                        f"Stock: แก้ล็อตวันหมดอายุ {item['name']} ({item['code']})"):
                    st.rerun()
