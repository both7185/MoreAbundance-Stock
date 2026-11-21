"""
จับคู่บิลโอนเงิน (Electronic bill / สลิปโอนเงิน) กับบิลเขียนมือ — the logic behind bill_match_page.py.

* All pictures come in one upload (pictures and/or .zip files, any folder depth), in natural file-name order.
* analyze() reads every picture once with RapidOCR (PP-OCR, onnxruntime; it also reads handwritten digits) or,
  when that is not installed, with Tesseract:
    - a picture with a masked account number (xxx-x-x9503-x) and a money amount is a transfer slip; its amount is
      the first amount on it (5,082.00).
    - anything else is a handwritten bill / receipt; all numbers on it are kept. guess_bill_amount() then takes the
      largest of them that equals a slip amount (a 1-digit misread is accepted when only one slip fits).
  The user can switch a picture's kind and correct every amount.
* suggest(): a bill whose amount equals exactly one free slip (and no other free bill) is proposed as a pair; an
  amount shared by several slips/bills is proposed with all its slips for the user to pick. The user confirms pairs;
  confirmed pairs stay editable on the page.
* build_pdf(): A4 portrait (595.5 x 842.25 pt, like the user's Canva files). A slip without a handwritten bill
  gets a page of its own (centred); a pair gets the handwritten bill top-left and the slip bottom-right.
  The two are as big as fits without covering each other (side by side or one above the other).
  Without order files: unpaired slips come first, then the pairs, each in upload order.
* Order files (optional, e.g. 16-11.pdf, the ใบรับรองแทนใบเสร็จ made in Google Sheets): parse_order_pdf() reads
  their expense lines; plan_by_files() puts each pair / slip on the line with its amount, so every order file gets a
  PDF of its own with the receipts in the file's order. An amount found in several files is left for the user to
  choose.

Nothing is saved anywhere.
"""
from __future__ import annotations

import hashlib
import io
import math
import os
import re
import shutil
import subprocess
import threading
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

    def from_zip(name: str, data: bytes, depth: int = 0) -> None:
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            notes.append(f"เปิดไฟล์ zip {name} ไม่ได้")
            return
        inner, zips = [], []
        for info in zf.infolist():
            n = _zip_name(info)
            base = n.rsplit("/", 1)[-1]
            if info.is_dir() or "__MACOSX" in n or base.startswith("."):
                continue
            if base.lower().endswith(IMAGE_EXT):
                inner.append((base, zf.read(info)))
            elif base.lower().endswith(".zip") and depth < 3:     # a zip inside the zip
                zips.append((base, zf.read(info)))
        if not inner and not zips:
            notes.append(f"ใน {name} ไม่มีไฟล์รูป")
        found.extend(sorted(inner, key=lambda x: natural_key(x[0])))
        for zname, zdata in sorted(zips, key=lambda x: natural_key(x[0])):
            from_zip(zname, zdata, depth + 1)

    for name, data in files:
        if name.lower().endswith(".zip"):
            from_zip(name, data)
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
# Reading every picture: slip or bill, and its amount(s)
# --------------------------------------------------------------------------- #
_MASK = re.compile(r"[xX×]{3}\s*-\s*[xX×\d]")            # masked account number on a bank slip
_NUMBER = re.compile(r"\d[\d,.]*\d|\d")
_SLIP_WORDS = ("สำเร็จ", "สําเร็จ", "ธรรมเนียม", "เลขที่รายการ", "หมายเลขอ้างอิง", "บันทึกช่วยจ", "ตรวจสอบสลิป",
               "THB", "WARINLADA", "KSA0")
_rapid = None
_rapid_lock = threading.Lock()


def rapid_engine():
    """The RapidOCR engine (loaded once), or None when the package is not installed."""
    global _rapid
    if _rapid is None:
        try:
            from rapidocr import RapidOCR
            _rapid = RapidOCR(params={"Global.log_level": "error"})
        except Exception:
            _rapid = False
    return _rapid or None


def reader_name() -> str:
    """Which OCR will be used: 'rapid', 'tesseract' or '' (none: everything is typed by hand)."""
    if rapid_engine():
        return "rapid"
    return "" if ocr_ready() else "tesseract"


def numbers_in(text: str) -> list[float]:
    """Every number written on a bill: 8,460 / 4,540 / 2880.00 / 322.00 -> 8460, 4540, 2880, 322."""
    out = []
    for m in _NUMBER.finditer(text):
        t = m.group(0)
        cents = 0
        c = re.search(r"[.,](\d\d)$", t)
        if c and len(t) > 4:
            cents = int(c.group(1))
            t = t[:c.start()]
        digits = re.sub(r"\D", "", t)
        if digits and len(digits) <= 7:
            out.append(int(digits) + cents / 100)
    return out


def _rapid_text(img: Image.Image) -> str:
    import numpy as np
    w, h = img.size
    s = 1280 / max(w, h)
    if s < 1:
        img = img.resize((round(w * s), round(h * s)), Image.LANCZOS)
    with _rapid_lock:
        r = rapid_engine()(np.array(img))
    return "\n".join(r.txts or [])


def _tess_text(img: Image.Image) -> str:
    """Tesseract fallback: Thai+English on the dark-text mask, plus digits from the plain picture."""
    w, h = img.size
    img = img.resize((1600, max(1, round(h * 1600 / w))), Image.LANCZOS)
    r, g, b = img.split()
    top = ImageChops.lighter(ImageChops.lighter(r, g), b).point(lambda v: 255 if v > 130 else 0)
    return _tess(top, "tha+eng") + "\n" + _tess(img.convert("L"), "eng")


def analyze(data: bytes, how: str | None = None) -> dict:
    """{'kind': 'slip' | 'bill', 'amount': slip amount or None, 'sure': bool, 'numbers': [numbers on a bill]}."""
    how = reader_name() if how is None else how
    if not how:
        return {"kind": "bill", "amount": None, "sure": False, "numbers": []}
    img = open_image(data)
    text = _rapid_text(img) if how == "rapid" else _tess_text(img)
    amounts = amounts_in(text)
    words = sum(1 for k in _SLIP_WORDS if k in text)
    if amounts and (_MASK.search(text) or words >= 3):
        found = [float(a) for a in amounts]
        amount = found[0]
        sure = len(set(found)) == 1
        if not sure and ocr_ready() == "":
            r = read_amount(data)            # Tesseract votes when the slip shows more than one amount
            if r["amount"] is not None:
                amount, sure = r["amount"], r["sure"]
        return {"kind": "slip", "amount": amount, "sure": sure, "numbers": []}
    return {"kind": "bill", "amount": None, "sure": False, "numbers": numbers_in(text)}


def _one_digit_off(a: str, b: str) -> bool:
    return len(a) == len(b) and sum(x != y for x, y in zip(a, b)) == 1


def guess_bill_amount(numbers: list[float], slip_amounts: list[float]) -> tuple[float | None, str]:
    """(amount, 'exact' | 'near' | '') — the bill total, chosen among the numbers read on the bill by what the slips
    say: the largest number that is a slip amount; otherwise a number 1 digit away from exactly one slip amount."""
    by_whole: dict[int, float] = {}
    for a in slip_amounts:
        if a is not None:
            by_whole.setdefault(int(a), a)
    exact = [by_whole[int(n)] for n in numbers if int(n) in by_whole and int(n) >= 10]
    if exact:
        return max(exact), "exact"
    near = {by_whole[w] for n in numbers if int(n) >= 100
            for w in by_whole if _one_digit_off(str(int(n)), str(w))}
    if len(near) == 1:
        return near.pop(), "near"
    return None, ""


def analyze_all(datas: list[bytes], progress=None) -> list[dict]:
    """analyze() for many pictures. RapidOCR already uses every CPU, so it runs one at a time; Tesseract runs 4."""
    how = reader_name()
    out: list[dict | None] = [None] * len(datas)
    with ThreadPoolExecutor(max_workers=1 if how == "rapid" else 4) as ex:
        futs = {ex.submit(analyze, d, how): i for i, d in enumerate(datas)}
        for n, f in enumerate(futs, 1):
            try:
                out[futs[f]] = f.result()
            except Exception:
                out[futs[f]] = {"kind": "bill", "amount": None, "sure": False, "numbers": []}
            if progress:
                progress(n, len(datas))
    return out


# --------------------------------------------------------------------------- #
# Suggested pairs
# --------------------------------------------------------------------------- #
def same_amount(a: float | None, b: float | None) -> bool:
    return a is not None and b is not None and round(a, 2) == round(b, 2)


def suggest(slips: dict[str, float | None], bills: dict[str, float | None], confirmed: dict[str, str],
            rejected: set[tuple[str, str]] = frozenset()) -> dict:
    """slips {slip_id: amount} and bills {bill_id: amount} in upload order; confirmed {bill_id: slip_id};
    rejected {(bill_id, slip_id)} pairs the user said are wrong.

    -> {'pairs': [(bill_id, slip_id)] one bill, one slip with that amount,
        'choices': [(bill_id, [slip_id, ...])] the amount is shared: the user picks the slip,
        'free_bills': [bill_id] no slip with that amount (or no amount yet), 'free_slips': [slip_id]}"""
    taken = set(confirmed.values())
    free_s = [s for s in slips if s not in taken]
    free_b = [b for b in bills if b not in confirmed]
    cands = {b: [s for s in free_s if same_amount(bills[b], slips[s]) and (b, s) not in rejected] for b in free_b}
    want: dict[str, list[str]] = {}
    for b, ss in cands.items():
        for s in ss:
            want.setdefault(s, []).append(b)
    pairs, choices = [], []
    for b in free_b:
        ss = cands[b]
        if not ss:
            continue
        if len(ss) == 1 and len(want[ss[0]]) == 1:
            pairs.append((b, ss[0]))
        else:
            choices.append((b, ss))
    offered = {b for b, _ in pairs} | {b for b, _ in choices}
    offered_s = {s for _, s in pairs} | {s for _, ss in choices for s in ss}
    return {"pairs": pairs, "choices": choices,
            "free_bills": [b for b in free_b if b not in offered],
            "free_slips": [s for s in free_s if s not in offered_s]}


def pairs_of(confirmed: dict[str, str], slip_ids: list[str], bill_ids: list[str]) -> list[tuple[tuple[str, ...], str]]:
    """Confirmed pairs in slip upload order: [((bill_id, ...), slip_id)]. Usually one bill; several bills of one shop
    that add up to one transfer make a group (the bills in upload order)."""
    out = []
    for s in slip_ids:
        bs = tuple(b for b in bill_ids if confirmed.get(b) == s)
        if bs:
            out.append((bs, s))
    return out


def _bills(b) -> tuple[str, ...]:
    return tuple(b) if isinstance(b, (tuple, list)) else (b,)


def bill_candidates(typed: float | None, numbers: list[float], below: float | None = None, top: int = 6) -> list[float]:
    """The amounts a bill may stand for in a group: the typed amount, or else the largest numbers read on it."""
    if typed is not None:
        return [typed]
    vals = sorted({round(n, 2) for n in numbers if n >= 20 and (below is None or n < below)}, reverse=True)
    return vals[:top]


def find_groups(target: float | None, cands: dict[str, list[float]], sizes: tuple[int, ...] = (2, 3),
                limit: int = 3, max_bills: int = 30) -> list[list[tuple[str, float]]]:
    """Sets of 2-3 bills whose amounts add up to target (one transfer paying several bills of one shop):
    [[(bill_id, amount), ...]]. cands {bill_id: [possible amounts, likeliest first]} from bill_candidates().
    Only the likeliest sets are kept: those using each bill's likeliest amount (its largest number, usually the
    total), or as close to that as any set gets."""
    from itertools import combinations, product

    if target is None:
        return []
    ids = [b for b in cands if cands[b]][:max_bills]
    found = []
    for k in sizes:
        for combo in combinations(ids, k):
            best = None
            for idx in product(*(range(len(cands[b])) for b in combo)):
                vals = [cands[b][i] for b, i in zip(combo, idx)]
                if round(sum(vals), 2) == round(target, 2) and (best is None or sum(idx) < best[0]):
                    best = (sum(idx), list(zip(combo, vals)))
            if best:
                found.append((best[0], k, best[1]))
    found.sort(key=lambda t: (t[0], t[1]))
    return [f[2] for f in found if f[0] == found[0][0]][:limit]     # only the likeliest ones


# --------------------------------------------------------------------------- #
# Order files (ใบปะหน้า / ใบรับรองแทนใบเสร็จ): one output PDF per file, receipts in the file's order
# --------------------------------------------------------------------------- #
_MONEY_WORD = re.compile(r"^\d{1,3}(?:,\d{3})*\.\d{2}$|^\d+\.\d{2}$")
_DATE_WORD = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$")


def parse_order_pdf(data: bytes) -> dict:
    """The expense lines of a text PDF (e.g. the Google Sheets ใบรับรองแทนใบเสร็จรับเงิน):
    {'lines': [{'name', 'amount'}] in the file's order, 'total': float | None, 'note': str}.
    A line is a money amount (2,200.00) with the words to its left on the same row; the รวม row and below are skipped."""
    import pymupdf

    lines, total, note = [], None, ""
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as e:
        return {"lines": [], "total": None, "note": f"เปิดไฟล์ไม่ได้ ({e})"}
    for page in doc:
        words = [w for w in page.get_text("words") if w[4].strip()]
        stop = None
        for w in words:
            if w[4].startswith("รวม"):
                stop = min(stop or w[1], w[1])
        for w in sorted(words, key=lambda w: (round(w[1]), w[0])):
            if not _MONEY_WORD.match(w[4]):
                continue
            mid = (w[1] + w[3]) / 2
            if stop is not None and w[1] >= stop - 2:
                if total is None and abs(w[1] - stop) < 6:
                    total = float(w[4].replace(",", ""))
                continue
            name_words = [v for v in words if v is not w and abs((v[1] + v[3]) / 2 - mid) < 6 and v[2] <= w[0] + 1
                          and not _MONEY_WORD.match(v[4]) and not _DATE_WORD.match(v[4]) and v[4] != "รอบส่ง"]
            name = " ".join(v[4] for v in sorted(name_words, key=lambda v: v[0]))
            lines.append({"name": name, "amount": float(w[4].replace(",", ""))})
    if not lines:
        note = "ไม่พบรายการที่มีจำนวนเงินในไฟล์ (ต้องเป็น PDF ที่มีข้อความ ไม่ใช่รูปสแกน)"
    elif total is not None and abs(sum(l["amount"] for l in lines) - total) > 0.01:
        note = f"ยอดรวมของรายการ ({sum(l['amount'] for l in lines):,.2f}) ไม่เท่ากับรวมทั้งสิ้นในไฟล์ ({total:,.2f})"
    return {"lines": lines, "total": total, "note": note}


def order_units(slips: dict[str, float | None], bills: dict[str, float | None],
                pairs: list[tuple]) -> list[dict]:
    """What goes on the pages, in slip upload order: a confirmed pair (or group), a slip alone, or a bill alone
    -> [{'id', 'amount', 'page'}]. Bills alone come last. pairs [(bill_id or (bill_id, ...), slip_id)]."""
    paired_s = {s: _bills(b) for b, s in pairs}
    paired_b = {b for bs in paired_s.values() for b in bs}
    units = []
    for s, a in slips.items():
        if s in paired_s:
            units.append({"id": s, "amount": a, "page": ("pair", paired_s[s], s)})
        else:
            units.append({"id": s, "amount": a, "page": ("slip", s)})
    for b, a in bills.items():
        if b not in paired_b:
            units.append({"id": b, "amount": a, "page": ("bill", b)})
    return units


def plan_by_files(units: list[dict], files: list[dict], chosen: dict[str, str | None] | None = None) -> dict:
    """Spread the units over the order files by amount.

    units from order_units(); files [{'key', 'lines': [{'name', 'amount'}]}]; chosen {unit_id: file key, or '' for
    none} — the user's answers (they win over the automatic choice).
    A unit goes to the file whose line has its amount. Bills alone only stand in for an amount no slip has.
    When an amount is in several files, or there are more units than lines for it, the user is asked.

    -> {'pages': {key: [page]} in line order (units placed by hand without a line go last),
        'missing': {key: [line]} lines no unit fills, 'rows': {key: [{'name', 'amount', 'unit'}]} each line with the
        unit on it (name None: a unit placed by hand without a line), 'ask': [(unit_id, [file keys])] still to be answered,
        'options': {unit_id: [file keys]} for every asked unit, 'unused': [unit_id] in no file}"""
    chosen = chosen or {}
    slip_amounts = {round(u["amount"], 2) for u in units if u["page"][0] != "bill" and u["amount"] is not None}
    lines_at: dict[float, list[str]] = {}
    for f in files:
        for ln in f["lines"]:
            lines_at.setdefault(round(ln["amount"], 2), []).append(f["key"])
    by_amount: dict[float, list[dict]] = {}
    for u in units:
        if u["amount"] is None:
            continue
        a = round(u["amount"], 2)
        if u["page"][0] == "bill" and a in slip_amounts:
            continue                          # a bill alone never takes a slip's line
        by_amount.setdefault(a, []).append(u)

    target: dict[str, str] = {}
    options: dict[str, list[str]] = {}
    for a, us in by_amount.items():
        keys = lines_at.get(a, [])
        if not keys:
            continue
        distinct = list(dict.fromkeys(keys))
        if len(distinct) == 1 and len(us) <= len(keys):
            for u in us:
                target[u["id"]] = distinct[0]
        else:
            for u in us:
                options[u["id"]] = distinct
    ask = []
    for u in units:
        c = chosen.get(u["id"])
        if c is not None:
            if c:
                target[u["id"]] = c
            else:
                target.pop(u["id"], None)
        elif u["id"] in options:
            ask.append((u["id"], options[u["id"]]))

    pages: dict[str, list] = {}
    missing: dict[str, list] = {}
    rows: dict[str, list] = {}
    unit_by_id = {u["id"]: u for u in units}
    for f in files:
        mine = [unit_by_id[i] for i in unit_by_id if target.get(i) == f["key"]]
        used: set[str] = set()
        out, miss, rw = [], [], []
        for ln in f["lines"]:
            hit = next((u for u in mine if u["id"] not in used and u["amount"] is not None
                        and round(u["amount"], 2) == round(ln["amount"], 2)), None)
            if hit:
                used.add(hit["id"])
                out.append(hit["page"])
            else:
                miss.append(ln)
            rw.append({"name": ln["name"], "amount": ln["amount"], "unit": hit["id"] if hit else None})
        for u in mine:
            if u["id"] not in used:
                out.append(u["page"])
                rw.append({"name": None, "amount": u["amount"], "unit": u["id"]})
        pages[f["key"]] = out
        missing[f["key"]] = miss
        rows[f["key"]] = rw
    unused = [u["id"] for u in units if u["id"] not in target and u["id"] not in dict(ask)]
    return {"pages": pages, "missing": missing, "rows": rows, "ask": ask, "options": options, "unused": unused}


def with_cover(cover: bytes, body: bytes) -> bytes:
    """The order file's own pages first, then the receipts."""
    import pymupdf

    out = pymupdf.open(stream=cover, filetype="pdf")
    out.insert_pdf(pymupdf.open(stream=body, filetype="pdf"))
    return out.tobytes(garbage=3, deflate=True)


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #
PAGE_W, PAGE_H = 595.5, 842.25       # A4 in points, as the user's Canva PDFs
SINGLE_BOX = (70, 55, 525.5, 787)    # slip alone: centred in this box
MARGIN, GAP = 18, 10                 # pair pages: page margin and the space between the two pictures


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
    """(x, y, w, h) of the bill (top-left) and the slip (bottom-right), never covering each other: the page is split
    side by side or top/bottom, at the split where the two pictures are largest together (the product of their
    areas, so neither ends up tiny)."""
    x0, y0, x1, y1 = MARGIN, MARGIN, PAGE_W - MARGIN, PAGE_H - MARGIN
    best = None
    for split in range(20, 81):
        t = split / 100
        for side in (True, False):
            if side:
                cut = x0 + (x1 - x0 - GAP) * t
                rb = _fit(bill_size, (x0, y0, cut, y1), "top-left")
                rs = _fit(slip_size, (cut + GAP, y0, x1, y1), "bottom-right")
            else:
                cut = y0 + (y1 - y0 - GAP) * t
                rb = _fit(bill_size, (x0, y0, x1, cut), "top-left")
                rs = _fit(slip_size, (x0, cut + GAP, x1, y1), "bottom-right")
            score = rb[2] * rb[3] * rs[2] * rs[3]       # product of the two areas: big, and balanced
            if best is None or score > best[0]:
                best = (score, rb, rs)
    return best[1], best[2]


def group_layout(bill_sizes: list[tuple[int, int]], slip_size: tuple[int, int]) -> tuple[list[tuple], tuple]:
    """Several handwritten bills and their one transfer slip on a page, none covering another: the bills in a grid
    (e.g. two side by side on top) and the slip below at the right, or all in one grid with the slip in the
    bottom-right cell. The arrangement where the pictures come out largest wins (the slip weighs as much as all the bills)."""
    n = len(bill_sizes)
    x0, y0, x1, y1 = MARGIN, MARGIN, PAGE_W - MARGIN, PAGE_H - MARGIN

    def cells(box, cols, rows):
        bx0, by0, bx1, by1 = box
        cw = (bx1 - bx0 - GAP * (cols - 1)) / cols
        ch = (by1 - by0 - GAP * (rows - 1)) / rows
        return [(bx0 + c * (cw + GAP), by0 + r * (ch + GAP), bx0 + c * (cw + GAP) + cw, by0 + r * (ch + GAP) + ch)
                for r in range(rows) for c in range(cols)]

    def score(rb, rs):           # the slip counts as much as all the bills together, so it never ends up tiny
        return sum(math.log(max(r[2] * r[3], 1e-6)) for r in rb) / len(rb) + math.log(max(rs[2] * rs[3], 1e-6))

    best = None
    for cols in range(1, n + 1):                      # bills on top, slip below
        rows = -(-n // cols)
        for split in range(30, 86):
            cut = y0 + (y1 - y0 - GAP) * split / 100
            cs = cells((x0, y0, x1, cut), cols, rows)
            rb = [_fit(sz, c, "center") for sz, c in zip(bill_sizes, cs)]
            rs = _fit(slip_size, (x0, cut + GAP, x1, y1), "bottom-right")
            sc = score(rb, rs)
            if best is None or sc > best[0]:
                best = (sc, rb, rs)
    for cols in range(2, n + 2):                      # one grid, the slip in the last (bottom-right) cell
        rows = -(-(n + 1) // cols)
        cs = cells((x0, y0, x1, y1), cols, rows)
        rb = [_fit(sz, c, "center") for sz, c in zip(bill_sizes, cs)]
        rs = _fit(slip_size, cs[-1], "bottom-right")
        sc = score(rb, rs)
        if sc > best[0]:
            best = (sc, rb, rs)
    return best[1], best[2]


def page_plan(slip_ids: list[str], pairs: list[tuple], extra_bills: list[str] = ()) -> list[tuple]:
    """Pages in order: ('slip', slip_id) for slips without a bill, then ('pair', (bill_id, ...), slip_id),
    then ('bill', bill_id) for extra_bills (unpaired handwritten bills the user asked to include).
    pairs [(bill_id or (bill_id, ...), slip_id)]: several bills share one page with their transfer slip."""
    paired = {s for _, s in pairs}
    pages: list[tuple] = [("slip", s) for s in slip_ids if s not in paired]
    pages += [("pair", _bills(b), s) for b, s in pairs]
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
            bs = _bills(bill)
            if len(bs) == 1:
                rb, rs = pair_layout(pic(bs[0])[1], pic(slip)[1])
                rbs = [rb]
            else:
                rbs, rs = group_layout([pic(b)[1] for b in bs], pic(slip)[1])
            for b, rb in zip(bs, rbs):
                place(b, rb)
            place(slip, rs, frame=True)   # thin grey edge: the white slip stays visible on white paper
    return bytes(pdf.output())
