"""HTTP layer for spend reports and export (Excel + PDF). See
app/programs/expenses/routes/expense_routes.py docstring for the pattern -
all business logic lives in app/programs/expenses/services/reports.py."""
from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse

from app.templates_env import templates
from app.programs.expenses.services import reports as reports_service
from app.routes import guards
from app.chart_helpers import build_donut_chart

router = APIRouter()


@router.get("/reports", response_class=HTMLResponse)
def reports(request: Request, start: str = "", end: str = ""):
    user, deny = guards.require_login_page(request)
    if deny:
        return deny

    default_start, default_end = reports_service.default_date_range()
    start = start or default_start
    end = end or default_end
    by_category, by_cost_center, monthly, grand_total = reports_service.spend_breakdown(start, end)
    donut = build_donut_chart(by_category)

    return templates.TemplateResponse("expenses/reports.html", {
        "request": request, "user": user, "start": start, "end": end,
        "by_category": by_category, "by_cost_center": by_cost_center,
        "grand_total": grand_total, "donut": donut,
    })


@router.get("/reports/export.xlsx")
def export_xlsx(request: Request, start: str = "", end: str = "", status: str = "approved"):
    user, deny = guards.require_login_page(request)
    if deny:
        return deny
    rows = reports_service.export_rows(start, end, status)
    filters_desc = reports_service.describe_filters(start, end, status)
    document_no = reports_service.allocate_document_number("RPT")
    buf = reports_service.build_export_workbook(
        rows, document_no=document_no, generated_by=user["full_name"], filters_description=filters_desc,
    )
    filename = f"petty_cash_{start or 'all'}_to_{end or 'all'}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/reports/export.pdf")
def export_pdf(request: Request, start: str = "", end: str = "", status: str = "approved"):
    user, deny = guards.require_login_page(request)
    if deny:
        return deny
    rows = reports_service.export_rows(start, end, status)
    filters_desc = reports_service.describe_filters(start, end, status)
    pdf_bytes = reports_service.build_export_pdf(
        rows, generated_by=user["full_name"], filters_description=filters_desc,
    )
    filename = f"petty_cash_{start or 'all'}_to_{end or 'all'}.pdf"
    return Response(content=pdf_bytes, media_type="application/pdf",
                     headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/reports/export-summary.pdf")
def export_summary_pdf(request: Request, start: str = "", end: str = ""):
    user, deny = guards.require_login_page(request)
    if deny:
        return deny
    default_start, default_end = reports_service.default_date_range()
    start = start or default_start
    end = end or default_end
    by_category, by_cost_center, _monthly, grand_total = reports_service.spend_breakdown(start, end)
    filters_desc = reports_service.describe_filters(start, end)
    pdf_bytes = reports_service.build_summary_pdf(
        by_category, by_cost_center, grand_total,
        generated_by=user["full_name"], filters_description=filters_desc,
    )
    filename = f"spend_summary_{start}_to_{end}.pdf"
    return Response(content=pdf_bytes, media_type="application/pdf",
                     headers={"Content-Disposition": f'attachment; filename="{filename}"'})
