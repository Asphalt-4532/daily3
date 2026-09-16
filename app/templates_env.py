from pathlib import Path
from fastapi.templating import Jinja2Templates
from app.config import CURRENCY_SYMBOL, COMPANY_NAME, COMPANY_ADDRESS, VAT_RATE

APP_DIR = Path(__file__).resolve().parent
SHARED_TEMPLATES_DIR = APP_DIR / "templates"

# Every program's templates directory gets listed here, namespaced under its
# own program-name subfolder (e.g. "expenses/dashboard.html") so filenames
# can never collide across programs even if two happen to pick the same
# name. Adding a future program means adding one line here - nothing about
# an existing program's files needs to change.
PROGRAM_TEMPLATES_DIRS = [
    APP_DIR / "programs" / "expenses" / "templates",
    APP_DIR / "programs" / "weekly_productions" / "templates",
    APP_DIR / "programs" / "word_editor" / "templates",
    APP_DIR / "programs" / "assets" / "templates",
]

templates = Jinja2Templates(directory=[str(SHARED_TEMPLATES_DIR), *[str(d) for d in PROGRAM_TEMPLATES_DIRS]])
templates.env.globals["CURRENCY_SYMBOL"] = CURRENCY_SYMBOL
templates.env.globals["COMPANY_NAME"] = COMPANY_NAME
templates.env.globals["COMPANY_ADDRESS"] = COMPANY_ADDRESS
templates.env.globals["VAT_RATE"] = VAT_RATE


def money(value):
    try:
        return f"{CURRENCY_SYMBOL} {float(value):,.2f}"
    except (TypeError, ValueError):
        return value


templates.env.filters["money"] = money


def csrf_input(request):
    token = request.session.get("csrf_token", "")
    return f'<input type="hidden" name="csrf_token" value="{token}">'


templates.env.globals["csrf_input"] = csrf_input


# Toast messages - base.html calls this once per page. Registered here for
# the same reason csrf_input is: every template needs it, no route should
# have to remember to pass it. See app/flash.py.
from app.flash import pop_flash as _pop_flash  # noqa: E402

templates.env.globals["pop_flash"] = _pop_flash


def unread_notifications(request):
    """Badge count for the top bar. A template global for the same reason
    pop_flash is one: base.html is on every page, and no route should have
    to remember to pass it. Returns 0 for a logged-out visitor."""
    from app.services.notifications import count_unread
    user_id = request.session.get("user_id") if hasattr(request, "session") else None
    if not user_id:
        return 0
    return count_unread(user_id)


templates.env.globals["unread_notifications"] = unread_notifications


def notification_groups(request):
    """The notification centre's grouped list, for _notification_drawer.html.

    A template global for the same reason unread_notifications() is one: the
    panel is included from two different top bars (base.html and
    _standalone_header.html), and neither can rely on every route in the app
    remembering to pass it. Returns [] for a logged-out visitor."""
    from app.services.notifications import list_grouped
    user_id = request.session.get("user_id") if hasattr(request, "session") else None
    if not user_id:
        return []
    return list_grouped(user_id)


templates.env.globals["notification_groups"] = notification_groups


# Breadcrumbs - see app/breadcrumbs.py for why these are derived from the
# URL here rather than filled in by each page's own template block.
def breadcrumbs(request):
    from app.breadcrumbs import trail_for
    return trail_for(request.url.path)


def breadcrumb_siblings(request):
    from app.breadcrumbs import siblings_for
    return siblings_for(request.url.path)


templates.env.globals["breadcrumbs"] = breadcrumbs
templates.env.globals["breadcrumb_siblings"] = breadcrumb_siblings


# How long the Undo button on a toast stays visible. Read from app/undo.py
# rather than written as a literal in base.html so the countdown and the
# server's own window can never drift apart.
from app.undo import BUTTON_SECONDS as _UNDO_BUTTON_SECONDS  # noqa: E402

templates.env.globals["UNDO_BUTTON_SECONDS"] = _UNDO_BUTTON_SECONDS


def _can_manage_wd_users_global(request):
    """Template helper so nav can check this without every route passing it."""
    from app.auth import can_manage_wd_users
    role = getattr(request.session, "get", lambda k, d=None: d)("role", "")
    if not role and hasattr(request, "session"):
        role = request.session.get("role", "")
    return can_manage_wd_users(role)


templates.env.globals["can_manage_wd_users"] = False  # default; overridden per-request in routes
