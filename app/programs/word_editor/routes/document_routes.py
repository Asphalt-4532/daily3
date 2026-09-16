"""Word Editor HTTP routes.

All business logic lives in the service layer.
Every state-changing endpoint checks CSRF and permission.
"""
import io
from pathlib import Path

from fastapi import APIRouter, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse

from app.templates_env import templates
from app.routes import guards
from app.auth import (
    can_create_document, can_official_document,
    can_manage_wd_profile, can_manage_wd_users, can_manage_wd_universal,
)
from app.services.errors import NotFoundError, ForbiddenError, ValidationError
from app.programs.word_editor.services import profile as profile_svc
from app.programs.word_editor.services import documents as doc_svc
from app.programs.word_editor.services import comments as comment_svc
from app.programs.word_editor.services import wd_users as user_svc
from app.programs.word_editor.services import qr as qr_svc
from app.programs.word_editor.services import export as export_svc
from app.database import get_db

router = APIRouter()
PREFIX = "/word-editor"


def _flash(request: Request, msg: str, kind: str = "info"):
    request.session["flash"] = {"msg": msg, "kind": kind}


def _pop_flash(request: Request):
    return request.session.pop("flash", None)


def _redirect(path: str):
    return RedirectResponse(f"{PREFIX}{path}", status_code=302)


# ── SETUP ──────────────────────────────────────────────────────────────────

@router.get(f"{PREFIX}/setup", response_class=HTMLResponse)
def setup_get(request: Request):
    user, deny = guards.require_login_page(request)
    if deny:
        return deny
    if profile_svc.profile_exists():
        return _redirect("/")
    return templates.TemplateResponse("word_editor/setup.html", {
        "request": request, "user": user, "flash": _pop_flash(request),
    })


@router.post(f"{PREFIX}/setup")
async def setup_post(
    request: Request,
    csrf_token: str = Form(""),
    company_name: str = Form(""),
    phone: str = Form(""),
    document_location: str = Form(""),
    website: str = Form(""),
    company_location: str = Form(""),
    company_phone: str = Form(""),
    logo: UploadFile = File(None),
):
    user, deny = guards.require_login_action(request)
    if deny:
        return deny
    if err := guards.require_csrf(request, csrf_token):
        return err
    logo_path = None
    if logo and logo.filename:
        data = await logo.read()
        try:
            logo_path = profile_svc.save_logo(data, logo.filename)
        except ValidationError as e:
            _flash(request, str(e), "error")
            return _redirect("/setup")
    try:
        profile_svc.create_initial_profile(
            company_name=company_name, phone=phone,
            document_location=document_location, website=website,
            company_location=company_location, company_phone=company_phone,
            logo_path=logo_path, created_by=user["id"],
        )
    except ValidationError as e:
        _flash(request, str(e), "error")
        return _redirect("/setup")
    return _redirect("/")


# ── LOGO SERVE ─────────────────────────────────────────────────────────────

@router.get(f"{PREFIX}/logo")
def serve_logo():
    profile = profile_svc.get_active_profile()
    if not profile or not profile.get("logo_path"):
        return HTMLResponse("", status_code=204)
    path = Path(profile["logo_path"])
    if not path.exists():
        return HTMLResponse("", status_code=204)
    suffix = path.suffix.lower()
    mt = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
          "png": "image/png", "webp": "image/webp"}.get(suffix.lstrip("."), "image/png")
    return StreamingResponse(io.FileIO(path), media_type=mt,
                             headers={"Cache-Control": "max-age=3600"})


# ── DASHBOARD ──────────────────────────────────────────────────────────────

@router.get(f"{PREFIX}/", response_class=HTMLResponse)
def dashboard(request: Request):
    user, deny = guards.require_login_page(request)
    if deny:
        return deny
    if not profile_svc.profile_exists():
        return _redirect("/setup")
    profile = profile_svc.get_active_profile()
    docs = doc_svc.list_documents(user_id=user["id"], role=user["role"])
    drafts = [d for d in docs if d["status"] == "draft"]
    recent = docs[:5]
    templates_list = doc_svc.list_templates(user["id"])
    universal = doc_svc.list_universal_templates(user_id=user["id"])
    pending_official = []
    if can_official_document(user["role"]):
        with get_db() as conn:
            rows = conn.execute(
                """SELECT d.*, u.full_name as creator_name
                   FROM wd_documents d JOIN users u ON u.id=d.created_by
                   WHERE d.status='self_approved' ORDER BY d.self_approved_at DESC"""
            ).fetchall()
            pending_official = [dict(r) for r in rows]
    pending_profile = []
    if can_manage_wd_profile(user["role"]):
        pending_profile = profile_svc.list_pending_requests()
    return templates.TemplateResponse("word_editor/dashboard.html", {
        "request": request, "user": user, "profile": profile,
        "recent": recent, "drafts": drafts, "draft_count": len(drafts),
        "templates_list": templates_list, "universal": universal,
        "pending_official": pending_official,
        "pending_profile": pending_profile,
        "flash": _pop_flash(request),
        "can_official": can_official_document(user["role"]),
        "can_manage_profile": can_manage_wd_profile(user["role"]),
        "can_manage_users": can_manage_wd_users(user["role"]),
        "can_manage_universal": can_manage_wd_universal(user["role"]),
    })


# ── DOCUMENT LIST ──────────────────────────────────────────────────────────

@router.get(f"{PREFIX}/documents", response_class=HTMLResponse)
def doc_list(request: Request, status: str = "", subject: str = "",
             date_from: str = "", date_to: str = "", q: str = ""):
    user, deny = guards.require_login_page(request)
    if deny:
        return deny
    docs = doc_svc.list_documents(
        user_id=user["id"], role=user["role"],
        status_filter=status, subject_filter=subject,
        date_from=date_from, date_to=date_to, text_query=q,
    )
    return templates.TemplateResponse("word_editor/document_list.html", {
        "request": request, "user": user, "docs": docs,
        "filters": {"status": status, "subject": subject,
                    "date_from": date_from, "date_to": date_to, "q": q},
        "flash": _pop_flash(request),
        "can_create": can_create_document(user["role"]),
    })


# ── NEW DOCUMENT ───────────────────────────────────────────────────────────

@router.get(f"{PREFIX}/documents/new", response_class=HTMLResponse)
def doc_new_get(request: Request, template_id: int = 0):
    user, deny = guards.require_role_page(request, can_create_document)
    if deny:
        return deny
    subjects = doc_svc.list_subjects(user["id"])
    mention_list = doc_svc.get_mention_list()
    prefill = {}
    if template_id:
        with get_db() as conn:
            t = conn.execute("SELECT * FROM wd_user_templates WHERE user_id=? AND document_id=?",
                             (user["id"], template_id)).fetchone()
            if t:
                d = conn.execute("SELECT * FROM wd_documents WHERE id=?",
                                 (t["document_id"],)).fetchone()
                if d:
                    prefill = dict(d)
    return templates.TemplateResponse("word_editor/document_editor.html", {
        "request": request, "user": user,
        "doc": None, "prefill": prefill,
        "subjects": subjects, "mention_list": mention_list,
        "profile": profile_svc.get_active_profile(),
        "flash": _pop_flash(request),
    })


@router.post(f"{PREFIX}/documents/new")
async def doc_new_post(
    request: Request,
    csrf_token: str = Form(""),
    action: str = Form("draft"),
    title: str = Form(""),
    subject: str = Form(""),
    receiver_name: str = Form(""),
    receiver_phone: str = Form(""),
    content_html: str = Form(""),
    save_subject: str = Form(""),
):
    user, deny = guards.require_role_action(request, can_create_document)
    if deny:
        return deny
    if err := guards.require_csrf(request, csrf_token):
        return err
    try:
        doc_id = doc_svc.create_document(
            created_by=user["id"], title=title,
            subject=subject, receiver_name=receiver_name,
            receiver_phone=receiver_phone, content_html=content_html,
        )
        if action == "approve":
            doc_svc.self_approve(doc_id, user_id=user["id"], role=user["role"])
            _flash(request, "Document self-approved and published.", "ok")
            return _redirect(f"/documents/{doc_id}")
        _flash(request, "Draft saved.", "info")
        return _redirect(f"/documents/{doc_id}/edit")
    except (ValidationError, ForbiddenError) as e:
        _flash(request, str(e), "error")
        return _redirect("/documents/new")


# ── EDIT DOCUMENT ──────────────────────────────────────────────────────────

@router.get(f"{PREFIX}/documents/{{doc_id}}/edit", response_class=HTMLResponse)
def doc_edit_get(request: Request, doc_id: int):
    user, deny = guards.require_login_page(request)
    if deny:
        return deny
    try:
        doc = doc_svc.get_document(doc_id, user_id=user["id"], role=user["role"])
    except NotFoundError:
        return _redirect("/documents")
    if doc["status"] != "draft":
        return _redirect(f"/documents/{doc_id}")
    subjects = doc_svc.list_subjects(user["id"])
    mention_list = doc_svc.get_mention_list()
    return templates.TemplateResponse("word_editor/document_editor.html", {
        "request": request, "user": user, "doc": doc, "prefill": {},
        "subjects": subjects, "mention_list": mention_list,
        "profile": profile_svc.get_active_profile(),
        "flash": _pop_flash(request),
    })


@router.post(f"{PREFIX}/documents/{{doc_id}}/edit")
async def doc_edit_post(
    request: Request, doc_id: int,
    csrf_token: str = Form(""),
    action: str = Form("draft"),
    title: str = Form(""),
    subject: str = Form(""),
    receiver_name: str = Form(""),
    receiver_phone: str = Form(""),
    content_html: str = Form(""),
    save_subject: str = Form(""),
):
    user, deny = guards.require_login_action(request)
    if deny:
        return deny
    if err := guards.require_csrf(request, csrf_token):
        return err
    try:
        doc_svc.update_document(
            doc_id, user_id=user["id"], role=user["role"],
            title=title, subject=subject,
            receiver_name=receiver_name, receiver_phone=receiver_phone,
            content_html=content_html, save_subject=bool(save_subject),
        )
        if action == "approve":
            doc_svc.self_approve(doc_id, user_id=user["id"], role=user["role"])
            _flash(request, "Document self-approved and published.", "ok")
            return _redirect(f"/documents/{doc_id}")
        elif action == "discard":
            doc_svc.discard_draft(doc_id, user_id=user["id"], role=user["role"])
            _flash(request, "Draft discarded.", "info")
            return _redirect("/documents")
        _flash(request, "Draft saved.", "info")
        return _redirect(f"/documents/{doc_id}/edit")
    except (ValidationError, ForbiddenError) as e:
        _flash(request, str(e), "error")
        return _redirect(f"/documents/{doc_id}/edit")


# ── DOCUMENT DETAIL + COMMENTS ─────────────────────────────────────────────

@router.get(f"{PREFIX}/documents/{{doc_id}}", response_class=HTMLResponse)
def doc_detail(request: Request, doc_id: int):
    user, deny = guards.require_login_page(request)
    if deny:
        return deny
    try:
        doc = doc_svc.get_document(doc_id, user_id=user["id"], role=user["role"])
    except NotFoundError:
        return _redirect("/documents")
    threads = comment_svc.list_threads(doc_id, user_id=user["id"], role=user["role"])
    mention_list = doc_svc.get_mention_list()
    return templates.TemplateResponse("word_editor/document_detail.html", {
        "request": request, "user": user, "doc": doc,
        "threads": threads, "mention_list": mention_list,
        "profile": profile_svc.get_active_profile(),
        "flash": _pop_flash(request),
        "can_official": can_official_document(user["role"]),
    })


@router.post(f"{PREFIX}/documents/{{doc_id}}/official")
async def make_official(
    request: Request, doc_id: int,
    csrf_token: str = Form(""),
    manager_note: str = Form(""),
):
    user, deny = guards.require_role_action(request, can_official_document)
    if deny:
        return deny
    if err := guards.require_csrf(request, csrf_token):
        return err
    try:
        doc_svc.make_official(doc_id, manager_id=user["id"], note=manager_note)
        _flash(request, "Document marked as Official.", "ok")
    except (ForbiddenError, ValidationError) as e:
        _flash(request, str(e), "error")
    return _redirect(f"/documents/{doc_id}")


@router.post(f"{PREFIX}/documents/{{doc_id}}/delete")
async def delete_doc(
    request: Request, doc_id: int,
    csrf_token: str = Form(""),
    reason: str = Form(""),
):
    user, deny = guards.require_login_action(request)
    if deny:
        return deny
    if err := guards.require_csrf(request, csrf_token):
        return err
    try:
        doc_svc.delete_document(doc_id, user_id=user["id"], role=user["role"], reason=reason)
        _flash(request, "Document deleted.", "info")
    except (ForbiddenError, ValidationError) as e:
        _flash(request, str(e), "error")
    return _redirect("/documents")


# ── PRINT ──────────────────────────────────────────────────────────────────

@router.get(f"{PREFIX}/documents/{{doc_id}}/print", response_class=HTMLResponse)
def doc_print(request: Request, doc_id: int):
    user, deny = guards.require_login_page(request)
    if deny:
        return deny
    try:
        doc = doc_svc.get_document(doc_id, user_id=user["id"], role=user["role"])
    except NotFoundError:
        return _redirect("/documents")
    if doc["status"] == "draft":
        _flash(request, "Draft documents cannot be printed. Self-approve first.", "error")
        return _redirect(f"/documents/{doc_id}/edit")
    profile = profile_svc.get_active_profile()
    from datetime import datetime
    now_str = datetime.utcnow().strftime("%d %B %Y %H:%M UTC")
    return templates.TemplateResponse("word_editor/document_print.html", {
        "request": request, "user": user, "doc": doc, "profile": profile,
        "now": now_str,
    })


@router.get(f"{PREFIX}/documents/{{doc_id}}/qr")
def doc_qr(request: Request, doc_id: int, p: str = "1", n: str = "1"):
    """PNG QR for one page of one document. `p` = this page, `n` = total
    pages - the paginator in document_print.html knows both and asks for
    one image per page."""
    user, deny = guards.require_login_action(request)
    if deny:
        return deny
    try:
        page, pages = qr_svc.validate_page(p, n)
    except ValidationError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    try:
        doc = doc_svc.get_document(doc_id, user_id=user["id"], role=user["role"])
    except NotFoundError:
        return JSONResponse({"error": "Document not found."}, status_code=404)
    except ForbiddenError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    if doc["status"] != "official":
        return JSONResponse(
            {"error": "QR codes are issued for official documents only."},
            status_code=400,
        )
    payload = qr_svc.build_payload(
        doc, profile_svc.get_active_profile(),
        page=page, pages=pages, base_url=str(request.base_url),
    )
    png = qr_svc.render_png(payload)
    return StreamingResponse(
        io.BytesIO(png), media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


# ── VERSION HISTORY / SNAPSHOTS ────────────────────────────────────────────

@router.get(f"{PREFIX}/documents/{{doc_id}}/history", response_class=HTMLResponse)
def doc_history(request: Request, doc_id: int):
    user, deny = guards.require_login_page(request)
    if deny:
        return deny
    try:
        doc = doc_svc.get_document(doc_id, user_id=user["id"], role=user["role"])
        versions = doc_svc.list_versions(doc_id, user_id=user["id"], role=user["role"])
    except (NotFoundError, ForbiddenError) as e:
        _flash(request, str(e), "error")
        return _redirect("/documents")
    return templates.TemplateResponse("word_editor/document_history.html", {
        "request": request, "user": user, "doc": doc, "versions": versions,
        "flash": _pop_flash(request),
    })


@router.get(f"{PREFIX}/documents/{{doc_id}}/history/{{version_no}}", response_class=HTMLResponse)
def doc_version_view(request: Request, doc_id: int, version_no: int):
    user, deny = guards.require_login_page(request)
    if deny:
        return deny
    try:
        doc = doc_svc.get_document(doc_id, user_id=user["id"], role=user["role"])
        version = doc_svc.get_version(doc_id, version_no,
                                      user_id=user["id"], role=user["role"])
    except (NotFoundError, ForbiddenError) as e:
        _flash(request, str(e), "error")
        return _redirect("/documents")
    return templates.TemplateResponse("word_editor/document_version.html", {
        "request": request, "user": user, "doc": doc, "version": version,
        "flash": _pop_flash(request),
    })


@router.post(f"{PREFIX}/documents/{{doc_id}}/history/{{version_no}}/restore")
async def doc_version_restore(request: Request, doc_id: int, version_no: int,
                              csrf_token: str = Form("")):
    user, deny = guards.require_login_action(request)
    if deny:
        return deny
    if err := guards.require_csrf(request, csrf_token):
        return err
    try:
        doc_svc.restore_version(doc_id, version_no,
                                user_id=user["id"], role=user["role"])
        _flash(request, f"Restored version {version_no}.", "success")
    except (NotFoundError, ForbiddenError, ValidationError) as e:
        _flash(request, str(e), "error")
    return _redirect(f"/documents/{doc_id}/history")


# ── EXPORT ─────────────────────────────────────────────────────────────────

@router.get(f"{PREFIX}/documents/{{doc_id}}/export")
def doc_export(request: Request, doc_id: int, fmt: str = "doc"):
    """Download the document body. `doc` = Word-openable HTML, `html` = plain.

    Only available while the document is still editable - an official one is
    issued as PDF only."""
    user, deny = guards.require_login_page(request)
    if deny:
        return deny
    if fmt not in export_svc.EDITABLE_FORMATS:
        return JSONResponse({"error": "Unsupported format."}, status_code=400)
    try:
        doc = doc_svc.get_document(doc_id, user_id=user["id"], role=user["role"])
    except NotFoundError:
        _flash(request, "Document not found.", "error")
        return _redirect("/documents")
    except ForbiddenError as e:
        _flash(request, str(e), "error")
        return _redirect("/documents")
    # An official document is PDF-only - see export_svc.export_allowed() for
    # why. Sent to the print view rather than refused outright, because the
    # user wanted a copy of this document and there is a legitimate way to
    # get one.
    if not export_svc.export_allowed(doc, fmt):
        _flash(request, export_svc.OFFICIAL_EXPORT_MESSAGE, "error")
        return _redirect(f"/documents/{doc_id}/print")
    profile = profile_svc.get_active_profile() or {}
    body = export_svc.build_export_html(doc, profile)
    name = (doc["doc_no"] or f"draft-{doc_id}")
    media = "application/msword" if fmt == "doc" else "text/html"
    return StreamingResponse(
        io.BytesIO(body.encode("utf-8")), media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{name}.{fmt}"'},
    )


@router.get(f"{PREFIX}/documents-export.xlsx")
def doc_list_export(request: Request, status: str = "", subject: str = "",
                    date_from: str = "", date_to: str = "", q: str = ""):
    """Excel export of the document register, honouring the current filters."""
    user, deny = guards.require_login_page(request)
    if deny:
        return deny
    docs = doc_svc.list_documents(
        user_id=user["id"], role=user["role"],
        status_filter=status, subject_filter=subject,
        date_from=date_from, date_to=date_to, text_query=q,
    )
    wb_bytes = export_svc.build_register_workbook(
        docs, profile_svc.get_active_profile() or {}, generated_by=user["full_name"])
    return StreamingResponse(
        io.BytesIO(wb_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="wd-documents.xlsx"'},
    )


# ── PUBLIC VERIFICATION (no login - this is the QR target) ─────────────────

@router.get(f"{PREFIX}/verify", response_class=HTMLResponse)
@router.get(f"{PREFIX}/verify/{{doc_no}}", response_class=HTMLResponse)
def doc_verify(request: Request, doc_no: str = ""):
    """Scanning a document QR lands here. Deliberately unauthenticated and
    metadata-only: it proves a number is genuine without exposing content."""
    result, error = None, None
    if doc_no:
        try:
            result = doc_svc.verify_by_doc_no(doc_no)
        except (NotFoundError, ValidationError) as e:
            error = str(e)
    return templates.TemplateResponse("word_editor/verify.html", {
        "request": request, "doc_no": doc_no, "result": result, "error": error,
        "profile": profile_svc.get_active_profile(),
    })


# ── COMMENTS ───────────────────────────────────────────────────────────────

@router.post(f"{PREFIX}/documents/{{doc_id}}/comments")
async def post_comment(
    request: Request, doc_id: int,
    csrf_token: str = Form(""),
    content: str = Form(""),
):
    user, deny = guards.require_login_action(request)
    if deny:
        return deny
    if err := guards.require_csrf(request, csrf_token):
        return err
    try:
        comment_svc.post_comment(doc_id, content, author_id=user["id"])
    except ValidationError as e:
        _flash(request, str(e), "error")
    return _redirect(f"/documents/{doc_id}")


@router.post(f"{PREFIX}/comments/{{comment_id}}/reply")
async def post_reply(
    request: Request, comment_id: int,
    csrf_token: str = Form(""),
    content: str = Form(""),
    doc_id: int = Form(0),
):
    user, deny = guards.require_login_action(request)
    if deny:
        return deny
    if err := guards.require_csrf(request, csrf_token):
        return err
    try:
        comment_svc.post_reply(comment_id, content, author_id=user["id"])
    except (ValidationError, NotFoundError) as e:
        _flash(request, str(e), "error")
    return _redirect(f"/documents/{doc_id}")


# ── IMAGE UPLOAD ───────────────────────────────────────────────────────────

@router.post(f"{PREFIX}/upload-image")
async def upload_image(request: Request, image: UploadFile = File(...)):
    user, deny = guards.require_login_action(request)
    if deny:
        return JSONResponse({"error": "Not logged in."}, status_code=401)
    import uuid
    from app.config import UPLOADS_DIR
    from app.sandbox import validate_file_signature, set_nonexecutable
    WD_IMG_DIR = UPLOADS_DIR / "wd_images"
    WD_IMG_DIR.mkdir(parents=True, exist_ok=True)
    data = await image.read()
    suffix = Path(image.filename).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        return JSONResponse({"error": "Only JPG, PNG, WEBP allowed."}, status_code=400)
    fname = f"{uuid.uuid4()}{suffix}"
    dest = WD_IMG_DIR / fname
    dest.write_bytes(data)
    set_nonexecutable(dest)
    return JSONResponse({"url": f"{PREFIX}/images/{fname}"})


@router.get(f"{PREFIX}/images/{{filename}}")
def serve_image(filename: str):
    from app.config import UPLOADS_DIR
    from app.sandbox import safe_join
    WD_IMG_DIR = UPLOADS_DIR / "wd_images"
    try:
        path = safe_join(WD_IMG_DIR, filename)
    except ValueError:
        return HTMLResponse("", status_code=404)
    if not path.exists():
        return HTMLResponse("", status_code=404)
    suffix = path.suffix.lower().lstrip(".")
    mt = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
          "png": "image/png", "webp": "image/webp"}.get(suffix, "image/png")
    return StreamingResponse(io.FileIO(path), media_type=mt)


# ── MENTION LIST API ───────────────────────────────────────────────────────

@router.get(f"{PREFIX}/api/mentions")
def api_mentions(request: Request):
    user, deny = guards.require_login_action(request)
    if deny:
        return JSONResponse({"error": "Not logged in."}, status_code=401)
    return JSONResponse(doc_svc.get_mention_list())


# ── TEMPLATE SLOTS ─────────────────────────────────────────────────────────

@router.post(f"{PREFIX}/templates/save")
async def save_template(
    request: Request,
    csrf_token: str = Form(""),
    doc_id: int = Form(0),
    slot: int = Form(1),
    label: str = Form(""),
):
    user, deny = guards.require_login_action(request)
    if deny:
        return deny
    if err := guards.require_csrf(request, csrf_token):
        return err
    try:
        doc_svc.save_to_template_slot(doc_id, slot, label,
                                       user_id=user["id"], role=user["role"])
        _flash(request, f"Saved to template slot {slot}.", "ok")
    except (ValidationError, ForbiddenError) as e:
        _flash(request, str(e), "error")
    return _redirect(f"/documents/{doc_id}")


@router.post(f"{PREFIX}/templates/delete")
async def delete_template(
    request: Request,
    csrf_token: str = Form(""),
    slot: int = Form(0),
):
    user, deny = guards.require_login_action(request)
    if deny:
        return deny
    if err := guards.require_csrf(request, csrf_token):
        return err
    doc_svc.delete_template_slot(slot, user_id=user["id"])
    _flash(request, "Template slot cleared.", "info")
    return _redirect("/")


# ── SUBJECTS ───────────────────────────────────────────────────────────────

@router.post(f"{PREFIX}/subjects/delete")
async def delete_subject(
    request: Request,
    csrf_token: str = Form(""),
    subject_id: int = Form(0),
):
    user, deny = guards.require_login_action(request)
    if deny:
        return deny
    if err := guards.require_csrf(request, csrf_token):
        return err
    try:
        doc_svc.delete_subject(subject_id, user_id=user["id"])
    except (NotFoundError, ValidationError) as e:
        _flash(request, str(e), "error")
    return _redirect("/")


# ── PROFILE ────────────────────────────────────────────────────────────────

@router.get(f"{PREFIX}/profile", response_class=HTMLResponse)
def profile_get(request: Request):
    user, deny = guards.require_login_page(request)
    if deny:
        return deny
    profile = profile_svc.get_active_profile()
    pending = profile_svc.list_pending_requests() if can_manage_wd_profile(user["role"]) else []
    return templates.TemplateResponse("word_editor/profile.html", {
        "request": request, "user": user, "profile": profile,
        "pending": pending,
        "can_manage_profile": can_manage_wd_profile(user["role"]),
        "flash": _pop_flash(request),
    })


@router.post(f"{PREFIX}/profile/request-change")
async def profile_change_request(
    request: Request,
    csrf_token: str = Form(""),
    company_name: str = Form(""),
    phone: str = Form(""),
    document_location: str = Form(""),
    website: str = Form(""),
    company_location: str = Form(""),
    company_phone: str = Form(""),
    logo: UploadFile = File(None),
):
    user, deny = guards.require_login_action(request)
    if deny:
        return deny
    if err := guards.require_csrf(request, csrf_token):
        return err
    logo_path = None
    if logo and logo.filename:
        data = await logo.read()
        try:
            logo_path = profile_svc.save_logo(data, logo.filename)
        except ValidationError as e:
            _flash(request, str(e), "error")
            return _redirect("/profile")
    try:
        profile_svc.submit_change_request(
            requested_by=user["id"],
            company_name=company_name, phone=phone,
            document_location=document_location, website=website,
            company_location=company_location, company_phone=company_phone,
            logo_path=logo_path,
        )
        _flash(request, "Change request submitted. A manager will review it.", "ok")
    except ValidationError as e:
        _flash(request, str(e), "error")
    return _redirect("/profile")


@router.post(f"{PREFIX}/profile/approve/{{req_id}}")
async def approve_profile(
    request: Request, req_id: int,
    csrf_token: str = Form(""),
):
    user, deny = guards.require_role_action(request, can_manage_wd_profile)
    if deny:
        return deny
    if err := guards.require_csrf(request, csrf_token):
        return err
    try:
        profile_svc.approve_change_request(req_id, user["id"])
        _flash(request, "Profile change approved and applied.", "ok")
    except (NotFoundError, ValidationError) as e:
        _flash(request, str(e), "error")
    return _redirect("/profile")


@router.post(f"{PREFIX}/profile/reject/{{req_id}}")
async def reject_profile(
    request: Request, req_id: int,
    csrf_token: str = Form(""),
    reason: str = Form(""),
):
    user, deny = guards.require_role_action(request, can_manage_wd_profile)
    if deny:
        return deny
    if err := guards.require_csrf(request, csrf_token):
        return err
    try:
        profile_svc.reject_change_request(req_id, user["id"], reason)
        _flash(request, "Change request rejected.", "info")
    except (NotFoundError, ValidationError) as e:
        _flash(request, str(e), "error")
    return _redirect("/profile")


# ── USER MANAGEMENT ────────────────────────────────────────────────────────

@router.get(f"{PREFIX}/users", response_class=HTMLResponse)
def wd_users_get(request: Request):
    user, deny = guards.require_role_page(request, can_manage_wd_users)
    if deny:
        return deny
    users = user_svc.list_wd_users()
    with get_db() as conn:
        cost_centers = conn.execute(
            "SELECT id, code, name FROM cost_centers WHERE active=1 ORDER BY name"
        ).fetchall()
    return templates.TemplateResponse("word_editor/users.html", {
        "request": request, "user": user, "wd_users": users,
        "cost_centers": [dict(c) for c in cost_centers],
        "flash": _pop_flash(request),
    })


@router.post(f"{PREFIX}/users/{{target_id}}/update")
async def wd_user_update(
    request: Request, target_id: int,
    csrf_token: str = Form(""),
    display_name: str = Form(""),
    phone: str = Form(""),
    access_note: str = Form(""),
    location_ids: list[int] = Form([]),
):
    user, deny = guards.require_role_action(request, can_manage_wd_users)
    if deny:
        return deny
    if err := guards.require_csrf(request, csrf_token):
        return err
    try:
        user_svc.update_wd_user(
            target_id, display_name=display_name,
            phone=phone, access_note=access_note,
            updated_by=user["id"],
        )
        user_svc.assign_locations(target_id, location_ids, assigned_by=user["id"])
        _flash(request, "User profile updated.", "ok")
    except (NotFoundError, ValidationError) as e:
        _flash(request, str(e), "error")
    return _redirect("/users")


# ── UNIVERSAL TEMPLATES (manager) ──────────────────────────────────────────

@router.get(f"{PREFIX}/universal-templates", response_class=HTMLResponse)
def universal_templates_get(request: Request):
    user, deny = guards.require_role_page(request, can_manage_wd_universal)
    if deny:
        return deny
    templates_list = doc_svc.list_universal_templates(manager=True)
    all_users = user_svc.list_wd_users()
    universal_subjects = doc_svc.list_universal_subjects(manager=True)
    return templates.TemplateResponse("word_editor/manager_templates.html", {
        "request": request, "user": user,
        "templates_list": templates_list,
        "all_users": all_users,
        "universal_subjects": universal_subjects,
        "flash": _pop_flash(request),
    })


@router.post(f"{PREFIX}/universal-templates/create")
async def universal_template_create(
    request: Request,
    csrf_token: str = Form(""),
    title: str = Form(""),
    subject: str = Form(""),
    content_html: str = Form(""),
    user_ids: list[int] = Form([]),
):
    user, deny = guards.require_role_action(request, can_manage_wd_universal)
    if deny:
        return deny
    if err := guards.require_csrf(request, csrf_token):
        return err
    try:
        tid = doc_svc.create_universal_template(
            title=title, content_html=content_html,
            subject=subject, created_by=user["id"],
        )
        if user_ids:
            doc_svc.assign_universal_template(tid, user_ids, user["id"])
        _flash(request, "Universal template created.", "ok")
    except ValidationError as e:
        _flash(request, str(e), "error")
    return _redirect("/universal-templates")


@router.post(f"{PREFIX}/universal-templates/{{tid}}/assign")
async def universal_template_assign(
    request: Request, tid: int,
    csrf_token: str = Form(""),
    user_ids: list[int] = Form([]),
):
    user, deny = guards.require_role_action(request, can_manage_wd_universal)
    if deny:
        return deny
    if err := guards.require_csrf(request, csrf_token):
        return err
    doc_svc.assign_universal_template(tid, user_ids, user["id"])
    _flash(request, "Template assignments updated.", "ok")
    return _redirect("/universal-templates")


@router.post(f"{PREFIX}/universal-templates/{{tid}}/delete")
async def universal_template_delete(
    request: Request, tid: int,
    csrf_token: str = Form(""),
):
    user, deny = guards.require_role_action(request, can_manage_wd_universal)
    if deny:
        return deny
    if err := guards.require_csrf(request, csrf_token):
        return err
    doc_svc.delete_universal_template(tid)
    _flash(request, "Universal template deleted.", "info")
    return _redirect("/universal-templates")


@router.post(f"{PREFIX}/universal-templates/subjects/create")
async def universal_subject_create(
    request: Request,
    csrf_token: str = Form(""),
    subject: str = Form(""),
):
    user, deny = guards.require_role_action(request, can_manage_wd_universal)
    if deny:
        return deny
    if err := guards.require_csrf(request, csrf_token):
        return err
    try:
        doc_svc.create_universal_subject(subject, created_by=user["id"])
        _flash(request, "Universal subject added.", "ok")
    except ValidationError as e:
        _flash(request, str(e), "error")
    return _redirect("/universal-templates")
