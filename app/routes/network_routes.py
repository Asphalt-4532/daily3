"""HTTP layer for the trusted-hostname allowlist (Settings -> Network).
Business logic lives in app/services/network.py. Shared/app-wide, not owned
by any one program - registered unprefixed in app.main, same as
user_routes/backup_routes/audit_routes."""
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse

from app.auth import can_manage_settings
from app.templates_env import templates
from app.services import network as network_service
from app.services.errors import ValidationError, NotFoundError
from app.routes import guards
from app.network_middleware import _host_without_port

router = APIRouter()


@router.get("/settings/network", response_class=HTMLResponse)
def network_settings(request: Request):
    user, deny = guards.require_role_page(request, can_manage_settings)
    if deny:
        return deny
    current_host = _host_without_port(request.headers.get("host", ""))
    return templates.TemplateResponse("network_settings.html", {
        "request": request, "user": user,
        "configs": network_service.list_network_configs(),
        "current_host": current_host,
    })


@router.post("/settings/network/new")
def add_network_config(request: Request, csrf_token: str = Form(...), name: str = Form(...),
                        hostname: str = Form(...), notes: str = Form("")):
    user, deny = guards.require_role_action(request, can_manage_settings)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    try:
        network_service.add_network_config(name, hostname, notes, user["id"])
    except ValidationError as e:
        return HTMLResponse(str(e), status_code=400)
    return RedirectResponse("/settings/network", status_code=302)


@router.post("/settings/network/{config_id}/edit")
def edit_network_config(request: Request, config_id: int, csrf_token: str = Form(...),
                         name: str = Form(...), hostname: str = Form(...), notes: str = Form("")):
    user, deny = guards.require_role_action(request, can_manage_settings)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    try:
        network_service.update_network_config(config_id, name, hostname, notes, user["id"])
    except ValidationError as e:
        return HTMLResponse(str(e), status_code=400)
    except NotFoundError as e:
        return HTMLResponse(str(e), status_code=404)
    return RedirectResponse("/settings/network", status_code=302)


@router.post("/settings/network/{config_id}/toggle")
def toggle_network_config(request: Request, config_id: int, csrf_token: str = Form(...)):
    user, deny = guards.require_role_action(request, can_manage_settings)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    try:
        network_service.toggle_network_config(config_id, user["id"])
    except NotFoundError as e:
        return HTMLResponse(str(e), status_code=404)
    return RedirectResponse("/settings/network", status_code=302)


@router.post("/settings/network/{config_id}/delete")
def delete_network_config(request: Request, config_id: int, csrf_token: str = Form(...)):
    user, deny = guards.require_role_action(request, can_manage_settings)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    try:
        network_service.delete_network_config(config_id, user["id"])
    except NotFoundError as e:
        return HTMLResponse(str(e), status_code=404)
    return RedirectResponse("/settings/network", status_code=302)
