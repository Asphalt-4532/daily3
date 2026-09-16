"""HTTP layer for the employee salary-advance ledger.

Access rules:
  - manager / accountant : see ALL employees and their full ledger.
  - user                 : sees ONLY advances linked to their own submitted vouchers
                           (i.e. advance_ledger rows where the reference expense was
                           prepared by them).  Route enforces this at query time.

Settlement approval rules (enforced in service layer):
  - manager / accountant : settlement recorded immediately (no pending).
  - user                 : settlement created as pending_approval=1; manager/accountant
                           must approve before it reduces the outstanding balance.
"""
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse

from app.auth import can_review_expense, can_manage_settings
from app.templates_env import templates
from app.programs.expenses.services import advances as advances_svc
from app.services.errors import ValidationError, NotFoundError, ForbiddenError
from app.routes import guards

router = APIRouter()


def _can_see_all(user) -> bool:
    return user["role"] in ("manager", "accountant")


# ---------------------------------------------------------------------------
# Ledger overview
# ---------------------------------------------------------------------------

@router.get("/advances")
def advances_overview(request: Request):
    user, deny = guards.require_login_page(request)
    if deny:
        return deny

    if _can_see_all(user):
        balances = advances_svc.get_outstanding_balances()
        pending = advances_svc.get_pending_settlements()
    else:
        # plain user: only employees they personally advanced money to
        balances = advances_svc.get_outstanding_balances_for_user(user["id"])
        pending = []

    total_outstanding = sum(b["outstanding"] for b in balances)
    total_settled_this_month = advances_svc.get_settled_this_month()

    return templates.TemplateResponse("expenses/advances.html", {
        "request": request, "user": user,
        "balances": balances,
        "pending": pending,
        "total_outstanding": total_outstanding,
        "total_settled_this_month": total_settled_this_month,
        "can_see_all": _can_see_all(user),
        "flash_info": request.session.pop("flash_info", None),
    })


# ---------------------------------------------------------------------------
# Per-employee detail + settlement form
# ---------------------------------------------------------------------------

@router.get("/advances/{employee_id}")
def advance_detail(request: Request, employee_id: int):
    user, deny = guards.require_login_page(request)
    if deny:
        return deny

    try:
        ledger = advances_svc.get_employee_ledger(employee_id)
    except NotFoundError as e:
        return HTMLResponse(str(e), status_code=404)

    # plain user can only view employees they personally advanced
    if not _can_see_all(user):
        if not advances_svc.user_has_advanced_employee(user["id"], employee_id):
            return HTMLResponse("You don't have access to this employee's ledger.", status_code=403)

    return templates.TemplateResponse("expenses/advance_detail.html", {
        "request": request, "user": user,
        "ledger": ledger,
        "settlement_methods": advances_svc.SETTLEMENT_METHODS,
        "can_see_all": _can_see_all(user),
        "flash_info": request.session.pop("flash_info", None),
    })


# ---------------------------------------------------------------------------
# Record a settlement (POST)
# ---------------------------------------------------------------------------

@router.post("/advances/{employee_id}/settle")
def record_settlement(
    request: Request,
    employee_id: int,
    csrf_token: str = Form(...),
    amount: float = Form(...),
    method: str = Form(...),
    note: str = Form(""),
):
    user, deny = guards.require_login_action(request)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    try:
        advances_svc.record_settlement(
            employee_id=employee_id,
            amount=amount,
            method=method,
            note=note,
            acting_user=user,
        )
    except (ValidationError, NotFoundError) as e:
        return HTMLResponse(str(e), status_code=400)

    if user["role"] == "user":
        request.session["flash_info"] = "Settlement submitted — awaiting manager approval."
    return RedirectResponse(f"/expense-program/advances/{employee_id}", status_code=302)


# ---------------------------------------------------------------------------
# Approve a pending settlement (manager / accountant only)
# ---------------------------------------------------------------------------

@router.post("/advances/settlements/{ledger_id}/approve")
def approve_settlement(
    request: Request,
    ledger_id: int,
    csrf_token: str = Form(...),
):
    user, deny = guards.require_role_action(request, can_review_expense)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    try:
        advances_svc.approve_settlement(ledger_id, user)
    except NotFoundError as e:
        return HTMLResponse(str(e), status_code=404)
    return RedirectResponse("/expense-program/advances", status_code=302)


# ---------------------------------------------------------------------------
# Reject a pending settlement (manager / accountant only)
# ---------------------------------------------------------------------------

@router.post("/advances/settlements/{ledger_id}/reject")
def reject_settlement(
    request: Request,
    ledger_id: int,
    csrf_token: str = Form(...),
    reason: str = Form(...),
):
    user, deny = guards.require_role_action(request, can_review_expense)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    try:
        advances_svc.reject_settlement(ledger_id, user, reason)
    except NotFoundError as e:
        return HTMLResponse(str(e), status_code=404)
    return RedirectResponse("/expense-program/advances", status_code=302)
