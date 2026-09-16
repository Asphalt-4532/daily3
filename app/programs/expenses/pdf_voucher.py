"""Generates a standard-format petty cash voucher PDF for a single voucher,
which may hold multiple transaction lines (some VAT-able, some not). A4
portrait, as required for all PDF output from this app - see
app/pdf_reports.py for the tabular report PDFs that share the same page
size and document-header conventions."""
import io
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from app.config import COMPANY_NAME, COMPANY_ADDRESS, CURRENCY_SYMBOL
from app.pdf_common import safe_text, standard_table_style

_ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
          "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
          "Seventeen", "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _two_digit_words(n):
    if n < 20:
        return _ONES[n]
    return (_TENS[n // 10] + (" " + _ONES[n % 10] if n % 10 else "")).strip()


def _three_digit_words(n):
    if n >= 100:
        return _ONES[n // 100] + " Hundred" + (" " + _two_digit_words(n % 100) if n % 100 else "")
    return _two_digit_words(n)


def amount_to_words(amount: float) -> str:
    """Convert an amount to words, e.g. 1520.50 -> 'One Thousand Five Hundred
    Twenty and 50/100'."""
    rupees = int(amount)
    paise = round((amount - rupees) * 100)
    if rupees == 0:
        words = "Zero"
    else:
        parts = []
        crore = rupees // 10000000
        rupees %= 10000000
        lakh = rupees // 100000
        rupees %= 100000
        thousand = rupees // 1000
        rupees %= 1000
        hundred = rupees
        if crore:
            parts.append(_three_digit_words(crore) + " Crore")
        if lakh:
            parts.append(_three_digit_words(lakh) + " Lakh")
        if thousand:
            parts.append(_three_digit_words(thousand) + " Thousand")
        if hundred:
            parts.append(_three_digit_words(hundred))
        words = " ".join(parts) if parts else "Zero"
    return f"{words} and {paise:02d}/100" if paise else words


def _money(value) -> str:
    return f"{CURRENCY_SYMBOL} {float(value or 0):,.2f}"


def build_voucher_pdf(expense: dict, cost_center_name: str,
                       prepared_by_name: str, approved_by_name: str,
                       printed_by_name: str) -> bytes:
    """Returns the PDF file bytes for a single petty cash voucher, A4
    portrait. `expense` must include a "lines" list (see
    app.programs.expenses.services.expenses.get_expense). printed_by_name is
    whoever is viewing/downloading this copy right now - distinct from
    prepared_by/approved_by, which record who actioned the voucher itself."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, pageCompression=0,
        topMargin=22 * mm, bottomMargin=20 * mm,
        leftMargin=25 * mm, rightMargin=25 * mm,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("VTitle", parent=styles["Heading2"], alignment=1, spaceAfter=2)
    addr_style = ParagraphStyle("VAddr", parent=styles["Normal"], alignment=1, fontSize=8.5,
                                 textColor=colors.grey, spaceAfter=2)
    sub_style = ParagraphStyle("VSub", parent=styles["Normal"], alignment=1, textColor=colors.grey)
    label_style = ParagraphStyle("Label", parent=styles["Normal"], fontSize=9, textColor=colors.grey)
    value_style = ParagraphStyle("Value", parent=styles["Normal"], fontSize=11)
    head_cell = ParagraphStyle("HeadCell", parent=styles["Normal"], fontSize=8, textColor=colors.white,
                                fontName="Helvetica-Bold")
    cell = ParagraphStyle("Cell", parent=styles["Normal"], fontSize=9)

    story = []
    story.append(Paragraph(COMPANY_NAME, title_style))
    if COMPANY_ADDRESS:
        story.append(Paragraph(safe_text(COMPANY_ADDRESS), addr_style))
    story.append(Paragraph("PETTY CASH VOUCHER", sub_style))
    story.append(Spacer(1, 10 * mm))

    status_label = {"pending": "AWAITING APPROVAL", "approved": "APPROVED",
                     "rejected": "REJECTED"}.get(expense["status"], expense["status"].upper())

    header_data = [
        [Paragraph("Voucher No.", label_style), Paragraph("Date", label_style)],
        [Paragraph(f"<b>{safe_text(expense['voucher_no'])}</b>", value_style),
         Paragraph(safe_text(expense["expense_date"]), value_style)],
    ]
    header_table = Table(header_data, colWidths=[90 * mm, 70 * mm])
    header_table.setStyle(TableStyle([
        ("BOTTOMPADDING", (0, 0), (-1, 0), 2),
        ("TOPPADDING", (0, 1), (-1, 1), 0),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 7 * mm))

    rows = [
        ["Cost centre / Department", cost_center_name or "-"],
        ["Paid to", expense["paid_to"]],
        ["Payment mode", expense["payment_mode"]],
        ["Status", status_label],
    ]
    detail_table = Table(
        [[Paragraph(k, label_style), Paragraph(safe_text(v), value_style)] for k, v in rows],
        colWidths=[55 * mm, 105 * mm],
    )
    detail_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, colors.whitesmoke),
    ]))
    story.append(detail_table)
    story.append(Spacer(1, 6 * mm))

    # Itemized transaction lines - a voucher can hold several, some VAT-able
    # and some not.
    line_headers = ["#", "Category", "Particulars", "VAT?", "Amount (excl. VAT)", "Total"]
    line_data = [[Paragraph(h, head_cell) for h in line_headers]]
    for i, line in enumerate(expense.get("lines", []), start=1):
        line_total = line["amount"] + (line.get("vat_amount") or 0)
        particulars = line["particulars"]
        if line["is_vatable"] and line.get("supplier_name"):
            particulars += f" ({line['supplier_name']}, VAT: {line['supplier_vat']})"
        if line.get("employee_name"):
            particulars += f" — {line['employee_name']}, {line.get('employee_phone', '')}"
        line_data.append([
            Paragraph(str(i), cell),
            Paragraph(safe_text(line["category_name"]), cell),
            Paragraph(safe_text(particulars), cell),
            Paragraph("Yes" if line["is_vatable"] else "No", cell),
            Paragraph(_money(line["amount"]), cell),
            Paragraph(_money(line_total), cell),
        ])
    lines_table = Table(line_data, colWidths=[7 * mm, 30 * mm, 45 * mm, 13 * mm, 28 * mm, 27 * mm])
    lines_table.setStyle(TableStyle(standard_table_style()))
    story.append(lines_table)
    story.append(Spacer(1, 4 * mm))

    total_amount = expense.get("total_amount", 0) or 0
    total_vat = expense.get("total_vat", 0) or 0
    net_amount = total_amount - total_vat
    summary_rows = [
        ["Subtotal (excl. VAT)", _money(net_amount)],
        ["VAT", _money(total_vat)],
    ]
    summary_table = Table(
        [[Paragraph(k, label_style), Paragraph(v, value_style)] for k, v in summary_rows],
        colWidths=[123 * mm, 27 * mm],
    )
    summary_table.setStyle(TableStyle([
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2), ("TOPPADDING", (0, 0), (-1, -1), 2),
    ]))
    story.append(summary_table)

    total_table = Table(
        [[Paragraph("Total amount", label_style),
          Paragraph(f"<b>{_money(total_amount)}</b>",
                     ParagraphStyle("Amt", parent=value_style, fontSize=15))]],
        colWidths=[123 * mm, 27 * mm],
    )
    total_table.setStyle(TableStyle([("ALIGN", (1, 0), (1, -1), "RIGHT")]))
    story.append(total_table)
    story.append(Paragraph(
        f"<i>Rupees {amount_to_words(total_amount)} only</i>",
        ParagraphStyle("Words", parent=styles["Normal"], fontSize=9, spaceBefore=3, spaceAfter=10)
    ))

    story.append(Spacer(1, 10 * mm))
    sign_table = Table(
        [["Prepared by", "Approved by"],
         ["", ""],
         [safe_text(prepared_by_name) or "-", safe_text(approved_by_name) or "(pending)"]],
        colWidths=[80 * mm, 80 * mm],
        rowHeights=[6 * mm, 16 * mm, 6 * mm],
    )
    sign_table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, 0), 9),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.grey),
        ("LINEABOVE", (0, 2), (0, 2), 0.6, colors.black),
        ("LINEABOVE", (1, 2), (1, 2), 0.6, colors.black),
        ("FONTSIZE", (0, 2), (-1, 2), 9),
    ]))
    story.append(sign_table)

    if expense["status"] == "approved" and expense.get("approved_at"):
        story.append(Spacer(1, 6 * mm))
        story.append(Paragraph(
            f"Approved on {safe_text(expense['approved_at'])}",
            ParagraphStyle("Foot", parent=styles["Normal"], fontSize=8, textColor=colors.grey)
        ))

    story.append(Spacer(1, 8 * mm))
    story.append(Paragraph(
        f"Printed by {safe_text(printed_by_name)} on {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        ParagraphStyle("Printed", parent=styles["Normal"], fontSize=8, textColor=colors.grey)
    ))
    story.append(Paragraph(
        f"Voucher {safe_text(expense['voucher_no'])} — Expense Program",
        ParagraphStyle("Gen", parent=styles["Normal"], fontSize=7, textColor=colors.lightgrey)
    ))

    doc.build(story)
    return buf.getvalue()
