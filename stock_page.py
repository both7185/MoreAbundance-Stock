"""
หน้า 📦 สต็อกสินค้า (separate from the PO pages). Data logic lives in stock.py.

* Main table: click 📜 ดูประวัติ at the end of a row for that item's in/out history in a pop-up; click the
  วันหมดอายุ cell to edit its expiry lots (one lot per line). ✏️ แก้ไขตาราง switches the table to editing name / หมวด / หน่วย / ขั้นต่ำ (💾 บันทึก).
* 📜 ประวัติทั้งหมด: every item's movements in one pop-up.
* ➕➖ เข้า-ออกสินค้า: search an item, then เข้า / ออก / จำนวนในสต็อก / วันหมดอายุ / วันที่บันทึก → ยอดใหม่.
  When taking out, pick which expiry lot it comes from (the earliest expiry is picked first).
* 🆕 เพิ่มสินค้าใหม่: name, รหัส, หมวด only — quantities come from เข้า-ออกสินค้า.
* 🗑️ ลบสินค้า: search an item, tick to confirm, delete (its old movements stay in ประวัติทั้งหมด).
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


MAX_LOT_LINES = 3   # the table shows at most this many lots per row (every row gets the same height)
NBSP = "\u00a0"


def lots_text(item: dict, sep: str = " · ", limit: int | None = None) -> str:
    """Expiry dates of an item, earliest first: '26/03/2027 (20) · 01/05/2027 (12)'.
    With `limit`, the last part says how many more lots there are."""
    today = stock.today().isoformat()
    parts = []
    for lot in item["lots"]:
        mark = "⚠️ " if lot["exp"] < today else ""
        parts.append(f"{mark}{fmt_date(lot['exp'])} ({fmt_num(lot['qty'])})")
    if limit and len(parts) > limit:
        parts = parts[:limit - 1] + [f"…อีก {len(parts) - limit + 1} ล็อต"]
    return sep.join(parts)


def lots_cell(item: dict) -> str:
    """Expiry column of the main table: one lot per line. The table cell ignores line breaks, so each lot is
    glued with non-breaking spaces and the column is too narrow for two lots — the wrap puts each on its own line."""
    return " ".join(p.replace(" ", NBSP) for p in lots_text(item, " · ", MAX_LOT_LINES).split(" · "))


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

    lot = None
    if qty_out > 0 and after >= 0 and item["lots"]:
        lot = lot_picker(item, count, qty_out, unit)

    recount = stock.num(count) != item["qty"]
    if c_after.button("💾 บันทึก", type="primary", key=f"mv_save_{code}", disabled=after < 0, width="stretch"):
        def change(d: dict) -> str:
            return stock.record(d, code, qty_in, qty_out, count if recount else None, exp, on, lot)
        parts = [f"+{fmt_num(qty_in)}" if qty_in else "", f"−{fmt_num(qty_out)}" if qty_out else "",
                 f"นับใหม่ {fmt_num(count)}" if recount else ""]
        msg = f"Stock: {item['name']} ({code}) {' '.join(p for p in parts if p)} → {fmt_num(after)} {unit}"
        if save(change, msg.replace("  ", " ")):
            st.session_state.pop("mv_search", None)
            st.rerun()


def lot_picker(item: dict, count: float, qty_out: float, unit: str) -> str | None:
    """เอาออกจากล็อตไหน: the earliest expiry is picked first; shows where the amount will come from."""
    left = stock.plan_out(item, count, count)             # every lot (and the undated stock) at this count
    if not left:
        return None
    lots = {t["exp"]: t["qty"] for t in left}
    options = [e for e in sorted(lots) if e] + ([""] if "" in lots else [])
    first = options[0]

    def label(e: str) -> str:
        when = f"หมดอายุ {fmt_date(e)}" if e else "ไม่รู้วันหมดอายุ"
        return f"{when} — มี {fmt_num(lots[e])} {unit}".rstrip() + (" (หมดก่อน)" if e and e == first else "")

    c_lot, c_plan = st.columns([1.3, 2])
    lot = c_lot.selectbox("เอาออกจากล็อต", options, format_func=label, key=f"mv_lot_{item['code']}",
                          help="เลือกล็อตที่จะเอาออก — ถ้าล็อตนี้มีไม่พอ ส่วนที่เหลือเอาจากล็อตที่หมดอายุก่อน")
    taken = stock.plan_out(item, count, qty_out, lot)
    parts = [f"{fmt_date(t['exp']) if t['exp'] else 'ไม่รู้วันหมดอายุ'} −{fmt_num(t['qty'])}" for t in taken]
    rest = stock.num(qty_out - sum(t["qty"] for t in taken))
    if rest > 0:
        parts.append(f"ของที่รับเข้ารอบนี้ −{fmt_num(rest)}")
    c_plan.markdown("<div style='font-size:0.875rem'>จะเอาออกจาก</div>"
                    f"<div style='padding-top:6px'>{' · '.join(parts)}</div>", unsafe_allow_html=True)
    return lot


@st.dialog("🗑️ ลบสินค้า")
def delete_dialog(data: dict) -> None:
    items = {it["code"]: it for it in data["items"]}
    code = st.selectbox(
        "ค้นหาสินค้าที่จะลบ", list(items), index=None, format_func=lambda c: item_label(items[c]),
        placeholder="พิมพ์ชื่อสินค้าหรือรหัส", key="del_search",
    )
    if code is None:
        st.caption("พิมพ์ชื่อหรือรหัสในช่องด้านบน แล้วเลือกสินค้าที่ต้องการลบ")
        return
    item = items[code]
    unit = item["unit"]
    st.markdown(f"**{item['name']}** ({item['code']} · {item['category']}) — ตอนนี้มี "
                f"**{fmt_num(item['qty'])} {unit}**".rstrip())
    if item["lots"]:
        st.caption("วันหมดอายุ: " + lots_text(item))
    if item["qty"] > 0:
        st.warning(f"สินค้านี้ยังมีในสต็อก {fmt_num(item['qty'])} {unit} — ลบแล้วยอดนี้จะหายไปด้วย".replace("  ", " "))
    st.caption("ประวัติการเข้า-ออกเดิมยังดูได้ใน 📜 ประวัติทั้งหมด (ขึ้นว่า ลบแล้ว) • ลบแล้วกู้คืนไม่ได้ "
               "ต้องเพิ่มสินค้าใหม่")
    sure = st.checkbox(f"ยืนยันว่าจะลบ {item['name']}", key=f"del_sure_{code}")
    if st.button("🗑️ ลบสินค้า", type="primary", disabled=not sure, key=f"del_go_{code}"):
        if save(lambda d: stock.delete_item(d, code), f"Stock: ลบสินค้า {item['name']} ({code})"):
            st.session_state.pop("del_search", None)
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
    st.caption("ดูยอดคงเหลือของสินค้าทั้งหมด • กด **📜 ดูประวัติ** ท้ายแถวเพื่อดูการเข้า-ออกของสินค้านั้น • "
               "กดช่อง **วันหมดอายุ** เพื่อแก้วันหมดอายุ • "
               "เปิด **✏️ แก้ไขตาราง** เพื่อแก้ ชื่อสินค้า / หมวด / หน่วย / ขั้นต่ำ "
               "(ย้ายหมวด = ได้รหัสใหม่เป็นเลขถัดไปของหมวดนั้น)")
    if not github_config(st.secrets):
        st.info("💡 ตอนนี้บันทึกลงไฟล์ในเครื่องที่รันแอปเท่านั้น — ถ้าแอปอยู่บน Streamlit Cloud "
                "ข้อมูลจะหายเมื่อแอปรีสตาร์ท จนกว่าจะตั้งค่า GitHub token (ดู README)")
    data = stock.load()
    items = data["items"]
    ver = st.session_state.get("stock_ver", 0)
    dirty = st.session_state.get("stock_dirty", False)

    b1, b2, b3, b4, _ = st.columns([1.2, 1.2, 1.2, 1.2, 1.6])
    if b1.button("➕➖ เข้า-ออกสินค้า", type="primary", width="stretch", disabled=dirty):
        st.session_state["mv_search"] = None
        movement_dialog(data)
    if b2.button("🆕 เพิ่มสินค้าใหม่", width="stretch", disabled=dirty):
        add_dialog(data)
    if b3.button("📜 ประวัติทั้งหมด", width="stretch", disabled=dirty):
        all_history_dialog(data)
    if b4.button("🗑️ ลบสินค้า", width="stretch", disabled=dirty or not items):
        st.session_state["del_search"] = None
        delete_dialog(data)
    open_code = st.session_state.pop("hist_open", None)   # set by a click in the table, see below
    if open_code and stock.find(data, open_code):
        history_dialog(data, open_code)
    exp_code = st.session_state.pop("exp_open", None)
    if exp_code and stock.find(data, exp_code):
        expiry_dialog(data, exp_code)

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

    status_by_row = [stock.status(it) for it in view]

    def row_colors(row: pd.Series) -> list[str]:
        fg, bg = COLORS[status_by_row[row.name]]
        return [f"color:{fg};font-weight:600" if c == C_QTY else
                f"color:{fg};background-color:{bg};font-weight:600" if c == C_STATUS else "" for c in row.index]

    base_config = {
        C_NAME: st.column_config.TextColumn(width=230),
        C_CODE: st.column_config.TextColumn(width=70),
        C_CAT: st.column_config.TextColumn(width=95),
        C_QTY: st.column_config.NumberColumn(width=115, help="เปลี่ยนยอดที่ปุ่ม ➕➖ เข้า-ออกสินค้า"),
        C_UNIT: st.column_config.TextColumn(width=70),
        C_MIN: st.column_config.NumberColumn(width=70, help="ต่ำกว่าขั้นต่ำ = Low stock • 0 = Out of stock"),
        C_STATUS: st.column_config.TextColumn(width=110),
        C_EXP: st.column_config.TextColumn(width=175, help="ทุกล็อตที่ยังเหลือ (จำนวน) • ⚠️ = หมดอายุแล้ว "
                                                               "• กดเพื่อแก้วันหมดอายุ"),
    }
    lines = min(max([len(it["lots"]) for it in view] + [1]), MAX_LOT_LINES)   # one lot per line
    row_height = 35 if lines == 1 else 12 + 21 * lines
    height = min(38 + row_height * len(view), 700)
    if st.session_state.pop("stock_edit_off", False):
        st.session_state["stock_edit"] = False
    editing = st.toggle("✏️ แก้ไขตาราง (ชื่อสินค้า / หมวด / หน่วย / ขั้นต่ำ)", key="stock_edit", disabled=dirty,
                        help="บันทึกหรือยกเลิกการแก้ไขก่อนปิด" if dirty else None)

    if not editing:
        # ---- view: click 📜 ดูประวัติ in a row -> that item's history pop-up
        df = pd.DataFrame([{
            C_NAME: it["name"], C_CODE: it["code"], C_CAT: it["category"], C_QTY: it["qty"], C_UNIT: it["unit"],
            C_MIN: it["min"], C_STATUS: STATUS_TH[stock.status(it)], C_EXP: lots_cell(it), C_HIST: "📜 ดูประวัติ",
        } for it in view])
        event = st.dataframe(
            df.style.apply(row_colors, axis=1).format({C_QTY: fmt_num, C_MIN: fmt_num}),
            hide_index=True, width="content", height=height, row_height=row_height,
            column_config={**base_config,
                           C_HIST: st.column_config.TextColumn(width=110, help="กดเพื่อดูประวัติการเข้า-ออก")},
            on_select="rerun", selection_mode="single-cell",
            key=f"stock_table_{st.session_state.get('hist_ver', 0)}_{cat_filter}_{q}",
        )
        cells = event.selection.cells if event else []
        if cells and cells[0][1] in (C_HIST, C_EXP) and 0 <= cells[0][0] < len(view):
            target = "hist_open" if cells[0][1] == C_HIST else "exp_open"
            st.session_state[target] = view[cells[0][0]]["code"]
            st.session_state["hist_ver"] = st.session_state.get("hist_ver", 0) + 1   # clear the selection
            st.rerun()
        return

    # ---- edit: name / หมวด / หน่วย / ขั้นต่ำ
    df = pd.DataFrame([{
        C_NAME: it["name"], C_CODE: it["code"], C_CAT: it["category"], C_QTY: it["qty"], C_UNIT: it["unit"],
        C_MIN: it["min"], C_STATUS: STATUS_TH[stock.status(it)], C_EXP: lots_cell(it),
    } for it in view])
    edited = st.data_editor(
        df.style.apply(row_colors, axis=1).format({C_QTY: fmt_num, C_MIN: fmt_num}),
        hide_index=True,
        width="content",
        height=height,
        row_height=row_height,
        disabled=[C_CODE, C_QTY, C_STATUS, C_EXP],
        column_config={
            **base_config,
            C_NAME: st.column_config.TextColumn(required=True, width=230),
            C_CAT: st.column_config.SelectboxColumn(options=list(CATEGORIES), required=True, width=110,
                                                    help="ย้ายหมวด = ได้รหัสใหม่ (ตัวอักษรหมวด + เลขถัดไป) ตอนบันทึก"),
            C_MIN: st.column_config.NumberColumn(min_value=0, width=70, format="localized",
                                                 help="ต่ำกว่าขั้นต่ำ = Low stock • 0 = Out of stock"),
        },
        key=f"stock_editor_{ver}_{cat_filter}_{q}",
    )
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
                st.session_state["stock_edit_off"] = True
                st.rerun()
        if s2.button("↩️ ยกเลิกการแก้ไข", key="stock_reset"):
            st.session_state["stock_dirty"] = False
            st.session_state["stock_edit_off"] = True
            _bump()
            st.rerun()
        s3.caption(f"⚠️ แก้ไข {len(changes)} รายการ ยังไม่ได้บันทึก")


def history_rows(rows: list[dict], items: dict[str, dict] | None = None, unit: str = "") -> pd.DataFrame:
    """Movements as a table; with `items` (code -> item) the product is shown too (all-items history)."""
    out = []
    for m in rows:
        it = (items or {}).get(m["code"])
        u = it["unit"] if it else m.get("unit", unit)
        row = {"วันที่": fmt_date(m["date"])}
        if items is not None:
            row.update({"รหัส": m["code"], "สินค้า": it["name"] if it else f"{m.get('name', '')} (ลบแล้ว)".lstrip()})
        row.update({
            "เข้า (+)": fmt_num(m["in"]) if m["in"] else "",
            "ออก (−)": fmt_num(m["out"]) if m["out"] else "",
            "ยอดก่อน": fmt_num(m["before"]),
            "นับใหม่": fmt_num(m["count"]) if m["count"] != m["before"] else "",
            "ยอดหลัง": f"{fmt_num(m['after'])} {u}".strip(),
            "วันหมดอายุ": " · ".join(
                ([f"เข้า {fmt_date(m['exp'])}"] if m["exp"] and m["in"] else [fmt_date(m["exp"])] if m["exp"] else [])
                + [f"ออก {fmt_date(t['exp']) if t['exp'] else 'ไม่รู้วันหมดอายุ'} ({fmt_num(t['qty'])})"
                   for t in m.get("out_lots", [])]),
            "บันทึกเมื่อ": m.get("saved_at", ""),
        })
        out.append(row)
    return pd.DataFrame(out)


@st.dialog("📜 ประวัติทั้งหมด", width="large")
def all_history_dialog(data: dict) -> None:
    items = {it["code"]: it for it in data["items"]}
    rows = sorted(enumerate(data["movements"]),
                  key=lambda r: (r[1]["date"], r[1].get("saved_at", ""), r[0]), reverse=True)
    rows = [m for _, m in rows]
    if not rows:
        st.caption(f"ยังไม่มีการเข้า-ออกใน {stock.HISTORY_DAYS} วันที่ผ่านมา")
        return
    f1, f2 = st.columns([3, 1])
    q = f1.text_input("ค้นหา", placeholder="ค้นหาชื่อสินค้าหรือรหัส...", label_visibility="collapsed",
                      key="all_hist_q").strip().lower()
    cat = f2.selectbox("หมวด", ["ทุกหมวด"] + list(CATEGORIES), label_visibility="collapsed", key="all_hist_cat")
    if q or cat != "ทุกหมวด":
        def keep(m: dict) -> bool:
            it = items.get(m["code"], {})
            return ((cat == "ทุกหมวด" or it.get("category") == cat)
                    and (not q or q in m["code"].lower() or q in it.get("name", "").lower()))
        rows = [m for m in rows if keep(m)]
    st.caption(f"{len(rows)} รายการ ย้อนหลัง {stock.HISTORY_DAYS} วัน (ใหม่สุดอยู่บน)")
    if rows:
        st.dataframe(history_rows(rows, items), hide_index=True, width="stretch",
                     height=min(38 + 35 * len(rows), 560))


@st.dialog("📜 ประวัติการเข้า-ออก", width="large")
def history_dialog(data: dict, code: str) -> None:
    item = stock.find(data, code)
    last = stock.last_movement(data).get(code)
    unit = item["unit"]
    s = stock.status(item)
    st.markdown(
        f"**{item['name']}** ({item['code']} · {item['category']}) — ตอนนี้มี "
        f"<span style='color:{COLORS[s][0]};font-weight:600'>{fmt_num(item['qty'])} {unit}</span> "
        f"· <span style='color:{COLORS[s][0]}'>{STATUS_TH[s]}</span>",
        unsafe_allow_html=True,
    )
    rows = stock.history(data, item["code"])
    if not rows:
        st.caption(f"ยังไม่มีการเข้า-ออกใน {stock.HISTORY_DAYS} วันที่ผ่านมา")
    else:
        st.caption(f"{len(rows)} รายการ ย้อนหลัง {stock.HISTORY_DAYS} วัน • ล่าสุด {fmt_date(last)}")
        st.dataframe(history_rows(rows, unit=unit), hide_index=True, width="stretch")

    st.caption("แก้วันหมดอายุ: กดช่อง วันหมดอายุ ของสินค้านี้ในตาราง")


@st.dialog("🗓️ แก้วันหมดอายุ", width="large")
def expiry_dialog(data: dict, code: str) -> None:
    item = stock.find(data, code)
    unit = item["unit"]
    dated = sum(l["qty"] for l in item["lots"])
    st.markdown(f"**{item['name']}** ({item['code']}) — ในสต็อก **{fmt_num(item['qty'])} {unit}**".rstrip())
    st.caption(f"มีวันหมดอายุ {fmt_num(dated)} • ไม่รู้วันหมดอายุ {fmt_num(stock.num(item['qty'] - dated))} — "
               "รวมทุกล็อตต้องไม่เกินยอดในสต็อก • แก้วันที่ / จำนวนในตารางได้เลย • "
               "เพิ่มล็อต: ปุ่ม + ใต้ตาราง • ลบล็อต: ติ๊กหน้าแถวแล้วกดถังขยะ")
    lots_df = pd.DataFrame({"วันหมดอายุ": pd.to_datetime([l["exp"] for l in item["lots"]]).date,
                            "จำนวน": [float(l["qty"]) for l in item["lots"]]})
    ver = st.session_state.get("stock_ver", 0)
    lots = st.data_editor(
        lots_df, num_rows="dynamic", hide_index=True, width="stretch", key=f"lots_{code}_{ver}",
        column_config={
            "วันหมดอายุ": st.column_config.DateColumn(format="DD/MM/YYYY", required=True),
            "จำนวน": st.column_config.NumberColumn(min_value=0, required=True, format="localized"),
        },
    )
    new_lots = [(pd.Timestamp(d).date(), float(q)) for d, q in zip(lots["วันหมดอายุ"], lots["จำนวน"])
                if not pd.isna(d) and not pd.isna(q)]
    current = [(pd.Timestamp(l["exp"]).date(), float(l["qty"])) for l in item["lots"]]
    total = stock.num(sum(q for _, q in new_lots))
    if total > item["qty"]:
        st.error(f"รวมทุกล็อต {fmt_num(total)} เกินยอดในสต็อก {fmt_num(item['qty'])} {unit}")
    if st.button("💾 บันทึกวันหมดอายุ", type="primary", key=f"lots_save_{code}",
                 disabled=new_lots == current or total > item["qty"]):
        if save(lambda d: stock.set_lots(d, code, new_lots),
                f"Stock: แก้วันหมดอายุ {item['name']} ({code})"):
            st.rerun()
