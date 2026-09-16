"""HTTP layer for the Asset registry: list, create, edit, delete, set status,
and tag-rule settings. Business logic in services/assets.py. Custody and
service events have their own route files.

Pattern mirrors expense_routes.py and production_routes.py exactly:
  * guards.require_role_page / require_role_action for permissions
  * guards.require_csrf on every state-changing POST
  * ServiceError -> 400 / redirect with flash, never a raw exception
  * StreamingResponse for file downloads, never embedding binary in a page
"""
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse, StreamingResponse

from app import auth
from app.templates_env import templates
from app.programs.assets.services import assets as assets_svc
from app.programs.assets.services import custody, timeline as timeline_svc
from app.programs.assets.services import asset_reports
from app.services.errors import (
    ValidationError, NotFoundError, ConflictError, DuplicateError,
)
from app.routes import guards

router = APIRouter()

ASSET_PREFIX = "/assets"


def _redirect(path, **kwargs):
    return RedirectResponse(f"{ASSET_PREFIX}{path}", status_code=302)


# --------------------------------------------------------------------------
# list / dashboard
# --------------------------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
def asset_list(request: Request, category: str = "", status: str = "",
               q: str = ""):
    user, deny = guards.require_role_page(request, auth.can_view_assets)
    if deny:
        return deny
    assets = assets_svc.list_assets(
        category=category or None,
        status=status or None,
        q=q or None,
    )
    return templates.TemplateResponse("assets/list.html", {
        "request": request, "user": user,
        "assets": assets,
        "filters": {"category": category, "status": status, "q": q},
        "asset_categories": assets_svc.ASSET_CATEGORIES,
        "statuses": assets_svc.ASSET_STATUSES,
        "can_manage": auth.can_manage_assets(user["role"]),
    })


# --------------------------------------------------------------------------
# create
# --------------------------------------------------------------------------
@router.get("/new", response_class=HTMLResponse)
def new_asset_form(request: Request):
    user, deny = guards.require_role_page(request, auth.can_manage_assets)
    if deny:
        return deny
    from app.programs.expenses.services.settings import list_cost_centers
    return templates.TemplateResponse("assets/form.html", {
        "request": request, "user": user,
        "asset": None,
        "asset_categories": assets_svc.ASSET_CATEGORIES,
        "cost_centers": list_cost_centers(),
        "tag_rules": assets_svc.get_tag_rules(),
        "error": None,
    })


@router.post("/new")
def create_asset(
    request: Request,
    csrf_token: str = Form(...),
    asset_tag: str = Form(...),
    name: str = Form(...),
    category: str = Form(...),
    serial_number: str = Form(""),
    purchase_date: str = Form(""),
    purchase_cost: str = Form("0"),
    cost_center_id: str = Form(""),
    notes: str = Form(""),
):
    user, deny = guards.require_role_action(request, auth.can_manage_assets)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    try:
        asset_id = assets_svc.create_asset(
            asset_tag=asset_tag, name=name, category=category,
            serial_number=serial_number, purchase_date=purchase_date or None,
            purchase_cost=purchase_cost, cost_center_id=cost_center_id or None,
            notes=notes, created_by=user["id"],
        )
    except (ValidationError, DuplicateError) as e:
        from app.programs.expenses.services.settings import list_cost_centers
        return templates.TemplateResponse("assets/form.html", {
            "request": request, "user": user,
            "asset": None,
            "asset_categories": assets_svc.ASSET_CATEGORIES,
            "cost_centers": list_cost_centers(),
            "tag_rules": assets_svc.get_tag_rules(),
            "error": str(e),
        }, status_code=400)
    return _redirect(f"/{asset_id}")


# --------------------------------------------------------------------------
# detail
# --------------------------------------------------------------------------
@router.get("/{asset_id}", response_class=HTMLResponse)
def asset_detail(request: Request, asset_id: int,
                 date_from: str = "", date_to: str = "",
                 kinds: str = ""):
    user, deny = guards.require_role_page(request, auth.can_view_assets)
    if deny:
        return deny
    try:
        summary = timeline_svc.asset_summary(asset_id)
    except Exception:
        summary = None
    if not summary:
        return HTMLResponse("Asset not found.", status_code=404)

    selected_kinds = [k.strip() for k in kinds.split(",") if k.strip()] or None
    events = timeline_svc.asset_timeline(
        asset_id,
        date_from=date_from or None,
        date_to=date_to or None,
        kinds=selected_kinds,
    )
    holders = custody.active_holders(asset_id)
    history = custody.assignment_history(asset_id)
    from app.programs.expenses.services.settings import list_cost_centers
    from app.programs.assets.services.service_log import SERVICE_TYPES, may_log_service
    from app.database import get_connection
    conn = get_connection()
    try:
        can_log = may_log_service(conn, asset_id, user)
    finally:
        conn.close()

    return templates.TemplateResponse("assets/detail.html", {
        "request": request, "user": user,
        "asset": summary,
        "events": events,
        "holders": holders,
        "history": history,
        "filters": {"date_from": date_from, "date_to": date_to, "kinds": kinds},
        "all_kinds": ["registration", "cost", "service", "custody"],
        "can_manage": auth.can_manage_assets(user["role"]),
        "can_assign": auth.can_assign_asset(user["role"]),
        "can_divest": auth.can_divest_asset(user["role"]),
        "can_log_service": can_log,
        "service_types": SERVICE_TYPES,
        "cost_centers": list_cost_centers(),
    })


# --------------------------------------------------------------------------
# edit
# --------------------------------------------------------------------------
@router.get("/{asset_id}/edit", response_class=HTMLResponse)
def edit_asset_form(request: Request, asset_id: int):
    user, deny = guards.require_role_page(request, auth.can_manage_assets)
    if deny:
        return deny
    try:
        asset = assets_svc.get_asset(asset_id)
    except NotFoundError:
        return HTMLResponse("Asset not found.", status_code=404)
    from app.programs.expenses.services.settings import list_cost_centers
    return templates.TemplateResponse("assets/form.html", {
        "request": request, "user": user,
        "asset": asset,
        "asset_categories": assets_svc.ASSET_CATEGORIES,
        "cost_centers": list_cost_centers(),
        "tag_rules": assets_svc.get_tag_rules(),
        "error": None,
    })


@router.post("/{asset_id}/edit")
def edit_asset(
    request: Request, asset_id: int,
    csrf_token: str = Form(...),
    asset_tag: str = Form(...),
    name: str = Form(...),
    category: str = Form(...),
    serial_number: str = Form(""),
    purchase_date: str = Form(""),
    purchase_cost: str = Form("0"),
    cost_center_id: str = Form(""),
    notes: str = Form(""),
):
    user, deny = guards.require_role_action(request, auth.can_manage_assets)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    try:
        assets_svc.update_asset(
            asset_id, updated_by=user["id"],
            asset_tag=asset_tag, name=name, category=category,
            serial_number=serial_number, purchase_date=purchase_date or None,
            purchase_cost=purchase_cost, cost_center_id=cost_center_id or None,
            notes=notes,
        )
    except (ValidationError, DuplicateError, NotFoundError) as e:
        from app.programs.expenses.services.settings import list_cost_centers
        return templates.TemplateResponse("assets/form.html", {
            "request": request, "user": user,
            "asset": {"id": asset_id, "asset_tag": asset_tag, "name": name,
                      "category": category, "serial_number": serial_number,
                      "purchase_date": purchase_date, "purchase_cost": purchase_cost,
                      "cost_center_id": cost_center_id, "notes": notes},
            "asset_categories": assets_svc.ASSET_CATEGORIES,
            "cost_centers": list_cost_centers(),
            "tag_rules": assets_svc.get_tag_rules(),
            "error": str(e),
        }, status_code=400)
    return _redirect(f"/{asset_id}")


# --------------------------------------------------------------------------
# delete
# --------------------------------------------------------------------------
@router.post("/{asset_id}/delete")
def delete_asset(request: Request, asset_id: int,
                 csrf_token: str = Form(...),
                 reason: str = Form("")):
    user, deny = guards.require_role_action(request, auth.can_manage_assets)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    try:
        assets_svc.delete_asset(asset_id, deleted_by=user["id"], reason=reason)
    except (ValidationError, ConflictError, NotFoundError) as e:
        return HTMLResponse(str(e), status_code=400)
    return _redirect("/")


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------
@router.post("/{asset_id}/status")
def set_status(request: Request, asset_id: int,
               csrf_token: str = Form(...),
               status: str = Form(...),
               note: str = Form("")):
    user, deny = guards.require_role_action(request, auth.can_manage_assets)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    try:
        assets_svc.set_status(asset_id, status, updated_by=user["id"], note=note)
    except (ValidationError, ConflictError, NotFoundError) as e:
        return HTMLResponse(str(e), status_code=400)
    return _redirect(f"/{asset_id}")


# --------------------------------------------------------------------------
# reports / exports
# --------------------------------------------------------------------------
@router.get("/{asset_id}/report/pdf")
def asset_history_pdf(request: Request, asset_id: int,
                      date_from: str = "", date_to: str = "",
                      kinds: str = ""):
    user, deny = guards.require_role_page(request, auth.can_view_assets)
    if deny:
        return deny
    selected_kinds = [k.strip() for k in kinds.split(",") if k.strip()] or None
    doc_no = asset_reports.allocate_document_number("ASR")
    try:
        summary = timeline_svc.asset_summary(asset_id)
    except Exception:
        return HTMLResponse("Asset not found.", status_code=404)
    pdf = asset_reports.build_asset_history_pdf(
        asset_id,
        document_no=doc_no,
        generated_by=user["full_name"],
        date_from=date_from or None,
        date_to=date_to or None,
        kinds=selected_kinds,
    )
    fname = f"asset_{summary['asset_tag'] if summary else asset_id}_{doc_no}.pdf"
    return StreamingResponse(
        iter([pdf]),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/{asset_id}/report/xlsx")
def asset_history_xlsx(request: Request, asset_id: int,
                       date_from: str = "", date_to: str = "",
                       kinds: str = ""):
    user, deny = guards.require_role_page(request, auth.can_view_assets)
    if deny:
        return deny
    selected_kinds = [k.strip() for k in kinds.split(",") if k.strip()] or None
    doc_no = asset_reports.allocate_document_number("ASR")
    try:
        summary = timeline_svc.asset_summary(asset_id)
    except Exception:
        return HTMLResponse("Asset not found.", status_code=404)
    data = asset_reports.build_asset_history_workbook(
        asset_id,
        document_no=doc_no,
        generated_by=user["full_name"],
        date_from=date_from or None,
        date_to=date_to or None,
        kinds=selected_kinds,
    )
    fname = f"asset_{summary['asset_tag'] if summary else asset_id}_{doc_no}.xlsx"
    return StreamingResponse(
        iter([data]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
