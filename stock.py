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
         "before": 20, "count": 20, "after": 32, "exp": "2027-05-01", "saved_at": "2026-10-10 16:30"},
        {..., "out": 5, "out_lots": [{"exp": "2027-03-26", "qty": 5}], "note": "ใบเบิก ส่ง 14/10/2026"}
      ],
      "withdrawals": [                                  # ใบเบิก: one per delivery date
        {"date": "2026-10-14", "saved_at": "2026-10-13 16:30",
         "lines": [{"code": "I-001", "name": "...", "unit": "กล.", "exp": "2027-03-26", "qty": 5,
                    "taken": [{"exp": "2027-03-26", "qty": 5}]}]}
      ]
    }

* code   = category letter + running number (M-001). Moving an item to another category gives it
           the next number of that category (highest number in use + 1).
* lots   = how much of the stock has which expiry date. Stock without a known date is not in a lot,
           so sum(lots) <= qty.
* withdrawals = ใบเบิก (stock out). Each line takes from the lot the user picked (default: the earliest
           expiry). The stock is cut when the sheet is saved; saving it again puts back what the old
           version took and takes the new lines, so an edit changes the stock only by the difference.
* movements are kept for HISTORY_DAYS days (by the record date). Deleting an item keeps its movements
           (with the item's name and unit added) so the history still shows what it was.
"""
from __future__ import annotations

import copy
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
    return {"items": [], "movements": [], "withdrawals": []}


def parse(text: str | bytes) -> dict:
    data = json.loads(text) if text else empty()
    data.setdefault("items", [])
    data.setdefault("movements", [])
    data.setdefault("withdrawals", [])
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
    used += [code_number(l["code"]) for w in data["withdrawals"] for l in w["lines"]
             if l["code"].startswith(letter + "-")]
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


def _take_out(item: dict, amount: float, lot: str | None = None) -> list[dict]:
    """
    Take `amount` out of the current stock (item["qty"] is lowered by the caller) and return where it came
    from: [{"exp": "2027-06-24", "qty": 5}, ...], exp "" = stock without an expiry date.
    lot = the expiry date the user picked to take from first ("" = the undated stock, None = earliest);
    whatever that lot cannot cover comes from the earliest other lots, then from the undated stock.
    """
    item["lots"].sort(key=lambda l: l["exp"])
    undated = num(item["qty"] - sum(l["qty"] for l in item["lots"]))
    sources = [l for l in item["lots"] if l["exp"] == lot] + [l for l in item["lots"] if l["exp"] != lot]
    taken = []
    if lot == "" and undated > 0:
        take = min(undated, amount)
        taken.append({"exp": "", "qty": take})
        amount = num(amount - take)
        undated = num(undated - take)
    for l in sources:
        if amount <= 0:
            break
        take = min(l["qty"], amount)
        l["qty"] = num(l["qty"] - take)
        amount = num(amount - take)
        taken.append({"exp": l["exp"], "qty": take})
    if amount > 0 and undated > 0:
        taken.append({"exp": "", "qty": min(undated, amount)})
    item["lots"] = [l for l in item["lots"] if l["qty"] > 0]
    return taken


def plan_out(item: dict, count, qty_out, lot: str | None = None) -> list[dict]:
    """Preview of _take_out() for the entry form (does not change `item`)."""
    copy = {**item, "qty": num(count), "lots": [dict(l) for l in item["lots"]]}
    _fit_lots(copy)
    return _take_out(copy, num(qty_out), lot)


def prune(data: dict, ref: date | None = None) -> None:
    cutoff = ((ref or today()) - timedelta(days=HISTORY_DAYS)).isoformat()
    data["movements"] = [m for m in data["movements"] if m["date"] >= cutoff]
    data["withdrawals"] = [w for w in data["withdrawals"] if w["date"] >= cutoff]


def record(data: dict, code: str, qty_in, qty_out, count, exp: date | None, on: date,
           lot: str | None = None) -> str:
    """
    One stock entry (ปุ่ม สินค้าเข้า — the page no longer takes stock out here, see save_withdrawal):
      count = stock before this entry: None = the current stock, another number = re-count
      after = count + in - out
      exp   = expiry of what came in; with nothing coming in it dates the stock that has no date yet
      lot   = expiry date of the lot to take `out` from ("" = stock without a date, None = earliest first)
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
        return "ยังไม่ได้กรอกจำนวนเข้า หรือแก้ยอดในสต็อก"
    if on < today() - timedelta(days=HISTORY_DAYS):
        return f"วันที่บันทึกย้อนหลังได้ไม่เกิน {HISTORY_DAYS} วัน"

    before = item["qty"]
    item["qty"] = count
    _fit_lots(item)                      # re-count lower than the dated stock
    out_lots = _take_out(item, qty_out, lot) if qty_out else []
    item["qty"] = after
    _fit_lots(item)
    if exp is not None:
        undated = num(after - sum(l["qty"] for l in item["lots"]))
        _add_lot(item, exp.isoformat(), min(qty_in, undated) if qty_in else undated)

    data["movements"].append({
        "id": uuid.uuid4().hex[:12], "code": code, "date": on.isoformat(),
        "in": qty_in, "out": qty_out, "before": before, "count": count, "after": after,
        "exp": exp.isoformat() if exp else "", "saved_at": now_text(),
        **({"out_lots": out_lots} if out_lots else {}),
    })
    prune(data)
    return ""


# --------------------------------------------------------------------------- #
# ใบเบิก (ปุ่ม เบิกสินค้าออก): one sheet per delivery date, each line = item + lot + qty
# --------------------------------------------------------------------------- #
def sheet(data: dict, on: str) -> dict | None:
    """The ใบเบิก of one delivery date ('2026-10-14'), or None."""
    return next((w for w in data["withdrawals"] if w["date"] == on), None)


def _restore(item: dict, taken: list[dict]) -> None:
    """Put stock that a ใบเบิก took back into its lots."""
    for t in taken:
        item["qty"] = num(item["qty"] + t["qty"])
        if t["exp"]:
            _add_lot(item, t["exp"], t["qty"])


def lots_for_sheet(data: dict, on: str, code: str) -> dict[str, float]:
    """Stock of one item per lot ('' = no expiry date) as if the saved ใบเบิก of `on` was not saved —
    what that sheet can take when it is saved again."""
    item = find(data, code)
    if item is None:
        return {}
    copy = {**item, "lots": [dict(l) for l in item["lots"]]}
    for line in (sheet(data, on) or {"lines": []})["lines"]:
        if line["code"] == code:
            _restore(copy, line["taken"])
    return {t["exp"]: t["qty"] for t in plan_out(copy, copy["qty"], copy["qty"])}


def _taken_by_code(lines: list[dict]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for line in lines:
        per = out.setdefault(line["code"], {})
        for t in line["taken"]:
            per[t["exp"]] = num(per.get(t["exp"], 0) + t["qty"])
    return out


def save_withdrawal(data: dict, on: date, lines: list[dict]) -> str:
    """
    Save the ใบเบิก of delivery date `on`: lines = [{"code": "I-001", "exp": "2027-03-26", "qty": 5}, ...]
    (exp "" = stock without an expiry date). What the saved version of this sheet took is put back first,
    then the new lines are taken out, so editing a sheet changes the stock only by the difference.
    No lines = the sheet is deleted (its stock goes back). On an error `data` is left as it was.
    """
    if on < today() - timedelta(days=HISTORY_DAYS):
        return f"แก้ใบเบิกย้อนหลังได้ไม่เกิน {HISTORY_DAYS} วัน"
    work = copy.deepcopy(data)
    err = _save_withdrawal(work, on, lines)
    if not err:
        data.clear()
        data.update(work)
    return err


def _save_withdrawal(data: dict, on: date, lines: list[dict]) -> str:
    key = on.isoformat()
    old = sheet(data, key)
    old_lines = old["lines"] if old else []
    merged: dict[tuple[str, str], float] = {}
    for line in lines:
        qty = num(line["qty"])
        if qty < 0:
            return "จำนวนต้องไม่ติดลบ"
        if qty:
            k = (line["code"], line["exp"])
            merged[k] = num(merged.get(k, 0) + qty)

    codes = list(dict.fromkeys([l["code"] for l in old_lines] + [c for c, _ in merged]))
    before = {c: find(data, c)["qty"] for c in codes if find(data, c)}
    for line in old_lines:
        item = find(data, line["code"])
        if item:                                  # a deleted item cannot get its stock back
            _restore(item, line["taken"])
    new_lines = []
    for (code, exp), qty in merged.items():
        item = find(data, code)
        if item is None:
            return f"ไม่พบสินค้ารหัส {code}"
        have = {t["exp"]: t["qty"] for t in plan_out(item, item["qty"], item["qty"])}.get(exp, 0)
        if qty > have:
            where = f"ล็อตหมดอายุ {fmt_date(exp)}" if exp else "ส่วนที่ไม่รู้วันหมดอายุ"
            return (f"{item['name']}: {where} มีแค่ {fmt_num(have)} {item['unit']} "
                    f"(จะเบิก {fmt_num(qty)})").replace("  ", " ")
        taken = _take_out(item, qty, exp)
        item["qty"] = num(item["qty"] - qty)
        new_lines.append({"code": code, "name": item["name"], "unit": item["unit"], "exp": exp, "qty": qty,
                          "taken": taken})

    # history: one movement per item whose withdrawal changed
    old_taken, new_taken = _taken_by_code(old_lines), _taken_by_code(new_lines)
    note = f"{'แก้' if old else ''}ใบเบิก ส่ง {fmt_date(key)}"
    for code in codes:
        item = find(data, code)
        a, b = old_taken.get(code, {}), new_taken.get(code, {})
        if item is None or a == b:
            continue
        delta = {e: num(b.get(e, 0) - a.get(e, 0)) for e in sorted(set(a) | set(b))}
        diff = num(item["qty"] - before[code])
        data["movements"].append({
            "id": uuid.uuid4().hex[:12], "code": code, "date": key,
            "in": max(diff, 0), "out": max(-diff, 0), "before": before[code], "count": before[code],
            "after": item["qty"], "exp": "", "saved_at": now_text(), "note": note,
            **({"out_lots": [{"exp": e, "qty": d} for e, d in delta.items() if d > 0]}
               if any(d > 0 for d in delta.values()) else {}),
            **({"back_lots": [{"exp": e, "qty": -d} for e, d in delta.items() if d < 0]}
               if any(d < 0 for d in delta.values()) else {}),
        })

    if old:
        data["withdrawals"].remove(old)
    if new_lines:
        data["withdrawals"].append({"date": key, "lines": new_lines, "saved_at": now_text()})
        data["withdrawals"].sort(key=lambda w: w["date"])
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
    old = next((m for m in data["movements"] if m["code"] == code), None)
    if old:
        return f"รหัส {code} เคยใช้กับ {old.get('name') or 'สินค้าที่ลบไปแล้ว'} (ยังอยู่ในประวัติ) — ใช้รหัสอื่น"
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
        for w in data["withdrawals"]:
            for line in w["lines"]:
                if line["code"] == code:
                    line["code"] = new_code
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


def delete_item(data: dict, code: str) -> str:
    """Remove an item. Its movements stay in the history (with the name) until they age out."""
    item = find(data, code)
    if item is None:
        return f"ไม่พบสินค้ารหัส {code}"
    data["items"].remove(item)
    for m in data["movements"]:
        if m["code"] == code:
            m.update(name=item["name"], unit=item["unit"])
    return ""
