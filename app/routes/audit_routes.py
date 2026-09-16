"""HTTP layer for the system-wide audit log. Business logic lives in
app/services/audit.py. Shared infrastructure - not owned by any one
program, since every program logs into the same audit_log table."""
from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse

from app.auth import can_view_audit
from app.templates_env import templates
from app.services import audit as audit_service
from app.routes import guards

router = APIRouter()


@router.get("/audit", response_class=HTMLResponse)
def audit_log(request: Request, start: str = "", end: str = ""):
    user, deny = guards.require_role_page(request, can_view_audit)
    if deny:
        return deny
    entries = audit_service.list_audit_log(start=start, end=end)
    return templates.TemplateResponse("audit.html", {
        "request": request, "user": user, "entries": entries,
        "filters": {"start": start, "end": end},
    })


@router.get("/audit/export.xlsx")
def audit_export_xlsx(request: Request, start: str = "", end: str = ""):
    user, deny = guards.require_role_page(request, can_view_audit)
    if deny:
        return deny
    entries = audit_service.list_audit_log(limit=0, start=start, end=end)
    filters_desc = audit_service.describe_filters(start, end)
    document_no = audit_service.allocate_document_number("AUD")
    buf = audit_service.build_audit_export_workbook(
        entries, document_no=document_no, generated_by=user["full_name"], filters_description=filters_desc,
    )
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="audit_log.xlsx"'},
    )


@router.get("/audit/export.pdf")
def audit_export_pdf(request: Request, start: str = "", end: str = ""):
    user, deny = guards.require_role_page(request, can_view_audit)
    if deny:
        return deny
    entries = audit_service.list_audit_log(limit=0, start=start, end=end)
    filters_desc = audit_service.describe_filters(start, end)
    pdf_bytes = audit_service.build_audit_export_pdf(
        entries, generated_by=user["full_name"], filters_description=filters_desc,
    )
    return Response(content=pdf_bytes, media_type="application/pdf",
                     headers={"Content-Disposition": 'attachment; filename="audit_log.pdf"'})
