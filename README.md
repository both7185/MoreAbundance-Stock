# PO Summary App (สรุปและรวมรายการใบสั่งซื้อ)

Upload any number of Thai PO PDFs → get the summary Excel in the exact layout of
`ตารางสรุปและรวมรายการใบสั่งซื้อวัตถุดิบประกอบอาหาร`, with live formulas.

**Sheet layout:** ลำดับ | หมวดหมู่ | รายการสินค้า | หน่วย | **จำนวนรวม** | ราคา/หน่วย | one *จำนวน* column per date | จำนวนเงินรวม.
จำนวนรวม is `=SUM(<date columns>)` and จำนวนเงินรวม is `=จำนวนรวม*ราคา/หน่วย`, so editing a day's quantity or a price updates the totals.

## Run

**Windows:** double-click `run.bat`. The first run creates `.venv` and installs the packages, then it opens http://localhost:8501.
Requirements: Python 3.10+ ("Add python.exe to PATH" ticked when installing) and internet access on the first run.
To copy the app to another PC, copy everything **except the `.venv` folder**. `run.bat` rebuilds a broken `.venv` automatically anyway.

**Any OS:**
```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt     # Windows: .venv\Scripts\pip ...
.venv/bin/streamlit run app.py
```

## Stack

| Layer | Choice | Why |
|---|---|---|
| UI | **Streamlit** | Multi-file upload, editable review tables, and download in about 150 lines; no frontend build |
| PDF | **PyMuPDF** (`find_tables` + `get_texttrace`) + **fontTools** | Table grid detection plus glyph-level access, needed to repair the Thai text (see below) |
| Excel | **openpyxl** | Writes formulas, merged headers, fills, borders and freeze panes |
| Glue | pandas | Only for the on-screen tables |

## Files

| File | Purpose |
|---|---|
| `app.py` | Streamlit UI: upload → check each file → preview/edit categories → download |
| `po_extractor.py` | PDF → `PurchaseOrder` (PO no., วันที่, ใช้ในวันที่, items, printed total) |
| `report_builder.py` | Group by (name, unit, price), split qty by date, build the styled workbook |
| `catalog.json` | Category per item, keyword rules for new items, name/unit aliases, category order |

## Things worth knowing

**Broken Thai text in the PDFs.** These POs are exported from Word with TH SarabunPSK, which writes a faulty
ToUnicode table. As a result, pdfplumber and plain PyMuPDF return `น ้าตาล`, `น าตาล`, `ชิ น`, `ซีอิ๊วด า`, and the
garbage differs per file. The extractor ignores that table. It maps each glyph ID back to Unicode through the
embedded font's own cmap, folds Thai presentation forms (U+F700–F71A), and rebuilds `ำ`. Both sample files
come out clean and reconcile to their printed `เป็นเงิน` totals to the satang.

**Which date makes the columns.** Both sample POs have the header `วันที่ ๒ ตุลาคม ๒๕๖๙`. Your template
splits by `ใช้ในวันที่` (6 and 7 ต.ค.), so that is the default. The sidebar switches to the header date. You can also
type a date per file in step 1 if one can't be read. POs that land on the same date are summed into one column pair.

**Name/unit aliases.** To reproduce the template exactly, `catalog.json` renames
`ซีอิ๊วขาวตราง่วนเชียง → ซีอิ๊วขาวง่วนเชียง`, `คื่นช่าย → คืนช่าย`, and `ปี๊บ → ปิ๊บ`. Edit or empty these maps as
you like, or switch them off in the sidebar.

**Categories.** Matching uses the exact item names from the template first, then the `keyword_rules` (first match wins),
then `default_category` (blank). You can change a category in the preview table, and
"💾 จำหมวดหมู่ที่แก้ไว้" saves it to `catalog.json` for next time.

**Sort order.** Rows follow `category_order`, then the item name in Thai Unicode order. This is the same order as the template.

**Checks shown per file.** Each line's qty × price is compared with the printed amount, the sum is compared with the printed total,
row numbers must be continuous, and both dates must be found. The Excel also keeps the template's
`Original PO Total` and `Variance Check` rows.

**Limits.** The PDFs must be text-based, which these are. A scanned or photographed PO has no text layer and would need OCR first.
The parser expects this PO form's 8-column table (ลำดับ | รายการ | หน่วยนับ | จำนวน | หน่วยละ บาท/สต. | จำนวนเงิน บาท/สต.).
It re-reads the header row when present, so moved columns are handled.
