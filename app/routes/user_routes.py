"""HTTP layer for user management. Business logic lives in
app/services/users.py; permission/CSRF checks come from app/routes/guards.py.
This file's only job is translating between the two."""
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse

from app.auth import can_manage_users
from app.templates_env import templates
from app.config import ROLES
from app.services import users as users_service
from app.services import roles as roles_service
from app.services.errors import ValidationError, DuplicateError, ConflictError
from app.routes import guards

router = APIRouter()


@router.get("/users")
def user_list(request: Request):
    user, deny = guards.require_role_page(request, can_manage_users)
    if deny:
        return deny
    return templates.TemplateResponse("users.html", {
        "request": request, "user": user, "users": users_service.list_users(),
        "roles": ROLES, "error": None,
    })


@router.get("/users/roles")
def roles_permissions_page(request: Request):
    """Sub-page of Users - view/edit which permissions each role has. See
    app/services/roles.py for the business rules and
    CHANGE_IMPACT_GUIDE.md's "Roles / permissions" row for what a *bigger*
    role change (adding/renaming a role itself) additionally touches."""
    user, deny = guards.require_role_page(request, can_manage_users)
    if deny:
        return deny
    return templates.TemplateResponse("roles_permissions.html", {
        "request": request, "user": user, "roles": ROLES,
        "permission_matrix": roles_service.list_permission_matrix(), "error": None,
    })


@router.post("/users/roles/update")
async def update_role_permission(request: Request):
    """Parsed manually (like expense_routes.py's multi-line form) rather
    than via typed Form(...) params, since unchecked checkboxes simply
    don't appear in the submitted form at all - there's no single fixed
    set of field names to declare."""
    user, deny = guards.require_role_action(request, can_manage_users)
    if deny:
        return deny
    form = await request.form()
    if bad := guards.require_csrf(request, form.get("csrf_token", "")):
        return bad

    permission = form.get("permission", "")
    allowed_roles = set(form.getlist("allowed_roles"))
    error = None
    try:
        roles_service.set_permission_for_roles(
            permission=permission, allowed_roles=allowed_roles, acting_user_id=user["id"],
        )
    except (ValidationError, ConflictError) as e:
        error = str(e)

    return templates.TemplateResponse("roles_permissions.html", {
        "request": request, "user": user, "roles": ROLES,
        "permission_matrix": roles_service.list_permission_matrix(), "error": error,
    }, status_code=400 if error else 200)


@router.post("/users/new")
def create_user(request: Request, csrf_token: str = Form(...), username: str = Form(...),
                 full_name: str = Form(...), password: str = Form(...), role: str = Form(...)):
    admin, deny = guards.require_role_action(request, can_manage_users)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    try:
        users_service.create_user(username=username, full_name=full_name, password=password,
                                   role=role, acting_user_id=admin["id"])
    except (ValidationError, DuplicateError) as e:
        return templates.TemplateResponse("users.html", {
            "request": request, "user": admin, "users": users_service.list_users(),
            "roles": ROLES, "error": str(e),
        }, status_code=400)
    return RedirectResponse("/users", status_code=302)


@router.post("/users/{user_id}/toggle")
def toggle_user(request: Request, user_id: int, csrf_token: str = Form(...)):
    admin, deny = guards.require_role_action(request, can_manage_users)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    users_service.toggle_user(user_id, admin["id"])
    return RedirectResponse("/users", status_code=302)


@router.post("/users/{user_id}/reset-password")
def reset_password(request: Request, user_id: int, csrf_token: str = Form(...), new_password: str = Form(...)):
    admin, deny = guards.require_role_action(request, can_manage_users)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    try:
        users_service.reset_password(user_id, new_password, admin["id"])
    except ValidationError as e:
        return templates.TemplateResponse("users.html", {
            "request": request, "user": admin, "users": users_service.list_users(),
            "roles": ROLES, "error": str(e),
        }, status_code=400)
    return RedirectResponse("/users", status_code=302)
