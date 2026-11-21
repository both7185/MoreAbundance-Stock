"""
หน้า 🔗 จับคู่บิล (separate from the other pages). Logic lives in bill_match.py; nothing is saved.

1. Upload the transfer slips (Electronic bill) and the handwritten bills: pictures or .zip, any number of files.
2. The slip amounts are read automatically (OCR) and can be corrected. The user types each handwritten bill's price.
3. A price matching one slip is paired automatically. Amounts shared by several slips/bills are shown with their
   pictures for the user to choose.
4. 📥 PDF (A4 portrait): unpaired slips one per page, then each pair with the bill top-left and the slip bottom-right.
"""
from __future__ import annotations

import io

import streamlit as st
from PIL import Image

import bill_match as bm

COLS = 4
LETTERS = "กขคงจฉชซฌญฎฏฐฑฒณดตถทธนบปผฝพฟภม"
UP_TYPES = ["jpg", "jpeg", "png", "webp", "zip"]


# --------------------------------------------------------------------------- #
# Per-session caches (pictures stay in memory only while the page is open)
# --------------------------------------------------------------------------- #
def _cache(name: str) -> dict:
    return st.session_state.setdefault(name, {})


def _thumb(img: dict) -> bytes:
    """Picture padded into a 2:3 card so the grid rows line up (click the picture's ⛶ to enlarge)."""
    cache = _cache("bm_thumbs")
    if img["id"] not in cache:
        im = bm.open_image(img["data"])
        im.thumbnail((800, 1200), Image.LANCZOS)
        card = Image.new("RGB", (800, 1200), (245, 246, 248))
        card.paste(im, ((800 - im.width) // 2, (1200 - im.height) // 2))
        buf = io.BytesIO()
        card.save(buf, "JPEG", quality=80)
        cache[img["id"]] = buf.getvalue()
    return cache[img["id"]]


def _collect(ups, key: str) -> list[dict]:
    files = [(u.name, u.getvalue()) for u in ups or []]
    sig = tuple((n, len(d)) for n, d in files)
    cache = _cache("bm_sets")
    if cache.get(key, (None,))[0] != sig:
        cache[key] = (sig, *bm.collect_images(files))
    _, images, notes = cache[key]
    for n in notes:
        st.warning(n)
    return images


def _read_slips(slips: list[dict]) -> dict[str, dict]:
    """OCR each slip once per session: {id: {'amount', 'sure'}}."""
    done = _cache("bm_ocr")
    todo = [s for s in slips if s["id"] not in done]
    if todo:
        why = bm.ocr_ready()
        if why:
            for s in todo:
                done[s["id"]] = {"amount": None, "sure": False}
            st.warning(f"อ่านยอดเงินจากรูปอัตโนมัติไม่ได้: {why} — พิมพ์ยอดของบิลโอนแต่ละใบเองในข้อ ①")
        else:
            bar = st.progress(0.0, text=f"กำลังอ่านยอดเงินจากบิลโอน 0/{len(todo)}...")

            def step(n, total):
                bar.progress(n / total, text=f"กำลังอ่านยอดเงินจากบิลโอน {n}/{total}...")

            for s, r in zip(todo, bm.read_amounts([s["data"] for s in todo], step)):
                done[s["id"]] = r
            bar.empty()
    return {s["id"]: done[s["id"]] for s in slips}


def _price(key: str) -> tuple[float | None, bool]:
    """(value, ok) of a text box holding a price."""
    try:
        return bm.parse_price(st.session_state.get(key)), True
    except ValueError:
        return None, False


def _grid(items: list, draw, cols: int = COLS) -> None:
    for start in range(0, len(items), cols):
        row = st.columns(cols)
        for col, item in zip(row, items[start:start + cols]):
            with col:
                draw(item)


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #
def render(show_flash=None) -> None:
    st.title("🔗 จับคู่บิลโอนกับบิลเขียนมือ")
    if show_flash:
        show_flash()
    st.caption("อัปโหลดรูปบิลโอน (Electronic bill) กับรูปบิลเขียนมือ → ใส่ราคาของบิลเขียนมือ → "
               "บิลที่ราคาตรงกับยอดโอนจะจับคู่กันให้ → ดาวน์โหลดเป็น PDF (A4) • ไม่มีการบันทึกข้อมูลใดๆ")

    c1, c2 = st.columns(2)
    up_s = c1.file_uploader("1. บิลโอนเงิน (Electronic bill) — รูป หรือ .zip", type=UP_TYPES,
                            accept_multiple_files=True, key="bm_up_slip",
                            help="เลือกได้หลายไฟล์ ไฟล์ .zip จะใช้ทุกรูปที่อยู่ข้างใน")
    up_b = c2.file_uploader("2. บิลเขียนมือ / ใบเสร็จ — รูป หรือ .zip", type=UP_TYPES,
                            accept_multiple_files=True, key="bm_up_bill",
                            help="เลือกได้หลายไฟล์ ไฟล์ .zip จะใช้ทุกรูปที่อยู่ข้างใน")
    with c1:
        slips = _collect(up_s, "slip")
        if slips:
            st.caption(f"✅ บิลโอน {len(slips)} รูป")
    with c2:
        bills = _collect(up_b, "bill")
        if bills:
            st.caption(f"✅ บิลเขียนมือ {len(bills)} รูป")
    if not slips or not bills:
        st.info("อัปโหลดรูปให้ครบทั้ง 2 ช่อง")
        return
    both = {s["id"] for s in slips} & {b["id"] for b in bills}
    if both:
        st.warning(f"มี {len(both)} รูปที่อยู่ทั้ง 2 ช่อง — รูปนั้นจะใช้เป็นบิลโอนเท่านั้น")
        bills = [b for b in bills if b["id"] not in both]

    # ---------------- 1. slip amounts ---------------- #
    ocr = _read_slips(slips)
    for s in slips:
        k = f"bm_s_{s['id']}"
        if k not in st.session_state:
            a = ocr[s["id"]]["amount"]
            st.session_state[k] = "" if a is None else bm.money(a)
    unsure = [s for s in slips if not ocr[s["id"]]["sure"]]
    slip_amount, slip_bad = {}, []
    for s in slips:
        v, ok = _price(f"bm_s_{s['id']}")
        slip_amount[s["id"]] = v
        if not ok or v is None:
            slip_bad.append(s["id"])

    st.subheader("① ยอดเงินในบิลโอน")
    st.caption("อ่านจากรูปให้อัตโนมัติ ถ้าไม่ตรงแก้ตัวเลขในช่องได้เลย • ⚠️ = อ่านไม่ชัด ช่วยตรวจดู")
    with st.expander(f"ดูบิลโอนทั้ง {len(slips)} รูป และยอดที่อ่านได้"
                     + (f" — ⚠️ ตรวจดู {len(set(s['id'] for s in unsure) | set(slip_bad))} รูป" if unsure or slip_bad else ""),
                     expanded=bool(unsure or slip_bad)):
        def draw_slip(s):
            st.image(_thumb(s), width="stretch")
            flag = "⚠️ " if (not ocr[s["id"]]["sure"] or s["id"] in slip_bad) else ""
            st.text_input(f"{flag}ยอดโอน (บาท)", key=f"bm_s_{s['id']}")
            if s["id"] in slip_bad:
                st.caption("⚠️ ใส่ยอดเป็นตัวเลข")
        _grid(slips, draw_slip)

    # ---------------- 2. handwritten prices ---------------- #
    st.subheader("② ใส่ราคาของบิลเขียนมือ")
    st.caption("พิมพ์ราคารวมของแต่ละบิล แล้วกด 🔗 จับคู่บิล • บิลที่ไม่ต้องการจับคู่ (เช่น ใบที่ไม่เกี่ยว) เว้นว่างไว้ได้")
    with st.form("bm_prices", border=False):
        def draw_bill(b):
            st.image(_thumb(b), width="stretch")
            st.text_input("ราคา (บาท)", key=f"bm_b_{b['id']}", placeholder="เช่น 2,220")
        _grid(bills, draw_bill)
        st.form_submit_button("🔗 จับคู่บิล", type="primary")

    bill_price, bill_bad = {}, []
    for b in bills:
        v, ok = _price(f"bm_b_{b['id']}")
        bill_price[b["id"]] = v
        if not ok:
            bill_bad.append(b["id"])
    typed = sum(1 for v in bill_price.values() if v is not None)
    if bill_bad:
        st.error(f"ราคาของบิลเขียนมือ {len(bill_bad)} รูปไม่ใช่ตัวเลข — แก้ในข้อ ②")
    if typed == 0:
        st.info(f"ใส่ราคาแล้ว 0/{len(bills)} รูป")
        return

    picks = {b["id"]: st.session_state.get(f"bm_pick_{b['id']}") for b in bills}
    result = bm.match(slip_amount, bill_price, picks)
    by_id = {x["id"]: x for x in slips + bills}

    # ---------------- 3. results ---------------- #
    st.subheader("③ ผลการจับคู่")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("จับคู่แล้ว", f"{len(result['pairs'])} คู่")
    m2.metric("ยอดซ้ำ รอเลือก", len(result["open_groups"]))
    m3.metric("บิลโอนที่ไม่มีบิลเขียนมือ", len(slips) - len(result["pairs"]))
    m4.metric("บิลเขียนมือที่ไม่มีคู่", len(bills) - len(result["pairs"]))
    st.caption(f"ใส่ราคาแล้ว {typed}/{len(bills)} รูป" + (f" • เว้นว่าง {len(result['blank'])} รูป" if result["blank"] else ""))

    if result["groups"]:
        st.markdown("#### ยอดซ้ำ — เลือกคู่ที่ถูก")
        st.caption("ยอดเดียวกันมีหลายใบ ดูรูปแล้วเลือกว่าบิลเขียนมือใบนี้คู่กับบิลโอนใบไหน")
        for g in result["groups"]:
            _duplicate_group(g, by_id, result)

    if result["no_slip"]:
        st.markdown("#### ⚠️ บิลเขียนมือที่หาบิลโอนยอดเดียวกันไม่เจอ")
        st.caption("ตรวจราคาที่พิมพ์ในข้อ ② อีกครั้ง หรือบิลนี้อาจยังไม่มีบิลโอน")

        def draw_ns(bid):
            st.image(_thumb(by_id[bid]), width="stretch")
            st.markdown(f"ราคา **{bm.money(bill_price[bid])}** บาท")
        _grid(result["no_slip"], draw_ns)

    with st.expander(f"✅ คู่ที่จับแล้ว ({len(result['pairs'])} คู่)"):
        def draw_pair(p):
            bid, sid = p
            a, b = st.columns(2)
            a.image(_thumb(by_id[bid]), width="stretch", caption="บิลเขียนมือ")
            b.image(_thumb(by_id[sid]), width="stretch", caption="บิลโอน")
            st.markdown(f"ยอด **{bm.money(slip_amount[sid])}** บาท")
        _grid(result["pairs"], draw_pair, cols=2)

    paired = {s for _, s in result["pairs"]}
    lone = [s["id"] for s in slips if s["id"] not in paired]
    with st.expander(f"บิลโอนที่ไม่มีบิลเขียนมือ ({len(lone)} รูป) — ใน PDF อยู่หน้าแรกๆ หน้าละใบ"):
        def draw_lone(sid):
            st.image(_thumb(by_id[sid]), width="stretch")
            st.markdown(f"ยอด **{bm.money(slip_amount[sid])}** บาท")
        _grid(lone, draw_lone)

    # ---------------- 4. PDF ---------------- #
    st.subheader("④ ดาวน์โหลด PDF")
    paired_bills = {b for b, _ in result["pairs"]}
    extra = [b["id"] for b in bills if b["id"] not in paired_bills]
    add_extra = False
    if extra:
        add_extra = st.checkbox(f"ใส่บิลเขียนมือที่ไม่มีคู่ ({len(extra)} รูป) ไว้ท้ายไฟล์ด้วย", key="bm_extra")
    if result["open_groups"]:
        st.warning("ยังเลือกคู่ของยอดซ้ำไม่ครบ: " + ", ".join(bm.money(a) for a in result["open_groups"])
                   + " บาท — PDF ตอนนี้จะยังไม่มีคู่เหล่านั้น")
    if result["blank"]:
        st.caption(f"บิลเขียนมือ {len(result['blank'])} รูปยังไม่ได้ใส่ราคา (จะไม่อยู่ใน PDF ถ้าไม่ติ๊กช่องด้านบน)")
    pages = bm.page_plan([s["id"] for s in slips], result["pairs"], extra if add_extra else [])
    images = {k: v["data"] for k, v in by_id.items()}
    st.caption(f"PDF {len(pages)} หน้า (A4 แนวตั้ง): บิลโอนที่ไม่มีคู่ {len(lone)} หน้า แล้วต่อด้วยคู่บิล "
               f"{len(result['pairs'])} หน้า (บิลเขียนมือมุมซ้ายบน บิลโอนมุมขวาล่าง)"
               + (f" และบิลเขียนมือที่ไม่มีคู่ {len(extra)} หน้า" if add_extra else ""))
    st.download_button("📥 ดาวน์โหลด PDF", data=lambda: bm.build_pdf(pages, images), file_name="จับคู่บิล.pdf",
                       mime="application/pdf", type="primary", on_click="ignore")


def _duplicate_group(g: dict, by_id: dict, result: dict) -> None:
    label = {sid: f"บิลโอน {LETTERS[i % len(LETTERS)]}" for i, sid in enumerate(g["slips"])}
    with st.container(border=True):
        st.markdown(f"**ยอด {bm.money(g['amount'])} บาท** — บิลโอน {len(g['slips'])} ใบ, บิลเขียนมือ {len(g['bills'])} ใบ")
        if g["amount"] in result["conflicts"]:
            st.error("เลือกบิลโอนใบเดียวกันให้บิลเขียนมือ 2 ใบ — เลือกใหม่")
        st.caption("บิลโอนยอดนี้")

        def draw_s(sid):
            st.image(_thumb(by_id[sid]), width="stretch", caption=label[sid])
        _grid(g["slips"], draw_s)
        st.caption("บิลเขียนมือยอดนี้ — เลือกว่าคู่กับบิลโอนใบไหน")

        def draw_b(bid):
            st.image(_thumb(by_id[bid]), width="stretch")
            if st.session_state.get(f"bm_pick_{bid}") not in g["slips"] + ["", None]:
                del st.session_state[f"bm_pick_{bid}"]   # the price changed: old answer is for another amount
            st.radio("คู่กับ", g["slips"] + [""], index=None, key=f"bm_pick_{bid}",
                     format_func=lambda s: label.get(s, "ไม่จับคู่"))
            if bid in result["auto_picked"]:
                st.caption(f"→ คู่กับ {label[result['auto_picked'][bid]]} (เหลือใบเดียว)")
        _grid(g["bills"], draw_b)
