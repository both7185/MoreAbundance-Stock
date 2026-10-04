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

## Monthly total tab (📅 รวมยอดทั้งเดือน)

Upload all of a month's POs (many PDFs, or one `.zip` of the folder). You get an Excel file with two columns,
**ใบสั่งซื้อ** (the PO name from the file name) and **ยอดรวม**, plus **รวมทั้งเดือน** (`=SUM`) at the bottom.

* `(อัพเดทใหม่)` / `(อัปเดตใหม่)` in a file name is ignored for ordering and removed from the shown name.
  For example, `PO 14 ก.ย. 69 (มื้อเย็น - อัพเดทใหม่)` → `PO 14 ก.ย. 69 (มื้อเย็น)`.
* Rows are sorted by the date in the file name. On the same date the order is: no bracket → `(มื้อเย็น)` → `(กล่องโฟม)` → anything else.
  If a file name has no date, the PDF's ใช้ในวันที่ is used instead. The order lives in `SUFFIX_ORDER` in `monthly_report.py`.
* ยอดรวม is the printed `เป็นเงิน` of each PO by default. A switch uses the sum of qty × price instead.
  Some POs round their printed total to whole baht, and the screen flags those.

## Product data tab (🗂️ ข้อมูลสินค้า)

* Edit cells directly. Add a row with **+** on the table toolbar. Delete rows by ticking them and clicking the bin. Then press **💾 บันทึก**.
* A blank Sup no. is filled in from another product that has the same ร้านหลัก.
* **ชื่อใน PO** lists other spellings used in POs, separated by `|` (e.g. `ปลาช่อน | ปลาช่อนสด`).
* Download as Excel, or import an Excel file such as `DATA P BOTH.xlsx`. Importing **replaces** the whole list.
  It uses the values Excel saved for XLOOKUP cells, so save the file in Excel first.

### Keep edits on Streamlit Community Cloud (GitHub token)

The disk on Streamlit Cloud is temporary, so edits made on the website are lost when the app restarts.
To keep them, let the app commit `products.csv` back to your repo:

1. On GitHub, go to **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**.
   Under Repository access choose *Only select repositories* (this repo). Under Permissions, set **Contents: Read and write**.
2. On share.streamlit.io, open the app → **⋮ → Settings → Secrets** and paste:
   ```toml
   [github]
   token  = "github_pat_xxxxxxxx"
   repo   = "your-name/your-repo"
   branch = "main"
   path   = "products.csv"
   ```
3. Each save now creates a commit. Before you push from your own computer next time, run **`git pull`** first,
   or the push will be rejected because the website added commits.

When running locally (`run.bat`), saves go to `products.csv` in the folder. Push it as usual.

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
| `catalog.json` | Name/unit aliases (e.g. ปี๊บ → ปิ๊บ) |
| `products.py` | Product list logic: matching, Sup no. sort, Excel import/export, GitHub save |
| `products.csv` | **Product list (Data)**: หมวดหมู่, รายการ, หน่วยนับ, ร้านหลัก, Sup no., ชื่อใน PO. Edited in the 🗂️ tab |

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

**Categories and sort order (Data).** Each PO item is looked up by name in `products.csv`, ignoring spaces
and any extra spellings listed in **ชื่อใน PO**. หมวดหมู่ comes from the product list. Rows are sorted by
**Sup no.**: the letter in the order A → C → M → S → X, then the number (A-001, A-002 …), then the row order of the product list.
Items not in the list go to the bottom with a blank หมวดหมู่, and the daily tab offers to link them or add them.
Sup no. appears on the web page only; it is not a column in the Excel file.

**Checks shown per file.** Each line's qty × price is compared with the printed amount, the sum is compared with the printed total,
row numbers must be continuous, and both dates must be found. The Excel also keeps the template's
`Original PO Total` and `Variance Check` rows.

**Limits.** The PDFs must be text-based, which these are. A scanned or photographed PO has no text layer and would need OCR first.
The parser expects this PO form's 8-column table (ลำดับ | รายการ | หน่วยนับ | จำนวน | หน่วยละ บาท/สต. | จำนวนเงิน บาท/สต.).
It re-reads the header row when present, so moved columns are handled.
