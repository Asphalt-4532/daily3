"""HTTP layer for the program hub - the post-login landing page a person
sees before entering any specific program. Deliberately has no service
module: there's no business logic here, just a static list of what exists
and a placeholder for what doesn't yet. When a real second program gets
built, its own routes/services/templates arrive the same way the Expense
Program's did - this file only needs a new entry in PROGRAMS."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.templates_env import templates
from app.routes import guards

router = APIRouter()

PROGRAMS = [
    {
        "key": "expenses",
        "name": "Expense Program",
        "description": "Record, approve, and report on petty cash vouchers.",
        "url": "/expense-program/",
        "available": True,
    },
    {
        "key": "weekly-productions",
        "name": "Weekly Productions",
        "description": "Track weekly production output.",
        "url": "/weekly-productions/",
        "available": True,
    },
    {
        "key": "word-editor",
        "name": "Word Editor",
        "description": "Create, approve, and share internal documents.",
        "url": "/word-editor/",
        "available": True,
    },
    {
        "key": "assets",
        "name": "Asset & Maintenance",
        "description": "Asset registry, custody chain, cost history, and maintenance log.",
        "url": "/assets/",
        "available": True,
    },
    {
        "key": "machinery-reports",
        "name": "Machinery Reports",
        "description": "Machinery usage, maintenance, and downtime reporting.",
        "available": False,
    },
    {
        "key": "key-updates",
        "name": "Key Updates",
        "description": "Important announcements and updates.",
        "available": False,
    },
]

_BY_KEY = {p["key"]: p for p in PROGRAMS}


@router.get("/programs", response_class=HTMLResponse)
def program_hub(request: Request):
    user, deny = guards.require_login_page(request)
    if deny:
        return deny
    return templates.TemplateResponse("programs.html", {
        "request": request, "user": user, "programs": PROGRAMS,
    })


@router.get("/programs/coming-soon", response_class=HTMLResponse)
def coming_soon(request: Request, key: str = ""):
    user, deny = guards.require_login_page(request)
    if deny:
        return deny
    program = _BY_KEY.get(key)
    program_name = program["name"] if program else "This program"
    return templates.TemplateResponse("coming_soon.html", {
        "request": request, "user": user, "program_name": program_name,
    })
