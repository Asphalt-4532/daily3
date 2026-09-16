"""HTTP layer for VAT Input Tax Summary report.
Business logic in app/programs/expenses/services/vat_report.py.
"""
from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse

from app.templates_env import templates
from app.programs.expenses.services import vat_report as vat_svc
from app.programs.expenses.services import reports as reports_svc
from app.routes import guards

router = APIRouter()


@router.get("/vat-report")
def vat_report_page(request: Request, start: str = "", end: str = ""):
    user, deny = guards.require_login_page(request)
    if deny:
        return deny
    default_start, default_end = reports_svc.default_date_range()
    start = start or default_start
    end = end or default_end
    summary = vat_svc.vat_summary(start, end)
    return templates.TemplateResponse("expenses/vat_report.html", {
        "request": request, "user": user,
        "start": start, "end": end,
        "summary": summary,
    })


@router.get("/vat-report/export.xlsx")
def vat_export_xlsx(request: Request, start: str = "", end: str = ""):
    user, deny = guards.require_login_page(request)
    if deny:
        return deny
    default_start, default_end = reports_svc.default_date_range()
    start = start or default_start
    end = end or default_end
    summary = vat_svc.vat_summary(start, end)
    document_no = vat_svc.allocate_document_number("VAT")
    filters_desc = f"Period: {start} to {end} · Status: Approved · VAT-able lines only"
    buf = vat_svc.build_vat_xlsx(
        summary,
        document_no=document_no,
        generated_by=user["full_name"],
        filters_description=filters_desc,
    )
    filename = f"vat_summary_{start}_to_{end}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/vat-report/export.pdf")
def vat_export_pdf(request: Request, start: str = "", end: str = ""):
    user, deny = guards.require_login_page(request)
    if deny:
        return deny
    default_start, default_end = reports_svc.default_date_range()
    start = start or default_start
    end = end or default_end
    summary = vat_svc.vat_summary(start, end)
    document_no = vat_svc.allocate_document_number("VAT")
    filters_desc = f"Period: {start} to {end} · Status: Approved · VAT-able lines only"
    pdf_bytes = vat_svc.build_vat_pdf(
        summary,
        document_no=document_no,
        generated_by=user["full_name"],
        filters_description=filters_desc,
    )
    filename = f"vat_summary_{start}_to_{end}.pdf"
    return Response(
        content=pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
