"""
Read two price lists and line up the same product (page ⚖️ เทียบราคา, UI in compare_page.py).

    ไฟล์ราคา จปร              -> รายการ, หน่วย, ราคาพค, ราคาซื้อ
    ไฟล์ราคากลางบ้านราชวิถี   -> รายการ, ราคากลาง/หน่วย, หน่วย

Reading a file (Excel .xlsx: the header row gives the columns, see read_price_excel)
  * text PDF (e.g. printed from Google Sheets): words + positions from PyMuPDF. The columns come from the
    header words of each page (รายการ / หน่วย / ราคาพค / ราคาซื้อ / ราคากลาง), so the column positions may
    change from page to page.
  * scanned PDF (pages are only pictures): OCR with Tesseract (Thai). Each page is turned upright,
    straightened, the table lines are found, and each column is read on its own (names and units in Thai,
    prices as digits only). Needs the `tesseract` program with Thai data — on Streamlit Cloud it comes from
    packages.txt; on a PC without it only text PDFs can be read.

Matching
  the same name (ignoring spaces, products.norm) -> same row; otherwise names more than 70% similar are
  paired automatically (the most similar first, each item used once) and the user checks them on the page;
  the rest stay unpaired.
"""
from __future__ import annotations

import csv
import io
import re
import shutil
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher

import pymupdf

from po_extractor import THAI_DIGITS, normalize_thai
from products import norm

# price columns of each file: (column label shown on the page, words that identify its header)
JPR = {"key": "jpr", "title": "ราคา จปร", "prices": [("ราคาพค", ("พค",)), ("ราคาซื้อ", ("ซื้อ",))]}
RW = {"key": "rw", "title": "ราคากลางบ้านราชวิถี", "prices": [("ราคากลาง/หน่วย", ("กลาง",))]}

AUTO_MIN = 0.70         # names more similar than this are paired automatically (the user checks them)


class ReadError(RuntimeError):
    """The file cannot be read as a price list (message is shown to the user)."""


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def to_price(text: str) -> float | None:
    """'1,045' / '65.00' -> number; None when there is no number (a speck next to it is ignored)."""
    t = (text or "").translate(THAI_DIGITS).replace(",", "").replace(" ", "")
    m = re.search(r"\d+(?:\.\d+)?", t)
    return float(m.group()) if m else None


def tidy_unit(text: str) -> str:
    """'กก .' / '. กก .' / 'ขวด/ แกลลอน' (OCR) -> 'กก.' / 'ขวด/แกลลอน'."""
    s = re.sub(r"\.{2,}", ".", re.sub(r"\s+", "", normalize_thai(text or "")))
    return re.sub(r"[,:;'|\]\[]+$", "", re.sub(r"^[.,:;'|\]\[]+", "", s))


def tidy_name(text: str) -> str:
    """Thai text from PDF / OCR -> one clean line."""
    s = (text or "").replace("้้า", "้ำ")   # OCR reads น้ำ as น้้า
    s = normalize_thai(s)
    s = re.sub("([่-๋])\\1+", "\\1", s)        # a tone mark read twice
    s = re.sub(r"\(\s+", "(", s)
    s = re.sub(r"\s+\)", ")", s)
    s = re.sub(r"\s*[:;|_~]+\s*$", "", s)                 # specks at the end of a scanned cell
    return s.strip()


def _header_key(text: str) -> str:
    return re.sub(r"\s+", "", normalize_thai(text))


def _fuzzy_in(word: str, text: str, cutoff: float = 0.75) -> bool:
    """`word` appears in `text`, allowing an OCR mistake or two."""
    if word in text:
        return True
    n = len(word)
    return any(SequenceMatcher(None, word, text[i:i + n]).ratio() >= cutoff for i in range(max(1, len(text) - n + 1)))


def header_roles(texts: list[str], spec: dict) -> dict[int, str]:
    """Header text of each column -> {column index: 'no' | 'name' | 'unit' | price label}."""
    t = [_header_key(x) for x in texts]
    roles: dict[int, str] = {}
    for label, keys in spec["prices"]:
        def has_key(h: str) -> bool:
            return any(_fuzzy_in(k, h, 0.99 if len(k) <= 2 else 0.75) for k in keys)
        # 'ราคาซื้อ' rather than 'ปริมาณที่คาดว่าจะสั่งซื้อ': a header that also says ราคา wins
        found = next((i for i, h in enumerate(t) if i not in roles and has_key(h) and _fuzzy_in("ราคา", h)), None)
        if found is None:
            found = next((i for i, h in enumerate(t) if i not in roles and has_key(h)), None)
        if found is not None:
            roles[found] = label
    tests = (
        ("name", lambda h: _fuzzy_in("รายการ", h)),
        ("unit", lambda h: _fuzzy_in("หน่วย", h) and not _fuzzy_in("ราคา", h)),
        ("no", lambda h: _fuzzy_in("ลำดับ", h) or h == "ที่"),
    )
    for role, test in tests:
        for i, h in enumerate(t):
            if i not in roles and test(h):
                roles[i] = role
                break
    return roles


def _missing_roles(roles: dict[int, str], spec: dict) -> list[str]:
    need = {"name": "รายการ", "unit": "หน่วย", **{lab: lab for lab, _ in spec["prices"]}}
    return [th for role, th in need.items() if role not in roles.values()]


def _item(no: str, name: str, unit: str, prices: dict) -> dict:
    return {"no": no, "name": tidy_name(name), "unit": tidy_name(unit), "prices": prices}


# --------------------------------------------------------------------------- #
# Text PDFs
# --------------------------------------------------------------------------- #
def _lines(words: list) -> list[list]:
    """PyMuPDF words -> lines (top to bottom), each sorted left to right."""
    lines: list[tuple[float, float, list]] = []
    for w in sorted(words, key=lambda w: (w[1] + w[3]) / 2):
        yc, h = (w[1] + w[3]) / 2, w[3] - w[1]
        if lines and abs(yc - lines[-1][0]) <= max(3.0, 0.5 * min(h, lines[-1][1])):
            lines[-1][2].append(w)
        else:
            lines.append((yc, h, [w]))
    return [sorted(l[2], key=lambda w: w[0]) for l in lines]


def _header_cells(words: list) -> list[dict]:
    """Header words -> one cell per column (words close together, or one above the other, are one cell)."""
    cells: list[dict] = []
    for w in sorted(words, key=lambda w: w[0]):
        if cells and w[0] - cells[-1]["x1"] < 8:
            c = cells[-1]
            c["x1"] = max(c["x1"], w[2])
            c["words"].append(w)
        else:
            cells.append({"x0": w[0], "x1": w[2], "words": [w]})
    for c in cells:
        c["text"] = " ".join(w[4] for w in sorted(c["words"], key=lambda w: (round(w[1]), w[0])))
    return cells


def _bounds(cells: list[dict], roles: dict[int, str]) -> list[float]:
    """x where each column ends. Names are left-aligned and may start left of their header, so the
    column before the name ends right after its own header; numbers / units are centred."""
    out = []
    for i in range(len(cells) - 1):
        a, b = cells[i], cells[i + 1]
        if roles.get(i + 1) == "name":
            out.append(a["x1"] + 2)
        elif roles.get(i) == "name":
            out.append(b["x0"] - 2)
        else:
            out.append((a["x1"] + b["x0"]) / 2)
    return out + [float("inf")]


def parse_text_pdf(doc: pymupdf.Document, spec: dict) -> list[dict]:
    items: list[dict] = []
    layout = None                                   # (cells, roles, bounds) of the last header seen
    for page in doc:
        lines = _lines(page.get_text("words"))
        start = 0
        for i, line in enumerate(lines):
            texts = [w[4] for w in line]
            if any("รายการ" in t for t in texts) and any("ราคา" in t for t in texts):
                head = list(line)
                # a header may be two lines ("ราคากลาง/" + "หน่วย"); the next line is part of it if it
                # does not start with a number like a data row does
                if i + 1 < len(lines) and not re.fullmatch(r"\d+", lines[i + 1][0][4]) \
                        and (lines[i + 1][0][1] - line[0][3]) < 12:
                    head += lines[i + 1]
                    start = i + 2
                else:
                    start = i + 1
                cells = _header_cells(head)
                roles = header_roles([c["text"] for c in cells], spec)
                layout = (cells, roles, _bounds(cells, roles))
                break
        if layout is None:
            continue
        cells, roles, bounds = layout
        for line in lines[start:]:
            cols: dict[str, list[str]] = {}
            words = list(line)
            no = ""
            # the row number is the first word when it is only digits and stands apart from the name
            if len(words) > 1 and re.fullmatch(r"\d+", words[0][4]) and words[1][0] - words[0][2] > 4 \
                    and "no" in roles.values():
                no = words.pop(0)[4]
            for w in words:
                xc = (w[0] + w[2]) / 2
                col = next(k for k, b in enumerate(bounds) if xc < b)
                role = roles.get(col)
                if role:
                    cols.setdefault(role, []).append(w[4])
            if "no" in cols and not no:
                no = " ".join(cols.pop("no"))
            name = " ".join(cols.get("name", []))
            prices = {lab: to_price(" ".join(cols.get(lab, []))) for lab, _ in spec["prices"]}
            unit = " ".join(cols.get("unit", []))
            if not name:
                continue
            if not no and not unit and all(v is None for v in prices.values()) and items:
                items[-1]["name"] = tidy_name(items[-1]["name"] + " " + name)   # name wrapped onto 2 lines
                continue
            items.append(_item(no, name, unit, prices))
    if layout is None:
        raise ReadError("ไม่พบหัวตาราง (คำว่า 'รายการ' และ 'ราคา') ในไฟล์")
    missing = _missing_roles(layout[1], spec)
    if missing:
        raise ReadError("หาคอลัมน์ " + ", ".join(missing) + " ในหัวตารางไม่เจอ")
    return items


# --------------------------------------------------------------------------- #
# Scanned PDFs (OCR)
# --------------------------------------------------------------------------- #
def ocr_ready() -> str:
    """'' when Tesseract with Thai can be used, else why not."""
    if not shutil.which("tesseract"):
        return "เครื่องที่รันแอปไม่มีโปรแกรม Tesseract (ใช้อ่านไฟล์สแกน)"
    try:
        langs = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=30).stdout
    except Exception as e:
        return f"เรียก Tesseract ไม่ได้ ({e})"
    return "" if "tha" in langs.split() else "Tesseract ไม่มีภาษาไทย (tesseract-ocr-tha)"


def _png(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# one thread per Tesseract: several run side by side, and their own threads would fight over the CPU
_TESS_ENV = {**os.environ, "OMP_THREAD_LIMIT": "1"}


def _tess_words(img, lang: str, extra: tuple = ()) -> list[dict]:
    r = subprocess.run(["tesseract", "stdin", "stdout", "-l", lang, "--psm", "6", *extra, "tsv"],
                       input=_png(img), capture_output=True, timeout=180, env=_TESS_ENV)
    rows = csv.DictReader(io.StringIO(r.stdout.decode("utf-8", errors="ignore")), delimiter="\t",
                          quoting=csv.QUOTE_NONE)
    out = []
    for w in rows:
        try:
            if w["text"] and w["text"].strip() and float(w["conf"]) >= 0:
                out.append({"text": w["text"].strip(), "x0": int(w["left"]), "y0": int(w["top"]),
                            "x1": int(w["left"]) + int(w["width"]), "y1": int(w["top"]) + int(w["height"])})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _join(words: list[dict]) -> str:
    """OCR words of one cell (in Tesseract's reading order) -> text. Thai comes back almost letter by
    letter, with tone marks as separate pieces, so a space is kept only where the gap is wide."""
    if not words:
        return ""
    heights = sorted(w["y1"] - w["y0"] for w in words)
    gap = 0.4 * max(10, heights[len(heights) // 2])
    out, prev = words[0]["text"], words[0]
    for w in words[1:]:
        if w["x0"] - prev["x1"] > gap or w["x1"] < prev["x0"]:     # wide gap, or the next line
            out += " "
        out += w["text"]
        if w["x1"] > prev["x1"] or w["x1"] < prev["x0"]:
            prev = w
    return out


def _osd_rotation(img) -> int:
    """Degrees to turn the page clockwise so the text is upright (Tesseract OSD), 0 if unsure."""
    small = img.resize((img.width // 2, img.height // 2))
    try:
        r = subprocess.run(["tesseract", "stdin", "stdout", "--psm", "0"], input=_png(small),
                           capture_output=True, timeout=60, env=_TESS_ENV)
        m = re.search(r"Rotate:\s*(\d+)", r.stdout.decode(errors="ignore"))
        return int(m.group(1)) % 360 if m else 0
    except Exception:
        return 0


def _deskew(img):
    import numpy as np
    from PIL import Image
    small = img.resize((img.width // 4, img.height // 4))
    best, best_score = 0.0, -1.0
    for ang in np.arange(-2.0, 2.01, 0.1):
        a = np.asarray(small.rotate(float(ang), fillcolor=255)) < 140
        s = a.sum(1).astype(float)
        score = float((s * s).sum())
        if score > best_score:
            best, best_score = float(ang), score
    return img.rotate(best, fillcolor=255, resample=Image.BICUBIC) if abs(best) > 0.05 else img


def _runs(idx, gap: int = 4) -> list[int]:
    """Indexes of dark rows / columns -> the middle of each run (one per table line)."""
    out: list[list[int]] = []
    for i in idx:
        if out and i - out[-1][-1] <= gap:
            out[-1].append(int(i))
        else:
            out.append([int(i)])
    return [(g[0] + g[-1]) // 2 for g in out]


def _grid(img):
    """Table lines of an upright page -> (column x edges, rows [(y0, y1)] from the top, header row first
    when the page has one) or None when no table is found."""
    import numpy as np
    a = np.asarray(img) < 140
    h, w = a.shape
    xs = _runs(np.where(a.sum(0) / h > 0.35)[0])
    if len(xs) < 4:
        return None
    best: list[int] = []
    for x0, x1 in zip(xs, xs[1:]):                   # row lines, found inside each column (no skew there)
        strip = a[:, x0 + 8:x1 - 8]
        if strip.shape[1] < 20:
            continue
        ys = _runs(np.where(strip.sum(1) / strip.shape[1] > 0.8)[0])
        if len(ys) > len(best):
            best = ys
    # at 300 dpi one line of text is ~110 px high; a gap under 60 px is a double border line
    rows = [(y0, y1) for y0, y1 in zip(best, best[1:]) if 60 <= y1 - y0 <= 900]
    return (xs, rows) if rows else None


def _is_header(text: str) -> bool:
    t = _header_key(text)
    return _fuzzy_in("รายการ", t) or (_fuzzy_in("ราคา", t) and _fuzzy_in("หน่วย", t))


_RARE = set("ฃฅฆฌฎฏฐฑฒณษฬ")
_THAI_CONS = set(chr(c) for c in range(0x0E01, 0x0E2F))


def ocr_name(text: str) -> str:
    """Tidy a product name read by OCR: specks / stamp marks at the start or end, common misreadings."""
    s = tidy_name(text)
    s = re.sub(r"น้\s?า", "น้ำ", s)                  # ำ is often read as า (น้ำ is in many names; น้า is in none)
    s = s.replace("ตำกว่า", "ต่ำกว่า").replace("กะหลำ", "กะหล่ำ").replace("กะหล้า", "กะหล่ำ")
    s = re.sub(r"^[\u0E50-\u0E59=\-_|.,:;'\"\[\]0-9\s]+", "", s)   # marks before the first letter
    tokens = s.split(" ")

    def bare(t: str) -> str:
        return re.sub(r"[^\u0E00-\u0E7Fa-zA-Z0-9]", "", t)

    def junk_start(t: str) -> bool:
        b = bare(t)
        return not b or len(b) == 1 or (len(b) <= 3 and bool(set(b) & _RARE)) or re.fullmatch("[0-9\u0E50-\u0E59]+", b) is not None

    def junk_end(t: str) -> bool:
        b = bare(t)
        return not b or len(b) == 1 or (len(b) <= 2 and set(b) <= _THAI_CONS) or (len(b) <= 3 and bool(set(b) & _RARE))

    while len(tokens) > 1 and junk_start(tokens[0]):
        tokens.pop(0)
    while len(tokens) > 1 and junk_end(tokens[-1]):
        tokens.pop()
    return " ".join(tokens).strip()


def _read_column(img, x0: int, x1: int, spans: list[tuple[int, int]], lang: str, extra: tuple = ()) -> list[str]:
    """OCR the cells of one column in one go (cells stacked with white space between them)."""
    from PIL import Image
    pieces = [img.crop((x0 + 10, y0 + 8, x1 - 10, y1 - 8)) for y0, y1 in spans]
    width = max(p.width for p in pieces) + 40
    canvas = Image.new("L", (width, sum(p.height + 40 for p in pieces) + 20), 255)
    y, slots = 20, []
    for p in pieces:
        canvas.paste(p, (20, y))
        slots.append((y - 20, y + p.height + 20))
        y += p.height + 40
    cells: list[list[dict]] = [[] for _ in spans]
    for wd in _tess_words(canvas, lang, extra):
        cy = (wd["y0"] + wd["y1"]) / 2
        for i, (s, e) in enumerate(slots):
            if s <= cy < e:
                cells[i].append(wd)
                break
    return [_join(c) for c in cells]


def _page_image(page: pymupdf.Page):
    """Page -> greyscale picture at 300 dpi, with red / blue ink (stamps, signatures) made white."""
    import numpy as np
    from PIL import Image
    pix = page.get_pixmap(dpi=300, colorspace=pymupdf.csRGB, alpha=False)
    rgb = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3).astype(np.int16)
    grey = rgb.mean(axis=2)
    coloured = (rgb.max(axis=2) - rgb.min(axis=2)) > 60
    grey[coloured] = 255
    return Image.fromarray(grey.astype(np.uint8), "L")


def parse_scanned_pdf(doc: pymupdf.Document, spec: dict, progress=None) -> tuple[list[dict], list[str]]:
    why = ocr_ready()
    if why:
        raise ReadError(f"ไฟล์นี้เป็นภาพสแกน ต้องอ่านด้วย OCR แต่{why} — บนเว็บ (Streamlit Cloud) อ่านได้ "
                        "หรือใช้ไฟล์ PDF ที่เลือกข้อความได้แทน")
    items: list[dict] = []
    notes: list[str] = []
    rotation = None
    layout = None                                    # column edges + roles, from the first header found
    for pno, page in enumerate(doc, start=1):
        if progress:
            progress(pno - 1, len(doc))
        base = _page_image(page)
        found = None
        tries = [rotation] if rotation is not None else []
        for rot in tries + ["osd"]:
            r = _osd_rotation(base) if rot == "osd" else rot
            img = _deskew(base.rotate(-r, expand=True) if r else base)
            found = _grid(img)
            if found:
                rotation = r
                break
        if not found:
            notes.append(f"หน้า {pno}: ไม่พบตาราง — ข้ามหน้านี้")
            continue
        xs, rows = found
        y0, y1 = rows[0]
        if _is_header(_join(_tess_words(img.crop((xs[0], y0 + 8, xs[-1], y1 - 8)), "tha"))):
            if layout is None or len(layout[0]) != len(xs):
                heads = [_join(_tess_words(img.crop((a + 8, y0 + 8, b - 8, y1 - 8)), "tha"))
                         for a, b in zip(xs, xs[1:])]
                roles = header_roles(heads, spec)
                missing = _missing_roles(roles, spec)
                if missing:
                    notes.append(f"หน้า {pno}: หาคอลัมน์ {', '.join(missing)} ในหัวตารางไม่เจอ — ข้ามหน้านี้")
                    continue
                layout = (xs, roles)
            rows = rows[1:]
        if layout is None or len(layout[0]) != len(xs):
            notes.append(f"หน้า {pno}: ไม่พบหัวตาราง — ข้ามหน้านี้")
            continue
        if not rows:
            continue
        col = {role: i for i, role in layout[1].items()}
        jobs = {"name": ("tha", ()), "unit": ("tha", ())}
        for lab, _ in spec["prices"]:
            jobs[lab] = ("eng", ("-c", "tessedit_char_whitelist=0123456789.,"))
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {role: pool.submit(_read_column, img, xs[col[role]], xs[col[role] + 1], rows, lang, extra)
                       for role, (lang, extra) in jobs.items()}
            got = {role: f.result() for role, f in futures.items()}
        for k in range(len(rows)):
            name, unit = ocr_name(got["name"][k]), tidy_unit(got["unit"][k])
            prices = {lab: to_price(got[lab][k]) for lab, _ in spec["prices"]}
            if not name or _is_header(name + " " + unit):
                continue
            items.append(_item("", name, unit, prices))
    if progress:
        progress(len(doc), len(doc))
    if not items:
        raise ReadError("อ่านตารางจากไฟล์สแกนไม่ได้" + (" — " + "; ".join(notes[:5]) if notes else ""))
    return items, notes


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def read_price_pdf(data: bytes, spec: dict, progress=None) -> dict:
    """-> {'items': [...], 'method': 'text' | 'ocr', 'notes': [...]}; raises ReadError."""
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as e:
        raise ReadError(f"เปิดไฟล์ PDF ไม่ได้: {e}") from e
    with doc:
        has_text = sum(len(p.get_text("text").strip()) for p in doc) > 20 * len(doc)
        if has_text:
            return {"items": parse_text_pdf(doc, spec), "method": "text", "notes": []}
        items, notes = parse_scanned_pdf(doc, spec, progress)
        return {"items": items, "method": "ocr", "notes": notes}


def read_price_excel(data: bytes, spec: dict) -> dict:
    """Excel (.xlsx): the first sheet with a header row (รายการ + ราคา) gives the columns; every row below it
    with a name is an item. -> same shape as read_price_pdf()."""
    import openpyxl
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    except Exception as e:
        raise ReadError(f"เปิดไฟล์ Excel ไม่ได้: {e}") from e

    def text(v) -> str:
        if v is None:
            return ""
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        return str(v).strip()

    def price(v) -> float | None:
        if isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            return float(v)
        return to_price(text(v))

    problem = "ไม่พบหัวตาราง (คำว่า 'รายการ' และ 'ราคา') ในไฟล์"
    for ws in wb.worksheets:
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        for h, row in enumerate(rows[:40]):
            heads = [text(v) for v in row]
            if not (any("รายการ" in t for t in heads) and any("ราคา" in t for t in heads)):
                continue
            roles = header_roles(heads, spec)
            start = h + 1
            if _missing_roles(roles, spec) and h + 1 < len(rows):     # header written over two rows
                two = [f"{a} {text(b)}" for a, b in zip(heads, rows[h + 1] + [None] * len(heads))]
                if not _missing_roles(header_roles(two, spec), spec):
                    roles, start = header_roles(two, spec), h + 2
            missing = _missing_roles(roles, spec)
            if missing:
                problem = "หาคอลัมน์ " + ", ".join(missing) + " ในหัวตารางไม่เจอ"
                continue
            col = {role: i for i, role in roles.items()}
            items = []
            for r in rows[start:]:
                r = r + [None] * (len(heads) - len(r))
                name = text(r[col["name"]])
                if not name:
                    continue
                items.append(_item(text(r[col["no"]]) if "no" in col else "", name, text(r[col["unit"]]),
                                   {lab: price(r[col[lab]]) for lab, _ in spec["prices"]}))
            if items:
                return {"items": items, "method": "excel", "notes": []}
    raise ReadError(problem)


def read_price_file(data: bytes, filename: str, spec: dict, progress=None) -> dict:
    """PDF or Excel, by the file name."""
    if filename.lower().endswith((".xlsx", ".xlsm")):
        return read_price_excel(data, spec)
    return read_price_pdf(data, spec, progress)


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #
_SPEC = re.compile(r"ขนาด|บรรจุ|ไม่น้อยกว่า|ไม่ต่ำกว่า|ประมาณ|ตรา|ยี่ห้อ")


def base(name: str) -> str:
    """Name without its description: no text in brackets, cut where the size / brand starts.
    'อกไก่ ไม่ติดกระดูก (สดใหม่ ไม่แช่จนแข็ง)' -> 'อกไก่ ไม่ติดกระดูก', 'หมูยอ ขนาดบรรจุ…' -> 'หมูยอ'."""
    s = re.sub(r"\(.*?(\)|$)", " ", name or "")
    m = _SPEC.search(s)
    if m and m.start() > 0:
        s = s[:m.start()]
    return re.sub(r"\s+", " ", s).strip()


def _keys(name: str) -> tuple[str, str, str]:
    """(whole name, name without description, first word), all through norm()."""
    n, bs = norm(name), base(name)
    return n, norm(bs) or n, norm(bs.split(" ")[0])


def _score(ka: tuple[str, str, str], kb: tuple[str, str, str], floor: float = 0.0) -> float:
    """similarity() on precomputed _keys(); 0 when it is surely below `floor` (skips the slow part)."""
    na, ba, ha = ka
    nb, bb, hb = kb
    if not na or not nb:
        return 0.0
    score = 0.0
    if ha and ha == hb:                                   # same first word: 'ผักชี' / 'ผักชี สด ไม่แช่น้ำ'
        score = 0.97 if ba == bb else 0.93
    short, long_ = sorted((ba, bb), key=len)
    if len(short) >= 3 and long_.startswith(short):       # 'กระเทียมแห้ง' / 'กระเทียมแห้ง ชนิดเมล็ดใหญ่'
        score = max(score, 0.9)
    hs, other = (ha, bb) if len(ha) <= len(hb) else (hb, ba)
    if len(hs) >= 4 and hs in other:                      # 'ตะไคร้' / 'ต้นตะไคร้ตัดราก'
        score = max(score, 0.8)
    for x, y in ((na, nb), (ba, bb)):
        need = max(score, floor)
        sm = SequenceMatcher(None, x, y)
        if sm.real_quick_ratio() > need and sm.quick_ratio() > need:
            score = max(score, sm.ratio())
    return score


def similarity(a: str, b: str) -> float:
    """0..1 — how likely two names are the same product."""
    return _score(_keys(a), _keys(b))


def match(jpr: list[dict], rw: list[dict]) -> dict:
    """
    -> {'rows': [(jpr index | None, rw index | None, how, score)]}
    how: 'same' (same name) / 'similar' (names more than AUTO_MIN alike, paired automatically) / '' (only in one file).
    Rows follow the ราชวิถี order (the user's request); items only in the จปร file come last, in their own order.
    """
    j_of: dict[int, tuple[int, str, float]] = {}     # rw index -> (jpr index, how, score)
    used_j: set[int] = set()
    by_key: dict[str, list[int]] = {}
    for i, it in enumerate(jpr):
        by_key.setdefault(norm(it["name"]), []).append(i)

    for r, it in enumerate(rw):
        i = next((i for i in by_key.get(norm(it["name"]), []) if i not in used_j), None)
        if i is not None:
            j_of[r] = (i, "same", 1.0)
            used_j.add(i)

    free_j = [i for i in range(len(jpr)) if i not in used_j]
    jkeys = {i: _keys(jpr[i]["name"]) for i in free_j}
    cands = []                                         # (score, tie-break, rw index, jpr index)
    for r, it in enumerate(rw):
        if r in j_of:
            continue
        rk = _keys(it["name"])
        for i in free_j:
            sc = _score(rk, jkeys[i], AUTO_MIN)
            if sc > AUTO_MIN:   # equal scores: the closer whole name wins ('กระชาย (หั่นฝอย)' -> 'กระชายสดหั่นฝอย')
                cands.append((sc, SequenceMatcher(None, rk[0], jkeys[i][0]).ratio(), r, i))
    for sc, _, r, i in sorted(cands, key=lambda c: (-c[0], -c[1], c[2], c[3])):   # most similar first, one-to-one
        if r not in j_of and i not in used_j:
            j_of[r] = (i, "similar", sc)
            used_j.add(i)

    rows = [(j_of[r][0], r, j_of[r][1], j_of[r][2]) if r in j_of else (None, r, "", 0.0) for r in range(len(rw))]
    rows += [(i, None, "", 0.0) for i in range(len(jpr)) if i not in used_j]
    return {"rows": rows}
