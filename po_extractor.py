"""
PO (ใบสั่งซื้อ) PDF extractor.

Why not plain pdfplumber / PyMuPDF text?
-----------------------------------------
These POs are exported from MS Word using the TH SarabunPSK font. Word writes a
broken ToUnicode table for some Thai glyphs, so normal extraction returns text
like "น ้าตาล", "น าตาล", "ชิ น", "ซีอิ๊วด า" (and different garbage per file).

The glyph IDs themselves are correct, though. So we read every glyph with
PyMuPDF's `get_texttrace()` (unicode + glyph id + position), and translate the
glyph id back to Unicode using the *embedded font's own cmap*. Thai
presentation-form glyphs (Private Use Area U+F700–U+F71A) are folded back to
normal Thai characters, and "ํ + า" is recombined into "ำ".

Table structure (cell boxes) comes from PyMuPDF's `find_tables()`; each cell is
then filled with the repaired characters that fall inside it, in content-stream
(=logical) order.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import pymupdf
from fontTools.ttLib import TTFont

# --------------------------------------------------------------------------- #
# Thai helpers
# --------------------------------------------------------------------------- #
THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")

THAI_MONTHS_FULL = [
    "มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
    "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม",
]
THAI_MONTHS_ABBR = [
    "ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
    "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค.",
]

# Thai presentation forms (Microsoft / Apple Private Use Area) -> base char
_PUA_TO_THAI = {
    0xF700: 0x0E10, 0xF701: 0x0E34, 0xF702: 0x0E35, 0xF703: 0x0E36,
    0xF704: 0x0E37, 0xF705: 0x0E48, 0xF706: 0x0E49, 0xF707: 0x0E4A,
    0xF708: 0x0E4B, 0xF709: 0x0E4C, 0xF70A: 0x0E48, 0xF70B: 0x0E49,
    0xF70C: 0x0E4A, 0xF70D: 0x0E4B, 0xF70E: 0x0E4C, 0xF70F: 0x0E0D,
    0xF710: 0x0E31, 0xF711: 0x0E4D, 0xF712: 0x0E47, 0xF713: 0x0E48,
    0xF714: 0x0E49, 0xF715: 0x0E4A, 0xF716: 0x0E4B, 0xF717: 0x0E4C,
    0xF718: 0x0E38, 0xF719: 0x0E39, 0xF71A: 0x0E3A,
}


def _is_thai_or_pua(cp: int) -> bool:
    return 0x0E00 <= cp <= 0x0E7F or 0xF700 <= cp <= 0xF71A


def normalize_thai(text: str) -> str:
    """Fold presentation forms, rebuild SARA AM, tidy whitespace."""
    text = "".join(chr(_PUA_TO_THAI.get(ord(c), ord(c))) for c in text)
    # NIKHAHIT (+ optional tone mark) + SARA AA  ->  tone + SARA AM
    text = re.sub("\u0E4D([\u0E48-\u0E4B]?)\u0E32", "\\1\u0E33", text)
    text = re.sub("([\u0E48-\u0E4B])\u0E4D\u0E32", "\\1\u0E33", text)
    text = text.replace("\u0E4D\u0E32", "\u0E33")
    return re.sub(r"\s+", " ", text).strip()


def to_number(text: str | None) -> float:
    """'1,300' / '๑๒' / '-' / '' -> float."""
    if text is None:
        return 0.0
    t = text.translate(THAI_DIGITS).replace(",", "").replace(" ", "").strip()
    if t in ("", "-", "–", "—"):
        return 0.0
    m = re.search(r"-?\d+(?:\.\d+)?", t)
    return float(m.group()) if m else 0.0


def satang_value(text: str | None) -> float:
    """
    Value of a 'สต.' cell, in baht, exactly as written on the PO (no rounding):
      '50' -> 0.50, '9' / '09' -> 0.09, '0.09' / '.09' -> 0.09, '-' / '' -> 0
    """
    if text is None:
        return 0.0
    t = text.translate(THAI_DIGITS).replace(",", "").replace(" ", "").strip()
    if t in ("", "-", "–", "—"):
        return 0.0
    m = re.search(r"\d*\.\d+|\d+", t)
    if not m:
        return 0.0
    num = m.group()
    if "." in num:
        v = Decimal(num)
        return float(v if v < 1 else v / 100)   # '0.09' is already baht; '9.5' means 9.5 satang
    return float(Decimal(num) / 100)            # whole satang


def baht(baht_text: str | None, satang_text: str | None) -> float:
    """Baht column + satang column, added exactly (Decimal) to avoid float noise."""
    b = Decimal(str(to_number(baht_text)))
    return float(b + Decimal(str(satang_value(satang_text))))


def thai_short_date(d: date) -> str:
    """date(2026,10,6) -> '6 ต.ค. 69'"""
    return f"{d.day} {THAI_MONTHS_ABBR[d.month - 1]} {(d.year + 543) % 100:02d}"


def parse_thai_date(segment: str) -> date | None:
    """Parse '....๖….ตุลาคม.….๒๕๖๙' (dots / ellipses / Thai digits tolerated)."""
    seg = segment.translate(THAI_DIGITS)
    month = None
    month_pos = -1
    for i, name in enumerate(THAI_MONTHS_FULL):
        p = seg.find(name)
        if p != -1 and (month_pos == -1 or p < month_pos):
            month, month_pos = i + 1, p
    if month is None:
        for i, name in enumerate(THAI_MONTHS_ABBR):
            p = seg.find(name)
            if p != -1 and (month_pos == -1 or p < month_pos):
                month, month_pos = i + 1, p
    if month is None:
        return None
    day_m = re.findall(r"\d{1,2}", seg[:month_pos])
    year_m = re.search(r"\d{4}|\d{2}", seg[month_pos:])
    if not day_m or not year_m:
        return None
    day = int(day_m[-1])
    year = int(year_m.group())
    if year < 100:
        year += 2500
    if year > 2400:  # Buddhist Era -> CE
        year -= 543
    try:
        return date(year, month, day)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Glyph-level text repair
# --------------------------------------------------------------------------- #
def _font_glyph_maps(doc: pymupdf.Document) -> dict[str, dict[int, int]]:
    """{font name without subset prefix: {glyph id: unicode codepoint}}"""
    maps: dict[str, dict[int, int]] = {}
    seen: set[int] = set()
    for page in doc:
        for xref, *_rest in page.get_fonts(full=True):
            if xref in seen:
                continue
            seen.add(xref)
            try:
                basename, ext, _type, buf = doc.extract_font(xref)
                if not buf or ext not in ("ttf", "otf", "cff"):
                    continue
                font = TTFont(io.BytesIO(buf), lazy=True)
                order = font.getGlyphOrder()
                gid_map: dict[int, int] = {}
                cmap = font.getBestCmap() or {}
                name_to_gid = {n: i for i, n in enumerate(order)}
                for cp, gname in sorted(cmap.items(), reverse=True):
                    gid = name_to_gid.get(gname)
                    if gid is not None:
                        gid_map[gid] = cp  # lowest codepoint wins
                for gid, gname in enumerate(order):  # fallback: 'uni0E49' names
                    if gid not in gid_map:
                        m = re.match(r"uni([0-9A-Fa-f]{4})", gname)
                        if m:
                            gid_map[gid] = int(m.group(1), 16)
                key = basename.split("+")[-1]
                maps.setdefault(key, {})
                for g, cp in gid_map.items():
                    maps[key].setdefault(g, cp)
            except Exception:
                continue
    return maps


@dataclass
class _Char:
    text: str
    x: float  # origin x
    y: float  # baseline y


def _page_chars(page: pymupdf.Page, glyph_maps: dict[str, dict[int, int]]) -> list[_Char]:
    out: list[_Char] = []
    for span in page.get_texttrace():
        fmap = glyph_maps.get(span["font"].split("+")[-1], {})
        for uc, gid, origin, _bbox in span["chars"]:
            cp = uc
            fixed = fmap.get(gid)
            # Only override when the result is Thai, or ToUnicode gave
            # Thai/space/unknown. Leaves Latin text and digits untouched.
            if fixed is not None and fixed != uc and (
                _is_thai_or_pua(fixed) and (uc in (0x20, 0xFFFD) or _is_thai_or_pua(uc) or uc < 0)
            ):
                cp = fixed
            if cp < 0:
                continue
            out.append(_Char(chr(cp), origin[0], origin[1]))
    return out


def _page_text(chars: list[_Char]) -> str:
    """Content-stream order, newline when the baseline moves."""
    parts, last_y = [], None
    for c in chars:
        if last_y is not None and abs(c.y - last_y) > 3:
            parts.append("\n")
        parts.append(c.text)
        last_y = c.y
    lines = [normalize_thai(l) for l in "".join(parts).split("\n")]
    return "\n".join(l for l in lines if l)


def _cell_text(chars: list[_Char], bbox) -> str:
    if bbox is None:
        return ""
    x0, y0, x1, y1 = bbox
    s = "".join(c.text for c in chars if x0 - 0.5 <= c.x < x1 - 0.5 and y0 < c.y <= y1 + 1)
    return normalize_thai(s)


# --------------------------------------------------------------------------- #
# PO parsing
# --------------------------------------------------------------------------- #
@dataclass
class POItem:
    seq: int
    name: str
    unit: str
    qty: float
    unit_price: float
    amount: float  # as printed on the PO (for verification only)


@dataclass
class PurchaseOrder:
    filename: str
    po_number: str = ""
    po_date: date | None = None        # วันที่ (header)
    delivery_date: date | None = None  # ใช้ในวันที่ (delivery / usage date)
    items: list[POItem] = field(default_factory=list)
    stated_total: float | None = None  # "เป็นเงิน" on the PO
    warnings: list[str] = field(default_factory=list)

    @property
    def computed_total(self) -> float:
        # Decimal so 560 × 2.09 is exactly 1170.40 (float noise only, no business rounding)
        return float(sum(Decimal(str(i.qty)) * Decimal(str(i.unit_price)) for i in self.items))


# Default column layout of this PO form:
# ลำดับ | รายการ | หน่วยนับ | จำนวน | หน่วยละ(บาท) | หน่วยละ(สต.) | จำนวนเงิน(บาท) | จำนวนเงิน(สต.)
_DEFAULT_COLS = dict(seq=0, name=1, unit=2, qty=3, price_b=4, price_s=5, amt_b=6, amt_s=7)


def _detect_columns(header_cells: list[str]) -> dict | None:
    """Map columns from a header row, if this row is one."""
    h = [c.replace(" ", "") for c in header_cells]
    if not any("รายการ" in c for c in h):
        return None
    cols = dict(_DEFAULT_COLS)
    for i, c in enumerate(h):
        if "ลำดับ" in c:
            cols["seq"] = i
        elif "รายการ" in c:
            cols["name"] = i
        elif "นับ" in c and "จำนวน" not in c:
            cols["unit"] = i
        elif "จำนวนเงิน" in c:
            cols["amt_b"], cols["amt_s"] = i, i + 1
        elif "จำนวน" in c:
            cols["qty"] = i
        elif "หน่วยละ" in c or "ราคา" in c:
            cols["price_b"], cols["price_s"] = i, i + 1
    return cols


def parse_po(pdf_bytes: bytes, filename: str = "") -> PurchaseOrder:
    po = PurchaseOrder(filename=filename)
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    glyph_maps = _font_glyph_maps(doc)

    cols = dict(_DEFAULT_COLS)
    full_text_pages = []

    for page in doc:
        chars = _page_chars(page, glyph_maps)
        full_text_pages.append(_page_text(chars))

        for table in page.find_tables().tables:
            for row in table.rows:
                cells = [_cell_text(chars, b) for b in row.cells]
                joined = "".join(cells)

                detected = _detect_columns(cells)
                if detected:
                    cols = detected
                    continue

                if "เป็นเงิน" in joined:
                    nums = [c for c in cells if re.search(r"[\d๐-๙]", c) and "เป็นเงิน" not in c]
                    if nums:
                        po.stated_total = baht(nums[0], nums[1] if len(nums) > 1 else None)
                    continue

                if len(cells) <= max(cols.values()):
                    continue
                seq_txt = cells[cols["seq"]].translate(THAI_DIGITS).strip()
                if not seq_txt.isdigit():
                    continue  # header / blank / signature rows
                name = cells[cols["name"]]
                if not name:
                    continue
                qty = to_number(cells[cols["qty"]])
                # exactly as printed on the PO - no rounding
                price = baht(cells[cols["price_b"]], cells[cols["price_s"]])
                amount = baht(cells[cols["amt_b"]], cells[cols["amt_s"]])
                item = POItem(int(seq_txt), name, cells[cols["unit"]], qty, price, amount)
                if abs(Decimal(str(item.qty)) * Decimal(str(item.unit_price)) - Decimal(str(item.amount))) > Decimal("0.005"):
                    po.warnings.append(
                        f"แถว {item.seq} '{item.name}': {item.qty:g} × {item.unit_price:,.2f} "
                        f"≠ {item.amount:,.2f} ที่พิมพ์ในใบสั่ง"
                    )
                po.items.append(item)

    text = "\n".join(full_text_pages)

    m = re.search(r"ใบสั่งที่[\s.…]*([0-9๐-๙]+\s*/\s*[0-9๐-๙]+)", text)
    if m:
        po.po_number = m.group(1).replace(" ", "")

    m = re.search(r"ใช้ในวันที่([^\n]{0,80})", text)
    if m:
        po.delivery_date = parse_thai_date(m.group(1))

    m = re.search(r"(?<!ใน)วันที่([^\n]{0,80})", text)
    if m:
        po.po_date = parse_thai_date(m.group(1))

    # Sanity checks
    seqs = [i.seq for i in po.items]
    if seqs and seqs != list(range(1, len(seqs) + 1)):
        po.warnings.append("ลำดับรายการไม่ต่อเนื่อง — อาจมีแถวที่อ่านไม่ได้")
    if po.stated_total is not None and abs(po.computed_total - po.stated_total) > 0.005:
        po.warnings.append(
            f"ยอดรวมที่คำนวณ {po.computed_total:,.2f} ≠ ยอดในใบสั่ง {po.stated_total:,.2f}"
        )
    if not po.items:
        po.warnings.append("ไม่พบตารางรายการสินค้าในไฟล์นี้")
    if po.delivery_date is None:
        po.warnings.append("อ่าน 'ใช้ในวันที่' ไม่ได้")
    if po.po_date is None:
        po.warnings.append("อ่าน 'วันที่' ของใบสั่งไม่ได้")
    return po
