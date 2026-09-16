"""Word Editor company profile service.

First-time setup: any user can create the profile (no profile row yet).
Future changes: submitted as change requests, approved by manage_wd_profile role.
Logo stored in UPLOADS_DIR/wd_logos/, auto-resized to 160x80 by Pillow.
"""
import uuid
from pathlib import Path

from app.config import UPLOADS_DIR
from app.database import get_db
from app.services.errors import NotFoundError, ForbiddenError, ValidationError

__all__ = [
    "get_active_profile", "profile_exists",
    "create_initial_profile", "save_logo",
    "submit_change_request", "list_pending_requests",
    "approve_change_request", "reject_change_request",
]

WD_LOGOS_DIR = UPLOADS_DIR / "wd_logos"
WD_LOGOS_DIR.mkdir(parents=True, exist_ok=True)


def get_active_profile():
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM wd_company_profile WHERE is_active = 1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def profile_exists() -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM wd_company_profile WHERE is_active = 1 LIMIT 1"
        ).fetchone()
        return row is not None


def _validate_profile_fields(company_name, phone, document_location,
                              website, company_location, company_phone):
    errors = []
    if not company_name.strip():
        errors.append("Company name is required.")
    if not phone.strip():
        errors.append("Phone number is required.")
    if not document_location.strip():
        errors.append("Document location is required.")
    if not website.strip():
        errors.append("Company website is required.")
    if not company_location.strip():
        errors.append("Company location (footer address) is required.")
    if not company_phone.strip():
        errors.append("Company phone (footer) is required.")
    if errors:
        raise ValidationError(" ".join(errors))


def create_initial_profile(*, company_name, phone, document_location,
                            website, company_location, company_phone,
                            logo_path=None, created_by):
    _validate_profile_fields(company_name, phone, document_location,
                              website, company_location, company_phone)
    with get_db() as conn:
        conn.execute("UPDATE wd_company_profile SET is_active = 0")
        conn.execute(
            """INSERT INTO wd_company_profile
               (company_name, logo_path, phone, document_location,
                website, company_location, company_phone, is_active, updated_by, approved_by)
               VALUES (?,?,?,?,?,?,?,1,?,?)""",
            (company_name.strip(), logo_path, phone.strip(),
             document_location.strip(), website.strip(),
             company_location.strip(), company_phone.strip(),
             created_by, created_by),
        )


def save_logo(file_bytes: bytes, original_filename: str) -> str:
    """Save uploaded logo, resize to 160x80, return stored path string."""
    from PIL import Image
    import io

    suffix = Path(original_filename).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise ValidationError("Logo must be JPG, PNG, or WEBP.")

    fname = f"{uuid.uuid4()}{suffix}"
    dest = WD_LOGOS_DIR / fname

    img = Image.open(io.BytesIO(file_bytes))
    img.thumbnail((160, 80), Image.LANCZOS)
    img.save(dest)
    dest.chmod(0o644)

    return str(dest)


def submit_change_request(*, requested_by, company_name, phone,
                           document_location, website,
                           company_location, company_phone, logo_path=None):
    _validate_profile_fields(company_name, phone, document_location,
                              website, company_location, company_phone)
    with get_db() as conn:
        conn.execute(
            """INSERT INTO wd_profile_change_requests
               (requested_by, company_name, logo_path, phone,
                document_location, website, company_location, company_phone)
               VALUES (?,?,?,?,?,?,?,?)""",
            (requested_by, company_name.strip(), logo_path, phone.strip(),
             document_location.strip(), website.strip(),
             company_location.strip(), company_phone.strip()),
        )


def list_pending_requests():
    with get_db() as conn:
        rows = conn.execute(
            """SELECT r.*, u.full_name as requester_name
               FROM wd_profile_change_requests r
               JOIN users u ON u.id = r.requested_by
               WHERE r.status = 'pending'
               ORDER BY r.created_at DESC"""
        ).fetchall()
        return [dict(r) for r in rows]


def approve_change_request(request_id: int, reviewed_by: int):
    with get_db() as conn:
        req = conn.execute(
            "SELECT * FROM wd_profile_change_requests WHERE id = ? AND status = 'pending'",
            (request_id,)
        ).fetchone()
        if not req:
            raise NotFoundError("Change request not found or already reviewed.")
        req = dict(req)
        conn.execute(
            "UPDATE wd_profile_change_requests SET status='approved', reviewed_by=?, reviewed_at=datetime('now') WHERE id=?",
            (reviewed_by, request_id)
        )
        conn.execute("UPDATE wd_company_profile SET is_active = 0")
        conn.execute(
            """INSERT INTO wd_company_profile
               (company_name, logo_path, phone, document_location,
                website, company_location, company_phone, is_active, updated_by, approved_by)
               VALUES (?,?,?,?,?,?,?,1,?,?)""",
            (req["company_name"], req["logo_path"], req["phone"],
             req["document_location"], req["website"],
             req["company_location"], req["company_phone"],
             req["requested_by"], reviewed_by),
        )


def reject_change_request(request_id: int, reviewed_by: int, reason: str):
    if not reason.strip():
        raise ValidationError("A rejection reason is required.")
    with get_db() as conn:
        req = conn.execute(
            "SELECT id FROM wd_profile_change_requests WHERE id = ? AND status = 'pending'",
            (request_id,)
        ).fetchone()
        if not req:
            raise NotFoundError("Change request not found or already reviewed.")
        conn.execute(
            """UPDATE wd_profile_change_requests
               SET status='rejected', reviewed_by=?, reviewed_at=datetime('now'), reject_reason=?
               WHERE id=?""",
            (reviewed_by, reason.strip(), request_id)
        )
