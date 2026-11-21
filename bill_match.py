"""
จับคู่บิลโอนเงิน (Electronic bill / สลิปโอนเงิน) กับบิลเขียนมือ — the logic behind bill_match_page.py.

* Images come from uploaded picture files or .zip files (any folder depth), in natural file-name order.
* The amount of each transfer slip is read by Tesseract OCR (K PLUS green slips, Krungsri yellow slips, ...);
  the user can correct it on screen. Without Tesseract the amounts are typed by hand.
* The user types the price of each handwritten bill. A price equal to exactly one slip amount (and no other
  handwritten bill with that price) is paired automatically; amounts shared by several slips and/or bills are
  returned as "duplicates" for the user to pick.
* build_pdf(): A4 portrait (595.5 x 842.25 pt, like the user's Canva files). A slip without a handwritten bill
  gets a page of its own (centred); a pair gets the handwritten bill top-left and the slip bottom-right.
  The two are as big as fits without covering each other. Unpaired slips come first, then the pairs, each in upload order.

Nothing is saved anywhere.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import subprocess
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from PIL import Image, ImageChops, ImageOps

IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff")


# --------------------------------------------------------------------------- #
# Files
# --------------------------------------------------------------------------- #
def natural_key(name: str) -> list:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def _zip_name(info: zipfile.ZipInfo) -> str:
    """File name inside a zip; names written without the UTF-8 flag (Windows/Mac zips) are decoded as Thai/UTF-8."""
    name = info.filename
    if not info.flag_bits & 0x800:
        raw = name.encode("cp437", errors="replace")
        for enc in ("utf-8", "cp874"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                pass
    return name


def collect_images(files: list[tuple[str, bytes]]) -> tuple[list[dict], list[str]]:
    """[(name, data)] of uploads (images or zips) -> ([{id, name, data}], notes). Same picture twice is kept once."""
    found: list[tuple[str, bytes]] = []
    notes: list[str] = []
    for name, data in files:
        if name.lower().endswith(".zip"):
            try:
                zf = zipfile.ZipFile(io.BytesIO(data))
            except zipfile.BadZipFile:
                notes.append(f"เปิดไฟล์ zip {name} ไม่ได้")
                continue
            inner = []
            for info in zf.infolist():
                n = _zip_name(info)
                base = n.rsplit("/", 1)[-1]
                if info.is_dir() or "__MACOSX" in n or base.startswith("."):
                    continue
                if base.lower().endswith(IMAGE_EXT):
                    inner.append((base, zf.read(info)))
            if not inner:
                notes.append(f"ใน {name} ไม่มีไฟล์รูป")
            found.extend(sorted(inner, key=lambda x: natural_key(x[0])))
        elif name.lower().endswith(IMAGE_EXT):
            found.append((name, data))
    out, seen = [], set()
    for name, data in found:
        h = hashlib.sha1(data).hexdigest()[:16]
        if h in seen:
            continue
        try:
            Image.open(io.BytesIO(data)).verify()
        except Exception:
            notes.append(f"{name} ไม่ใช่ไฟล์รูปที่เปิดได้ — ข้ามไป")
            continue
        seen.add(h)
        out.append({"id": h, "name": name, "data": data})
    return out, notes


def open_image(data: bytes) -> Image.Image:
    """Upright RGB picture (phone photos carry their rotation in EXIF)."""
    im = Image.open(io.BytesIO(data))
    im = ImageOps.exif_transpose(im)
    if im.mode != "RGB":
        bg = Image.new("RGB", im.size, "white")
        bg.paste(im.convert("RGBA"), mask=im.convert("RGBA").split()[-1])
        im = bg
    return im


def jpeg(data: bytes, longest: int) -> bytes:
    """Upright JPEG copy, at most `longest` px on the long side (smaller pages / PDF)."""
    im = open_image(data)
    if max(im.size) > longest:
        im.thumbnail((longest, longest), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85, optimize=True)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Reading slip amounts (OCR)
# --------------------------------------------------------------------------- #
_AMOUNT = re.compile(r"(?<![\d,.])(\d{1,3}(?:[,.]\d{3})+|\d+)[.,](\d{2})(?!\d)")
_TESS_ENV = {**os.environ, "OMP_THREAD_LIMIT": "1"}


def ocr_ready() -> str:
    """'' when Tesseract can be used, else why not."""
    if not shutil.which("tesseract"):
        return "เครื่องที่รันแอปไม่มีโปรแกรม Tesseract (ใช้อ่านยอดเงินจากรูป)"
    try:
        langs = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=30).stdout
    except Exception as e:
        return f"เรียก Tesseract ไม่ได้ ({e})"
    return "" if "eng" in langs.split() else "Tesseract ไม่มีภาษาอังกฤษ (eng)"


def _tess(img: Image.Image, lang: str) -> str:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    try:
        r = subprocess.run(["tesseract", "stdin", "stdout", "-l", lang, "--psm", "6"], input=buf.getvalue(),
                           capture_output=True, timeout=120, env=_TESS_ENV)
    except Exception:
        return ""
    return r.stdout.decode("utf-8", errors="ignore")


def amounts_in(text: str) -> list[str]:
    """Money amounts like 5,082.00 / 15,461.50 / 990.00 in OCR text, as '5082.00' strings (no zero amounts and
    no dates such as 9.11 / 11.11 from the slip's memo)."""
    out = []
    for m in _AMOUNT.finditer(text):
        whole = re.sub(r"[,.]", "", m.group(1)).lstrip("0") or "0"
        cents = m.group(2)
        if whole == "0" and cents == "00":
            continue
        if int(whole) < 32 and cents != "00":   # 9.11, 10.11 ... (day.month written in the memo)
            continue
        out.append(f"{whole}.{cents}")
    return out


def _passes(img: Image.Image, langs: tuple[str, ...]) -> list[list[str]]:
    """Text is dark grey on a pastel background with flowers/leaves: keep pixels whose brightest channel is dark."""
    w, h = img.size
    if w != 1600:
        img = img.resize((1600, max(1, round(h * 1600 / w))), Image.LANCZOS)
    r, g, b = img.split()
    top = ImageChops.lighter(ImageChops.lighter(r, g), b)
    out = []
    for cut in (90, 130):
        bw = top.point(lambda v, c=cut: 255 if v > c else 0)
        for lang in langs:
            out.append(amounts_in(_tess(bw, lang)))
    return out


def _vote(passes: list[list[str]]) -> tuple[float | None, int, int]:
    """(amount, votes, passes) — an amount whose digits end another amount's (82.00 vs 5082.00, a dropped digit)
    counts for the longer one."""
    votes = Counter()
    for found in passes:
        for a in set(found):
            votes[a] += 1
    if not votes:
        return None, 0, len(passes)
    score = Counter()
    for a, n in votes.items():
        longer = [b for b in votes if b != a and b.endswith(a)]
        target = max(longer, key=lambda b: (votes[b], len(b))) if longer else a
        score[target] += n
    best = max(score, key=lambda a: (score[a], float(a)))
    return float(best), score[best], len(passes)


def read_amount(data: bytes) -> dict:
    """{'amount': float | None, 'sure': bool} for one slip picture."""
    img = open_image(data)
    first = _passes(img, ("eng",))
    amount, votes, n = _vote(first)
    if amount is not None and votes == n:
        return {"amount": amount, "sure": True}
    more = first + _passes(img, ("tha",))
    amount, votes, n = _vote(more)
    return {"amount": amount, "sure": amount is not None and votes >= n - 1}


def read_amounts(datas: list[bytes], progress=None, workers: int = 4) -> list[dict]:
    done = 0
    out: list[dict | None] = [None] * len(datas)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(read_amount, d): i for i, d in enumerate(datas)}
        for f in futs:
            i = futs[f]
            try:
                out[i] = f.result()
            except Exception:
                out[i] = {"amount": None, "sure": False}
            done += 1
            if progress:
                progress(done, len(datas))
    return out


# --------------------------------------------------------------------------- #
# Prices typed by the user
# --------------------------------------------------------------------------- #
def parse_price(text: str | None) -> float | None:
    """'2,220' / '2220.50' / ' 990 บาท' -> float; '' -> None; anything else -> ValueError."""
    t = (text or "").strip().replace(",", "").replace("บาท", "").replace("฿", "").strip()
    if not t:
        return None
    v = float(t)
    if v <= 0:
        raise ValueError(t)
    return round(v, 2)


def money(v: float | None) -> str:
    return "-" if v is None else f"{v:,.2f}"


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #
def match(slips: dict[str, float | None], bills: dict[str, float | None],
          picks: dict[str, str | None] | None = None) -> dict:
    """slips {slip_id: amount}, bills {bill_id: price} (both in upload order; None = unknown / not typed).
    picks {bill_id: slip_id or "" (= no pair)} are the user's answers for duplicated amounts.

    -> {'pairs': [(bill_id, slip_id)] in slip order,
        'groups': [{'amount', 'slips', 'bills'}] amounts with more than one slip or bill (the user picks),
        'open_groups': amounts of groups still waiting for a pick,
        'auto_picked': {bill_id: slip_id} filled in because only one choice was left,
        'conflicts': {amount: [slip_id]} slips chosen for two bills,
        'no_slip': [bill_id] price typed but no slip has that amount,
        'blank': [bill_id] no price yet}"""
    picks = picks or {}
    by_amount_s: dict[float, list[str]] = {}
    for sid, a in slips.items():
        if a is not None:
            by_amount_s.setdefault(round(a, 2), []).append(sid)
    by_amount_b: dict[float, list[str]] = {}
    blank = []
    for bid, p in bills.items():
        if p is None:
            blank.append(bid)
        else:
            by_amount_b.setdefault(round(p, 2), []).append(bid)

    pair_of_slip: dict[str, str] = {}
    groups, open_groups, conflicts, auto_picked, no_slip = [], [], {}, {}, []
    for amount, bids in by_amount_b.items():
        sids = by_amount_s.get(amount, [])
        if not sids:
            no_slip.extend(bids)
            continue
        if len(sids) == 1 and len(bids) == 1:
            pair_of_slip[sids[0]] = bids[0]
            continue
        groups.append({"amount": amount, "slips": sids, "bills": bids})
        chosen = {b: picks.get(b) for b in bids if picks.get(b) is not None}
        used = [s for s in chosen.values() if s]
        dup = sorted({s for s in used if used.count(s) > 1})
        if dup:
            conflicts[amount] = dup
            open_groups.append(amount)
            continue
        waiting = [b for b in bids if b not in chosen]
        free = [s for s in sids if s not in used]
        # one bill left and one slip left: nothing to choose
        if len(waiting) == 1 and len(free) == 1 and len(sids) == len(bids):
            chosen[waiting[0]] = free[0]
            auto_picked[waiting[0]] = free[0]
            waiting = []
        if waiting:
            open_groups.append(amount)
        for b, s in chosen.items():
            if s:
                pair_of_slip[s] = b
    pairs = [(pair_of_slip[s], s) for s in slips if s in pair_of_slip]
    return {"pairs": pairs, "groups": groups, "open_groups": open_groups, "auto_picked": auto_picked,
            "conflicts": conflicts, "no_slip": no_slip, "blank": blank}


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #
PAGE_W, PAGE_H = 595.5, 842.25       # A4 in points, as the user's Canva PDFs
SINGLE_BOX = (70, 55, 525.5, 787)    # slip alone: centred in this box
BILL_MAX = (20, 20, 420, 580)        # handwritten bill: from the top-left corner, at most this box
SLIP_MAX = (215, 262, 575, 822)      # slip next to a bill: from the bottom-right corner, at most this box
OVERLAP = 25                         # the slip may cover this much of the bill's edge (photos have margins)


def _fit(size: tuple[int, int], box: tuple[float, float, float, float], anchor: str) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0, y1 - y0
    w, h = size
    s = min(bw / w, bh / h)
    w, h = w * s, h * s
    if anchor == "center":
        return x0 + (bw - w) / 2, y0 + (bh - h) / 2, w, h
    if anchor == "top-left":
        return x0, y0, w, h
    return x1 - w, y1 - h, w, h     # bottom-right


def pair_layout(bill_size: tuple[int, int], slip_size: tuple[int, int]) -> tuple[tuple, tuple]:
    """(x, y, w, h) of the bill (top-left) and the slip (bottom-right): as big as possible while they sit
    side by side or one above the other (overlapping at most OVERLAP points)."""
    bx, by, bw, bh = _fit(bill_size, BILL_MAX, "top-left")
    sx, sy, sw, sh = _fit(slip_size, SLIP_MAX, "bottom-right")
    right, bottom = SLIP_MAX[2], SLIP_MAX[3]
    # shrink both by f until bill.right <= slip.left (+OVERLAP) or bill.bottom <= slip.top (+OVERLAP)
    f_side = (right - bx + OVERLAP) / (bw + sw)
    f_stack = (bottom - by + OVERLAP) / (bh + sh)
    f = min(1.0, max(f_side, f_stack))
    bill = (bx, by, bw * f, bh * f)
    slip = (right - sw * f, bottom - sh * f, sw * f, sh * f)
    return bill, slip


def page_plan(slip_ids: list[str], pairs: list[tuple[str, str]], extra_bills: list[str] = ()) -> list[tuple]:
    """Pages in order: ('slip', slip_id) for slips without a bill, then ('pair', bill_id, slip_id),
    then ('bill', bill_id) for extra_bills (unpaired handwritten bills the user asked to include)."""
    paired = {s for _, s in pairs}
    pages: list[tuple] = [("slip", s) for s in slip_ids if s not in paired]
    pages += [("pair", b, s) for b, s in pairs]
    pages += [("bill", b) for b in extra_bills]
    return pages


def build_pdf(pages: list[tuple], images: dict[str, bytes], longest: int = 1400) -> bytes:
    """pages from page_plan(); images {id: original picture bytes}."""
    from fpdf import FPDF

    pdf = FPDF(unit="pt", format=(PAGE_W, PAGE_H))
    pdf.set_auto_page_break(False)
    pdf.set_margins(0, 0, 0)
    pdf.set_draw_color(200, 200, 200)
    pdf.set_line_width(0.5)
    cache: dict[str, tuple[bytes, tuple[int, int]]] = {}

    def pic(i: str):
        if i not in cache:
            data = jpeg(images[i], longest)
            cache[i] = (data, Image.open(io.BytesIO(data)).size)
        return cache[i]

    def place(i: str, rect, frame: bool = False) -> None:
        x, y, w, h = rect
        pdf.image(io.BytesIO(pic(i)[0]), x=x, y=y, w=w, h=h)
        if frame:
            pdf.rect(x, y, w, h)

    for page in pages:
        pdf.add_page()
        if page[0] in ("slip", "bill"):
            place(page[1], _fit(pic(page[1])[1], SINGLE_BOX, "center"))
        else:
            _, bill, slip = page
            rb, rs = pair_layout(pic(bill)[1], pic(slip)[1])
            place(bill, rb)
            place(slip, rs, frame=True)   # on top, with a thin grey edge where it meets the bill
    return bytes(pdf.output())
