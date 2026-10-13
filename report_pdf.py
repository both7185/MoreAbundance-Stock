"""
PDF version of the PO summary (same table as the Excel from report_builder.build_workbook),
without the ทุน / ทุนรวม / กำไร / กำไรรวม columns: it stops at รวมทั้งสิ้น (จำนวนเงินรวม).

The Thai font (Sarabun, SIL Open Font License) is bundled in fonts/ so the PDF looks the same
locally and on Streamlit Cloud. The title and the two table header rows repeat on every page.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from fpdf import FPDF
from fpdf.fonts import FontFace

from report_builder import FIXED_COLS, Summary, _header_width, col_label, date_range_label

FONT_DIR = Path(__file__).with_name("fonts")
FONT = "Sarabun"

C_GROUP = (0x1F, 0x49, 0x7D)    # same colours as the Excel header rows
C_HEADER = (0x2F, 0x55, 0x97)
C_STRIPE = (0xDD, 0xEB, 0xF7)
C_WHITE = (255, 255, 255)

# width per column in Excel units; scaled to the page width
DATE_W = 10
TOTAL_W = 15


def _qty(v: float) -> str:
    """1,234 / 2.5 - like the Excel's #,##0 (decimals only when the value has them)."""
    if float(v).is_integer():
        return f"{int(v):,}"
    return f"{v:,.4f}".rstrip("0").rstrip(".") if round(v, 2) != v else f"{v:,.2f}"


def _money(v: float) -> str:
    """#,##0.00; extra decimals only if the value really has them."""
    if round(v, 2) == v:
        return f"{v:,.2f}"
    return f"{v:,.6f}".rstrip("0")


def _dsum(values) -> float:
    return float(sum((Decimal(str(v)) for v in values), Decimal(0)))


class _SummaryPDF(FPDF):
    def __init__(self, title: str, subtitle: str):
        super().__init__(orientation="L", unit="mm", format="A4")
        self.title_text, self.subtitle_text = title, subtitle
        self.set_margins(8, 8, 8)
        self.set_auto_page_break(True, margin=12)
        self.add_font(FONT, "", str(FONT_DIR / "Sarabun-Regular.ttf"))
        self.add_font(FONT, "B", str(FONT_DIR / "Sarabun-Bold.ttf"))
        self.set_text_shaping(True)   # correct placement of Thai vowel / tone marks

    def header(self):
        self.set_text_color(0, 0, 0)
        self.set_font(FONT, "B", 14)
        self.cell(0, 7, self.title_text, new_x="LMARGIN", new_y="NEXT")
        self.set_font(FONT, "", 11)
        self.cell(0, 6, self.subtitle_text, new_x="LMARGIN", new_y="NEXT")
        self.ln(2)

    def footer(self):
        self.set_y(-10)
        self.set_text_color(90, 90, 90)
        self.set_font(FONT, "", 8)
        self.cell(0, 5, f"หน้า {self.page_no()}/{{nb}}", align="R")


def build_pdf(summary: Summary, po_number_label: str | None = None) -> bytes:
    """
      ลำดับ | หมวดหมู่ | รายการสินค้า | หน่วย | จำนวนรวม | ราคา/หน่วย | one จำนวน per date + tag | จำนวนเงินรวม
    then the same 3 footer rows as the Excel (Calculated Total, Original PO Total, Variance Check).
    """
    po_label = po_number_label if po_number_label is not None else ", ".join(summary.po_numbers)
    subtitle = f"เปรียบเทียบและรวมยอดใบสั่งซื้อประจำวันที่ {date_range_label(summary.dates)}"
    if po_label:
        subtitle += f" (ใบสั่งที่ {po_label})"
    pdf = _SummaryPDF("ตารางสรุปและรวมรายการใบสั่งซื้อวัตถุดิบประกอบอาหาร", subtitle)
    pdf.add_page()

    cols = summary.columns
    n_fixed, n_dates = len(FIXED_COLS), len(cols)
    # same proportions as the Excel; ลำดับ a bit wider and รายการสินค้า wider so names wrap less
    fixed_w = {"ลำดับ": 7, "รายการสินค้า": 30}
    widths = ([fixed_w.get(h, w) for h, w in FIXED_COLS]
              + [_header_width(f"({tag})") if tag else DATE_W for _, tag in cols] + [TOTAL_W])
    # many date columns: smaller text so the table still fits the page width
    size = max(6.0, min(9.0, round(9 * 140 / sum(widths), 1)))
    pdf.set_font(FONT, "", size)
    pdf.set_draw_color(0, 0, 0)
    pdf.set_line_width(0.2)

    head_group = FontFace(family=FONT, emphasis="B", color=C_WHITE, fill_color=C_GROUP)
    head = FontFace(family=FONT, emphasis="B", color=C_WHITE, fill_color=C_HEADER)
    plain = FontFace(family=FONT, emphasis="", fill_color=C_WHITE)
    plain_b = FontFace(family=FONT, emphasis="B", fill_color=C_WHITE)
    stripe = FontFace(family=FONT, emphasis="", fill_color=C_STRIPE)
    stripe_b = FontFace(family=FONT, emphasis="B", fill_color=C_STRIPE)

    with pdf.table(
        col_widths=widths,
        width=pdf.epw,
        num_heading_rows=2,
        repeat_headings=1,
        headings_style=head,
        line_height=size * 0.5,
        padding=(0.8, 1),
        text_align="LEFT",
        v_align="MIDDLE",
    ) as table:
        # header row 1: group / dates / รวมทั้งสิ้น
        r = table.row()
        r.cell("รายละเอียดสินค้า", colspan=n_fixed, align="C", style=head_group)
        for col in cols:
            r.cell(col_label(col, "\n"), align="C", style=head_group)
        r.cell("รวมทั้งสิ้น", align="C", style=head_group)
        # header row 2: column names
        r = table.row()
        for h in [h for h, _ in FIXED_COLS] + ["จำนวน"] * n_dates + ["จำนวนเงินรวม"]:
            r.cell(h, align="C", style=head)

        # body
        for idx, s in enumerate(summary.rows):
            st, st_b = (stripe, stripe_b) if idx % 2 else (plain, plain_b)
            total_qty = _dsum(s.qty_by_col.values())
            r = table.row()
            r.cell(str(idx + 1), align="R", style=st)
            r.cell(s.category or "", style=st)
            r.cell(s.name, style=st)
            r.cell(s.unit, style=st)
            r.cell(_qty(total_qty), align="R", style=st_b)
            r.cell(_money(s.unit_price), align="R", style=st)
            for col in cols:
                r.cell(_qty(s.qty_by_col[col]) if col in s.qty_by_col else "", align="R", style=st)
            amount = float(Decimal(str(total_qty)) * Decimal(str(s.unit_price)))
            r.cell(_money(round(amount, 6)), align="R", style=st_b)

        # footer: totals, PO totals, variance
        calc_total = _dsum(
            Decimal(str(_dsum(s.qty_by_col.values()))) * Decimal(str(s.unit_price)) for s in summary.rows)
        orig_total = _dsum(summary.po_total_by_col.values())
        variance = float(Decimal(str(calc_total)) - Decimal(str(orig_total)))
        variance = 0.0 if abs(variance) < 0.005 else variance   # never "-0.00"
        footer_rows = [
            ("รวมทั้งสิ้น (Calculated Total)", None, calc_total),
            ("ยอดรวมตามใบสั่งซื้อต้นฉบับ (Original PO Total)", "-", orig_total),
            ("ผลต่าง / ตรวจสอบความถูกต้อง (Variance Check)", "-", variance),
        ]
        for label, qty_text, money in footer_rows:
            r = table.row()
            r.cell(label, colspan=4, align="R", style=plain_b)
            if qty_text is None:   # Calculated Total: sum of each quantity column
                r.cell(_qty(_dsum(q for s in summary.rows for q in s.qty_by_col.values())), align="R", style=plain_b)
                r.cell("", style=plain_b)
                for col in cols:
                    r.cell(_qty(_dsum(s.qty_by_col[col] for s in summary.rows if col in s.qty_by_col)),
                           align="R", style=plain_b)
            else:
                r.cell(qty_text, align="C", style=plain_b)
                r.cell("", style=plain_b)
                for _ in cols:
                    r.cell(qty_text, align="C", style=plain_b)
            r.cell(_money(round(money, 6)), align="R", style=plain_b)

    return bytes(pdf.output())
