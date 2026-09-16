"""HTTP layer for Weekly Productions. Business logic lives in
app/programs/weekly_productions/services/production.py; permission/CSRF
checks come from app/routes/guards.py (shared) - see
app/programs/expenses/routes/expense_routes.py's docstring for the pattern
this mirrors.

A production report's lines are submitted as three same-named form field
lists (line_material_type, line_desc_dims, line_quantity) - the same
"repeat this group of inputs N times" convention Expense Program's voucher
lines use (see expense_routes.py's docstring and expense_form.html).
`line_desc_dims` is one free-text box per line ("cable tray 100x50x2.44mx0.7"
or "fitting 90 degree elbow 200x50xx0.7mm") - see
production_service.parse_line_input() for how it's split into a description
and the width/height/length/thickness/weight actually stored.
"""
from datetime import date

from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse, StreamingResponse

from app.auth import can_record_production, can_delete_production, can_manage_settings
from app.templates_env import templates
from app.programs.weekly_productions.services import production as production_service
from app.programs.weekly_productions.pdf_reports import build_production_listing_pdf
from app.services.errors import ValidationError, NotFoundError
from app.routes import guards

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    user, deny = guards.require_login_page(request)
    if deny:
        return deny
    stats = production_service.dashboard_stats()
    return templates.TemplateResponse("weekly_productions/dashboard.html", {
        "request": request, "user": user, **stats,
    })


@router.get("/reports", response_class=HTMLResponse)
def reports(request: Request, status: str = "recorded", line_name: str = "",
            material_type: str = "", start: str = "", end: str = ""):
    user, deny = guards.require_login_page(request)
    if deny:
        return deny
    rows, total_qty, total_weight, lines = production_service.list_reports(
        status, line_name, material_type, start, end)
    by_week, by_line, by_material, grand_total, grand_weight = production_service.weekly_summary(
        start, end)
    daily = production_service.daily_summary(start, end)
    return templates.TemplateResponse("weekly_productions/reports.html", {
        "request": request, "user": user, "reports": rows, "total_qty": total_qty,
        "total_weight": total_weight, "lines": lines, "by_week": by_week, "by_line": by_line,
        "by_material": by_material, "grand_total": grand_total, "grand_weight": grand_weight,
        "daily": daily,
        "material_types": production_service.MATERIAL_TYPES,
        "filters": {"status": status, "line_name": line_name, "material_type": material_type,
                    "start": start, "end": end},
    })


@router.get("/reports/export.xlsx")
def reports_export_xlsx(request: Request, status: str = "recorded", line_name: str = "",
                         material_type: str = "", start: str = "", end: str = ""):
    user, deny = guards.require_login_page(request)
    if deny:
        return deny
    rows = production_service.export_rows(status, line_name, material_type, start, end)
    filters_desc = production_service.describe_filters(start, end, status, line_name, material_type)
    document_no = production_service.allocate_document_number("RPT")
    buf = production_service.build_export_workbook(
        rows, document_no=document_no, generated_by=user["full_name"], filters_description=filters_desc,
    )
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="weekly_production.xlsx"'},
    )


@router.get("/reports/export.pdf")
def reports_export_pdf(request: Request, status: str = "recorded", line_name: str = "",
                        material_type: str = "", start: str = "", end: str = ""):
    user, deny = guards.require_login_page(request)
    if deny:
        return deny
    rows = production_service.export_rows(status, line_name, material_type, start, end)
    filters_desc = production_service.describe_filters(start, end, status, line_name, material_type)
    document_no = production_service.allocate_document_number("RPT")
    pdf_bytes = build_production_listing_pdf(
        rows, document_no=document_no, generated_by=user["full_name"], filters_description=filters_desc,
    )
    return StreamingResponse(
        iter([pdf_bytes]), media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="weekly_production.pdf"'},
    )


@router.get("/reports/new", response_class=HTMLResponse)
def new_report_form(request: Request):
    user, deny = guards.require_role_page(request, can_record_production)
    if deny:
        return deny
    return templates.TemplateResponse("weekly_productions/report_form.html", {
        "request": request, "user": user, "error": None, "form_values": None,
        "material_types": production_service.MATERIAL_TYPES, "today": date.today().isoformat(),
    })


def _extract_lines_from_form(form):
    """Zips the positionally-aligned line_* field lists into a list of
    dicts, dropping any row that was never touched (added via '+ Add
    line' but left completely blank) rather than treating it as a
    validation error - same convention as Expense Program's
    _extract_lines_from_form() in expense_routes.py."""
    material_types = form.getlist("line_material_type")
    desc_dims = form.getlist("line_desc_dims")
    quantities = form.getlist("line_quantity")
    lines = []
    for i in range(len(material_types)):
        material_type = (material_types[i] if i < len(material_types) else "").strip()
        raw = (desc_dims[i] if i < len(desc_dims) else "").strip()
        quantity = (quantities[i] if i < len(quantities) else "").strip()
        if not any([material_type, raw, quantity]):
            continue  # untouched extra row - not an error, just skip it
        lines.append({"material_type": material_type, "raw": raw, "quantity": quantity})
    return lines


@router.post("/reports/new")
async def create_report(request: Request):
    user, deny = guards.require_role_action(request, can_record_production)
    if deny:
        return deny
    form = await request.form()
    csrf_token = form.get("csrf_token", "")
    if bad := guards.require_csrf(request, csrf_token):
        return bad

    report_date = form.get("report_date", "")
    line_name = form.get("line_name", "")
    shift = form.get("shift", "")
    notes = form.get("notes", "")
    lines = _extract_lines_from_form(form)

    def _rerender_with_error(message: str):
        return templates.TemplateResponse("weekly_productions/report_form.html", {
            "request": request, "user": user, "error": message,
            "material_types": production_service.MATERIAL_TYPES, "today": date.today().isoformat(),
            "form_values": {
                "report_date": report_date, "line_name": line_name, "shift": shift,
                "notes": notes, "lines": lines,
            },
        }, status_code=400)

    try:
        _report_id, _report_no = production_service.create_report(
            report_date=report_date, line_name=line_name, shift=shift, notes=notes,
            lines=lines, recorded_by=user["id"],
        )
    except ValidationError as e:
        return _rerender_with_error(str(e))
    return RedirectResponse("/weekly-productions/reports", status_code=302)


@router.get("/reports/{report_id}", response_class=HTMLResponse)
def report_detail(request: Request, report_id: int):
    user, deny = guards.require_login_page(request)
    if deny:
        return deny
    try:
        report = production_service.get_report(report_id)
    except NotFoundError as e:
        return HTMLResponse(str(e), status_code=404)
    return templates.TemplateResponse("weekly_productions/report_detail.html", {
        "request": request, "user": user, "r": report,
    })


@router.post("/reports/{report_id}/delete")
def delete_report(request: Request, report_id: int, csrf_token: str = Form(...),
                   reason: str = Form(...)):
    user, deny = guards.require_role_action(request, can_delete_production)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    try:
        production_service.delete_report(report_id, user["id"], reason)
    except ValidationError as e:
        return HTMLResponse(str(e), status_code=400)
    except NotFoundError as e:
        return HTMLResponse(str(e), status_code=404)
    return RedirectResponse(f"/weekly-productions/reports/{report_id}", status_code=302)


@router.get("/settings", response_class=HTMLResponse)
def settings(request: Request):
    user, deny = guards.require_role_page(request, can_manage_settings)
    if deny:
        return deny
    return templates.TemplateResponse("weekly_productions/settings.html", {
        "request": request, "user": user, "factors": production_service.get_weight_factors(),
        "material_types": production_service.MATERIAL_TYPES,
        "weight_targets": production_service.get_weight_targets(),
        "locked_months": production_service.list_locked_months(),
    })


@router.post("/settings/weight-factor")
def update_weight_factor(request: Request, csrf_token: str = Form(...),
                          material_type: str = Form(...), value: str = Form(...)):
    user, deny = guards.require_role_action(request, can_manage_settings)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    try:
        production_service.set_weight_factor(material_type, value, user["id"])
    except ValidationError as e:
        return HTMLResponse(str(e), status_code=400)
    return RedirectResponse("/weekly-productions/settings", status_code=302)


@router.post("/settings/weight-target")
def update_weight_target(request: Request, csrf_token: str = Form(...), period: str = Form(...),
                          value: str = Form(...)):
    user, deny = guards.require_role_action(request, can_manage_settings)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    try:
        production_service.set_weight_target(period, value, user["id"])
    except ValidationError as e:
        return HTMLResponse(str(e), status_code=400)
    return RedirectResponse("/weekly-productions/settings", status_code=302)


@router.post("/settings/lock-month")
def lock_month(request: Request, csrf_token: str = Form(...), month: str = Form(...)):
    user, deny = guards.require_role_action(request, can_manage_settings)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    try:
        production_service.lock_month(month, user["id"])
    except ValidationError as e:
        return HTMLResponse(str(e), status_code=400)
    return RedirectResponse("/weekly-productions/settings", status_code=302)


@router.post("/settings/unlock-month")
def unlock_month(request: Request, csrf_token: str = Form(...), month: str = Form(...)):
    user, deny = guards.require_role_action(request, can_manage_settings)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    try:
        production_service.unlock_month(month, user["id"])
    except NotFoundError as e:
        return HTMLResponse(str(e), status_code=404)
    return RedirectResponse("/weekly-productions/settings", status_code=302)
