"""Breadcrumb trails, derived from the URL path.

This app is four programs deep behind a hub (`/programs`), and a page like
`/expense-program/expenses/12` has three ancestors a user may well want to
jump back to. The browser's Back button walks *history*, which is not the
same thing: arriving at a voucher from a notification link means Back goes
to the notification, not to the Expenses list the voucher belongs to.

Shared infrastructure (app/, not app/programs/): it maps path prefixes to
labels and knows nothing about what a voucher or a document is. A new
program adds one entry to _SECTIONS and one to _CHILDREN - the same additive
shape as app/routes/programs_routes.py's PROGRAMS list and
app/templates_env.py's PROGRAM_TEMPLATES_DIRS.

Why a Python module rather than a `{% block breadcrumbs %}` in each
template: every page would have had to remember to fill it in, and the ones
that forgot would silently have none. Deriving it from `request.url.path`
means a page cannot have a wrong trail without having a wrong URL.
"""
__all__ = ["trail_for", "siblings_for", "SECTION_LABELS"]

# First path segment -> (label, home url). The hub itself is prepended to
# every trail below, so these are the second crumb.
_SECTIONS = {
    "expense-program": ("Petty Cash & Expenses", "/expense-program/"),
    "weekly-productions": ("Weekly Productions", "/weekly-productions/"),
    "word-editor": ("Word Editor", "/word-editor/"),
    "assets": ("Assets & Custody", "/assets/"),
}

# Second path segment -> label, per section. Anything not listed falls back
# to a title-cased version of the segment, which is right often enough
# ("budgets" -> "Budgets") that spelling out every page would be noise.
_CHILDREN = {
    "expense-program": {
        "expenses": "Expenses",
        "reports": "Reports",
        "vat-report": "VAT report",
        "advances": "Advances",
        "cash": "Cash float",
        "budgets": "Budgets",
        "settings": "Settings",
    },
    "weekly-productions": {
        "reports": "Reports",
        "settings": "Settings",
    },
    "word-editor": {
        "documents": "Documents",
        "profile": "Company profile",
        "users": "Users",
        "universal-templates": "Templates",
    },
    "assets": {
        "reports": "Reports",
        "employees": "Employees",
        "settings": "Settings",
    },
}

# Shared admin pages, which hang off the hub rather than any program.
_ADMIN = {
    "users": "Users & roles",
    "audit": "Audit log",
    "backups": "Backups",
    "notifications": "Alerts",
    "settings": "Settings",
}

SECTION_LABELS = {key: value[0] for key, value in _SECTIONS.items()}

# Segments that are an action or an id rather than a page in their own
# right. They still get a crumb (so the user can see where they are) but
# never a link, because "new" or "12" is not somewhere to navigate *back*
# to - it is where you already are.
_LEAF_ACTIONS = {"new", "edit", "submit", "discard", "print", "history",
                 "export", "clone", "roles"}


def _label_for_leaf(segment: str) -> str:
    """A record id becomes "#12"; anything else is title-cased. Recognised
    action words are listed in _LEAF_ACTIONS purely as documentation of what
    turns up here - they get the same treatment as any other segment."""
    if segment.isdigit():
        return f"#{segment}"
    return segment.replace("-", " ").replace("_", " ").capitalize()


def trail_for(path: str, user=None) -> list:
    """Return [{label, url, current}, ...] for a URL path.

    The last crumb is always `current` and carries no link. Returns an empty
    list for pages where a breadcrumb would be noise: the hub itself, login,
    and anything not recognised - a trail that guesses is worse than none.
    """
    if not path:
        return []
    path = path.split("?")[0]
    segments = [s for s in path.strip("/").split("/") if s]
    if not segments:
        return []
    head = segments[0]
    if head in ("login", "logout", "static", "programs"):
        return []

    crumbs = [{"label": "Programs", "url": "/programs", "current": False}]

    if head in _SECTIONS:
        label, home = _SECTIONS[head]
        crumbs.append({"label": label, "url": home, "current": False})
        rest = segments[1:]
        section_children = _CHILDREN.get(head, {})
        if rest:
            child = rest[0]
            child_label = section_children.get(
                child, child.replace("-", " ").capitalize())
            crumbs.append({
                "label": child_label,
                "url": f"/{head}/{child}",
                "current": False,
            })
            for segment in rest[1:]:
                crumbs.append({"label": _label_for_leaf(segment), "url": "",
                               "current": False})
    elif head in _ADMIN:
        crumbs.append({"label": _ADMIN[head], "url": f"/{head}", "current": False})
        for segment in segments[1:]:
            crumbs.append({"label": _label_for_leaf(segment), "url": "",
                           "current": False})
    else:
        return []

    crumbs[-1]["current"] = True
    crumbs[-1]["url"] = ""
    return crumbs


def siblings_for(path: str) -> list:
    """Pages at the same tier as the current one, for a crumb's drop-down.

    Kept separate from trail_for() because it answers a different question -
    "where else could I go from here", not "where am I" - and only the last
    linked crumb ever needs it.
    """
    segments = [s for s in (path or "").strip("/").split("/") if s]
    if not segments:
        return []
    head = segments[0]
    if head not in _CHILDREN:
        return []
    return [{"label": label, "url": f"/{head}/{slug}"}
            for slug, label in _CHILDREN[head].items()]
