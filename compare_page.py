"""
หน้า ⚖️ เทียบราคา (separate from the PO and stock pages). Reading + matching live in price_compare.py.

Upload the two price lists:
    ไฟล์ราคา จปร              -> รายการ, หน่วย, ราคาพค, ราคาซื้อ
    ไฟล์ราคากลางบ้านราชวิถี   -> รายการ, ราคากลาง/หน่วย, หน่วย
and the same product of both files is shown on one row, with the two files as two column groups.

Names that are the same after ignoring spaces go on one row by themselves. Names that are only similar are
listed in "ยืนยันชื่อที่สะกดต่างกัน": the closest จปร name is filled in, the user ticks ✔ (or picks another
name) and saves. Confirmed pairs are kept in price_matches.json (its own file in Supabase / the data
branch, see storage.py), so the next upload of the same lists lines up without asking again.
"""
from __future__ import annotations

import html

import pandas as pd
import streamlit as st

import price_compare as pc
import storage

C_RW, C_UNIT, C_PICK, C_SCORE, C_OK = "ชื่อในไฟล์ราชวิถี", "หน่วย (ราชวิถี)", "ชื่อในไฟล์ จปร", "ใกล้เคียง", "✔ ยืนยัน"
SHOW_ALL, SHOW_BOTH, SHOW_JPR, SHOW_RW = "ทั้งหมด", "มีทั้ง 2 ไฟล์", "มีแค่ไฟล์ จปร", "มีแค่ไฟล์ราชวิถี"


def _flash(kind: str, text: str) -> None:
    """Shown by app.show_flash() after st.rerun()."""
    st.session_state.setdefault("_flash", []).append((kind, text))


@st.cache_data(show_spinner=False, max_entries=8)
def _read(data: bytes, key: str) -> dict:
    return pc.read_price_pdf(data, pc.JPR if key == "jpr" else pc.RW)


@st.cache_data(show_spinner=False, max_entries=8)
def _match(jpr: list[dict], rw: list[dict], pairs: list[dict]) -> dict:
    return pc.match(jpr, rw, pairs)


def _load(up, spec: dict) -> dict | None:
    if up is None:
        return None
    try:
        with st.spinner(f"กำลังอ่าน{spec['title']}... (ไฟล์สแกนใช้เวลาราว 3 วินาทีต่อหน้า)"):
            got = _read(up.getvalue(), spec["key"])
    except pc.ReadError as e:
        st.error(f"อ่านไฟล์ {up.name} ไม่ได้: {e}")
        return None
    except Exception as e:
        st.error(f"อ่านไฟล์ {up.name} ไม่ได้: {str(e)[:200]}")
        return None
    how = "อ่านจากภาพสแกน (OCR) — ชื่อสินค้าอาจสะกดผิดเล็กน้อย" if got["method"] == "ocr" else "อ่านจากข้อความในไฟล์"
    st.caption(f"✅ {spec['title']}: {len(got['items'])} รายการ • {how}")
    for n in got["notes"]:
        st.warning(n)
    return got


def current_pairs() -> list[dict]:
    return pc.load_pairs()


def save_pairs(add: list[dict], remove: list[dict], message: str) -> bool:
    """Merge into the latest price_matches.json and save it (local file + Supabase / GitHub when set)."""
    cfg = storage.config(st.secrets)
    current = None
    if cfg:
        try:
            latest = storage.fetch(cfg, pc.MATCHES_FILE)
            current = pc.load_pairs(latest) if latest is not None else None
        except Exception as e:
            st.error(f"ดึงข้อมูลคู่สินค้าล่าสุดไม่ได้ — ยังไม่ได้บันทึก ลองใหม่อีกครั้ง ({str(e)[:150]})")
            return False
    if current is None:
        current = pc.load_pairs()
    text = pc.pairs_json(pc.merge_pairs(current, add, remove))
    pc.MATCHES_PATH.write_text(text, encoding="utf-8")
    if cfg:
        try:
            warning = storage.push(cfg, pc.MATCHES_FILE, text, message)
        except Exception as e:
            _flash("error", f"บันทึกไว้ชั่วคราวแล้ว แต่ส่งขึ้น {storage.where(cfg)} ไม่สำเร็จ — กดบันทึกอีกครั้ง "
                            f"ไม่อย่างนั้นจะหายเมื่อแอปรีสตาร์ท ({str(e)[:150]})")
            return True
        if warning:
            _flash("warning", warning)
    return True


# --------------------------------------------------------------------------- #
# Confirm similar names
# --------------------------------------------------------------------------- #
def _labels(jpr: list[dict]) -> list[str]:
    """One distinct label per จปร item (a name used twice gets its ลำดับ)."""
    count: dict[str, int] = {}
    for it in jpr:
        count[it["name"]] = count.get(it["name"], 0) + 1
    out, seen = [], set()
    for i, it in enumerate(jpr):
        lab = it["name"] if count[it["name"]] == 1 else f"{it['name']} (ลำดับ {it['no'] or i + 1})"
        while lab in seen:
            lab += " "
        seen.add(lab)
        out.append(lab)
    return out


def confirm_section(jpr: list[dict], rw: list[dict], result: dict) -> None:
    suggest = result["suggest"]
    if not suggest:
        return
    labels = _labels(jpr)
    used = {i for i, r, how in result["rows"] if i is not None and r is not None}
    free = [labels[i] for i in range(len(jpr)) if i not in used]
    index_of = {lab: i for i, lab in enumerate(labels)}
    ticked = pc.prefill(suggest)
    with_hint = sum(1 for s in suggest.values() if s)
    title = f"🔗 ยืนยันชื่อที่สะกดต่างกัน — ราชวิถี {len(suggest)} รายการยังไม่มีคู่ ({with_hint} รายการมีชื่อใกล้เคียงใน จปร)"
    with st.expander(title, expanded=True):
        st.caption(
            "ระบบเติม **ชื่อในไฟล์ จปร ที่ใกล้เคียงที่สุด** ให้ ถ้าเป็นสินค้าตัวเดียวกันให้ติ๊ก **✔ ยืนยัน** "
            "(ถ้าไม่ใช่ เลือกชื่ออื่นจากช่องนั้นได้ หรือปล่อยว่าง) แล้วกด 💾 บันทึกการจับคู่  \n"
            "แถวที่ชื่อใกล้เคียงมาก (≥ 93%) ติ๊กไว้ให้แล้ว — ตรวจก่อนบันทึก • "
            "คู่ที่บันทึกแล้วจะจำไว้ใช้ครั้งต่อไป"
        )
        order = sorted(suggest, key=lambda r: -(suggest[r][0][1] if suggest[r] else 0))
        recs = []
        for r in order:
            best = suggest[r][0] if suggest[r] else None
            recs.append({
                C_RW: rw[r]["name"],
                C_UNIT: rw[r]["unit"],
                C_PICK: labels[best[0]] if best else "",
                C_SCORE: f"{best[1]:.0%}" if best else "",
                C_OK: r in ticked,
            })
        edited = st.data_editor(
            pd.DataFrame(recs), hide_index=True, width="stretch",
            disabled=[C_RW, C_UNIT, C_SCORE],
            column_config={
                C_RW: st.column_config.TextColumn(width="large"),
                C_PICK: st.column_config.SelectboxColumn(options=[""] + free, width="large",
                                                         help="ชื่อสินค้าตัวเดียวกันในไฟล์ จปร"),
                C_SCORE: st.column_config.TextColumn(help="ความคล้ายของชื่อ (ของชื่อที่ระบบเติมให้)", width="small"),
                C_OK: st.column_config.CheckboxColumn(width="small"),
            },
            key="cmp_confirm_" + str(hash((tuple(rw[r]["name"] for r in order), len(jpr)))),
        )
        add, problems, taken = [], [], {}
        for k, rec in edited.iterrows():
            pick = rec[C_PICK] if isinstance(rec[C_PICK], str) else ""
            if not rec[C_OK]:
                continue
            if not pick:
                problems.append(f"'{rec[C_RW]}': ติ๊กยืนยันแล้วแต่ยังไม่ได้เลือกชื่อในไฟล์ จปร")
                continue
            if pick in taken:
                problems.append(f"'{pick}' ถูกจับคู่กับ 2 รายการ: '{taken[pick]}' และ '{rec[C_RW]}'")
                continue
            taken[pick] = rec[C_RW]
            add.append({"jpr": jpr[index_of[pick]]["name"], "rw": rec[C_RW]})
        for p in problems:
            st.error(p)
        if st.button(f"💾 บันทึกการจับคู่ ({len(add)} คู่)", type="primary", disabled=not add or bool(problems),
                     key="cmp_save"):
            if save_pairs(add, [], f"เทียบราคา: ยืนยันคู่สินค้า {len(add)} คู่"):
                _flash("success", f"บันทึกการจับคู่ {len(add)} คู่แล้ว — สินค้าที่ยืนยันอยู่แถวเดียวกันในตารางด้านล่าง")
                st.rerun()


def saved_pairs_section(jpr: list[dict], rw: list[dict], result: dict) -> None:
    rows = [(jpr[i]["name"], rw[r]["name"]) for i, r, how in result["rows"] if how == "saved"]
    if not rows:
        return
    with st.expander(f"📌 คู่ที่ยืนยันไว้แล้ว ({len(rows)} คู่) — ยกเลิกคู่ที่จับผิดได้ที่นี่"):
        df = pd.DataFrame([{"ชื่อในไฟล์ จปร": a, "ชื่อในไฟล์ราชวิถี": b, "ยกเลิก": False} for a, b in rows])
        edited = st.data_editor(df, hide_index=True, width="stretch", disabled=["ชื่อในไฟล์ จปร", "ชื่อในไฟล์ราชวิถี"],
                                column_config={"ยกเลิก": st.column_config.CheckboxColumn(width="small")},
                                key="cmp_saved_" + str(hash(tuple(rows))))
        remove = [{"jpr": rec["ชื่อในไฟล์ จปร"], "rw": rec["ชื่อในไฟล์ราชวิถี"]}
                  for _, rec in edited.iterrows() if rec["ยกเลิก"]]
        if st.button(f"ยกเลิกคู่ที่เลือก ({len(remove)} คู่)", disabled=not remove, key="cmp_unpair"):
            if save_pairs([], remove, f"เทียบราคา: ยกเลิกคู่สินค้า {len(remove)} คู่"):
                _flash("success", f"ยกเลิก {len(remove)} คู่แล้ว")
                st.rerun()


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

    def cells(it, cols, first_sep: bool, unit_diff: bool) -> str:
        out = []
        for n, (lab, key) in enumerate(cols):
            sep = " sep" if first_sep and n == 0 else ""
            if it is None:
                out.append(f'<td class="empty{sep}">{"—" if n == 0 else ""}</td>')
            elif key == "name":
                out.append(f'<td class="name{sep}">{html.escape(it["name"])}</td>')
            elif key == "unit":
                out.append(f'<td class="unit{sep}{" diff" if unit_diff else ""}">{html.escape(it["unit"])}</td>')
            else:
                out.append(f'<td class="num{sep}">{_money(it["prices"].get(key))}</td>')
        return "".join(out)

    body, shown = [], 0
    for i, r, how in result["rows"]:
        a = jpr[i] if i is not None else None
        b = rw[r] if r is not None else None
        kind = SHOW_BOTH if a and b else (SHOW_JPR if a else SHOW_RW)
        if show != SHOW_ALL and show != kind:
            continue
        if q and not any(q in norm(x["name"]) for x in (a, b) if x):
            continue
        unit_diff = bool(a and b and norm(a["unit"]).rstrip(".") != norm(b["unit"]).rstrip("."))
        body.append(f"<tr>{cells(a, jcols, False, unit_diff)}{cells(b, rcols, True, unit_diff)}</tr>")
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
               "ชื่อที่สะกดต่างกันนิดหน่อย ให้กดยืนยันก่อนจึงจะจับคู่")
    c1, c2 = st.columns(2)
    up_j = c1.file_uploader("1. ไฟล์ราคา จปร (PDF)", type=["pdf"], key="cmp_jpr",
                            help="ใช้คอลัมน์ รายการ, หน่วย, ราคาพค, ราคาซื้อ")
    up_r = c2.file_uploader("2. ไฟล์ราคากลางบ้านราชวิถี (PDF)", type=["pdf"], key="cmp_rw",
                            help="ใช้คอลัมน์ รายการ, ราคากลาง/หน่วย, หน่วย • ไฟล์สแกนก็ได้ (อ่านด้วย OCR)")
    with c1:
        got_j = _load(up_j, pc.JPR)
    with c2:
        got_r = _load(up_r, pc.RW)
    if not got_j or not got_r:
        st.info("เลือกไฟล์ให้ครบทั้ง 2 ไฟล์")
        return
    jpr, rw = got_j["items"], got_r["items"]
    with st.spinner("กำลังจับคู่ชื่อสินค้า..."):
        result = _match(jpr, rw, current_pairs())

    both = sum(1 for i, r, _ in result["rows"] if i is not None and r is not None)
    m1, m2, m3 = st.columns(3)
    m1.metric("จับคู่แล้ว (อยู่แถวเดียวกัน)", both)
    m2.metric("มีแค่ไฟล์ จปร", len(jpr) - both)
    m3.metric("มีแค่ไฟล์ราชวิถี", len(rw) - both)

    confirm_section(jpr, rw, result)
    saved_pairs_section(jpr, rw, result)

    st.subheader("ตารางเทียบราคา")
    f1, f2 = st.columns([2, 3])
    show = f1.radio("แสดง", [SHOW_ALL, SHOW_BOTH, SHOW_JPR, SHOW_RW], horizontal=True, key="cmp_show",
                    label_visibility="collapsed")
    query = f2.text_input("ค้นหา", placeholder="ค้นหาชื่อสินค้า...", key="cmp_query", label_visibility="collapsed")
    shown = comparison_table(jpr, rw, result, show, query)
    st.caption(f"แสดง {shown} แถว • เรียงตามลำดับในไฟล์ จปร แล้วต่อด้วยสินค้าที่มีแค่ในไฟล์ราชวิถี • "
               "ช่องหน่วยสีส้ม = หน่วยของ 2 ไฟล์ไม่เหมือนกัน (เทียบราคาตรงๆ ไม่ได้)")
