from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse

from app.auth import get_current_user, login_user, logout_user
from app.services import auth as auth_service
from app.templates_env import templates

router = APIRouter()


@router.get("/login")
def login_form(request: Request):
    if get_current_user(request):
        return RedirectResponse("/programs", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.get("/")
def home(request: Request):
    """Root IS the login page. Logged-out visitor sees the form; an already
    logged-in one goes straight to the program hub, same as GET /login."""
    return login_form(request)


@router.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    result = auth_service.attempt_login(username, password)
    if not result.ok:
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": result.error}, status_code=401,
        )
    login_user(request, result.user_row)
    return RedirectResponse("/programs", status_code=302)


@router.get("/logout")
def logout(request: Request):
    user = get_current_user(request)
    if user:
        auth_service.record_logout(user["id"])
    logout_user(request)
    return RedirectResponse("/login", status_code=302)
