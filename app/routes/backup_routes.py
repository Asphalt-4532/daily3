"""HTTP layer for backup/restore. All the actual logic (validation,
compression, zip-slip/zip-bomb defenses, atomic swap) lives in app/backup.py
- this file only turns HTTP requests into calls against it and results back
into responses. Permission/CSRF checks come from app/routes/guards.py."""
import os
import tempfile
import urllib.parse
from pathlib import Path

from fastapi import APIRouter, Request, Form, UploadFile, File
from fastapi.responses import RedirectResponse, HTMLResponse, FileResponse

from app.auth import can_manage_settings
from app.templates_env import templates
from app.config import BACKUPS_DIR, MAX_RESTORE_UPLOAD_MB
from app import backup as backup_module
from app.routes import guards

router = APIRouter()

MAX_REASON_LEN = 500


def _clean_reason(reason: str) -> str:
    return reason.strip()[:MAX_REASON_LEN]


def _safe_qs(text: str) -> str:
    return urllib.parse.quote(text[:300])


@router.get("/backups", response_class=HTMLResponse)
def backups_page(request: Request, error: str = ""):
    user, deny = guards.require_role_page(request, can_manage_settings)
    if deny:
        return deny
    backups = backup_module.list_backups()
    # flag any listing whose zip file is missing on disk, so the template can
    # offer "remove from list" instead of Download/Restore/View
    for b in backups:
        b["_missing"] = not (BACKUPS_DIR / b.get("filename", "")).exists()
        # surface whether this backup has a stored SHA-256 to verify against
        b["_has_sha256"] = bool(b.get("sha256"))
    return templates.TemplateResponse("backups.html", {
        "request": request, "user": user, "backups": backups,
        "max_upload_mb": MAX_RESTORE_UPLOAD_MB, "error": error,
    })


@router.post("/backups/{filename}/verify")
def verify_backup_route(request: Request, filename: str, csrf_token: str = Form(...)):
    user, deny = guards.require_role_action(request, can_manage_settings)
    if deny:
        return deny
    if err := guards.require_csrf(request, csrf_token):
        return err
    try:
        result = backup_module.verify_backup(filename)
    except (ValueError, FileNotFoundError) as e:
        result = {"ok": False, "reason": str(e)}
    from fastapi.responses import JSONResponse
    return JSONResponse(result)


@router.get("/backups/restore-log", response_class=HTMLResponse)
def restore_log_page(request: Request):
    user, deny = guards.require_role_page(request, can_manage_settings)
    if deny:
        return deny
    entries = backup_module.read_restore_log()
    return templates.TemplateResponse("restore_log.html", {
        "request": request, "user": user, "entries": entries,
    })


@router.post("/backups/create")
def create_backup_now(request: Request, csrf_token: str = Form(...)):
    user, deny = guards.require_role_action(request, can_manage_settings)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    try:
        backup_module.create_backup(trigger="manual", triggered_by=user["username"])
    except Exception as e:
        return RedirectResponse(f"/backups?error={_safe_qs(str(e))}", status_code=302)
    return RedirectResponse("/backups", status_code=302)


@router.get("/backups/{filename}/view", response_class=HTMLResponse)
def view_backup(request: Request, filename: str):
    user, deny = guards.require_role_page(request, can_manage_settings)
    if deny:
        return deny
    try:
        data = backup_module.inspect_backup(filename)
    except (ValueError, FileNotFoundError) as e:
        return HTMLResponse(f"Could not open this backup: {e}", status_code=400)
    return templates.TemplateResponse("backup_view.html", {
        "request": request, "user": user, "meta": data["meta"], "records": data["records"],
        "filename": filename,
    })


@router.get("/backups/{filename}/download")
def download_backup(request: Request, filename: str):
    user, deny = guards.require_role_page(request, can_manage_settings)
    if deny:
        return deny
    try:
        path = backup_module.validate_backup_filename(filename)
    except (ValueError, FileNotFoundError) as e:
        return HTMLResponse(f"Could not download this backup: {e}", status_code=400)
    return FileResponse(path, media_type="application/zip", filename=filename)


@router.post("/backups/{filename}/delete")
def delete_backup_route(request: Request, filename: str, csrf_token: str = Form(...)):
    user, deny = guards.require_role_action(request, can_manage_settings)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    try:
        backup_module.delete_backup(filename)
    except FileNotFoundError:
        # zip already gone (deleted outside the app) - clean up the orphaned
        # metadata entry instead so it stops cluttering the list
        try:
            backup_module.delete_orphaned_meta(filename)
        except ValueError:
            pass
    except ValueError as e:
        return RedirectResponse(f"/backups?error={_safe_qs(str(e))}", status_code=302)
    return RedirectResponse("/backups", status_code=302)


@router.post("/backups/{filename}/restore")
def restore_backup_route(
    request: Request, filename: str,
    csrf_token: str = Form(...), reason: str = Form(...), confirm_text: str = Form(...),
):
    user, deny = guards.require_role_action(request, can_manage_settings)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    if confirm_text.strip().upper() != "RESTORE":
        return RedirectResponse(
            "/backups?error=" + _safe_qs('Type RESTORE exactly to confirm - nothing was changed.'),
            status_code=302,
        )
    reason = _clean_reason(reason)
    if not reason:
        return RedirectResponse(
            "/backups?error=" + _safe_qs("A reason is required to restore a backup."), status_code=302
        )
    try:
        safety = backup_module.restore_backup(filename, user, reason)
    except (ValueError, FileNotFoundError) as e:
        return RedirectResponse(f"/backups?error={_safe_qs(str(e))}", status_code=302)
    return templates.TemplateResponse("restore_done.html", {
        "request": request, "user": user, "safety_filename": safety["filename"],
        "restored_from": filename,
    })


@router.post("/backups/restore-upload")
def restore_from_upload(
    request: Request, csrf_token: str = Form(...), reason: str = Form(...),
    confirm_text: str = Form(...), backup_file: UploadFile = File(...),
):
    user, deny = guards.require_role_action(request, can_manage_settings)
    if deny:
        return deny
    if bad := guards.require_csrf(request, csrf_token):
        return bad
    if confirm_text.strip().upper() != "RESTORE":
        return RedirectResponse(
            "/backups?error=" + _safe_qs("Type RESTORE exactly to confirm - nothing was changed."),
            status_code=302,
        )
    reason = _clean_reason(reason)
    if not reason:
        return RedirectResponse(
            "/backups?error=" + _safe_qs("A reason is required to restore a backup."), status_code=302
        )
    if not backup_file.filename or not backup_file.filename.lower().endswith(".zip"):
        return RedirectResponse(
            "/backups?error=" + _safe_qs("Please upload a .zip backup file."), status_code=302
        )

    max_bytes = MAX_RESTORE_UPLOAD_MB * 1024 * 1024
    tmp_fd, tmp_path_str = tempfile.mkstemp(suffix=".zip")
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(tmp_fd, "wb") as out:
            total = 0
            while True:
                chunk = backup_file.file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"File is too large (max {MAX_RESTORE_UPLOAD_MB} MB).")
                out.write(chunk)

        safety = backup_module.restore_uploaded_backup(tmp_path, user, reason)
    except ValueError as e:
        return RedirectResponse(f"/backups?error={_safe_qs(str(e))}", status_code=302)
    finally:
        tmp_path.unlink(missing_ok=True)

    return templates.TemplateResponse("restore_done.html", {
        "request": request, "user": user, "safety_filename": safety["filename"],
        "restored_from": f"uploaded file ({backup_file.filename})",
    })
