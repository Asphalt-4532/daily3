"""HTTP layer for custody actions (assign / close / transfer / divest) and
the multi-asset maintenance listing reports. Separate from asset_routes.py
so the permission boundaries are clear at a glance: viewing an asset is
view_assets; moving one is assign_asset or divest_asset; the reports are
view_assets for download, manage_assets for tag rules.
"""
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse, StreamingResponse

from app import auth
from app.templates_env import templates
from app.programs.assets.services import custody, assets as assets_svc
from app.programs.assets.services import asset_reports
from app.services.errors import (
    ValidationError, NotFoundError, ConflictError, DuplicateError,
)
from app.routes import guards

router = APIRouter()

ASSET_PREFIX = "/assets"


def _back(asset_id):
    return RedirectResponse(f"{ASSET_PREFIX}/{asset_id}", status_code=302)


# --------------------------------------------------------------------------
# assign
# --------------------------------------------------------------------------
@router.post("/{asset_id}/assign")
def assign(request: Request, asset_id: int,
           csrf_token: str = Form(...),
           employee_id: str = Form(""),
           cost_center_id: str = Form(""),
           role_note: str = Form(""),
           issued_date: str = Form(...)):
    user, deny = guards.require_role_action(request, auth.can_assign_asset)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    try:
        custody.assign_asset(
            asset_id,
            employee_id=int(employee_id) if employee_id else None,
            cost_center_id=int(cost_center_id) if cost_center_id else None,
            role_note=role_note,
            issued_date=issued_date,
            assigned_by=user["id"],
        )
    except (ValidationError, ConflictError, DuplicateError, NotFoundError) as e:
        return HTMLResponse(str(e), status_code=400)
    return _back(asset_id)


# --------------------------------------------------------------------------
# close assignment
# --------------------------------------------------------------------------
@router.post("/assignments/{assignment_id}/close")
def close_assignment(request: Request, assignment_id: int,
                     csrf_token: str = Form(...),
                     returned_date: str = Form(...),
                     reason: str = Form(...)):
    user, deny = guards.require_role_action(request, auth.can_assign_asset)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    from app.database import get_connection
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT asset_id FROM asset_assignments WHERE id=?", (assignment_id,)
        ).fetchone()
        asset_id = row["asset_id"] if row else None
    finally:
        conn.close()
    try:
        custody.close_assignment(assignment_id, returned_date=returned_date,
                                 reason=reason, closed_by=user["id"])
    except (ValidationError, ConflictError, NotFoundError) as e:
        return HTMLResponse(str(e), status_code=400)
    return _back(asset_id) if asset_id else RedirectResponse(f"{ASSET_PREFIX}/", status_code=302)


# --------------------------------------------------------------------------
# transfer
# --------------------------------------------------------------------------
@router.post("/{asset_id}/transfer/{assignment_id}")
def transfer(request: Request, asset_id: int, assignment_id: int,
             csrf_token: str = Form(...),
             to_employee_id: str = Form(""),
             to_cost_center_id: str = Form(""),
             transfer_date: str = Form(...),
             reason: str = Form(...)):
    user, deny = guards.require_role_action(request, auth.can_assign_asset)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    try:
        custody.transfer_asset(
            asset_id, assignment_id,
            to_employee_id=int(to_employee_id) if to_employee_id else None,
            to_cost_center_id=int(to_cost_center_id) if to_cost_center_id else None,
            transfer_date=transfer_date,
            reason=reason,
            by=user["id"],
        )
    except (ValidationError, ConflictError, DuplicateError, NotFoundError) as e:
        return HTMLResponse(str(e), status_code=400)
    return _back(asset_id)


# --------------------------------------------------------------------------
# divest
# --------------------------------------------------------------------------
@router.post("/{asset_id}/divest")
def divest(request: Request, asset_id: int,
           csrf_token: str = Form(...),
           divest_date: str = Form(...),
           settle_amount: str = Form(""),
           reason: str = Form(...)):
    user, deny = guards.require_role_action(request, auth.can_divest_asset)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    try:
        custody.divest_asset(
            asset_id,
            divest_date=divest_date,
            settle_amount=settle_amount if settle_amount else None,
            reason=reason,
            by=user["id"],
        )
    except (ValidationError, ConflictError, NotFoundError) as e:
        return HTMLResponse(str(e), status_code=400)
    return _back(asset_id)


# --------------------------------------------------------------------------
# link employee -> login
# --------------------------------------------------------------------------
@router.post("/employees/{employee_id}/link-user")
def link_employee_user(request: Request, employee_id: int,
                       csrf_token: str = Form(...),
                       user_id: str = Form("")):
    manager, deny = guards.require_role_action(request, auth.can_manage_assets)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    try:
        custody.link_employee_to_user(
            employee_id,
            int(user_id) if user_id else None,
            linked_by=manager["id"],
        )
    except (ValidationError, NotFoundError, DuplicateError) as e:
        return HTMLResponse(str(e), status_code=400)
    return RedirectResponse(f"{ASSET_PREFIX}/employees", status_code=302)


# --------------------------------------------------------------------------
# employees list (for the link-employee feature)
# --------------------------------------------------------------------------
@router.get("/employees", response_class=HTMLResponse)
def employees(request: Request):
    user, deny = guards.require_role_page(request, auth.can_manage_assets)
    if deny:
        return deny
    from app.database import get_connection
    conn = get_connection()
    try:
        emps = [dict(r) for r in conn.execute(
            "SELECT e.*, u.username, u.full_name AS user_name "
            "FROM employees e LEFT JOIN users u ON u.id = e.user_id "
            "WHERE e.status='active' ORDER BY e.name"
        ).fetchall()]
        all_users = [dict(r) for r in conn.execute(
            "SELECT id, username, full_name FROM users WHERE active=1 ORDER BY full_name"
        ).fetchall()]
    finally:
        conn.close()
    return templates.TemplateResponse("assets/employees.html", {
        "request": request, "user": user,
        "employees": emps, "all_users": all_users,
    })


# --------------------------------------------------------------------------
# settings: tag rules + category asset requirements
# --------------------------------------------------------------------------
@router.get("/settings", response_class=HTMLResponse)
def settings(request: Request):
    user, deny = guards.require_role_page(request, auth.can_manage_assets)
    if deny:
        return deny
    from app.programs.expenses.services.settings import list_categories
    return templates.TemplateResponse("assets/settings.html", {
        "request": request, "user": user,
        "tag_rules": assets_svc.get_tag_rules(),
        "asset_categories": assets_svc.ASSET_CATEGORIES,
        "categories": list_categories(),
    })


@router.post("/settings/tag-rule")
def update_tag_rule(request: Request,
                    csrf_token: str = Form(...),
                    category: str = Form(...),
                    pattern: str = Form(""),
                    example: str = Form("")):
    user, deny = guards.require_role_action(request, auth.can_manage_assets)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    try:
        assets_svc.set_tag_rule(category, pattern, example, updated_by=user["id"])
    except ValidationError as e:
        return HTMLResponse(str(e), status_code=400)
    return RedirectResponse(f"{ASSET_PREFIX}/settings", status_code=302)


@router.post("/settings/category-asset")
def update_category_asset(request: Request,
                          csrf_token: str = Form(...),
                          category_id: int = Form(...),
                          requires_asset: str = Form("0"),
                          asset_category: str = Form("")):
    user, deny = guards.require_role_action(request, auth.can_manage_assets)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    from app.database import get_db, log_action
    with get_db() as conn:
        row = conn.execute("SELECT name FROM categories WHERE id=?", (category_id,)).fetchone()
        if not row:
            return HTMLResponse("Category not found.", status_code=404)
        conn.execute(
            "UPDATE categories SET requires_asset=?, asset_category=? WHERE id=?",
            (1 if requires_asset == "1" else 0,
             asset_category if asset_category else None,
             category_id),
        )
        log_action(conn, user["id"], "category_asset_rule_updated",
                   f"{row['name']}: requires_asset={requires_asset}, "
                   f"asset_category={asset_category or 'any'}")
    return RedirectResponse(f"{ASSET_PREFIX}/settings", status_code=302)


# --------------------------------------------------------------------------
# multi-asset maintenance reports
# --------------------------------------------------------------------------
@router.get("/reports", response_class=HTMLResponse)
def reports(request: Request,
            date_from: str = "", date_to: str = "",
            asset_ids: str = "", asset_category: str = "",
            cost_center_id: str = "", kinds: str = "",
            include_pending: str = "0"):
    user, deny = guards.require_role_page(request, auth.can_view_assets)
    if deny:
        return deny
    selected_asset_ids = [int(x) for x in asset_ids.split(",") if x.strip().isdigit()]
    selected_kinds = [k.strip() for k in kinds.split(",") if k.strip()] or None
    rows = asset_reports.maintenance_rows(
        date_from=date_from or None, date_to=date_to or None,
        asset_ids=selected_asset_ids or None,
        asset_category=asset_category or None,
        cost_center_id=int(cost_center_id) if cost_center_id else None,
        kinds=selected_kinds,
        include_pending=include_pending == "1",
    )
    all_assets = assets_svc.list_assets()
    from app.programs.expenses.services.settings import list_cost_centers
    return templates.TemplateResponse("assets/reports.html", {
        "request": request, "user": user,
        "rows": rows,
        "all_assets": all_assets,
        "filters": {
            "date_from": date_from, "date_to": date_to,
            "asset_ids": asset_ids, "asset_category": asset_category,
            "cost_center_id": cost_center_id, "kinds": kinds,
            "include_pending": include_pending,
        },
        "asset_categories": assets_svc.ASSET_CATEGORIES,
        "cost_centers": list_cost_centers(),
    })


@router.get("/reports/export.pdf")
def reports_pdf(request: Request,
                date_from: str = "", date_to: str = "",
                asset_ids: str = "", asset_category: str = "",
                cost_center_id: str = "", kinds: str = "",
                include_pending: str = "0"):
    user, deny = guards.require_role_page(request, auth.can_view_assets)
    if deny:
        return deny
    selected_asset_ids = [int(x) for x in asset_ids.split(",") if x.strip().isdigit()]
    selected_kinds = [k.strip() for k in kinds.split(",") if k.strip()] or None
    pending = include_pending == "1"
    rows = asset_reports.maintenance_rows(
        date_from=date_from or None, date_to=date_to or None,
        asset_ids=selected_asset_ids or None,
        asset_category=asset_category or None,
        cost_center_id=int(cost_center_id) if cost_center_id else None,
        kinds=selected_kinds,
        include_pending=pending,
    )
    asset_tags = [a["asset_tag"] for a in assets_svc.list_assets()] if not selected_asset_ids else [
        a["asset_tag"] for a in assets_svc.list_assets()
        if a["id"] in selected_asset_ids
    ]
    filters = asset_reports.filter_label(
        date_from=date_from or None, date_to=date_to or None,
        asset_tags=asset_tags if selected_asset_ids else None,
        asset_category=asset_category or None,
        include_pending=pending,
    )
    doc_no = asset_reports.allocate_document_number("AMR")
    pdf = asset_reports.build_maintenance_listing_pdf(
        rows, document_no=doc_no, generated_by=user["full_name"],
        filters=filters, include_pending=pending,
    )
    return StreamingResponse(
        iter([pdf]), media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="maintenance_{doc_no}.pdf"'},
    )


@router.get("/reports/export.xlsx")
def reports_xlsx(request: Request,
                 date_from: str = "", date_to: str = "",
                 asset_ids: str = "", asset_category: str = "",
                 cost_center_id: str = "", kinds: str = "",
                 include_pending: str = "0"):
    user, deny = guards.require_role_page(request, auth.can_view_assets)
    if deny:
        return deny
    selected_asset_ids = [int(x) for x in asset_ids.split(",") if x.strip().isdigit()]
    selected_kinds = [k.strip() for k in kinds.split(",") if k.strip()] or None
    pending = include_pending == "1"
    rows = asset_reports.maintenance_rows(
        date_from=date_from or None, date_to=date_to or None,
        asset_ids=selected_asset_ids or None,
        asset_category=asset_category or None,
        cost_center_id=int(cost_center_id) if cost_center_id else None,
        kinds=selected_kinds,
        include_pending=pending,
    )
    asset_tags = None
    if selected_asset_ids:
        asset_tags = [a["asset_tag"] for a in assets_svc.list_assets()
                      if a["id"] in selected_asset_ids]
    filters = asset_reports.filter_label(
        date_from=date_from or None, date_to=date_to or None,
        asset_tags=asset_tags, asset_category=asset_category or None,
        include_pending=pending,
    )
    doc_no = asset_reports.allocate_document_number("AMR")
    data = asset_reports.build_maintenance_listing_workbook(
        rows, document_no=doc_no, generated_by=user["full_name"],
        filters=filters, include_pending=pending,
    )
    return StreamingResponse(
        iter([data]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="maintenance_{doc_no}.xlsx"'},
    )
