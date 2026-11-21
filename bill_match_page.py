"""
หน้า 🔗 จับคู่บิล (separate from the other pages). Logic lives in bill_match.py; nothing is saved.

1. One upload box for every picture: transfer slips (Electronic bill) and handwritten bills, pictures or .zip.
   A second, optional box takes order files (PDF, e.g. 16-11.pdf): one output PDF per file, receipts in its order.
2. Each picture is read once (OCR): slip or bill, the slip amount, the numbers on the bill. The bill total is the
   number that equals a slip amount. Kinds and amounts can be corrected.
3. ① รอยืนยัน: suggested pairs (same amount), ✅ confirm or ✖ reject; a shared amount shows its slips to pick from.
   ② ยังไม่มีคู่: bills/slips without a pair; fix the amount, pick any slip / bill by hand (also one that was
      rejected or is already paired: it moves), or switch the kind.
   ③ จับคู่แล้ว: confirmed pairs; the slip can be changed (to any slip) or the pair undone.
4. 📥 PDF (A4 portrait): pairs show the bill top-left and the slip bottom-right, as large as fits without covering.
   Without order files: one PDF, unpaired slips first, then the pairs. With order files: one PDF per file, in the
   file's line order; an amount found in several files is put where the user chooses; file names are editable.

Every picture can be clicked to see it large (a CSS-only lightbox; the pictures are served as Streamlit media files).
"""
from __future__ import annotations

import hashlib
import io
import re
import zipfile

import pandas as pd
import streamlit as st
from PIL import Image

import bill_match as bm

UP_TYPES = ["jpg", "jpeg", "png", "webp", "zip"]
LETTERS = "กขคงจฉชซฌญฎฏฐฑฒณดตถทธนบปผฝพฟภม"
NONE = ""            # file choice: in no order file

_CSS = """<style>
.bmlb{position:relative}
.bmlb input{display:none}
.bmlb .th{display:flex;align-items:center;justify-content:center;cursor:zoom-in;border-radius:6px;overflow:hidden;
  background:rgba(128,128,128,.10);margin:0;width:100%;aspect-ratio:2/3}
.bmlb .th img{width:100%;height:100%;object-fit:contain}
.bmlb .ov{display:none;position:fixed;inset:0;background:rgba(0,0,0,.88);z-index:1000000;cursor:zoom-out;
  align-items:center;justify-content:center;margin:0}
.bmlb input:checked ~ .ov{display:flex}
.bmlb .ov img{max-width:96vw;max-height:94vh;object-fit:contain;box-shadow:0 0 24px rgba(0,0,0,.6)}
.bmlb .ov span{position:absolute;top:10px;right:18px;color:#fff;font-size:28px;line-height:1}
.bmlb .tag{position:absolute;left:4px;top:4px;background:rgba(0,0,0,.6);color:#fff;font-size:12px;padding:1px 6px;
  border-radius:4px;pointer-events:none}
</style>"""


# --------------------------------------------------------------------------- #
# Pictures
# --------------------------------------------------------------------------- #
def _state(name: str, default):
    if name not in st.session_state:
        st.session_state[name] = default
    return st.session_state[name]


def _jpeg(im: Image.Image, longest: int, quality: int) -> bytes:
    im = im.copy()
    im.thumbnail((longest, longest), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def _media_url(data: bytes, key: str) -> str:
    """Serve bytes as a Streamlit media file (cached by the browser, kept while the page shows it)."""
    from streamlit.runtime import Runtime
    url = Runtime.instance().media_file_mgr.add(data, "image/jpeg", f"bm-{key}")
    return url.lstrip("/")      # relative: also right when the app runs under a sub-path (Streamlit Cloud)


def _pictures(img: dict) -> tuple[bytes, bytes]:
    cache = _state("bm_pics", {})
    if img["id"] not in cache:
        im = bm.open_image(img["data"])
        cache[img["id"]] = (_jpeg(im, 760, 78), _jpeg(im, 1700, 85))
    return cache[img["id"]]


def pic(img: dict, where: str, tag: str = "") -> None:
    """The picture at the column's full width (a 2:3 card); a click opens it large (click again to close)."""
    small, big = _pictures(img)
    s_url = _media_url(small, f"{img['id']}-s")
    b_url = _media_url(big, f"{img['id']}-b")
    n = st.session_state["bm_pic_n"] = st.session_state.get("bm_pic_n", 0) + 1
    cid = f"bm-{where}-{img['id']}-{n}"
    label = f'<span class="tag">{tag}</span>' if tag else ""
    st.html(f'<div class="bmlb"><input type="checkbox" id="{cid}">'
            f'<label class="th" for="{cid}">{label}<img src="{s_url}" alt=""></label>'
            f'<label class="ov" for="{cid}"><span>✕</span><img loading="lazy" src="{b_url}" alt=""></label></div>')


def _collect(ups) -> list[dict]:
    files = [(u.name, u.getvalue()) for u in ups or []]
    sig = tuple((n, len(d)) for n, d in files)
    cache = _state("bm_set", {})
    if cache.get("sig") != sig:
        images, notes = bm.collect_images(files)
        cache.update(sig=sig, images=images, notes=notes)
    for n in cache["notes"]:
        st.warning(n)
    return cache["images"]


def _orders(ups) -> list[dict]:
    """Order files: [{'key', 'name' (file name without .pdf), 'data', 'lines', 'total', 'note'}], in upload order."""
    cache = _state("bm_order_set", {})
    out, seen = [], set()
    for u in ups or []:
        data = u.getvalue()
        key = hashlib.sha1(data).hexdigest()[:12]
        if key in seen:
            continue
        seen.add(key)
        if key not in cache:
            cache[key] = bm.parse_order_pdf(data)
        out.append({"key": key, "name": re.sub(r"\.pdf$", "", u.name, flags=re.I), "data": data, **cache[key]})
    return out


def _analyze(images: list[dict]) -> dict[str, dict]:
    done = _state("bm_read", {})
    todo = [i for i in images if i["id"] not in done]
    if todo:
        how = bm.reader_name()
        if not how:
            st.warning("เครื่องที่รันแอปไม่มีตัวอ่านตัวเลขจากรูป (OCR) — ทุกรูปจะเป็นบิลเขียนมือ "
                       "ให้สลับประเภทและพิมพ์ยอดเองในข้อ ②")
        bar = st.progress(0.0, text=f"กำลังอ่านรูป 0/{len(todo)}... (ครั้งแรกใช้เวลาราว 1-2 วินาทีต่อรูป)")

        def step(n, total):
            bar.progress(n / total, text=f"กำลังอ่านรูป {n}/{total}...")

        for img, r in zip(todo, bm.analyze_all([i["data"] for i in todo], step)):
            done[img["id"]] = r
        bar.empty()
    return {i["id"]: done[i["id"]] for i in images}


def _amount(i: str) -> tuple[float | None, bool]:
    """(value, ok) of a picture's amount text (kept in bm_amt: widget keys vanish while their box is hidden)."""
    try:
        return bm.parse_price(st.session_state.bm_amt.get(i)), True
    except ValueError:
        return None, False


def _save_amount(i: str) -> None:
    st.session_state.bm_amt[i] = st.session_state.get(f"bm_in_{i}", "")


def amount_box(i: str, label: str, **kw) -> None:
    st.session_state[f"bm_in_{i}"] = st.session_state.bm_amt.get(i, "")
    st.text_input(label, key=f"bm_in_{i}", on_change=_save_amount, args=(i,), **kw)


def _grid(items: list, draw, cols: int) -> None:
    for start in range(0, len(items), cols):
        row = st.columns(cols)
        for col, item in zip(row, items[start:start + cols]):
            with col:
                draw(item)


def _file_name(name: str, fallback: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', " ", (name or "").strip()).strip() or fallback
    return name if name.lower().endswith(".pdf") else name + ".pdf"


# --------------------------------------------------------------------------- #
# Actions (button callbacks: they run before the page is drawn again)
# --------------------------------------------------------------------------- #
def _confirm(pairs: list[tuple[str, str]]) -> None:
    confirmed = st.session_state.bm_confirmed
    for b, s in pairs:
        if s not in confirmed.values() and b not in confirmed:
            confirmed[b] = s


def _pair(b: str, s: str) -> None:
    """Pair by hand, whatever happened before: the slip leaves the bill it was paired with (if any), and an earlier
    ✖ ไม่ใช่ between the two is forgotten."""
    confirmed = st.session_state.bm_confirmed
    for other in [k for k, v in confirmed.items() if v == s and k != b]:
        confirmed.pop(other)
    confirmed[b] = s
    st.session_state.bm_rejected.discard((b, s))


def _reject(b: str, s: str) -> None:
    st.session_state.bm_rejected.add((b, s))


def _clear_rejects() -> None:
    st.session_state.bm_rejected.clear()


def _undo(b: str) -> None:
    st.session_state.bm_confirmed.pop(b, None)


def _change_slip(b: str) -> None:
    s = st.session_state.get(f"bm_chg_{b}")
    if s and st.session_state.bm_confirmed.get(b) != s:
        _pair(b, s)


def _switch_kind(i: str, kind: str) -> None:
    st.session_state.bm_kind[i] = kind
    st.session_state.bm_confirmed = {b: s for b, s in st.session_state.bm_confirmed.items() if i not in (b, s)}


def _save_choice(uid: str) -> None:
    v = st.session_state.get(f"bm_fc_{uid}")
    if v is None:
        st.session_state.bm_file_choice.pop(uid, None)
    else:
        st.session_state.bm_file_choice[uid] = v


def _save_name(key: str) -> None:
    st.session_state.bm_names[key] = st.session_state.get(f"bm_nm_{key}", "")


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #
def render(show_flash=None) -> None:
    st.title("🔗 จับคู่บิลโอนกับบิลเขียนมือ")
    if show_flash:
        show_flash()
    st.caption("อัปโหลดรูปทั้งหมดในช่องเดียว → แอปแยกบิลโอนกับบิลเขียนมือ อ่านยอดเงิน แล้วจับคู่ให้ → ตรวจแล้วกดยืนยัน "
               "→ ดาวน์โหลด PDF (A4) • กดที่รูปเพื่อดูรูปใหญ่ • ไม่มีการบันทึกข้อมูลใดๆ")
    u1, u2 = st.columns([3, 2])
    ups = u1.file_uploader("รูปบิลทั้งหมด (บิลโอน + บิลเขียนมือ ปนกันได้) — รูป หรือ .zip", type=UP_TYPES,
                           accept_multiple_files=True, key="bm_up",
                           help="เลือกได้หลายไฟล์ ไฟล์ .zip จะใช้ทุกรูปที่อยู่ข้างใน")
    order_ups = u2.file_uploader("ไฟล์ลำดับ (PDF) — ไม่บังคับ", type=["pdf"], accept_multiple_files=True,
                                 key="bm_order",
                                 help="เช่น 16-11.pdf, 16-11.1.pdf: แต่ละไฟล์จะได้ PDF ของตัวเอง 1 ไฟล์ "
                                      "เรียงใบเสร็จตามรายการในไฟล์ (จับจากยอดเงิน)")
    images = _collect(ups)
    orders = _orders(order_ups)
    for o in orders:
        if o["note"]:
            st.warning(f"{o['name']}.pdf: {o['note']}")
    if not images:
        st.info("อัปโหลดรูปบิลโอนกับรูปบิลเขียนมือ (ทั้งหมดรวมกันในช่องเดียวได้เลย) • "
                "ถ้าต้องการเรียงตามไฟล์ เช่น 16-11.pdf ให้อัป PDF นั้นในช่องขวา")
        return
    st.html(_CSS)
    st.session_state["bm_pic_n"] = 0
    read = _analyze(images)
    by_id = {i["id"]: i for i in images}
    order = {i["id"]: n for n, i in enumerate(images, 1)}
    kind_over = _state("bm_kind", {})
    kind = {i: kind_over.get(i, read[i]["kind"]) for i in by_id}
    slips = [i for i in by_id if kind[i] == "slip"]
    bills = [i for i in by_id if kind[i] == "bill"]

    # amounts: slips from the picture; bills = the number on the bill that equals a slip amount.
    # Kept per picture and kind ("<id>" for a slip, "<id>-b" for a bill) so switching the kind starts fresh.
    amt = _state("bm_amt", {})
    for s in slips:
        if s not in amt:
            amt[s] = bm.money(read[s]["amount"]) if read[s]["amount"] is not None else ""
    slip_amt = {s: _amount(s)[0] for s in slips}
    guessed = _state("bm_guess", {})
    for b in bills:
        if f"{b}-b" not in amt:
            g, how = bm.guess_bill_amount(read[b]["numbers"], list(slip_amt.values()))
            guessed[b] = how
            amt[f"{b}-b"] = bm.money(g) if g is not None else ""
    bill_amt = {b: _amount(f"{b}-b")[0] for b in bills}

    confirmed = _state("bm_confirmed", {})
    rejected = _state("bm_rejected", set())
    for b, s in list(confirmed.items()):
        if b not in bill_amt or s not in slip_amt:
            confirmed.pop(b)
    sug = bm.suggest(slip_amt, bill_amt, confirmed, rejected)
    bill_of = {s: b for b, s in confirmed.items()}

    def slip_label(s: str, mine: str | None = None) -> str:
        a = slip_amt.get(s)
        text = f"{bm.money(a) if a is not None else '?'} บาท (รูปที่ {order[s]})"
        if s in bill_of and bill_of[s] != mine:
            text += " • จับคู่อยู่แล้ว (จะย้ายมา)"
        return text

    def bill_label(b: str) -> str:
        a = bill_amt.get(b)
        text = f"{bm.money(a) if a is not None else '?'} บาท (รูปที่ {order[b]})"
        return text + (" • จับคู่อยู่แล้ว (จะย้ายมา)" if b in confirmed else "")

    m = st.columns(5)
    m[0].metric("บิลโอน", len(slips))
    m[1].metric("บิลเขียนมือ", len(bills))
    m[2].metric("รอยืนยัน", len(sug["pairs"]) + len(sug["choices"]))
    m[3].metric("ยังไม่มีคู่", len(sug["free_bills"]) + len(sug["free_slips"]))
    m[4].metric("จับคู่แล้ว", len(confirmed))

    def amt_line(b: str, s: str) -> str:
        warn = " • ⚠️ ยอดบิลอ่านแบบเดา" if guessed.get(b) == "near" else ""
        return f"ยอด **{bm.money(slip_amt[s])}** • รูปที่ {order[b]} + {order[s]}{warn}"

    # ---------------- 1. suggested pairs ---------------- #
    st.subheader(f"① รอยืนยัน ({len(sug['pairs']) + len(sug['choices'])})")
    if rejected:
        st.button(f"↩️ ยกเลิก ✖ ไม่ใช่ ที่กดไปทั้งหมด ({len(rejected)})", key="bm_unrej", on_click=_clear_rejects,
                  type="tertiary", help="คู่ที่เคยกด ✖ จะกลับมาให้ยืนยันอีกครั้ง (เลือกจับคู่เองในข้อ ② ได้เสมออยู่แล้ว)")
    if not sug["pairs"] and not sug["choices"]:
        st.caption("ไม่มีคู่ที่รอยืนยัน")
    if sug["pairs"]:
        st.caption("บิลเขียนมือ (ซ้าย) กับบิลโอน (ขวา) ที่ยอดตรงกัน • ✅ ยืนยัน = ย้ายไปจับคู่แล้ว • "
                   "✖ ไม่ใช่ = ไม่เสนอคู่นี้อีก (ยังเลือกจับคู่เองในข้อ ② ได้)")
        st.button(f"✅ ยืนยันทั้งหมด ({len(sug['pairs'])} คู่)", type="primary", on_click=_confirm,
                  args=(sug["pairs"],), key="bm_all")

        def draw_pair(p):
            b, s = p
            with st.container(border=True):
                c1, c2 = st.columns(2, gap="small")
                with c1:
                    pic(by_id[b], "sp", "เขียนมือ")
                with c2:
                    pic(by_id[s], "sp", "บิลโอน")
                st.markdown(amt_line(b, s))
                k1, k2 = st.columns(2, gap="small")
                k1.button("✅ ยืนยัน", key=f"bm_ok_{b}_{s}", on_click=_confirm, args=([(b, s)],), width="stretch")
                k2.button("✖ ไม่ใช่", key=f"bm_no_{b}_{s}", on_click=_reject, args=(b, s), width="stretch")
        _grid(sug["pairs"], draw_pair, 2)

    if sug["choices"]:
        st.markdown("**ยอดซ้ำ — เลือกบิลโอนที่ถูก**")

        def draw_choice(c):
            b, ss = c
            with st.container(border=True):
                cols = st.columns(max(4, 1 + len(ss)), gap="small")
                with cols[0]:
                    pic(by_id[b], "ch", "เขียนมือ")
                    st.caption(f"ยอด {bm.money(bill_amt[b])} • รูปที่ {order[b]}")
                for n, (col, s) in enumerate(zip(cols[1:], ss)):
                    with col:
                        pic(by_id[s], "ch", f"บิลโอน {LETTERS[n % len(LETTERS)]}")
                        st.button(f"เลือก {LETTERS[n % len(LETTERS)]}", key=f"bm_pick_{b}_{s}", on_click=_confirm,
                                  args=([(b, s)],), width="stretch")
        _grid(sug["choices"], draw_choice, 1)

    # ---------------- 2. not paired ---------------- #
    free_b, free_s = sug["free_bills"], sug["free_slips"]
    st.subheader(f"② ยังไม่มีคู่ ({len(free_b) + len(free_s)})")
    st.caption("แก้ยอดให้ตรงกับบิลโอน แล้วคู่จะขึ้นในข้อ ① เอง • หรือเลือกจับคู่เอง (เลือกได้ทุกใบ "
               "รวมถึงใบที่เคยกด ✖ หรือจับคู่ไปแล้ว) • ถ้าแอปแยกประเภทผิด กด ↔ เพื่อสลับ")
    open_slips = [s for s in slips if s not in bill_of]
    all_slips = open_slips + [s for s in slips if s in bill_of]
    open_bills = [b for b in bills if b not in confirmed]
    all_bills = open_bills + [b for b in bills if b in confirmed]
    t_bill, t_slip = st.tabs([f"บิลเขียนมือ ({len(free_b)})", f"บิลโอนที่ไม่มีบิลเขียนมือ ({len(free_s)})"])
    with t_bill:
        if not free_b:
            st.caption("ไม่มี")

        def draw_free_bill(b):
            with st.container(border=True):
                pic(by_id[b], "fb", f"รูปที่ {order[b]}")
                v, ok = _amount(f"{b}-b")
                amount_box(f"{b}-b", "ยอด (บาท)" + ("" if v is not None or not ok else " ⚠️ ใส่เอง"),
                           placeholder="เช่น 2,220")
                if not ok:
                    st.caption("⚠️ ใส่ยอดเป็นตัวเลข")
                elif v is not None:
                    if any((b, s) in rejected and bm.same_amount(v, slip_amt[s]) for s in slips):
                        st.caption("เคยกด ✖ ไม่ใช่ กับบิลโอนยอดนี้ — ถ้ากดผิด เลือกจับคู่เองด้านล่างได้")
                    else:
                        st.caption("ไม่มีบิลโอนยอดนี้ที่ยังว่าง")
                pick = st.selectbox("เลือกบิลโอนเอง", all_slips, index=None, key=f"bm_man_{b}",
                                    format_func=slip_label, placeholder="เลือกบิลโอน...")
                if pick:
                    pic(by_id[pick], "man", "บิลโอน")
                    st.button("✅ จับคู่", key=f"bm_mok_{b}", on_click=_pair, args=(b, pick), width="stretch")
                st.button("↔ นี่คือบิลโอน", key=f"bm_kb_{b}", on_click=_switch_kind, args=(b, "slip"), type="tertiary")
        _grid(free_b, draw_free_bill, 4)
    with t_slip:
        if not free_s:
            st.caption("ไม่มี")
        else:
            st.caption("บิลโอนเหล่านี้จะได้หน้าละใบใน PDF")

        def draw_free_slip(s):
            with st.container(border=True):
                pic(by_id[s], "fs", f"รูปที่ {order[s]}")
                v, ok = _amount(s)
                amount_box(s, "ยอดโอน" + (" ⚠️ ตรวจดู" if not read[s]["sure"] or v is None else ""))
                if not ok:
                    st.caption("⚠️ ใส่ยอดเป็นตัวเลข")
                if all_bills:
                    pick = st.selectbox("เลือกบิลเขียนมือเอง", all_bills, index=None, key=f"bm_mans_{s}",
                                        format_func=bill_label, placeholder="เลือกบิลเขียนมือ...")
                    if pick:
                        pic(by_id[pick], "mans", "เขียนมือ")
                        st.button("✅ จับคู่", key=f"bm_msok_{s}", on_click=_pair, args=(pick, s), width="stretch")
                st.button("↔ นี่คือบิลเขียนมือ", key=f"bm_ks_{s}", on_click=_switch_kind, args=(s, "bill"),
                          type="tertiary")
        _grid(free_s, draw_free_slip, 4)

    # ---------------- 3. confirmed ---------------- #
    pairs = [(bill_of[s], s) for s in slips if s in bill_of]      # in slip upload order
    st.subheader(f"③ จับคู่แล้ว ({len(pairs)})")
    if not pairs:
        st.caption("ยังไม่มี — กด ✅ ยืนยัน ในข้อ ①")
    else:
        st.caption("จับคู่ผิด: เปลี่ยนบิลโอนในช่องด้านล่างรูป (เลือกได้ทุกใบ) หรือกด ↩️ ยกเลิกคู่ "
                   "(ทั้งสองใบจะกลับไปข้อ ②)")

    def draw_done(p):
        b, s = p
        with st.container(border=True):
            c1, c2 = st.columns(2, gap="small")
            with c1:
                pic(by_id[b], "dn", "เขียนมือ")
            with c2:
                pic(by_id[s], "dn", "บิลโอน")
            same = "" if bm.same_amount(bill_amt.get(b), slip_amt[s]) else " • ⚠️ ยอดไม่ตรงกัน"
            st.markdown(f"ยอด **{bm.money(slip_amt[s])}** • รูปที่ {order[b]} + {order[s]}{same}")
            options = [s] + [x for x in all_slips if x != s]
            st.session_state[f"bm_chg_{b}"] = s
            st.selectbox("บิลโอน", options, key=f"bm_chg_{b}", on_change=_change_slip, args=(b,),
                         format_func=lambda x: slip_label(x, mine=b), label_visibility="collapsed")
            st.button("↩️ ยกเลิกคู่", key=f"bm_undo_{b}", on_click=_undo, args=(b,), width="stretch")
    _grid(pairs, draw_done, 2)

    # ---------------- 4. PDF ---------------- #
    st.subheader("④ ดาวน์โหลด PDF")
    waiting = len(sug["pairs"]) + len(sug["choices"])
    if waiting:
        st.warning(f"ยังมี {waiting} คู่รอยืนยันในข้อ ① — PDF จะมีเฉพาะคู่ที่ยืนยันแล้ว")
    data = {i: by_id[i]["data"] for i in by_id}
    if orders:
        _by_files(orders, slip_amt, bill_amt, pairs, by_id, order, data)
        with st.expander("ไฟล์เดียวรวมทุกใบ (ไม่เรียงตามไฟล์ลำดับ)"):
            _single(slips, bills, pairs, data, "bm_extra1")
    else:
        st.caption("อยากเรียงใบเสร็จตามไฟล์ (เช่น 16-11.pdf) และได้ PDF แยกตามไฟล์: อัปไฟล์นั้นในช่อง "
                   "\"ไฟล์ลำดับ (PDF)\" ด้านบน")
        _single(slips, bills, pairs, data, "bm_extra")


def _single(slips: list[str], bills: list[str], pairs: list[tuple[str, str]], data: dict, key: str) -> None:
    """One PDF with everything: unpaired slips, then the pairs (and the unpaired bills when asked)."""
    paired_bills = {b for b, _ in pairs}
    extra = [b for b in bills if b not in paired_bills]
    add_extra = False
    if extra:
        add_extra = st.checkbox(f"ใส่บิลเขียนมือที่ยังไม่มีคู่ ({len(extra)} รูป) ไว้ท้ายไฟล์ด้วย", key=key)
    pages = bm.page_plan(slips, pairs, extra if add_extra else [])
    lone = len(slips) - len(pairs)
    st.caption(f"PDF {len(pages)} หน้า (A4 แนวตั้ง): บิลโอนที่ไม่มีคู่ {lone} หน้า แล้วต่อด้วยคู่บิล {len(pairs)} หน้า "
               f"(บิลเขียนมือมุมซ้ายบน บิลโอนมุมขวาล่าง ไม่ทับกัน)"
               + (f" และบิลเขียนมือที่ไม่มีคู่ {len(extra)} หน้า" if add_extra else ""))
    st.download_button("📥 ดาวน์โหลด PDF", data=lambda: bm.build_pdf(pages, data), file_name="จับคู่บิล.pdf",
                       mime="application/pdf", type="primary", on_click="ignore", key=f"{key}_dl")


def _by_files(orders: list[dict], slip_amt: dict, bill_amt: dict, pairs: list[tuple[str, str]], by_id: dict,
              order: dict, data: dict) -> None:
    """One PDF per order file, the receipts in the file's line order."""
    choice = _state("bm_file_choice", {})
    names = _state("bm_names", {})
    units = bm.order_units(slip_amt, bill_amt, pairs)
    unit_by_id = {u["id"]: u for u in units}
    plan = bm.plan_by_files(units, [{"key": o["key"], "lines": o["lines"]} for o in orders], choice)
    title = {o["key"]: o["name"] for o in orders}
    for o in orders:
        if o["key"] not in names:
            names[o["key"]] = o["name"]

    def unit_text(uid: str) -> str:
        u = unit_by_id[uid]
        a = bm.money(u["amount"]) if u["amount"] is not None else "?"
        if u["page"][0] == "pair":
            return f"{a} • คู่บิล (รูปที่ {order[u['page'][1]]} + {order[u['page'][2]]})"
        what = "บิลโอนอย่างเดียว" if u["page"][0] == "slip" else "บิลเขียนมืออย่างเดียว"
        return f"{a} • {what} (รูปที่ {order[u['page'][1]]})"

    def draw_unit(uid: str, options: list[str], where: str) -> None:
        u = unit_by_id[uid]
        with st.container(border=True):
            c1, c2 = st.columns(2, gap="small")
            if u["page"][0] == "pair":
                with c1:
                    pic(by_id[u["page"][1]], where, "เขียนมือ")
                with c2:
                    pic(by_id[u["page"][2]], where, "บิลโอน")
            else:
                with c1:
                    pic(by_id[u["page"][1]], where, "บิลโอน" if u["page"][0] == "slip" else "เขียนมือ")
            st.markdown(unit_text(uid))
            key = f"bm_fc_{uid}"
            st.session_state[key] = choice.get(uid) if choice.get(uid) in options else None
            st.selectbox("อยู่ไฟล์ไหน", options, key=key, on_change=_save_choice, args=(uid,),
                         format_func=lambda k: f"📄 {names.get(k) or title[k]}" if k else "ไม่ใส่ไฟล์ไหน",
                         placeholder="⚠️ เลือกไฟล์...")

    # --- the lines of each file and what was found for them
    st.markdown("**เรียงตามไฟล์ลำดับ** — แต่ละไฟล์จะได้ PDF 1 ไฟล์ เรียงใบเสร็จตามรายการในไฟล์ (จับจากยอดเงิน)")
    for o in orders:
        rows = plan["rows"][o["key"]]
        found = sum(1 for r in rows if r["unit"] and r["name"] is not None)
        total = f" • รวม {bm.money(o['total'])}" if o["total"] is not None else ""
        with st.expander(f"📄 {o['name']} — {len(o['lines'])} รายการ{total} • มีใบเสร็จ {found}/{len(o['lines'])}"):
            if not rows:
                st.caption("ไม่มีรายการ")
            else:
                st.table(pd.DataFrame([
                    {"ลำดับ": n, "รายการ": r["name"] if r["name"] is not None else "(เลือกใส่เอง ท้ายไฟล์)",
                     "ยอด": bm.money(r["amount"]) if r["amount"] is not None else "",
                     "ใบเสร็จ": unit_text(r["unit"]) if r["unit"] else "❌ ไม่มี"}
                    for n, r in enumerate(rows, 1)]).set_index("ลำดับ"))
    miss = [f"{title[k]}: {ln['name']} {bm.money(ln['amount'])}" for k in title for ln in plan["missing"][k]]
    if len(miss) > 6:
        miss = [f"{title[k]} ขาด {len(plan['missing'][k])} รายการ" for k in title if plan["missing"][k]]
    if miss:
        st.warning("ไม่มีใบเสร็จของรายการเหล่านี้ (ไม่มีบิลโอน/บิลเขียนมือที่ยอดเท่านี้ หรือยังไม่ได้ยืนยันคู่ — "
                   "ดูรายการที่ขึ้น ❌ ในตารางด้านบน): " + " • ".join(miss))

    # --- amounts found in several files (or more receipts than lines): the user chooses
    asked = list(plan["options"])
    if asked:
        todo = len(plan["ask"])
        st.markdown(f"**ยอดซ้ำ — เลือกว่าใบเสร็จนี้อยู่ไฟล์ไหน ({len(asked)})**"
                    + (f" • ⚠️ ยังไม่ได้เลือก {todo} ใบ" if todo else " • เลือกครบแล้ว ✅"))
        _grid(asked, lambda uid: draw_unit(uid, plan["options"][uid] + [NONE], "ask"), 2)

    # --- receipts in no file (their amount is in no file); they can be put in a file by hand
    loose = [u["id"] for u in units if u["id"] in plan["unused"] and u["id"] not in plan["options"]]
    loose += [uid for uid, k in choice.items() if k and uid in unit_by_id and uid not in plan["options"]
              and uid not in loose]
    if loose:
        with st.expander(f"ใบเสร็จที่ยอดไม่ตรงกับรายการในไฟล์ไหนเลย ({len(loose)}) — จะไม่อยู่ในไฟล์ไหน "
                         "เว้นแต่เลือกใส่เอง (ใส่ไว้ท้ายไฟล์)"):
            _grid(loose, lambda uid: draw_unit(uid, [NONE] + list(title), "loose"), 2)

    # --- downloads, one per file; the names start as the uploaded names and can be changed
    st.markdown("**ดาวน์โหลด** — แก้ชื่อไฟล์ได้ในช่องชื่อ")
    cover = st.checkbox("ใส่หน้าของไฟล์ลำดับ (ใบรับรองฯ ที่อัปโหลด) ไว้หน้าแรกของแต่ละไฟล์", key="bm_cover")
    outs = []
    for o in orders:
        pages = plan["pages"][o["key"]]
        c1, c2, c3 = st.columns([3, 2, 2], vertical_alignment="bottom")
        nk = f"bm_nm_{o['key']}"
        st.session_state[nk] = names[o["key"]]
        c1.text_input(f"ชื่อไฟล์ (จาก {o['name']}.pdf)", key=nk, on_change=_save_name, args=(o["key"],))
        fname = _file_name(names[o["key"]], o["name"])
        c2.caption(f"{len(pages)} หน้า" + (f" • ขาด {len(plan['missing'][o['key']])} รายการ"
                                          if plan["missing"][o["key"]] else ""))

        def make(pages=pages, o=o, with_cover=cover):
            body = bm.build_pdf(pages, data)
            return bm.with_cover(o["data"], body) if with_cover else body
        c3.download_button(f"📥 {fname}", data=make, file_name=fname, mime="application/pdf", type="primary",
                           on_click="ignore", key=f"bm_dl_{o['key']}", disabled=not pages, width="stretch")
        if pages:
            outs.append((fname, make))
    if len(outs) > 1:
        def make_zip(outs=outs):
            buf, used = io.BytesIO(), set()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for fname, make in outs:
                    n, stem = 1, fname[:-4]
                    while fname in used:
                        n += 1
                        fname = f"{stem} ({n}).pdf"
                    used.add(fname)
                    z.writestr(fname, make())
            return buf.getvalue()
        st.download_button(f"📦 ดาวน์โหลดทั้ง {len(outs)} ไฟล์ (.zip)", data=make_zip, file_name="จับคู่บิล.zip",
                           mime="application/zip", on_click="ignore", key="bm_dl_zip")
