"""Shared HTTP-layer guards used by every route module.

Before this module existed, four separate route files each defined their own
copy of "redirect if not logged in", "403/redirect if wrong role", and "403
if bad CSRF" - four independent implementations of the same three rules,
which is exactly the kind of duplication that lets small inconsistencies
creep in (a bug the test suite caught: some pages sent an unauthenticated
visitor to "/" and let the dashboard redirect them to "/login" a second time,
while others sent them to "/login" directly - same outcome, extra hop, and
inconsistent for no reason). Now there is exactly one implementation, so
there's exactly one place to get it right.

Note that "/" is now itself the login page (app/routes/auth_routes.py's
home()), so sending an unauthenticated visitor to "/login" is still the
right target here - it's the canonical URL, and "/" would only render the
same form via one extra layer.

This module intentionally only knows about "is there a user" and "does this
permission-check function say yes" - it has no idea what a voucher, backup,
or category is. That's what keeps it reusable across every route file
without becoming coupled to any one feature.
"""
from fastapi import Request
from fastapi.responses import RedirectResponse, HTMLResponse

from app.auth import get_current_user, verify_csrf

__all__ = [
    "require_login_page", "require_role_page", "require_login_action",
    "require_role_action", "require_csrf", "forbidden", "bad_csrf",
]


def forbidden(message: str = "You don't have permission to do that.") -> HTMLResponse:
    return HTMLResponse(message, status_code=403)


def bad_csrf() -> HTMLResponse:
    return HTMLResponse(
        "Your session expired or this form was already submitted. Please go back and try again.",
        status_code=403,
    )


def require_login_page(request: Request):
    """For a GET page that just needs *any* logged-in user. Returns
    (user, None) if allowed, or (None, redirect_response) if not."""
    user = get_current_user(request)
    if not user:
        return None, RedirectResponse("/login", status_code=302)
    return user, None


def require_role_page(request: Request, permission_fn):
    """For a GET page restricted to a role. Distinguishes *not logged in*
    (-> /login) from *logged in but wrong role* (-> /programs, the hub - the
    one page guaranteed to exist and make sense for any role, since this
    module is shared across every program and can't assume any one
    program's dashboard is "the" landing page). Returns (user, None) if
    allowed, or (None, redirect_response) if not."""
    user = get_current_user(request)
    if not user:
        return None, RedirectResponse("/login", status_code=302)
    if not permission_fn(user["role"]):
        return None, RedirectResponse("/programs", status_code=302)
    return user, None


def require_login_action(request: Request):
    """For a POST/action endpoint that just needs any logged-in user."""
    user = get_current_user(request)
    if not user:
        return None, RedirectResponse("/login", status_code=302)
    return user, None


def require_role_action(request: Request, permission_fn):
    """For a POST/action endpoint restricted to a role. A 403 either way
    (not logged in or wrong role) - an action isn't a page a browser
    navigated to, so there's no friendlier place to redirect it."""
    user = get_current_user(request)
    if not user or not permission_fn(user["role"]):
        return None, forbidden()
    return user, None


def require_csrf(request: Request, token: str):
    """Returns None if the token is valid, or the 403 response to return if not."""
    if not verify_csrf(request, token):
        return bad_csrf()
    return None
