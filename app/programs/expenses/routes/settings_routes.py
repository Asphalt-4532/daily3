"""HTTP layer for the Expense Program's reference data (categories, cost
centres). Business logic lives in
app/programs/expenses/services/settings.py. For the audit log, which used
to be handled by this same file, see the shared app/routes/audit_routes.py -
splitting it out is exactly the kind of separation this program's isolation
is meant to guarantee: reference data is Expense Program-specific, the
audit log is shared system-wide infrastructure, and the two shouldn't be
mixed in one file just because they happened to live under the same URL
prefix once."""
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse

from app.auth import can_manage_settings, can_delete_supplier
from app.templates_env import templates
from app.programs.expenses.services import settings as settings_service
from app.programs.expenses.services import data_integrity
from app.programs.expenses.services import suppliers as suppliers_service
from app.programs.expenses.services import period_lock as period_lock_svc
from app.services.errors import ValidationError, NotFoundError, ConflictError, ForbiddenError
from app.routes import guards

router = APIRouter()


@router.get("/settings")
def settings_page(request: Request, supplier_status: str = "active", supplier_search: str = ""):
    user, deny = guards.require_role_page(request, can_manage_settings)
    if deny:
        return deny
    return templates.TemplateResponse("expenses/settings.html", {
        "request": request, "user": user,
        "categories": settings_service.list_categories(),
        "cost_centers": settings_service.list_cost_centers(),
        "duplicate_groups": data_integrity.find_duplicate_lines(),
        "suppliers": suppliers_service.list_suppliers(status=supplier_status, search=supplier_search),
        "supplier_filters": {"status": supplier_status, "search": supplier_search},
        "locked_months": period_lock_svc.list_locked_months(),
    })


@router.post("/settings/categories/new")
def add_category(request: Request, csrf_token: str = Form(...), name: str = Form(...)):
    user, deny = guards.require_role_action(request, can_manage_settings)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    try:
        settings_service.add_category(name, user["id"])
    except ValidationError as e:
        return HTMLResponse(str(e), status_code=400)
    return RedirectResponse("/expense-program/settings", status_code=302)


@router.post("/settings/categories/{cat_id}/toggle")
def toggle_category(request: Request, cat_id: int, csrf_token: str = Form(...)):
    user, deny = guards.require_role_action(request, can_manage_settings)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    settings_service.toggle_category(cat_id, user["id"])
    return RedirectResponse("/expense-program/settings", status_code=302)


@router.post("/settings/cost-centers/new")
def add_cost_center(request: Request, csrf_token: str = Form(...), code: str = Form(...), name: str = Form(...)):
    user, deny = guards.require_role_action(request, can_manage_settings)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    try:
        settings_service.add_cost_center(code, name, user["id"])
    except ValidationError as e:
        return HTMLResponse(str(e), status_code=400)
    return RedirectResponse("/expense-program/settings", status_code=302)


@router.post("/settings/cost-centers/{cc_id}/toggle")
def toggle_cost_center(request: Request, cc_id: int, csrf_token: str = Form(...)):
    user, deny = guards.require_role_action(request, can_manage_settings)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    settings_service.toggle_cost_center(cc_id, user["id"])
    return RedirectResponse("/expense-program/settings", status_code=302)


@router.post("/settings/duplicates/clean")
def clean_duplicate_line_group(request: Request, csrf_token: str = Form(...),
                                expense_id: int = Form(...), category_id: int = Form(...),
                                particulars: str = Form(...), amount: float = Form(...),
                                is_vatable: int = Form(...)):
    """Removes one specific duplicate-line group, on a draft only. Each
    group requires its own explicit click - see data_integrity.py for why
    this is never a single "clean everything" bulk action."""
    user, deny = guards.require_role_action(request, can_manage_settings)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    try:
        data_integrity.remove_duplicate_line_group(
            expense_id, category_id, particulars, amount, bool(is_vatable), user,
        )
    except ForbiddenError as e:
        return HTMLResponse(str(e), status_code=403)
    except NotFoundError as e:
        return HTMLResponse(str(e), status_code=404)
    except ConflictError as e:
        return HTMLResponse(str(e), status_code=409)
    return RedirectResponse("/expense-program/settings#duplicates", status_code=302)


@router.post("/settings/suppliers/{supplier_id}/delete")
def delete_supplier(request: Request, supplier_id: int, csrf_token: str = Form(...),
                     reason: str = Form(...)):
    user, deny = guards.require_role_action(request, can_delete_supplier)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    try:
        suppliers_service.delete_supplier(supplier_id, user["id"], reason)
    except ValidationError as e:
        return HTMLResponse(str(e), status_code=400)
    except NotFoundError as e:
        return HTMLResponse(str(e), status_code=404)
    return RedirectResponse("/expense-program/settings#suppliers", status_code=302)


# --- Period close / month lock ---

@router.post("/settings/period-lock/lock")
def lock_month(request: Request, csrf_token: str = Form(...), month: str = Form(...)):
    user, deny = guards.require_role_action(request, can_manage_settings)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    try:
        period_lock_svc.lock_month(month, user["id"])
    except (ValidationError, ConflictError) as e:
        return HTMLResponse(str(e), status_code=409)
    return RedirectResponse("/expense-program/settings#period-lock", status_code=302)


@router.post("/settings/period-lock/unlock")
def unlock_month(request: Request, csrf_token: str = Form(...), month: str = Form(...)):
    user, deny = guards.require_role_action(request, can_manage_settings)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    try:
        period_lock_svc.unlock_month(month, user["id"])
    except NotFoundError as e:
        return HTMLResponse(str(e), status_code=404)
    return RedirectResponse("/expense-program/settings#period-lock", status_code=302)


# --- GL account code on categories ---

@router.post("/settings/categories/{cat_id}/gl-code")
def update_gl_code(
    request: Request,
    cat_id: int,
    csrf_token: str = Form(...),
    gl_code: str = Form(""),
):
    user, deny = guards.require_role_action(request, can_manage_settings)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    settings_service.update_gl_code(cat_id, gl_code.strip(), user["id"])
    return RedirectResponse("/expense-program/settings#categories", status_code=302)
