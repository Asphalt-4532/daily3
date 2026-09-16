"""HTTP layer for non-cost maintenance service log entries.
Permission gate is two-tier: role-level (log_asset_service) checked by
guards, then record-level (active assignee or manage_assets) checked by the
service. A ForbiddenError from the service means role was fine but the asset
isn't assigned to this person.
"""
from fastapi import APIRouter, Request, Form, UploadFile, File
from fastapi.responses import RedirectResponse, HTMLResponse, FileResponse

from app import auth
from app.programs.assets.services import service_log as svc
from app.programs.assets.services.assets import get_asset
from app.services.errors import (
    ValidationError, NotFoundError, ForbiddenError,
)
from app.routes import guards
from app.config import UPLOADS_DIR

router = APIRouter()

ASSET_PREFIX = "/assets"


def _back(asset_id):
    return RedirectResponse(f"{ASSET_PREFIX}/{asset_id}#service", status_code=302)


# --------------------------------------------------------------------------
# add service entry
# --------------------------------------------------------------------------
@router.post("/{asset_id}/service/new")
async def log_service(
    request: Request, asset_id: int,
    csrf_token: str = Form(...),
    service_date: str = Form(...),
    service_type: str = Form(...),
    description: str = Form(...),
    parts_used: str = Form(""),
    meter_reading: str = Form(""),
    meter_unit: str = Form(""),
    photo: UploadFile = File(None),
):
    user, deny = guards.require_role_action(request, auth.can_log_asset_service)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad

    photo_path = None
    if photo and photo.filename:
        try:
            photo_path = svc.save_service_photo(photo.filename, photo.file)
        except ValidationError as e:
            return HTMLResponse(str(e), status_code=400)

    try:
        _service_id, warnings = svc.log_service(
            asset_id,
            service_date=service_date,
            service_type=service_type,
            description=description,
            parts_used=parts_used,
            meter_reading=meter_reading if meter_reading else None,
            meter_unit=meter_unit if meter_unit else None,
            photo_path=photo_path,
            recorded_by_user=user,
        )
    except (ValidationError, ForbiddenError, NotFoundError) as e:
        return HTMLResponse(str(e), status_code=400)

    # Warnings (backwards meter) are preserved in the redirect via a flash
    # query param rather than session (session would need setup); for a
    # homelab app where the form and list are on the same detail page this
    # is good enough without adding a flash-message system.
    return _back(asset_id)


# --------------------------------------------------------------------------
# delete service entry
# --------------------------------------------------------------------------
@router.post("/service/{service_id}/delete")
def delete_service(request: Request, service_id: int,
                   csrf_token: str = Form(...),
                   reason: str = Form(...)):
    user, deny = guards.require_role_action(request, auth.can_log_asset_service)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    from app.database import get_connection
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT asset_id FROM asset_service_log WHERE id=?", (service_id,)
        ).fetchone()
        asset_id = row["asset_id"] if row else None
    finally:
        conn.close()
    try:
        svc.delete_service(service_id, acting_user=user, reason=reason)
    except (ValidationError, ForbiddenError, NotFoundError) as e:
        return HTMLResponse(str(e), status_code=400)
    return _back(asset_id) if asset_id else RedirectResponse(f"{ASSET_PREFIX}/", status_code=302)


# --------------------------------------------------------------------------
# serve a service photo (reuses the same upload dir as receipts)
# --------------------------------------------------------------------------
@router.get("/service/photo/{filename}")
def serve_photo(request: Request, filename: str):
    user, deny = guards.require_role_page(request, auth.can_view_assets)
    if deny:
        return deny
    from app.sandbox import safe_join
    try:
        path = safe_join(UPLOADS_DIR, filename)
    except Exception:
        return HTMLResponse("Not found.", status_code=404)
    if not path.exists():
        return HTMLResponse("Not found.", status_code=404)
    return FileResponse(str(path))
