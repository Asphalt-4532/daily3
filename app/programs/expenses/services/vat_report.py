"""VAT input tax summary service.

Shows total VAT paid to suppliers on approved vatable expense lines,
grouped by supplier (name + VAT number). Purpose: give the cost accountant
a single-page summary to cross-check against QOYOD entries, not to
generate a ZATCA filing directly.

Per supplier row:
  - trx_count:    number of expense_lines that are vatable and reference this supplier
  - voucher_count: DISTINCT expense_id count (how many vouchers)
  - net_amount:   SUM(amount) (VAT-exclusive)
  - vat_amount:   SUM(vat_amount)
  - gross_amount: SUM(amount + vat_amount)

Lines with no supplier (is_vatable=1 but supplier_id is NULL) are grouped
under a synthetic "(No supplier recorded)" row.

Only approved expenses are included (confirmed spend, not pending).

No FastAPI imports - see app/services/errors.py.
"""
import io
from datetime import date

from openpyxl import Workbook
from openpyxl.styles import Font

from app.database import get_db, next_document_number
from app.programs.expenses import pdf_reports
from app.excel_common import write_metadata_block, style_header_row
from app.pdf_common import (
    safe_text, money, styles, HEADCELL, CELL, META, TOTAL,
    document_header, footer, standard_table_style,
)
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer

__all__ = [
    "vat_summary",
    "build_vat_xlsx",
    "build_vat_pdf",
    "allocate_document_number",
]

_NO_SUPPLIER_LABEL = "(No supplier recorded)"
_NO_SUPPLIER_VAT   = "—"


def vat_summary(start: str = "", end: str = "") -> dict:
    """Return supplier-level VAT summary for approved vatable lines.

    Returns:
        {
          "rows": [...per-supplier dicts...],
          "grand": {trx_count, voucher_count, net_amount, vat_amount, gross_amount},
          "start": str, "end": str,
        }
    """
    with get_db() as conn:
        query = (
            "SELECT "
            "  COALESCE(s.name, ?) supplier_name, "
            "  COALESCE(s.vat_number, ?) supplier_vat, "
            "  COUNT(el.id) trx_count, "
            "  COUNT(DISTINCT el.expense_id) voucher_count, "
            "  SUM(el.amount) net_amount, "
            "  SUM(el.vat_amount) vat_amount, "
            "  SUM(el.amount + el.vat_amount) gross_amount "
            "FROM expense_lines el "
            "JOIN expenses e ON e.id = el.expense_id "
            "LEFT JOIN suppliers s ON s.id = el.supplier_id "
            "WHERE el.is_vatable = 1 "
            "  AND e.status = 'approved' "
        )
        params: list = [_NO_SUPPLIER_LABEL, _NO_SUPPLIER_VAT]
        if start:
            query += " AND e.expense_date >= ? "
            params.append(start)
        if end:
            query += " AND e.expense_date <= ? "
            params.append(end)
        query += " GROUP BY s.id ORDER BY gross_amount DESC"

        rows = [dict(r) for r in conn.execute(query, params).fetchall()]

    grand = {
        "trx_count":    sum(r["trx_count"]    for r in rows),
        "voucher_count": sum(r["voucher_count"] for r in rows),
        "net_amount":   sum(r["net_amount"]    for r in rows),
        "vat_amount":   sum(r["vat_amount"]    for r in rows),
        "gross_amount": sum(r["gross_amount"]  for r in rows),
    }
    return {"rows": rows, "grand": grand, "start": start, "end": end}


def allocate_document_number(doc_type: str = "VAT") -> str:
    with get_db() as conn:
        return next_document_number(conn, doc_type, str(date.today().year))


def build_vat_xlsx(
    summary: dict,
    *,
    document_no: str,
    generated_by: str,
    filters_description: str,
) -> io.BytesIO:
    wb = Workbook()
    ws = wb.active
    ws.title = "VAT Summary"

    header_row = write_metadata_block(
        ws, "VAT Input Tax Summary", document_no, generated_by, filters_description
    )

    headers = [
        "Supplier", "VAT Number",
        "Transactions", "Vouchers",
        "Net Amount (excl. VAT)", "VAT Paid", "Gross Amount",
    ]
    ws.append(headers)
    style_header_row(ws, header_row)

    for r in summary["rows"]:
        ws.append([
            r["supplier_name"], r["supplier_vat"],
            r["trx_count"], r["voucher_count"],
            r["net_amount"], r["vat_amount"], r["gross_amount"],
        ])

    g = summary["grand"]
    ws.append([])
    ws.append([
        "TOTAL", "",
        g["trx_count"], g["voucher_count"],
        g["net_amount"], g["vat_amount"], g["gross_amount"],
    ])
    total_row = ws.max_row
    for col in [1, 3, 4, 5, 6, 7]:
        ws.cell(row=total_row, column=col).font = Font(bold=True)

    for col, width in zip("ABCDEFG", [30, 20, 14, 12, 22, 16, 18]):
        ws.column_dimensions[col].width = width

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def build_vat_pdf(
    summary: dict,
    *,
    document_no: str,
    generated_by: str,
    filters_description: str,
) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=landscape(A4),
        leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=15 * mm, bottomMargin=20 * mm,
    )

    story = document_header(
        "VAT Input Tax Summary", document_no, generated_by, filters_description
    )
    story.append(Spacer(1, 6 * mm))

    col_widths = [90 * mm, 50 * mm, 30 * mm, 30 * mm, 42 * mm, 35 * mm, 42 * mm]
    table_data = [[
        Paragraph("Supplier", HEADCELL),
        Paragraph("VAT Number", HEADCELL),
        Paragraph("Trx", HEADCELL),
        Paragraph("Vouchers", HEADCELL),
        Paragraph("Net (excl. VAT)", HEADCELL),
        Paragraph("VAT Paid", HEADCELL),
        Paragraph("Gross", HEADCELL),
    ]]

    for r in summary["rows"]:
        table_data.append([
            Paragraph(safe_text(r["supplier_name"]), CELL),
            Paragraph(safe_text(r["supplier_vat"]), CELL),
            Paragraph(str(r["trx_count"]), CELL),
            Paragraph(str(r["voucher_count"]), CELL),
            Paragraph(money(r["net_amount"]), CELL),
            Paragraph(money(r["vat_amount"]), CELL),
            Paragraph(money(r["gross_amount"]), CELL),
        ])

    g = summary["grand"]
    table_data.append([
        Paragraph("TOTAL", TOTAL),
        Paragraph("", CELL),
        Paragraph(str(g["trx_count"]), TOTAL),
        Paragraph(str(g["voucher_count"]), TOTAL),
        Paragraph(money(g["net_amount"]), TOTAL),
        Paragraph(money(g["vat_amount"]), TOTAL),
        Paragraph(money(g["gross_amount"]), TOTAL),
    ])

    style = standard_table_style(zebra_end=len(table_data) - 2)
    t = Table(table_data, colWidths=col_widths, repeatRows=1)
    t.setStyle(style)
    story.append(t)

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
