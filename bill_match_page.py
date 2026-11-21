"""
หน้า 🔗 จับคู่บิล (separate from the other pages). Logic lives in bill_match.py; nothing is saved.

1. One upload box for every picture: transfer slips (Electronic bill) and handwritten bills, pictures or .zip.
2. Each picture is read once (OCR): slip or bill, the slip amount, the numbers on the bill. The bill total is the
   number that equals a slip amount. Kinds and amounts can be corrected.
3. ① รอยืนยัน: suggested pairs (same amount), ✅ confirm or ✖ reject; a shared amount shows its slips to pick from.
   ② ยังไม่มีคู่: bills/slips without a pair; fix the amount, pick a slip by hand, or switch the kind.
   ③ จับคู่แล้ว: confirmed pairs; the slip can be changed or the pair undone.
4. 📥 PDF (A4 portrait): unpaired slips one per page, then each confirmed pair (bill top-left, slip bottom-right).

Every picture can be clicked to see it large (a CSS-only lightbox; the pictures are served as Streamlit media files).
"""
from __future__ import annotations

import io

import streamlit as st
from PIL import Image

import bill_match as bm

UP_TYPES = ["jpg", "jpeg", "png", "webp", "zip"]
LETTERS = "กขคงจฉชซฌญฎฏฐฑฒณดตถทธนบปผฝพฟภม"

_CSS = """<style>
.bmlb{position:relative}
.bmlb input{display:none}
.bmlb .th{display:flex;align-items:center;justify-content:center;cursor:zoom-in;border-radius:6px;overflow:hidden;
  background:rgba(128,128,128,.10);margin:0}
.bmlb .th img{max-width:100%;max-height:100%;object-fit:contain}
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
        cache[img["id"]] = (_jpeg(im, 420, 72), _jpeg(im, 1700, 85))
    return cache[img["id"]]


def pic(img: dict, where: str, height: int = 180, tag: str = "") -> None:
    """Thumbnail that opens the full picture when clicked (click again to close)."""
    small, big = _pictures(img)
    s_url = _media_url(small, f"{img['id']}-s")
    b_url = _media_url(big, f"{img['id']}-b")
    n = st.session_state["bm_pic_n"] = st.session_state.get("bm_pic_n", 0) + 1
    cid = f"bm-{where}-{img['id']}-{n}"
    label = f'<span class="tag">{tag}</span>' if tag else ""
    st.html(f'<div class="bmlb"><input type="checkbox" id="{cid}">'
            f'<label class="th" for="{cid}" style="height:{height}px">{label}<img src="{s_url}" alt=""></label>'
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


# --------------------------------------------------------------------------- #
# Actions (button callbacks: they run before the page is drawn again)
# --------------------------------------------------------------------------- #
def _confirm(pairs: list[tuple[str, str]]) -> None:
    confirmed = st.session_state.bm_confirmed
    for b, s in pairs:
        if s not in confirmed.values() and b not in confirmed:
            confirmed[b] = s


def _reject(b: str, s: str) -> None:
    st.session_state.bm_rejected.add((b, s))


def _undo(b: str) -> None:
    st.session_state.bm_confirmed.pop(b, None)


def _change_slip(b: str) -> None:
    s = st.session_state.get(f"bm_chg_{b}")
    if s:
        st.session_state.bm_confirmed[b] = s


def _switch_kind(i: str, kind: str) -> None:
    st.session_state.bm_kind[i] = kind
    st.session_state.bm_confirmed = {b: s for b, s in st.session_state.bm_confirmed.items() if i not in (b, s)}


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #
def render(show_flash=None) -> None:
    st.title("🔗 จับคู่บิลโอนกับบิลเขียนมือ")
    if show_flash:
        show_flash()
    st.caption("อัปโหลดรูปทั้งหมดในช่องเดียว → แอปแยกบิลโอนกับบิลเขียนมือ อ่านยอดเงิน แล้วจับคู่ให้ → ตรวจแล้วกดยืนยัน "
               "→ ดาวน์โหลด PDF (A4) • กดที่รูปเพื่อดูรูปใหญ่ • ไม่มีการบันทึกข้อมูลใดๆ")
    ups = st.file_uploader("รูปบิลทั้งหมด (บิลโอน + บิลเขียนมือ ปนกันได้) — รูป หรือ .zip", type=UP_TYPES,
                           accept_multiple_files=True, key="bm_up",
                           help="เลือกได้หลายไฟล์ ไฟล์ .zip จะใช้ทุกรูปที่อยู่ข้างใน")
    images = _collect(ups)
    if not images:
        st.info("อัปโหลดรูปบิลโอนกับรูปบิลเขียนมือ (ทั้งหมดรวมกันในช่องเดียวได้เลย)")
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

    m = st.columns(5)
    m[0].metric("บิลโอน", len(slips))
    m[1].metric("บิลเขียนมือ", len(bills))
    m[2].metric("รอยืนยัน", len(sug["pairs"]) + len(sug["choices"]))
    m[3].metric("ยังไม่มีคู่", len(sug["free_bills"]) + len(sug["free_slips"]))
    m[4].metric("จับคู่แล้ว", len(confirmed))

    def amt_line(b: str, s: str) -> str:
        warn = " • ⚠️ ยอดบิลอ่านแบบเดา" if guessed.get(b) == "near" else ""
        return f"ยอด **{bm.money(slip_amt[s])}**{warn}"

    # ---------------- 1. suggested pairs ---------------- #
    st.subheader(f"① รอยืนยัน ({len(sug['pairs']) + len(sug['choices'])})")
    if not sug["pairs"] and not sug["choices"]:
        st.caption("ไม่มีคู่ที่รอยืนยัน")
    if sug["pairs"]:
        st.caption("บิลเขียนมือ (ซ้าย) กับบิลโอน (ขวา) ที่ยอดตรงกัน • ✅ ยืนยัน = ย้ายไปจับคู่แล้ว • ✖ ไม่ใช่ = ไม่จับคู่สองใบนี้")
        st.button(f"✅ ยืนยันทั้งหมด ({len(sug['pairs'])} คู่)", type="primary", on_click=_confirm,
                  args=(sug["pairs"],), key="bm_all")

        def draw_pair(p):
            b, s = p
            with st.container(border=True):
                c1, c2 = st.columns(2, gap="small")
                with c1:
                    pic(by_id[b], "sp", 150, "เขียนมือ")
                with c2:
                    pic(by_id[s], "sp", 150, "บิลโอน")
                st.markdown(amt_line(b, s))
                k1, k2 = st.columns(2, gap="small")
                k1.button("✅ ยืนยัน", key=f"bm_ok_{b}_{s}", on_click=_confirm, args=([(b, s)],), width="stretch")
                k2.button("✖ ไม่ใช่", key=f"bm_no_{b}_{s}", on_click=_reject, args=(b, s), width="stretch")
        _grid(sug["pairs"], draw_pair, 4)

    if sug["choices"]:
        st.markdown("**ยอดซ้ำ — เลือกบิลโอนที่ถูก**")

        def draw_choice(c):
            b, ss = c
            with st.container(border=True):
                cols = st.columns(1 + len(ss), gap="small")
                with cols[0]:
                    pic(by_id[b], "ch", 150, "เขียนมือ")
                    st.caption(f"ยอด {bm.money(bill_amt[b])}")
                for n, (col, s) in enumerate(zip(cols[1:], ss)):
                    with col:
                        pic(by_id[s], "ch", 150, f"บิลโอน {LETTERS[n % len(LETTERS)]}")
                        st.button(f"เลือก {LETTERS[n % len(LETTERS)]}", key=f"bm_pick_{b}_{s}", on_click=_confirm,
                                  args=([(b, s)],), width="stretch")
        _grid(sug["choices"], draw_choice, 2)

    # ---------------- 2. not paired ---------------- #
    free_b, free_s = sug["free_bills"], sug["free_slips"]
    st.subheader(f"② ยังไม่มีคู่ ({len(free_b) + len(free_s)})")
    st.caption("แก้ยอดให้ตรงกับบิลโอน แล้วคู่จะขึ้นในข้อ ① เอง • หรือเลือกบิลโอนเอง • "
               "ถ้าแอปแยกประเภทผิด กด ↔ เพื่อสลับ")
    open_slips = [s for s in slips if s not in confirmed.values()]
    t_bill, t_slip = st.tabs([f"บิลเขียนมือ ({len(free_b)})", f"บิลโอนที่ไม่มีบิลเขียนมือ ({len(free_s)})"])
    with t_bill:
        if not free_b:
            st.caption("ไม่มี")

        def draw_free_bill(b):
            with st.container(border=True):
                pic(by_id[b], "fb", 190)
                v, ok = _amount(f"{b}-b")
                amount_box(f"{b}-b", "ยอด (บาท)" + ("" if v is not None or not ok else " ⚠️ ใส่เอง"),
                           placeholder="เช่น 2,220")
                if not ok:
                    st.caption("⚠️ ใส่ยอดเป็นตัวเลข")
                elif v is not None:
                    st.caption("ไม่มีบิลโอนยอดนี้ที่ยังว่าง")
                choices = [s for s in open_slips if (b, s) not in rejected] or open_slips
                pick = st.selectbox("เลือกบิลโอนเอง", choices, index=None, key=f"bm_man_{b}",
                                    format_func=lambda s: f"{bm.money(slip_amt[s])} บาท (รูปที่ {order[s]})",
                                    placeholder="เลือกบิลโอน...")
                if pick:
                    pic(by_id[pick], "man", 150, "บิลโอน")
                    st.button("✅ จับคู่", key=f"bm_mok_{b}", on_click=_confirm, args=([(b, pick)],), width="stretch")
                st.button("↔ นี่คือบิลโอน", key=f"bm_kb_{b}", on_click=_switch_kind, args=(b, "slip"), type="tertiary")
        _grid(free_b, draw_free_bill, 4)
    with t_slip:
        if not free_s:
            st.caption("ไม่มี")
        else:
            st.caption("บิลโอนเหล่านี้จะอยู่หน้าแรกๆ ของ PDF หน้าละใบ")

        def draw_free_slip(s):
            with st.container(border=True):
                pic(by_id[s], "fs", 170)
                v, ok = _amount(s)
                amount_box(s, "ยอดโอน" + (" ⚠️ ตรวจดู" if not read[s]["sure"] or v is None else ""))
                if not ok:
                    st.caption("⚠️ ใส่ยอดเป็นตัวเลข")
                st.button("↔ นี่คือบิลเขียนมือ", key=f"bm_ks_{s}", on_click=_switch_kind, args=(s, "bill"),
                          type="tertiary")
        _grid(free_s, draw_free_slip, 5)

    # ---------------- 3. confirmed ---------------- #
    pairs = [(b, s) for s in slips for b, s2 in confirmed.items() if s2 == s]     # in slip upload order
    st.subheader(f"③ จับคู่แล้ว ({len(pairs)})")
    if not pairs:
        st.caption("ยังไม่มี — กด ✅ ยืนยัน ในข้อ ①")
    else:
        st.caption("จับคู่ผิด: เปลี่ยนบิลโอนในช่องด้านล่างรูป หรือกด ↩️ ยกเลิกคู่ (ทั้งสองใบจะกลับไปข้อ ②)")

    def draw_done(p):
        b, s = p
        with st.container(border=True):
            c1, c2 = st.columns(2, gap="small")
            with c1:
                pic(by_id[b], "dn", 130, "เขียนมือ")
            with c2:
                pic(by_id[s], "dn", 130, "บิลโอน")
            same = "" if bm.same_amount(bill_amt.get(b), slip_amt[s]) else " • ⚠️ ยอดไม่ตรงกัน"
            st.markdown(f"ยอด **{bm.money(slip_amt[s])}**{same}")
            options = [s] + [x for x in open_slips if x != s]
            if st.session_state.get(f"bm_chg_{b}") not in options:
                st.session_state[f"bm_chg_{b}"] = s
            st.selectbox("บิลโอน", options, key=f"bm_chg_{b}", on_change=_change_slip, args=(b,),
                         format_func=lambda x: f"{bm.money(slip_amt[x])} บาท (รูปที่ {order[x]})",
                         label_visibility="collapsed")
            st.button("↩️ ยกเลิกคู่", key=f"bm_undo_{b}", on_click=_undo, args=(b,), width="stretch")
    _grid(pairs, draw_done, 4)

    # ---------------- 4. PDF ---------------- #
    st.subheader("④ ดาวน์โหลด PDF")
    paired_bills = {b for b, _ in pairs}
    extra = [b for b in bills if b not in paired_bills]
    add_extra = False
    if extra:
        add_extra = st.checkbox(f"ใส่บิลเขียนมือที่ยังไม่มีคู่ ({len(extra)} รูป) ไว้ท้ายไฟล์ด้วย", key="bm_extra")
    waiting = len(sug["pairs"]) + len(sug["choices"])
    if waiting:
        st.warning(f"ยังมี {waiting} คู่รอยืนยันในข้อ ① — PDF จะมีเฉพาะคู่ที่ยืนยันแล้ว")
    pages = bm.page_plan(slips, pairs, extra if add_extra else [])
    lone = len(slips) - len(pairs)
    st.caption(f"PDF {len(pages)} หน้า (A4 แนวตั้ง): บิลโอนที่ไม่มีคู่ {lone} หน้า แล้วต่อด้วยคู่บิล {len(pairs)} หน้า "
               f"(บิลเขียนมือมุมซ้ายบน บิลโอนมุมขวาล่าง)"
               + (f" และบิลเขียนมือที่ไม่มีคู่ {len(extra)} หน้า" if add_extra else ""))
    data = {i: by_id[i]["data"] for i in by_id}
    st.download_button("📥 ดาวน์โหลด PDF", data=lambda: bm.build_pdf(pages, data), file_name="จับคู่บิล.pdf",
                       mime="application/pdf", type="primary", on_click="ignore")
