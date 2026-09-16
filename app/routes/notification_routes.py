"""The one page where a user sees what has been pointed at them.

Shared, not program-specific: budget over-runs come from Expense Program
and voucher hand-offs come from the same place today, but the inbox itself
belongs to the user, not to a program - the same way /audit and /backups do.
Registered unprefixed in main.py.

Opening this *page* marks everything read - arriving here is a deliberate
act, so it is safe to treat as "seen". The slide-over notification centre
(_notification_drawer.html) deliberately does not: it can be flicked open
from any page, and clearing the badge every time somebody glanced at it
would make the badge meaningless. Hence the two explicit POST endpoints
below, which the panel uses instead.

The permanent record of what happened is the audit log, not this - marking
a notification read never deletes anything.
"""
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from app.templates_env import templates
from app.routes import guards
from app.services import notifications as notifications_service

router = APIRouter()


def _back(request: Request):
    """Return the user to the page whose panel they clicked from.

    Taken from the Referer header rather than a form field, and only
    honoured when it is a same-site absolute path - an unchecked redirect
    target is an open redirect no matter which header it arrives in. Falls
    back to the full notifications page."""
    referer = request.headers.get("referer", "") or ""
    path = ""
    if referer:
        marker = "://"
        if marker in referer:
            rest = referer.split(marker, 1)[1]
            path = "/" + rest.split("/", 1)[1] if "/" in rest else "/"
        elif referer.startswith("/"):
            path = referer
    if not path.startswith("/") or path.startswith("//"):
        path = "/notifications"
    return RedirectResponse(path, status_code=302)


@router.get("/notifications", response_class=HTMLResponse)
def notifications_page(request: Request):
    user, deny = guards.require_login_page(request)
    if deny:
        return deny
    # Read before marking, or the page would always render an empty list.
    items = notifications_service.list_for_user(user["id"])
    notifications_service.mark_all_read(user["id"])
    return templates.TemplateResponse("notifications.html", {
        "request": request, "user": user, "items": items,
    })


@router.post("/notifications/read-all")
def mark_all_read(request: Request, csrf_token: str = Form(...)):
    user, deny = guards.require_login_action(request)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    notifications_service.mark_all_read(user["id"])
    return _back(request)


@router.post("/notifications/{notification_id}/read")
def mark_one_read(request: Request, notification_id: int, csrf_token: str = Form(...)):
    user, deny = guards.require_login_action(request)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    # Ownership is enforced inside mark_read()'s WHERE clause, not by a
    # separate lookup here - see that function for why. A miss is silent:
    # the redirect looks identical whether the id was someone else's or
    # never existed, which keeps this from being an existence oracle.
    notifications_service.mark_read(notification_id, user["id"])
    return _back(request)
