"""HTTP layer for vouchers. Business logic lives in
app/programs/expenses/services/expenses.py; permission/CSRF checks come
from app/routes/guards.py (shared).

A voucher's transaction lines are submitted as several same-named form
fields (line_category_id, line_particulars, line_amount, line_is_vatable) -
standard HTML for "repeat this group of inputs N times" without needing a
JS framework. The browser submits them in DOM order, so position i across
all four lists is always the same line, as long as the template keeps all
four inputs for a row together (see expense_form.html) - the JS there only
ever adds/removes a whole row's worth of inputs at once, never partial rows.

The New Voucher form has two submit buttons sharing one route: a hidden
"action" field ("draft" or "submit") tells create_expense()/edit_expense()
which of the two behaviors (save incomplete work vs. validate and submit
for real) the click was for. Role checks (can this role touch vouchers at
all) happen here via guards; record-level ownership checks (is this
*specific* draft yours) happen in the service layer, which raises
ForbiddenError - see app/services/errors.py for why that's a separate
concern from a role check.
"""
from datetime import date
import json

from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, Response, HTMLResponse, FileResponse, StreamingResponse

from app.auth import can_create_expense, can_review_expense, can_delete_expense
from app.templates_env import templates
from app.programs.expenses.pdf_voucher import build_voucher_pdf
from app.config import UPLOADS_DIR, ADVANCE_CATEGORY_NAME
from app.programs.assets.services.assets import pickable_assets as _pickable_assets
from app.programs.expenses.services import expenses as expenses_service
from app.programs.expenses.services import settings as settings_service
from app.programs.expenses.services import reports as reports_service
from app.programs.expenses.services import suppliers as suppliers_service
from app.programs.expenses.services import advances as advances_service
from app.programs.expenses.services import sharing as sharing_service
from app.services.errors import ValidationError, NotFoundError, ConflictError, ForbiddenError
from app.routes import guards
from app.flash import set_flash
from app import undo
from app import sandbox
from app.chart_helpers import build_donut_chart

router = APIRouter()


def _active_suppliers_json(suppliers):
    """Small helper for expense_form.html's VAT-autofill JS - a {vat:
    name} map of active suppliers, serialized once here rather than with
    a `tojson` Jinja filter (this app's plain Jinja2 Environment doesn't
    register one - see app/templates_env.py)."""
    return json.dumps({s["vat_number"]: s["name"] for s in suppliers})


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    user, deny = guards.require_login_page(request)
    if deny:
        return deny
    stats = expenses_service.dashboard_stats()
    donut = build_donut_chart(stats["by_category"])
    # Unread manager notifications (budget over-runs).
    notifications = []
    if user["role"] == "manager":
        from app.programs.expenses.services import budgets as _budgets_svc
        notifications = _budgets_svc.get_notifications(user["id"], unread_only=True)
    return templates.TemplateResponse("expenses/dashboard.html", {
        "request": request, "user": user, "donut": donut,
        "notifications": notifications, **stats,
    })


@router.get("/expenses", response_class=HTMLResponse)
def expense_list(request: Request, status: str = "", category_id: str = "",
                  cost_center_id: str = "", start: str = "", end: str = ""):
    user, deny = guards.require_login_page(request)
    if deny:
        return deny
    rows, total = expenses_service.list_expenses(status, category_id, cost_center_id, start, end)
    return templates.TemplateResponse("expenses/expenses_list.html", {
        "request": request, "user": user, "expenses": rows, "total": total,
        "categories": settings_service.list_categories(active_only=True),
        "cost_centers": settings_service.list_cost_centers(active_only=True),
        "filters": {"status": status, "category_id": category_id,
                    "cost_center_id": cost_center_id, "start": start, "end": end},
    })


@router.get("/expenses/export.xlsx")
def expenses_export_xlsx(request: Request, status: str = "", category_id: str = "",
                          cost_center_id: str = "", start: str = "", end: str = ""):
    user, deny = guards.require_login_page(request)
    if deny:
        return deny
    rows = reports_service.export_rows(start, end, status, category_id, cost_center_id)
    category_name = _lookup_name(settings_service.list_categories(), category_id)
    cc_name = _lookup_name(settings_service.list_cost_centers(), cost_center_id)
    filters_desc = reports_service.describe_filters(start, end, status, category_name, cc_name)
    document_no = reports_service.allocate_document_number("RPT")
    buf = reports_service.build_export_workbook(
        rows, document_no=document_no, generated_by=user["full_name"], filters_description=filters_desc,
    )
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="petty_cash_expenses.xlsx"'},
    )


@router.get("/expenses/export.pdf")
def expenses_export_pdf(request: Request, status: str = "", category_id: str = "",
                         cost_center_id: str = "", start: str = "", end: str = ""):
    user, deny = guards.require_login_page(request)
    if deny:
        return deny
    rows = reports_service.export_rows(start, end, status, category_id, cost_center_id)
    category_name = _lookup_name(settings_service.list_categories(), category_id)
    cc_name = _lookup_name(settings_service.list_cost_centers(), cost_center_id)
    filters_desc = reports_service.describe_filters(start, end, status, category_name, cc_name)
    pdf_bytes = reports_service.build_export_pdf(
        rows, generated_by=user["full_name"], filters_description=filters_desc,
    )
    return Response(content=pdf_bytes, media_type="application/pdf",
                     headers={"Content-Disposition": 'attachment; filename="petty_cash_expenses.pdf"'})


def _lookup_name(items, item_id):
    if not item_id:
        return ""
    match = next((i for i in items if str(i["id"]) == str(item_id)), None)
    return match["name"] if match else ""




def _asset_picker_data(user):
    """Pickable assets for the voucher form. A manager/accountant sees the
    whole registry; a plain user sees only what is assigned to them.
    An empty list means the picker is hidden for that person - no assets
    to choose, so the field is not demanded even on maintenance categories.
    See _validate_lines() in expenses.py for the full decision."""
    from app import auth as _auth
    all_assets = _auth.can_manage_assets(user["role"]) or _auth.can_assign_asset(user["role"])
    return _pickable_assets(user_id=user["id"], all_assets=all_assets)

@router.get("/expenses/new", response_class=HTMLResponse)
def new_expense_form(request: Request):
    user, deny = guards.require_role_page(request, can_create_expense)
    if deny:
        return deny
    _active_suppliers = suppliers_service.list_suppliers(status="active")
    employee_balances = {
        e["name"] + "|" + e["phone"]: e["outstanding"]
        for e in advances_service.get_outstanding_balances()
        if e["outstanding"] > 0
    }
    return templates.TemplateResponse("expenses/expense_form.html", {
        "request": request, "user": user,
        "categories": settings_service.list_categories(active_only=True),
        "cost_centers": settings_service.list_cost_centers(active_only=True),
        "suppliers": _active_suppliers, "suppliers_json": _active_suppliers_json(_active_suppliers),
        "today": date.today().isoformat(), "error": None, "form_values": None,
        "edit_expense_id": None,
        "default_category_id": expenses_service.recent_category_id(user["id"]),
        "advance_category_name": ADVANCE_CATEGORY_NAME,
        "employee_balances_json": json.dumps(employee_balances),

        "pickable_assets": _asset_picker_data(user),
        "pickable_assets_json": json.dumps([{"id": a["id"], "tag": a["asset_tag"],
            "name": a["name"], "category": a["category"]} for a in _asset_picker_data(user)]),    })


def _extract_lines_from_form(form):
    """Zips the positionally-aligned line_* field lists into a list of dicts,
    dropping any row that was never touched (added via 'add line' but left
    completely blank) rather than treating it as a validation error."""
    cat_ids = form.getlist("line_category_id")
    particulars_list = form.getlist("line_particulars")
    amounts = form.getlist("line_amount")
    vatable_list = form.getlist("line_is_vatable")
    supplier_names = form.getlist("line_supplier_name")
    supplier_vats = form.getlist("line_supplier_vat")
    employee_names = form.getlist("line_employee_name")
    employee_phones = form.getlist("line_employee_phone")

    lines = []
    for i in range(len(cat_ids)):
        particulars = (particulars_list[i] if i < len(particulars_list) else "").strip()
        amount_raw = (amounts[i] if i < len(amounts) else "").strip()
        if not particulars and not amount_raw:
            continue  # untouched extra row - not an error, just skip it
        lines.append({
            "category_id": cat_ids[i],
            "particulars": particulars,
            "amount": amount_raw,
            "is_vatable": (vatable_list[i] if i < len(vatable_list) else "0") == "1",
            "supplier_name": (supplier_names[i] if i < len(supplier_names) else "").strip(),
            "supplier_vat": (supplier_vats[i] if i < len(supplier_vats) else "").strip(),
            "employee_name": (employee_names[i] if i < len(employee_names) else "").strip(),
            "employee_phone": (employee_phones[i] if i < len(employee_phones) else "").strip(),
        })
    return lines


@router.post("/expenses/new")
async def create_expense(request: Request):
    user, deny = guards.require_role_action(request, can_create_expense)
    if deny:
        return deny

    form = await request.form()
    csrf_token = form.get("csrf_token", "")
    if bad := guards.require_csrf(request, csrf_token):
        return bad

    action = form.get("action", "submit")
    is_draft = action == "draft"
    expense_date = form.get("expense_date", "")
    cost_center_id = form.get("cost_center_id", "")
    paid_to = form.get("paid_to", "")
    payment_mode = form.get("payment_mode", "Cash")
    receipt = form.get("receipt")
    lines = _extract_lines_from_form(form)
    _active_suppliers = suppliers_service.list_suppliers(status="active")

    def _rerender_with_error(message: str):
        _emp_balances = {
            e["name"] + "|" + e["phone"]: e["outstanding"]
            for e in advances_service.get_outstanding_balances()
            if e["outstanding"] > 0
        }
        return templates.TemplateResponse("expenses/expense_form.html", {
            "request": request, "user": user,
            "categories": settings_service.list_categories(active_only=True),
            "cost_centers": settings_service.list_cost_centers(active_only=True),
            "suppliers": _active_suppliers, "suppliers_json": _active_suppliers_json(_active_suppliers),
            "today": expense_date or date.today().isoformat(), "error": message,
            "form_values": {
                "cost_center_id": cost_center_id, "paid_to": paid_to, "payment_mode": payment_mode,
                "lines": lines,
            },
            "edit_expense_id": None,
            "advance_category_name": ADVANCE_CATEGORY_NAME,
            "employee_balances_json": json.dumps(_emp_balances),
            "pickable_assets": _asset_picker_data(user),
            "pickable_assets_json": json.dumps([{"id": a["id"], "tag": a["asset_tag"],
                "name": a["name"], "category": a["category"]} for a in _asset_picker_data(user)]),
        }, status_code=400)

    receipt_path = None
    if receipt is not None and getattr(receipt, "filename", ""):
        try:
            receipt_path = expenses_service.save_receipt(receipt.filename, receipt.file)
        except ValidationError as e:
            return _rerender_with_error(str(e))

    try:
        expense_id = expenses_service.create_expense(
            expense_date=expense_date, cost_center_id=cost_center_id, paid_to=paid_to,
            payment_mode=payment_mode, receipt_path=receipt_path, user_id=user["id"], lines=lines,
            is_draft=is_draft,
        )
    except ValidationError as e:
        return _rerender_with_error(str(e))

    set_flash(request,
              "Draft saved." if is_draft else "Voucher submitted for approval.")
    return RedirectResponse(f"/expense-program/expenses/{expense_id}", status_code=302)


@router.get("/expenses/{expense_id}/edit", response_class=HTMLResponse)
def edit_draft_form(request: Request, expense_id: int):
    user, deny = guards.require_role_page(request, can_create_expense)
    if deny:
        return deny
    expense = expenses_service.get_expense(expense_id)
    if not expense:
        return HTMLResponse("Voucher not found", status_code=404)
    if expense["status"] != "draft":
        return HTMLResponse("This voucher is no longer a draft and can't be edited.", status_code=409)
    if expense["prepared_by"] != user["id"] and user["role"] != "manager":
        return HTMLResponse("Only the person who started this draft (or a manager) can edit it.", status_code=403)

    form_values = {
        "cost_center_id": str(expense["cost_center_id"]) if expense["cost_center_id"] else "",
        "paid_to": expense["paid_to"], "payment_mode": expense["payment_mode"],
        "lines": [{"category_id": str(l["category_id"]), "particulars": l["particulars"],
                   "amount": l["amount"], "is_vatable": bool(l["is_vatable"]),
                   "supplier_name": l["supplier_name"] or "", "supplier_vat": l["supplier_vat"] or "",
                   "employee_name": l.get("employee_name") or "",
                   "employee_phone": l.get("employee_phone") or ""}
                  for l in expense["lines"]],
    }
    _active_suppliers = suppliers_service.list_suppliers(status="active")
    employee_balances = {
        e["name"] + "|" + e["phone"]: e["outstanding"]
        for e in advances_service.get_outstanding_balances()
        if e["outstanding"] > 0
    }
    return templates.TemplateResponse("expenses/expense_form.html", {
        "request": request, "user": user,
        "categories": settings_service.list_categories(active_only=True),
        "cost_centers": settings_service.list_cost_centers(active_only=True),
        "suppliers": _active_suppliers, "suppliers_json": _active_suppliers_json(_active_suppliers),
        "today": expense["expense_date"], "error": None, "form_values": form_values,
        "edit_expense_id": expense_id, "existing_receipt": expense["receipt_path"],
        "advance_category_name": ADVANCE_CATEGORY_NAME,
        "employee_balances_json": json.dumps(employee_balances),

        "pickable_assets": _asset_picker_data(user),
        "pickable_assets_json": json.dumps([{"id": a["id"], "tag": a["asset_tag"],
            "name": a["name"], "category": a["category"]} for a in _asset_picker_data(user)]),    })


@router.post("/expenses/{expense_id}/edit")
async def edit_draft(request: Request, expense_id: int):
    user, deny = guards.require_role_action(request, can_create_expense)
    if deny:
        return deny

    form = await request.form()
    csrf_token = form.get("csrf_token", "")
    if bad := guards.require_csrf(request, csrf_token):
        return bad

    action = form.get("action", "submit")
    expense_date = form.get("expense_date", "")
    cost_center_id = form.get("cost_center_id", "")
    paid_to = form.get("paid_to", "")
    payment_mode = form.get("payment_mode", "Cash")
    receipt = form.get("receipt")
    lines = _extract_lines_from_form(form)
    _active_suppliers = suppliers_service.list_suppliers(status="active")

    def _rerender_with_error(message: str, status_code: int = 400):
        _emp_balances = {
            e["name"] + "|" + e["phone"]: e["outstanding"]
            for e in advances_service.get_outstanding_balances()
            if e["outstanding"] > 0
        }
        return templates.TemplateResponse("expenses/expense_form.html", {
            "request": request, "user": user,
            "categories": settings_service.list_categories(active_only=True),
            "cost_centers": settings_service.list_cost_centers(active_only=True),
            "suppliers": _active_suppliers, "suppliers_json": _active_suppliers_json(_active_suppliers),
            "today": expense_date or date.today().isoformat(), "error": message,
            "form_values": {
                "cost_center_id": cost_center_id, "paid_to": paid_to, "payment_mode": payment_mode,
                "lines": lines,
            },
            "edit_expense_id": expense_id,
            "advance_category_name": ADVANCE_CATEGORY_NAME,
            "employee_balances_json": json.dumps(_emp_balances),
            "pickable_assets": _asset_picker_data(user),
            "pickable_assets_json": json.dumps([{"id": a["id"], "tag": a["asset_tag"],
                "name": a["name"], "category": a["category"]} for a in _asset_picker_data(user)]),
        }, status_code=status_code)

    receipt_path = None
    if receipt is not None and getattr(receipt, "filename", ""):
        try:
            receipt_path = expenses_service.save_receipt(receipt.filename, receipt.file)
        except ValidationError as e:
            return _rerender_with_error(str(e))

    try:
        expenses_service.update_draft(
            expense_id, user, expense_date=expense_date, cost_center_id=cost_center_id,
            paid_to=paid_to, payment_mode=payment_mode, receipt_path=receipt_path, lines=lines,
        )
        if action == "submit":
            expenses_service.submit_draft(expense_id, user)
    except ForbiddenError as e:
        return HTMLResponse(str(e), status_code=403)
    except NotFoundError as e:
        return HTMLResponse(str(e), status_code=404)
    except ConflictError as e:
        return HTMLResponse(str(e), status_code=409)
    except ValidationError as e:
        return _rerender_with_error(str(e))

    set_flash(request,
              "Voucher submitted for approval." if action == "submit" else "Draft saved.")
    return RedirectResponse(f"/expense-program/expenses/{expense_id}", status_code=302)


@router.post("/expenses/{expense_id}/submit")
def submit_draft(request: Request, expense_id: int, csrf_token: str = Form(...)):
    user, deny = guards.require_role_action(request, can_create_expense)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    try:
        expenses_service.submit_draft(expense_id, user)
    except ForbiddenError as e:
        return HTMLResponse(str(e), status_code=403)
    except NotFoundError as e:
        return HTMLResponse(str(e), status_code=404)
    except ConflictError as e:
        return HTMLResponse(str(e), status_code=409)
    except ValidationError as e:
        return HTMLResponse(str(e), status_code=400)
    set_flash(request, "Voucher submitted for approval.")
    return RedirectResponse(f"/expense-program/expenses/{expense_id}", status_code=302)


@router.post("/expenses/{expense_id}/discard")
def discard_draft(request: Request, expense_id: int, csrf_token: str = Form(...)):
    user, deny = guards.require_role_action(request, can_create_expense)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    try:
        expenses_service.discard_draft(expense_id, user)
    except ForbiddenError as e:
        return HTMLResponse(str(e), status_code=403)
    except NotFoundError as e:
        return HTMLResponse(str(e), status_code=404)
    except ConflictError as e:
        return HTMLResponse(str(e), status_code=409)
    set_flash(request, "Draft discarded.", "info")
    return RedirectResponse("/expense-program/expenses?status=draft", status_code=302)


@router.get("/expenses/{expense_id}", response_class=HTMLResponse)
def expense_detail(request: Request, expense_id: int):
    user, deny = guards.require_login_page(request)
    if deny:
        return deny
    expense = expenses_service.get_expense(expense_id)
    if not expense:
        return HTMLResponse("Expense not found", status_code=404)
    can_edit_draft = (
        expense["status"] == "draft"
        and (expense["prepared_by"] == user["id"] or user["role"] == "manager")
    )
    # Cloning makes a *new* voucher, so it follows create permission rather
    # than review permission - and it is offered only where the service
    # would actually allow it, so the button never leads to a 403.
    can_clone = (
        can_create_expense(user["role"])
        and expense["status"] != "deleted"
        and (expense["status"] != "draft" or can_edit_draft)
    )
    can_share = expense["status"] in sharing_service.SHAREABLE_STATUSES
    return templates.TemplateResponse("expenses/expense_detail.html", {
        "request": request, "user": user, "e": expense, "can_edit_draft": can_edit_draft,
        "can_clone": can_clone,
        "can_share": can_share,
        "shares": sharing_service.list_shares(expense_id),
        "shareable_users": sharing_service.list_shareable_users(user["id"]) if can_share else [],
    })


@router.post("/expenses/{expense_id}/clone")
def clone_expense(request: Request, expense_id: int, csrf_token: str = Form(...)):
    """Copies a voucher into a fresh draft and drops the user straight into
    editing it - the only thing usually left to change is the date and an
    amount. Needs create permission, not review permission: the result is a
    new voucher, not an action on the existing one."""
    user, deny = guards.require_role_action(request, can_create_expense)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    try:
        new_id = expenses_service.clone_expense(expense_id, user)
    except ForbiddenError as e:
        return HTMLResponse(str(e), status_code=403)
    except NotFoundError as e:
        return HTMLResponse(str(e), status_code=404)
    except ConflictError as e:
        return HTMLResponse(str(e), status_code=409)
    set_flash(request, "Copied to a new draft. Check the date and amounts before submitting.")
    return RedirectResponse(f"/expense-program/expenses/{new_id}/edit", status_code=302)


@router.post("/expenses/{expense_id}/share")
async def share_expense(request: Request, expense_id: int):
    """Hands a voucher to someone (normally the accountant) via an in-app
    notification. Any logged-in user may share - it grants the recipient
    nothing they could not already see, it only points them at it."""
    user, deny = guards.require_login_action(request)
    if deny:
        return deny

    form = await request.form()
    if bad := guards.require_csrf(request, form.get("csrf_token", "")):
        return bad

    # Boundary parsing: raw strings out of the form, validated in the service.
    user_ids = form.getlist("share_with")
    note = form.get("note", "")

    try:
        count = sharing_service.share_voucher(expense_id, user_ids, note, user)
    except ValidationError as e:
        set_flash(request, str(e), "warn")
        return RedirectResponse(f"/expense-program/expenses/{expense_id}", status_code=302)
    except NotFoundError as e:
        return HTMLResponse(str(e), status_code=404)
    except ConflictError as e:
        return HTMLResponse(str(e), status_code=409)

    set_flash(request, f"Shared with {count} " + ("person." if count == 1 else "people."))
    return RedirectResponse(f"/expense-program/expenses/{expense_id}", status_code=302)


@router.post("/expenses/{expense_id}/approve")
def approve_expense(request: Request, expense_id: int, csrf_token: str = Form(...)):
    user, deny = guards.require_role_action(request, can_review_expense)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    try:
        float_warning = expenses_service.approve_expense(expense_id, user["id"])
    except NotFoundError as e:
        return HTMLResponse(str(e), status_code=404)
    except ConflictError as e:
        return HTMLResponse(str(e), status_code=409)
    token = undo.offer(request, "expense_approve", [expense_id])
    if float_warning:
        set_flash(request,
                  "Voucher approved. Cash float is now negative — replenishment needed.",
                  "danger", undo_token=token)
    else:
        set_flash(request, "Voucher approved.", undo_token=token)
    return RedirectResponse(f"/expense-program/expenses/{expense_id}", status_code=302)


@router.post("/expenses/{expense_id}/reject")
def reject_expense(request: Request, expense_id: int, csrf_token: str = Form(...), reason: str = Form("")):
    user, deny = guards.require_role_action(request, can_review_expense)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    try:
        expenses_service.reject_expense(expense_id, user["id"], reason)
    except NotFoundError as e:
        return HTMLResponse(str(e), status_code=404)
    except ConflictError as e:
        return HTMLResponse(str(e), status_code=409)
    token = undo.offer(request, "expense_reject", [expense_id])
    set_flash(request, "Voucher rejected.", "warn", undo_token=token)
    return RedirectResponse(f"/expense-program/expenses/{expense_id}", status_code=302)


@router.post("/expenses/{expense_id}/delete")
def delete_expense(request: Request, expense_id: int, csrf_token: str = Form(...), reason: str = Form(...)):
    user, deny = guards.require_role_action(request, can_delete_expense)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    # Captured *before* the delete: the row does not keep what status it
    # held beforehand, and the undo has to put it back where it was rather
    # than guessing 'pending' for a voucher that was already approved.
    previous = expenses_service.statuses_for([expense_id])
    try:
        expenses_service.delete_expense(expense_id, user["id"], reason)
    except ValidationError as e:
        return HTMLResponse(str(e), status_code=400)
    except NotFoundError as e:
        return HTMLResponse(str(e), status_code=404)
    token = undo.offer(request, "expense_delete", [expense_id],
                       {"previous_status": {str(k): v for k, v in previous.items()}})
    set_flash(request, "Voucher deleted. It stays in the audit log.", "warn",
              undo_token=token)
    return RedirectResponse("/expense-program/expenses", status_code=302)


@router.post("/expenses/bulk")
async def bulk_action(request: Request):
    """The floating toolbar on the Expenses list.

    One endpoint for all three actions rather than three, because the form
    that posts here is one form with three submit buttons - the same shape
    the New Voucher page already uses for draft-vs-submit. The action name
    decides which permission is required, so `delete` is manager-only here
    exactly as it is on the single-voucher route; a bulk endpoint must never
    become a softer door to the same thing.

    Ids arrive as repeated `expense_ids` checkboxes. They are validated and
    de-duplicated in the service layer, and every id is re-checked against
    the normal per-voucher rules there - nothing is trusted because it was
    posted."""
    form = await request.form()
    action = (form.get("bulk_action") or "").strip()
    reason = (form.get("bulk_reason") or "").strip()
    ids = form.getlist("expense_ids")

    permission = can_delete_expense if action == "delete" else can_review_expense
    user, deny = guards.require_role_action(request, permission)
    if deny:
        return deny
    if bad := guards.require_csrf(request, form.get("csrf_token") or ""):
        return bad

    back = "/expense-program/expenses"
    try:
        if action == "approve":
            result, float_warning = expenses_service.bulk_approve(ids, user["id"])
            kind, verb, level = "expense_approve", "approved", "success"
            if float_warning:
                level = "danger"
        elif action == "reject":
            result = expenses_service.bulk_reject(ids, user["id"], reason)
            kind, verb, level = "expense_reject", "rejected", "warn"
        elif action == "delete":
            # Same reason as the single delete: capture the prior statuses
            # before they are overwritten.
            previous = expenses_service.statuses_for(
                [i for i in ids if str(i).isdigit()])
            result = expenses_service.bulk_delete(ids, user["id"], reason)
            kind, verb, level = "expense_delete", "deleted", "warn"
        else:
            return HTMLResponse("Unknown bulk action.", status_code=400)
    except ValidationError as e:
        set_flash(request, str(e), "warn")
        return RedirectResponse(back, status_code=302)

    extra = None
    if action == "delete":
        extra = {"previous_status": {str(k): v for k, v in previous.items()}}
    # Only what actually succeeded is offered for undo - reversing an id
    # that was skipped would be reversing somebody else's decision.
    token = undo.offer(request, kind, result.ok, extra) if result.ok else ""
    set_flash(request, result.summary(verb), level, undo_token=token)
    return RedirectResponse(back, status_code=302)


@router.get("/uploads/{filename}")
def get_upload(request: Request, filename: str):
    user, deny = guards.require_login_page(request)
    if deny:
        return deny
    try:
        path = sandbox.safe_join(UPLOADS_DIR, filename)
    except ValueError:
        return HTMLResponse("File not found", status_code=404)
    if not path.exists():
        return HTMLResponse("File not found", status_code=404)
    return FileResponse(path)


@router.get("/expenses/{expense_id}/voucher.pdf")
def voucher_pdf(request: Request, expense_id: int):
    user, deny = guards.require_login_page(request)
    if deny:
        return deny
    expense = expenses_service.get_expense(expense_id)
    if not expense:
        return HTMLResponse("Expense not found", status_code=404)
    if expense["status"] == "draft":
        return HTMLResponse("This voucher is still a draft - submit it first to generate its PDF.",
                             status_code=400)
    pdf_bytes = build_voucher_pdf(
        expense, expense["cost_center_name"],
        expense["prepared_by_name"], expense["approved_by_name"],
        printed_by_name=user["full_name"],
    )
    headers = {"Content-Disposition": f'inline; filename="{expense["voucher_no"]}.pdf"'}
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)
