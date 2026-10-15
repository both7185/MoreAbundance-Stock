"""
หน้า ⚖️ เทียบราคา (separate from the PO and stock pages). Reading + matching live in price_compare.py.

Upload the two price lists:
    ไฟล์ราคา จปร              -> รายการ, หน่วย, ราคาพค, ราคาซื้อ
    ไฟล์ราคากลางบ้านราชวิถี   -> รายการ, ราคากลาง/หน่วย, หน่วย
and the same product of both files is shown on one row, with the two files as two column groups.

Names that are the same after ignoring spaces go on one row; names more than 70% similar are paired automatically
(shaded yellow, for the user to check by eye); the rest stay unpaired. Nothing is saved.
"""
from __future__ import annotations

import html

import streamlit as st

import price_compare as pc

SHOW_ALL, SHOW_BOTH, SHOW_AUTO = "ทั้งหมด", "มีทั้ง 2 ไฟล์", "ชื่อไม่ตรงกัน (ตรวจดู)"
SHOW_JPR, SHOW_RW = "มีแค่ไฟล์ จปร", "มีแค่ไฟล์ราชวิถี"


# price_compare._file_mtime (set by app.py) is part of the cache keys below, so results cached by an older
# price_compare.py are not reused after an update (Streamlit Cloud keeps the cache across a git push)
@st.cache_data(show_spinner=False, max_entries=8)
def _read(data: bytes, name: str, key: str, ver: float = 0) -> dict:
    return pc.read_price_file(data, name, pc.JPR if key == "jpr" else pc.RW)


@st.cache_data(show_spinner=False, max_entries=8)
def _match(jpr: list[dict], rw: list[dict], ver: float = 0) -> dict:
    return pc.match(jpr, rw)


def _load(up, spec: dict) -> dict | None:
    if up is None:
        return None
    try:
        with st.spinner(f"กำลังอ่าน{spec['title']}... (PDF สแกนใช้เวลาราว 3 วินาทีต่อหน้า)"):
            got = _read(up.getvalue(), up.name, spec["key"], getattr(pc, "_file_mtime", 0))
    except pc.ReadError as e:
        st.error(f"อ่านไฟล์ {up.name} ไม่ได้: {e}")
        return None
    except Exception as e:
        st.error(f"อ่านไฟล์ {up.name} ไม่ได้: {str(e)[:200]}")
        return None
    how = {"ocr": "อ่านจากภาพสแกน (OCR) — ชื่อสินค้าอาจสะกดผิดเล็กน้อย ถ้ามีไฟล์ Excel ใช้ Excel จะแม่นกว่า",
           "excel": "อ่านจากไฟล์ Excel"}.get(got["method"], "อ่านจากข้อความในไฟล์")
    st.caption(f"✅ {spec['title']}: {len(got['items'])} รายการ • {how}")
    for n in got["notes"]:
        st.warning(n)
    return got


# --------------------------------------------------------------------------- #
# The comparison table
# --------------------------------------------------------------------------- #
_CSS = """
<style>
.cmp-wrap{max-height:70vh;overflow:auto;border:1px solid rgba(128,128,128,.35);border-radius:6px}
.cmp{border-collapse:separate;border-spacing:0;width:100%;min-width:760px;font-size:14px;color:inherit}
.cmp th,.cmp td{padding:4px 8px;border-bottom:1px solid rgba(128,128,128,.22);vertical-align:top}
.cmp thead th{position:sticky;background:HEAD;z-index:1;text-align:center;font-weight:600;white-space:nowrap}
.cmp thead tr:first-child th{top:0}
.cmp thead tr:nth-child(2) th{top:31px;font-weight:500}
.cmp th.g-jpr{background:JPR;color:JPRT}
.cmp th.g-rw{background:RW;color:RWT}
.cmp td.num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
.cmp td.unit{white-space:nowrap;text-align:center}
.cmp td.sep,.cmp th.sep{border-left:3px solid rgba(128,128,128,.45)}
.cmp td.empty{color:rgba(128,128,128,.8);text-align:center}
.cmp td.diff{background:rgba(255,160,0,.22)}
.cmp td.auto{background:rgba(250,215,0,.20)}
</style>
"""
_THEME = {
    "light": {"HEAD": "#f0f2f6", "JPR": "#dbe8fb", "JPRT": "#0d3c78", "RW": "#dcf2e3", "RWT": "#145c2e"},
    "dark": {"HEAD": "#262730", "JPR": "#1d3557", "JPRT": "#d6e4ff", "RW": "#1e4630", "RWT": "#d4f5df"},
}


def _css() -> str:
    try:
        kind = "dark" if st.context.theme.type == "dark" else "light"
    except Exception:
        kind = "light"
    css = _CSS
    for k, v in sorted(_THEME[kind].items(), key=lambda kv: -len(kv[0])):   # JPRT before JPR
        css = css.replace(k, v)
    return css


def _money(v) -> str:
    if v is None:
        return ""
    return f"{v:,.2f}".rstrip("0").rstrip(".") if v != int(v) else f"{v:,.0f}"


def comparison_table(jpr: list[dict], rw: list[dict], result: dict, show: str, query: str) -> int:
    from products import norm
    q = norm(query)
    jcols = [("รายการ", "name"), ("หน่วย", "unit")] + [(lab, lab) for lab, _ in pc.JPR["prices"]]
    rcols = [("รายการ", "name")] + [(lab, lab) for lab, _ in pc.RW["prices"]] + [("หน่วย", "unit")]

    def cells(it, cols, first_sep: bool, unit_diff: bool, auto: bool) -> str:
        out = []
        for n, (lab, key) in enumerate(cols):
            sep = " sep" if first_sep and n == 0 else ""
            if it is None:
                out.append(f'<td class="empty{sep}">{"—" if n == 0 else ""}</td>')
            elif key == "name":
                out.append(f'<td class="name{sep}{" auto" if auto else ""}">{html.escape(it["name"])}</td>')
            elif key == "unit":
                out.append(f'<td class="unit{sep}{" diff" if unit_diff else ""}">{html.escape(it["unit"])}</td>')
            else:
                out.append(f'<td class="num{sep}">{_money(it["prices"].get(key))}</td>')
        return "".join(out)

    body, shown = [], 0
    for i, r, how, _score in result["rows"]:
        a = jpr[i] if i is not None else None
        b = rw[r] if r is not None else None
        kind = SHOW_BOTH if a and b else (SHOW_JPR if a else SHOW_RW)
        auto = how == "similar"
        if show not in (SHOW_ALL, kind) and not (show == SHOW_AUTO and auto):
            continue
        if q and not any(q in norm(x["name"]) for x in (a, b) if x):
            continue
        unit_diff = bool(a and b and norm(a["unit"]).rstrip(".") != norm(b["unit"]).rstrip("."))
        body.append(f"<tr>{cells(a, jcols, False, unit_diff, auto)}{cells(b, rcols, True, unit_diff, auto)}</tr>")
        shown += 1
    head1 = (f'<tr><th class="g-jpr" colspan="{len(jcols)}">1. ไฟล์ราคา จปร</th>'
             f'<th class="g-rw sep" colspan="{len(rcols)}">2. ไฟล์ราคากลางบ้านราชวิถี</th></tr>')
    head2 = "<tr>" + "".join(f"<th>{html.escape(l)}</th>" for l, _ in jcols) + \
            "".join(f'<th class="{"sep" if n == 0 else ""}">{html.escape(l)}</th>' for n, (l, _) in enumerate(rcols)) + "</tr>"
    if not body:
        body = [f'<tr><td class="empty" colspan="{len(jcols) + len(rcols)}">ไม่มีรายการ</td></tr>']
    st.html(_css() + f'<div class="cmp-wrap"><table class="cmp"><thead>{head1}{head2}</thead>'
                   f'<tbody>{"".join(body)}</tbody></table></div>')
    return shown


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #
def render(show_flash=None) -> None:
    st.title("⚖️ เทียบราคา จปร กับ ราคากลางบ้านราชวิถี")
    if show_flash:
        show_flash()
    st.caption("อัปโหลด 2 ไฟล์ → สินค้าตัวเดียวกันจะเรียงอยู่แถวเดียวกัน (ซ้าย = ไฟล์ราคา จปร, ขวา = ไฟล์ราคากลางบ้านราชวิถี) • "
               "ชื่อที่คล้ายกันเกิน 70% จับคู่ให้อัตโนมัติ (ชื่อพื้นสีเหลือง ให้ตรวจดูอีกที)")
    c1, c2 = st.columns(2)
    up_j = c1.file_uploader("1. ไฟล์ราคา จปร (PDF / Excel)", type=["pdf", "xlsx"], key="cmp_jpr",
                            help="ใช้คอลัมน์ รายการ, หน่วย, ราคาพค, ราคาซื้อ")
    up_r = c2.file_uploader("2. ไฟล์ราคากลางบ้านราชวิถี (PDF / Excel)", type=["pdf", "xlsx"], key="cmp_rw",
                            help="ใช้คอลัมน์ รายการ, ราคากลาง/หน่วย, หน่วย • PDF สแกนก็ได้ (อ่านด้วย OCR) แต่ Excel แม่นกว่า")
    with c1:
        got_j = _load(up_j, pc.JPR)
    with c2:
        got_r = _load(up_r, pc.RW)
    if not got_j or not got_r:
        st.info("เลือกไฟล์ให้ครบทั้ง 2 ไฟล์")
        return
    jpr, rw = got_j["items"], got_r["items"]
    with st.spinner("กำลังจับคู่ชื่อสินค้า..."):
        result = _match(jpr, rw, getattr(pc, "_file_mtime", 0))

    both = sum(1 for i, r, _, _ in result["rows"] if i is not None and r is not None)
    auto = sum(1 for _, _, how, _ in result["rows"] if how == "similar")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("จับคู่แล้ว (อยู่แถวเดียวกัน)", both)
    m2.metric("ในนั้นชื่อไม่ตรงกัน (ตรวจดู)", auto)
    m3.metric("มีแค่ไฟล์ จปร", len(jpr) - both)
    m4.metric("มีแค่ไฟล์ราชวิถี", len(rw) - both)

    st.subheader("ตารางเทียบราคา")
    f1, f2 = st.columns([2, 3])
    show = f1.radio("แสดง", [SHOW_ALL, SHOW_BOTH, SHOW_AUTO, SHOW_JPR, SHOW_RW], horizontal=True, key="cmp_show",
                    label_visibility="collapsed")
    query = f2.text_input("ค้นหา", placeholder="ค้นหาชื่อสินค้า...", key="cmp_query", label_visibility="collapsed")
    shown = comparison_table(jpr, rw, result, show, query)
    st.caption(f"แสดง {shown} แถว • เรียงตามลำดับในไฟล์ราชวิถี แล้วต่อด้วยสินค้าที่มีแค่ในไฟล์ จปร • "
               "ชื่อพื้นสีเหลือง = ชื่อไม่ตรงกันแต่คล้ายกันเกิน 70% จึงจับคู่ให้อัตโนมัติ (ตรวจดูว่าเป็นสินค้าเดียวกันไหม) • "
               "ช่องหน่วยสีส้ม = หน่วยของ 2 ไฟล์ไม่เหมือนกัน (เทียบราคาตรงๆ ไม่ได้)")
