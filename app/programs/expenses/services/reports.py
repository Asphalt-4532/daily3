"""Business logic for spend reports and export (Excel + PDF).

Concern: aggregating approved spend for display and export, and generating
the downloadable artifacts. Read-only against voucher data - the only writes
this module makes are allocating a document number for each generated
report/export (see app.database.next_document_number).

Exports are at *line* granularity, not voucher granularity - a voucher can
hold several transactions in different categories with different VAT
treatment, so a listing that showed one row per voucher couldn't correctly
attribute amounts to categories or VAT. Voucher-level fields (voucher_no,
date, paid_to, status) repeat across every line belonging to that voucher.

Depends on: app.database (persistence + document numbering), openpyxl
(spreadsheet generation), app.pdf_reports (PDF rendering) - both kept behind
this module, so nothing else in the app needs to know which libraries are
used; upgrading either later means editing exactly one file.
Used by: app.programs.expenses.routes.report_routes, .expense_routes (for
the Expenses page's own Excel/PDF export, which reuses export_rows() with
its fuller filter set).

No FastAPI imports on purpose - see app/services/errors.py.
"""
import io
from datetime import date, datetime, timedelta

from openpyxl import Workbook
from openpyxl.styles import Font

from app.database import get_db, next_document_number
from app.programs.expenses import pdf_reports
from app.excel_common import write_metadata_block, style_header_row

__all__ = [
    "default_date_range", "spend_breakdown", "export_rows", "describe_filters",
    "allocate_document_number", "build_export_workbook", "build_export_pdf",
    "build_summary_pdf",
]


def default_date_range():
    start = (date.today() - timedelta(days=180)).isoformat()
    end = date.today().isoformat()
    return start, end


def describe_filters(start="", end="", status="", category_name="", cost_center_name=""):
    """Human-readable summary of what's been filtered, shown on every
    exported document so it's clear exactly what data it covers."""
    parts = []
    if start or end:
        parts.append(f"Period: {start or 'earliest'} to {end or 'latest'}")
    if status:
        parts.append(f"Status: {status.capitalize()}")
    if category_name:
        parts.append(f"Category: {category_name}")
    if cost_center_name:
        parts.append(f"Cost centre: {cost_center_name}")
    return " · ".join(parts) if parts else "All records"


def allocate_document_number(doc_type="RPT"):
    with get_db() as conn:
        return next_document_number(conn, doc_type, str(date.today().year))


def spend_breakdown(start, end):
    """Returns (by_category, by_cost_center, monthly, grand_total) for
    approved expenses in the given date range, summed at line level. Totals
    are gross (each line's amount plus its VAT, when VAT-able)."""
    with get_db() as conn:
        by_category = [dict(r) for r in conn.execute(
            "SELECT c.name, COALESCE(SUM(el.line_total),0) total FROM categories c "
            "LEFT JOIN ("
            "  SELECT el.category_id, (el.amount + el.vat_amount) AS line_total FROM expense_lines el "
            "  JOIN expenses e ON e.id = el.expense_id "
            "  WHERE e.status='approved' AND e.expense_date BETWEEN ? AND ?"
            ") el ON el.category_id = c.id "
            "GROUP BY c.id ORDER BY total DESC", (start, end)
        ).fetchall()]
        by_cost_center = [dict(r) for r in conn.execute(
            "SELECT cc.name, COALESCE(SUM(el.amount + el.vat_amount),0) total FROM cost_centers cc "
            "LEFT JOIN expenses e ON e.cost_center_id = cc.id AND e.status='approved' "
            "AND e.expense_date BETWEEN ? AND ? "
            "LEFT JOIN expense_lines el ON el.expense_id = e.id "
            "GROUP BY cc.id ORDER BY total DESC", (start, end)
        ).fetchall()]
        monthly = [dict(r) for r in conn.execute(
            "SELECT strftime('%Y-%m', e.expense_date) ym, SUM(el.amount + el.vat_amount) total "
            "FROM expense_lines el JOIN expenses e ON e.id = el.expense_id "
            "WHERE e.status='approved' AND e.expense_date BETWEEN ? AND ? "
            "GROUP BY ym ORDER BY ym", (start, end)
        ).fetchall()]
        grand_total = sum(r["total"] for r in by_category)
        return by_category, by_cost_center, monthly, grand_total


def export_rows(start="", end="", status="approved", category_id="", cost_center_id=""):
    """One row per transaction line. Same filter set as the Expenses page
    (status/category/cost-centre/date range) so both it and the Reports
    page can export through one function. `amount` is VAT-exclusive as
    entered; `line_total` is amount + vat_amount (what that line actually
    contributes to the voucher total)."""
    with get_db() as conn:
        query = (
            "SELECT e.voucher_no, e.expense_date, c.name category, cc.name cost_center, "
            "e.paid_to, el.particulars, el.amount, el.is_vatable, el.vat_rate, el.vat_amount, "
            "(el.amount + el.vat_amount) AS line_total, "
            "s.name supplier_name, s.vat_number supplier_vat, "
            "e.payment_mode, e.status, u1.full_name prepared_by, u2.full_name approved_by "
            "FROM expense_lines el "
            "JOIN expenses e ON e.id = el.expense_id "
            "JOIN categories c ON c.id = el.category_id "
            "LEFT JOIN cost_centers cc ON cc.id = e.cost_center_id "
            "LEFT JOIN suppliers s ON s.id = el.supplier_id "
            "JOIN users u1 ON u1.id = e.prepared_by "
            "LEFT JOIN users u2 ON u2.id = e.approved_by WHERE 1=1"
        )
        params = []
        if start:
            query += " AND e.expense_date >= ?"
            params.append(start)
        if end:
            query += " AND e.expense_date <= ?"
            params.append(end)
        if status:
            query += " AND e.status = ?"
            params.append(status)
        if category_id:
            query += " AND el.category_id = ?"
            params.append(category_id)
        if cost_center_id:
            query += " AND e.cost_center_id = ?"
            params.append(cost_center_id)
        query += " ORDER BY e.expense_date, e.id, el.line_no"
        return [dict(r) for r in conn.execute(query, params).fetchall()]


def build_export_workbook(rows, *, document_no, generated_by, filters_description):
    wb = Workbook()
    ws = wb.active
    ws.title = "Petty Cash"
    header_row = write_metadata_block(ws, "Petty Cash Transaction Listing",
                                       document_no, generated_by, filters_description)

    headers = ["Voucher No", "Date", "Category", "Cost Centre", "Paid To", "Particulars",
               "Amount (excl. VAT)", "VAT-able", "VAT Amount", "Line Total (incl. VAT)",
               "Supplier", "Supplier VAT",
               "Payment Mode", "Status", "Prepared By", "Approved By"]
    ws.append(headers)
    style_header_row(ws, header_row)

    total = 0.0
    total_vat = 0.0
    total_gross = 0.0
    for r in rows:
        ws.append([r["voucher_no"], r["expense_date"], r["category"], r["cost_center"] or "",
                   r["paid_to"], r["particulars"], r["amount"],
                   "Yes" if r["is_vatable"] else "No", r["vat_amount"], r["line_total"],
                   r["supplier_name"] or "", r["supplier_vat"] or "",
                   r["payment_mode"], r["status"], r["prepared_by"], r["approved_by"] or ""])
        total += r["amount"]
        total_vat += r["vat_amount"] or 0
        total_gross += r["line_total"]

    ws.append([])
    ws.append(["", "", "", "", "", "TOTAL", total, "", total_vat, total_gross])
    ws.cell(row=ws.max_row, column=6).font = Font(bold=True)
    ws.cell(row=ws.max_row, column=7).font = Font(bold=True)
    ws.cell(row=ws.max_row, column=9).font = Font(bold=True)
    ws.cell(row=ws.max_row, column=10).font = Font(bold=True)

    for col, width in zip("ABCDEFGHIJKLMN", [16, 12, 22, 16, 20, 30, 14, 9, 11, 16, 14, 11, 16, 16]):
        ws.column_dimensions[col].width = width

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def build_export_pdf(rows, *, generated_by, filters_description):
    document_no = allocate_document_number("RPT")
    return pdf_reports.build_transaction_listing_pdf(
        rows, title="Petty Cash Transaction Listing", document_no=document_no,
        generated_by=generated_by, filters_description=filters_description,
    )


def build_summary_pdf(by_category, by_cost_center, grand_total, *, generated_by, filters_description):
    document_no = allocate_document_number("RPT")
    return pdf_reports.build_summary_report_pdf(
        by_category, by_cost_center, grand_total, title="Spend Summary Report",
        document_no=document_no, generated_by=generated_by, filters_description=filters_description,
    )
