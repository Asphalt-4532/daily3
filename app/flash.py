"""One-shot user feedback messages ("toasts").

A route that ends in a redirect has nowhere to put "that worked" - the
response it returns is thrown away by the browser. This stores a single
short message in the session so the *next* rendered page can show it, then
removes it so it never appears twice.

Deliberately shared infrastructure (app/, not app/programs/): it knows
nothing about vouchers, documents or assets, exactly like app/routes/guards.py.

Session-backed rather than a query string because a message in the URL
survives a refresh, can be edited by whoever holds the link, and leaks into
the browser history. SessionMiddleware is already registered in main.py, so
this adds no new dependency.

Rendering: base.html calls pop_flash(request) once, via the template global
registered in app/templates_env.py. Pages that do not extend base.html
(login.html, programs.html, coming_soon.html) show nothing - they are
entry points, not redirect targets.
"""
from typing import Optional

__all__ = ["set_flash", "pop_flash", "LEVELS", "MAX_LENGTH"]

# Kept in step with the .toast-* classes in app/static/style.css.
LEVELS = ("success", "info", "warn", "danger")
DEFAULT_LEVEL = "info"

# A toast is a one-line confirmation, not an error report. Anything longer
# is truncated rather than rejected - a clipped message is still better
# feedback than none, and this is never the only record of an action (the
# audit log is).
MAX_LENGTH = 300

_SESSION_KEY = "_flash"


def set_flash(request, message: str, level: str = "success",
              undo_token: str = "", undo_label: str = "Undo") -> None:
    """Queue a message for the next rendered page. A second call before
    that page renders replaces the first - one toast at a time, by design.

    `undo_token` is optional and comes from app/undo.py. It is carried here
    rather than looked up by the template so that the toast and the thing
    it can reverse are queued in one step: a message with no token simply
    renders without an Undo button, which is every existing caller."""
    text = (message or "").strip()
    if not text:
        return
    if level not in LEVELS:
        level = DEFAULT_LEVEL
    request.session[_SESSION_KEY] = {
        "message": text[:MAX_LENGTH],
        "level": level,
        "undo_token": undo_token or "",
        "undo_label": (undo_label or "Undo").strip()[:40],
    }


def pop_flash(request) -> Optional[dict]:
    """Return and clear the queued message, or None. Called exactly once
    per rendered page, from base.html."""
    session = getattr(request, "session", None)
    if not session:
        return None
    data = session.pop(_SESSION_KEY, None)
    if not isinstance(data, dict) or not data.get("message"):
        return None
    # Defaults for a message queued before undo support existed, or by a
    # caller that does not offer one - the template can then read these
    # keys unconditionally.
    data.setdefault("undo_token", "")
    data.setdefault("undo_label", "Undo")
    return data
