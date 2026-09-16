"""HTTP layer for the cash float / imprest ledger.
Business logic in app/programs/expenses/services/cash_ledger.py.
"""
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse

from app.auth import can_manage_settings
from app.templates_env import templates
from app.programs.expenses.services import cash_ledger as ledger_svc
from app.services.errors import ValidationError
from app.routes import guards

router = APIRouter()


@router.get("/cash")
def cash_page(request: Request):
    user, deny = guards.require_login_page(request)
    if deny:
        return deny
    return templates.TemplateResponse("expenses/cash_ledger.html", {
        "request": request, "user": user,
        "float_row": ledger_svc.get_balance(),
        "movements": ledger_svc.get_movements(limit=60),
    })


@router.post("/cash/topup")
def cash_topup(
    request: Request,
    csrf_token: str = Form(...),
    amount: float = Form(...),
    note: str = Form(""),
):
    user, deny = guards.require_role_action(request, can_manage_settings)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    try:
        ledger_svc.top_up(amount, note, user["id"])
    except ValidationError as e:
        return HTMLResponse(str(e), status_code=400)
    return RedirectResponse("/expense-program/cash", status_code=302)


@router.post("/cash/adjust")
def cash_adjust(
    request: Request,
    csrf_token: str = Form(...),
    amount: float = Form(...),
    note: str = Form(...),
):
    user, deny = guards.require_role_action(request, can_manage_settings)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    try:
        ledger_svc.manual_adjust(amount, note, user["id"])
    except ValidationError as e:
        return HTMLResponse(str(e), status_code=400)
    return RedirectResponse("/expense-program/cash", status_code=302)
