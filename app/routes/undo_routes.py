"""The one endpoint the Undo button on a toast posts to.

app/undo.py owns the token (issue, expire, consume) and knows nothing about
what is being reversed. This module owns the other half: the map from an
undo `kind` to the service function that reverses it, and the permission
check that has to pass *again* before it runs.

Registered unprefixed in main.py because undo is not a program's feature -
a second program can add a kind here without touching a route file of its
own, the same way /audit and /notifications are shared.

Re-checking permission is not belt-and-braces. A token proves who performed
an action and how recently; it does not prove they may still perform it. A
manager could have had the permission revoked at Users -> Roles &
Permissions in the seconds since - that page applies immediately and with no
restart, precisely so a permission change takes effect right away, and an
undo token must not be a ten-second hole in it.
"""
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse

from app import undo
from app.auth import can_review_expense, can_delete_expense
from app.flash import set_flash
from app.routes import guards
from app.services.errors import ServiceError
from app.programs.expenses.services import expenses as expenses_service

router = APIRouter()


def _undo_approve(expense_id, user, extra):
    expenses_service.revert_approval(expense_id, user["id"])


def _undo_reject(expense_id, user, extra):
    expenses_service.revert_rejection(expense_id, user["id"])


def _undo_delete(expense_id, user, extra):
    previous = (extra or {}).get("previous_status", {})
    expenses_service.restore_deleted(
        expense_id, user["id"], previous.get(str(expense_id), "pending"))


# kind -> (permission function, reversal function, toast wording).
# A program adding a reversible action adds one row here; nothing else in
# this file changes.
_KINDS = {
    "expense_approve": (can_review_expense, _undo_approve, "Approval undone."),
    "expense_reject": (can_review_expense, _undo_reject, "Rejection undone."),
    "expense_delete": (can_delete_expense, _undo_delete, "Voucher restored."),
}


@router.post("/undo")
def undo_action(request: Request, csrf_token: str = Form(...),
                token: str = Form(...), next_url: str = Form("")):
    user, deny = guards.require_login_action(request)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad

    pending = undo.claim(request, token)
    if pending is None:
        # Expired, already used, or never issued - all the same from here,
        # and all recoverable by hand. Never a hard error page: the user
        # clicked a button that was about to vanish anyway.
        set_flash(request, "That undo window has closed.", "warn")
        return _back(next_url)

    entry = _KINDS.get(pending["kind"])
    if entry is None:
        set_flash(request, "That action can't be undone.", "warn")
        return _back(next_url)
    permission_fn, reverse, message = entry

    if not permission_fn(user["role"]):
        return guards.forbidden()

    reversed_ids, failures = [], []
    for expense_id in pending["ids"]:
        try:
            reverse(expense_id, user, pending.get("extra"))
            reversed_ids.append(expense_id)
        except ServiceError as e:
            # Same reasoning as the bulk actions in expenses.py: a batch is
            # partially reversible, and failing the lot because one record
            # moved on would be worse than saying so.
            failures.append(str(e))

    if not reversed_ids:
        set_flash(request, failures[0] if failures else "Nothing left to undo.", "warn")
    elif failures:
        set_flash(request,
                  f"{len(reversed_ids)} undone, {len(failures)} could not be.", "warn")
    else:
        set_flash(request, message)
    return _back(next_url)


def _back(next_url: str):
    """Return the user where they clicked from.

    Only same-site absolute paths are honoured. A `next` that a caller can
    set to anything is an open redirect, and this one arrives in a form
    body - the check is one line and removes the whole class of problem.
    """
    target = (next_url or "").strip()
    if not target.startswith("/") or target.startswith("//"):
        target = "/programs"
    return RedirectResponse(target, status_code=302)
