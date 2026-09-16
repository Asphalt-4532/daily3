"""HTTP layer for Budget vs Actual.
Business logic in app/programs/expenses/services/budgets.py.
"""
from datetime import date

from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse

from app.auth import can_manage_settings
from app.templates_env import templates
from app.programs.expenses.services import budgets as budgets_svc
from app.programs.expenses.services import settings as settings_svc
from app.services.errors import ValidationError, NotFoundError
from app.routes import guards

router = APIRouter()


def _current_period() -> str:
    return date.today().strftime("%Y-%m")


@router.get("/budgets")
def budgets_page(request: Request, period: str = ""):
    user, deny = guards.require_login_page(request)
    if deny:
        return deny
    period = period or _current_period()
    # Mark notifications read when manager opens the budget page.
    if user["role"] == "manager":
        budgets_svc.mark_notifications_read(user["id"])
    return templates.TemplateResponse("expenses/budgets.html", {
        "request": request, "user": user,
        "period": period,
        "budget_rows": budgets_svc.get_budget_vs_actual(period),
        "all_budgets": budgets_svc.list_budgets(period),
        "cost_centers": settings_svc.list_cost_centers(active_only=True),
        "categories": settings_svc.list_categories(active_only=True),
    })


@router.post("/budgets/set")
def set_budget(
    request: Request,
    csrf_token: str = Form(...),
    period: str = Form(...),
    amount: float = Form(...),
    cost_center_id: str = Form(""),
    category_id: str = Form(""),
):
    user, deny = guards.require_role_action(request, can_manage_settings)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    try:
        budgets_svc.set_budget(
            period=period,
            amount=amount,
            acting_user_id=user["id"],
            cost_center_id=int(cost_center_id) if cost_center_id else None,
            category_id=int(category_id) if category_id else None,
        )
    except (ValidationError, NotFoundError) as e:
        return HTMLResponse(str(e), status_code=400)
    return RedirectResponse(f"/expense-program/budgets?period={period}", status_code=302)


@router.post("/budgets/{budget_id}/delete")
def delete_budget(request: Request, budget_id: int, csrf_token: str = Form(...)):
    user, deny = guards.require_role_action(request, can_manage_settings)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    try:
        budgets_svc.delete_budget(budget_id, user["id"])
    except NotFoundError as e:
        return HTMLResponse(str(e), status_code=404)
    return RedirectResponse("/expense-program/budgets", status_code=302)
