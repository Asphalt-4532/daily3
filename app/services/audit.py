"""Business logic for reading and exporting the audit log.

Concern: read access to the immutable, system-wide action log. Every
program in this app (Expense Program today, others later) writes to the
same audit_log table via app.database.log_action() - this module is where
any of them read it back. It deliberately knows nothing about what any
specific program's action names mean.
Depends on: app.database (persistence + document numbering), openpyxl,
app.pdf_common (shared PDF document conventions).
Used by: app.routes.audit_routes.

No FastAPI imports on purpose - see app/services/errors.py.
"""
import io
from datetime import date

from openpyxl import Workbook

from app.database import get_db, next_document_number
from app import pdf_common
from app.excel_common import write_metadata_block, style_header_row

__all__ = [
    "list_audit_log", "describe_filters", "allocate_document_number",
    "build_audit_export_workbook", "build_audit_export_pdf",
]


def describe_filters(start: str = "", end: str = "") -> str:
    if start or end:
        return f"Period: {start or 'earliest'} to {end or 'latest'}"
    return "All records"


def allocate_document_number(doc_type: str = "AUD") -> str:
    with get_db() as conn:
        return next_document_number(conn, doc_type, str(date.today().year))


def list_audit_log(limit: int = 200, start: str = "", end: str = ""):
    with get_db() as conn:
        query = ("SELECT a.*, u.full_name FROM audit_log a "
                 "LEFT JOIN users u ON u.id = a.user_id WHERE 1=1")
        params = []
        if start:
            query += " AND date(a.created_at) >= ?"
            params.append(start)
        if end:
            query += " AND date(a.created_at) <= ?"
            params.append(end)
        query += " ORDER BY a.id DESC"
        if limit:
            query += " LIMIT ?"
            params.append(limit)
        return [dict(r) for r in conn.execute(query, params).fetchall()]


def build_audit_export_workbook(entries, *, document_no, generated_by, filters_description):
    wb = Workbook()
    ws = wb.active
    ws.title = "Audit Log"
    header_row = write_metadata_block(ws, "Audit Log", document_no, generated_by, filters_description)
    ws.append(["When", "User", "Action", "Details"])
    style_header_row(ws, header_row)
    for e in entries:
        ws.append([e["created_at"], e.get("full_name") or "system", e["action"], e.get("details") or ""])
    for col, width in zip("ABCD", [20, 24, 22, 60]):
        ws.column_dimensions[col].width = width
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def build_audit_export_pdf(entries, *, generated_by, filters_description):
    document_no = allocate_document_number("AUD")
    return pdf_common.build_audit_log_pdf(
        entries, document_no=document_no, generated_by=generated_by,
        filters_description=filters_description,
    )
