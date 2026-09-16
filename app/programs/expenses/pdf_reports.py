"""Generates the Expense Program's report PDFs: transaction listings and
spend summaries. A4, using the shared document conventions from
app.pdf_common (pre-numbered header, footer, text escaping) - see that
module if you're building a PDF report for a different program; this file
is Expense Program-specific and shouldn't be imported from anywhere else.

Every function here takes a `document_no` that the caller is responsible for
allocating via app.database.next_document_number() before calling in - this
module only renders, it never touches the database.
"""
import io

from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import ParagraphStyle

from app.pdf_common import (
    safe_text, money, styles, HEADCELL, CELL, META, TOTAL, document_header, footer,
    standard_table_style,
)

__all__ = ["build_transaction_listing_pdf", "build_summary_report_pdf"]


def build_transaction_listing_pdf(rows, *, title, document_no, generated_by, filters_description):
    """A4 landscape table of vouchers - used for both the Expenses page
    export (any filter combination) and the Reports page listing export."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=landscape(A4), pageCompression=0,
        topMargin=15 * mm, bottomMargin=18 * mm, leftMargin=15 * mm, rightMargin=15 * mm,
    )
    story = document_header(title, document_no, generated_by, filters_description)

    headers = ["Voucher No", "Date", "Category", "Paid To", "Amount (excl. VAT)", "VAT?",
               "VAT Amt", "Total", "Status"]
    data = [[Paragraph(h, HEADCELL) for h in headers]]
    grand_total = 0.0
    grand_vat = 0.0
    grand_gross = 0.0
    for r in rows:
        vat_amount = r.get("vat_amount") or 0
        line_total = r.get("line_total", r["amount"] + vat_amount)
        data.append([
            Paragraph(safe_text(r["voucher_no"]), CELL), Paragraph(safe_text(r["expense_date"]), CELL),
            Paragraph(safe_text(r.get("category") or r.get("category_name") or ""), CELL),
            Paragraph(safe_text(r["paid_to"]), CELL), Paragraph(money(r["amount"]), CELL),
            Paragraph("Yes" if r.get("is_vatable") else "No", CELL),
            Paragraph(money(vat_amount), CELL),
            Paragraph(money(line_total), CELL),
            Paragraph(safe_text(str(r["status"]).capitalize()), CELL),
        ])
        grand_total += r["amount"]
        grand_vat += vat_amount
        grand_gross += line_total
    data.append([
        "", "", "", Paragraph("<b>TOTAL</b>", CELL), Paragraph(f"<b>{money(grand_total)}</b>", CELL),
        "", Paragraph(f"<b>{money(grand_vat)}</b>", CELL), Paragraph(f"<b>{money(grand_gross)}</b>", CELL), "",
    ])

    table = Table(data, colWidths=[30 * mm, 18 * mm, 36 * mm, 42 * mm, 24 * mm, 15 * mm,
                                    22 * mm, 24 * mm, 22 * mm],
                   repeatRows=1)
    table.setStyle(TableStyle(standard_table_style(
        zebra_end=-2, valign="MIDDLE",
        extra=[
            ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.white),
            ("LINEBELOW", (0, -2), (-1, -2), 0.8, colors.grey),
        ],
    )))
    story.append(table)

    if not rows:
        story.append(Spacer(1, 6 * mm))
        story.append(Paragraph("No vouchers match the selected filters.", META))

    doc.build(story, onFirstPage=footer(document_no, "Expense Program"), onLaterPages=footer(document_no, "Expense Program"))
    return buf.getvalue()


def build_summary_report_pdf(by_category, by_cost_center, grand_total, *, title, document_no,
                              generated_by, filters_description):
    """A4 portrait summary: approved spend broken down by category and by
    cost centre, matching what the Reports page shows on screen."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, pageCompression=0,
        topMargin=20 * mm, bottomMargin=18 * mm, leftMargin=22 * mm, rightMargin=22 * mm,
    )
    story = document_header(title, document_no, generated_by, filters_description)

    def _breakdown_table(heading, rows):
        story.append(Paragraph(heading, ParagraphStyle(
            "SecHead", parent=styles["Heading4"], spaceBefore=6, spaceAfter=4)))
        data = [[Paragraph("Name", HEADCELL), Paragraph("Total", HEADCELL)]]
        for r in rows:
            if r["total"]:
                data.append([Paragraph(safe_text(r["name"]), CELL), Paragraph(money(r["total"]), CELL)])
        if len(data) == 1:
            story.append(Paragraph("No approved spend in this period.", META))
            return
        table = Table(data, colWidths=[110 * mm, 40 * mm])
        table.setStyle(TableStyle(standard_table_style(valign=None)))
        story.append(table)
        story.append(Spacer(1, 6 * mm))

    _breakdown_table("By category", by_category)
    _breakdown_table("By cost centre", by_cost_center)

    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph(f"Grand total: {money(grand_total)}", TOTAL))

    doc.build(story, onFirstPage=footer(document_no, "Expense Program"), onLaterPages=footer(document_no, "Expense Program"))
    return buf.getvalue()
