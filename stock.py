"""
Stock data (หน้า สต็อกสินค้า) — separate from the PO features.

Everything is one JSON file, stock.json, saved to the GitHub *data* branch like products.csv
(see datastore.py), so one save = one commit and items / lots / history never get out of step:

    {
      "items": [
        {"code": "I-001", "name": "น้ำปลารวมรส", "category": "เครื่องปรุง", "unit": "กล.",
         "qty": 32, "min": 10,
         "lots": [{"exp": "2027-03-26", "qty": 20}, {"exp": "2027-05-01", "qty": 12}]}
      ],
      "movements": [
        {"id": "...", "code": "I-001", "date": "2026-10-10", "in": 12, "out": 0,
         "before": 20, "count": 20, "after": 32, "exp": "2027-05-01", "saved_at": "2026-10-10 16:30"}
      ]
    }

* code   = category letter + running number (M-001). Moving an item to another category gives it
           the next number of that category (highest number in use + 1).
* lots   = how much of the stock has which expiry date. Stock without a known date is not in a lot,
           so sum(lots) <= qty. Taking stock out uses the earliest expiry first.
* movements are kept for HISTORY_DAYS days (by the record date).
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

STOCK_FILE = "stock.json"
STOCK_PATH = Path(__file__).parent / STOCK_FILE
HISTORY_DAYS = 90
TZ = ZoneInfo("Asia/Bangkok")

# category -> code letter (order = display order)
CATEGORIES: dict[str, str] = {
    "ของสด": "M",
    "ของแปรรูป": "P",
    "ผัก": "V",
    "ผลไม้": "F",
    "เครื่องปรุง": "I",
    "ของแห้ง": "D",
    "เครื่องดื่ม": "B",
    "ของหวาน": "S",
    "อื่นๆ": "X",
}
CAT_ORDER = {c: i for i, c in enumerate(CATEGORIES)}

AVAILABLE, LOW, OUT = "Available", "Low stock", "Out of stock"


def today() -> date:
    return datetime.now(TZ).date()


def now_text() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M")


def num(v) -> float:
    """Number from JSON / user input; whole numbers come back as int (3.0 -> 3)."""
    try:
        f = round(float(v), 4)
    except (TypeError, ValueError):
        return 0
    return int(f) if f == int(f) else f


def fmt_num(v) -> str:
    v = num(v)
    return f"{v:,}" if isinstance(v, int) else f"{v:,.4f}".rstrip("0").rstrip(".")


def fmt_date(iso: str) -> str:
    """'2027-03-26' -> '26/03/2027'."""
    try:
        return date.fromisoformat(iso).strftime("%d/%m/%Y")
    except (TypeError, ValueError):
        return iso or ""


# --------------------------------------------------------------------------- #
# Load / save
# --------------------------------------------------------------------------- #
def empty() -> dict:
    return {"items": [], "movements": []}


def parse(text: str | bytes) -> dict:
    data = json.loads(text) if text else empty()
    data.setdefault("items", [])
    data.setdefault("movements", [])
    for it in data["items"]:
        it.setdefault("unit", "")
        it["qty"] = num(it.get("qty", 0))
        it["min"] = num(it.get("min", 0))
        it["lots"] = [{"exp": l["exp"], "qty": num(l["qty"])} for l in it.get("lots", []) if num(l.get("qty")) > 0]
    return data


def load() -> dict:
    return parse(STOCK_PATH.read_bytes()) if STOCK_PATH.exists() else empty()


def to_json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1) + "\n"


def save_local(data: dict) -> None:
    STOCK_PATH.write_text(to_json(data), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Look-ups
# --------------------------------------------------------------------------- #
def status(item: dict) -> str:
    if item["qty"] <= 0:
        return OUT
    if item["qty"] < item["min"]:
        return LOW
    return AVAILABLE


def code_number(code: str) -> int:
    try:
        return int(code.split("-", 1)[1])
    except (IndexError, ValueError):
        return 0


def sort_key(item: dict):
    return CAT_ORDER.get(item["category"], len(CAT_ORDER)), item["code"][:1], code_number(item["code"])


def find(data: dict, code: str) -> dict | None:
    return next((it for it in data["items"] if it["code"] == code), None)


def next_code(data: dict, category: str) -> str:
    letter = CATEGORIES.get(category, "X")
    used = [code_number(it["code"]) for it in data["items"] if it["code"].startswith(letter + "-")]
    used += [code_number(m["code"]) for m in data["movements"] if m["code"].startswith(letter + "-")]
    return f"{letter}-{max(used, default=0) + 1:03d}"


def history(data: dict, code: str) -> list[dict]:
    """Movements of one item, newest first."""
    rows = [(i, m) for i, m in enumerate(data["movements"]) if m["code"] == code]
    rows.sort(key=lambda r: (r[1]["date"], r[1].get("saved_at", ""), r[0]), reverse=True)
    return [m for _, m in rows]


def last_movement(data: dict) -> dict[str, str]:
    """code -> latest record date."""
    out: dict[str, str] = {}
    for m in data["movements"]:
        if m["date"] > out.get(m["code"], ""):
            out[m["code"]] = m["date"]
    return out


# --------------------------------------------------------------------------- #
# Changes (each returns an error text, or "" when done; `data` is changed in place)
# --------------------------------------------------------------------------- #
def _trim_lots(item: dict, amount: float) -> None:
    """Remove `amount` from the lots, earliest expiry first."""
    item["lots"].sort(key=lambda l: l["exp"])
    for lot in item["lots"]:
        if amount <= 0:
            break
        take = min(lot["qty"], amount)
        lot["qty"] = num(lot["qty"] - take)
        amount = num(amount - take)
    item["lots"] = [l for l in item["lots"] if l["qty"] > 0]


def _fit_lots(item: dict) -> None:
    """Keep sum(lots) <= qty."""
    extra = num(sum(l["qty"] for l in item["lots"]) - item["qty"])
    if extra > 0:
        _trim_lots(item, extra)


def _add_lot(item: dict, exp: str, qty: float) -> None:
    if qty <= 0:
        return
    for lot in item["lots"]:
        if lot["exp"] == exp:
            lot["qty"] = num(lot["qty"] + qty)
            break
    else:
        item["lots"].append({"exp": exp, "qty": num(qty)})
    item["lots"].sort(key=lambda l: l["exp"])


def prune(data: dict, ref: date | None = None) -> None:
    cutoff = ((ref or today()) - timedelta(days=HISTORY_DAYS)).isoformat()
    data["movements"] = [m for m in data["movements"] if m["date"] >= cutoff]


def record(data: dict, code: str, qty_in, qty_out, count, exp: date | None, on: date) -> str:
    """
    One stock entry (ปุ่ม เข้า-ออกสินค้า):
      count = stock before this entry: None = the current stock, another number = re-count
      after = count + in - out
      exp   = expiry of what came in; with nothing coming in it dates the stock that has no date yet
    """
    item = find(data, code)
    if item is None:
        return f"ไม่พบสินค้ารหัส {code}"
    qty_in, qty_out = num(qty_in), num(qty_out)
    count = item["qty"] if count is None else num(count)
    if min(qty_in, qty_out, count) < 0:
        return "จำนวนต้องไม่ติดลบ"
    if qty_out > 0 and count <= 0:
        return f"{item['name']} หมดสต็อก (0) — เอาออกไม่ได้"
    after = num(count + qty_in - qty_out)
    if after < 0:
        return f"เอาออกได้ไม่เกิน {fmt_num(count + qty_in)} {item['unit']}".strip()
    if qty_in == 0 and qty_out == 0 and count == item["qty"] and exp is None:
        return "ยังไม่ได้กรอกจำนวนเข้า / ออก หรือแก้ยอดในสต็อก"
    if on < today() - timedelta(days=HISTORY_DAYS):
        return f"วันที่บันทึกย้อนหลังได้ไม่เกิน {HISTORY_DAYS} วัน"

    before = item["qty"]
    item["qty"] = count
    _fit_lots(item)                      # re-count lower than the dated stock
    if qty_out:
        _trim_lots(item, qty_out)        # earliest expiry goes out first
    item["qty"] = after
    _fit_lots(item)
    if exp is not None:
        undated = num(after - sum(l["qty"] for l in item["lots"]))
        _add_lot(item, exp.isoformat(), min(qty_in, undated) if qty_in else undated)

    data["movements"].append({
        "id": uuid.uuid4().hex[:12], "code": code, "date": on.isoformat(),
        "in": qty_in, "out": qty_out, "before": before, "count": count, "after": after,
        "exp": exp.isoformat() if exp else "", "saved_at": now_text(),
    })
    prune(data)
    return ""


def add_item(data: dict, name: str, code: str, category: str, unit: str = "") -> str:
    name, code, unit = name.strip(), code.strip().upper(), unit.strip()
    if not name:
        return "กรอกชื่อสินค้า"
    if category not in CATEGORIES:
        return "เลือกหมวด"
    if not code:
        return "กรอกรหัส"
    if find(data, code):
        return f"รหัส {code} มีอยู่แล้ว ({find(data, code)['name']})"
    if any(it["name"] == name for it in data["items"]):
        return f"มีสินค้าชื่อ {name} อยู่แล้ว"
    data["items"].append({"code": code, "name": name, "category": category, "unit": unit,
                          "qty": 0, "min": 0, "lots": []})
    data["items"].sort(key=sort_key)
    return ""


def update_item(data: dict, code: str, name: str, category: str, unit: str, min_level) -> tuple[str, str]:
    """Edit name / category / unit / min level. Returns (error, code) — the code changes with the category."""
    item = find(data, code)
    if item is None:
        return f"ไม่พบสินค้ารหัส {code}", code
    name, unit, min_level = name.strip(), unit.strip(), num(min_level)
    if not name:
        return "กรอกชื่อสินค้า", code
    if any(it["name"] == name and it is not item for it in data["items"]):
        return f"มีสินค้าชื่อ {name} อยู่แล้ว", code
    if min_level < 0:
        return "ขั้นต่ำต้องไม่ติดลบ", code
    if category not in CATEGORIES:
        return "เลือกหมวด", code
    new_code = code
    if category != item["category"] and CATEGORIES[category] != code[:1]:
        new_code = next_code(data, category)
        for m in data["movements"]:
            if m["code"] == code:
                m["code"] = new_code
    item.update(name=name, category=category, unit=unit, min=min_level, code=new_code)
    data["items"].sort(key=sort_key)
    return "", new_code


def set_lots(data: dict, code: str, lots: list[tuple[date, float]]) -> str:
    """Replace the expiry lots of one item (fixing a wrong date)."""
    item = find(data, code)
    if item is None:
        return f"ไม่พบสินค้ารหัส {code}"
    merged: dict[str, float] = {}
    for exp, qty in lots:
        qty = num(qty)
        if qty < 0:
            return "จำนวนต้องไม่ติดลบ"
        if qty:
            merged[exp.isoformat()] = num(merged.get(exp.isoformat(), 0) + qty)
    if sum(merged.values()) > item["qty"]:
        return f"รวมทุกล็อตต้องไม่เกินยอดในสต็อก ({fmt_num(item['qty'])})"
    item["lots"] = [{"exp": e, "qty": q} for e, q in sorted(merged.items())]
    return ""
