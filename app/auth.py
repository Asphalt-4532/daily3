"""Password hashing, session-based auth, role permissions, CSRF, and
login-attempt lockout helpers."""
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Request

from app.database import get_connection
from app.config import LOGIN_MAX_ATTEMPTS, LOGIN_LOCKOUT_MINUTES, DEFAULT_ROLE_PERMISSIONS


def _utc_now() -> datetime:
    """Naive UTC now - matches what's already stored in locked_until (plain
    ISO strings with no offset), just via the non-deprecated API."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def get_current_user(request: Request):
    """Return the logged-in user's row (as a dict) from the session, or None."""
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE id = ? AND active = 1", (user_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def login_user(request: Request, user_row) -> None:
    request.session["user_id"] = user_row["id"]
    request.session["role"] = user_row["role"]
    request.session["full_name"] = user_row["full_name"]
    request.session["csrf_token"] = secrets.token_urlsafe(32)


def logout_user(request: Request) -> None:
    request.session.clear()


# ---------------------------------------------------------------------------
# Role permissions - explicit, not ranked, since accountant and manager have
# different (not strictly nested) capabilities.
#
# The matrix itself lives in the `role_permissions` table (role, permission)
# -> allowed, editable at Settings -> Users -> Roles & Permissions
# (app/services/roles.py) rather than hardcoded here. These functions keep
# their original name/signature - a permission_fn(role) -> bool taking just
# the role string - so nothing calling them (app/routes/guards.py, every
# route module) needed to change; only *how* the answer is produced changed.
# A role/permission pair with no row yet (e.g. a fresh install before a
# manager has ever opened that page) falls back to DEFAULT_ROLE_PERMISSIONS
# in app/config.py, which is exactly what used to be hardcoded here.
# ---------------------------------------------------------------------------

def role_has_permission(role: str, permission: str) -> bool:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT allowed FROM role_permissions WHERE role = ? AND permission = ?",
            (role, permission),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return permission in DEFAULT_ROLE_PERMISSIONS.get(role, set())
    return bool(row["allowed"])


def can_create_expense(role: str) -> bool:
    return role_has_permission(role, "create_expense")


def can_review_expense(role: str) -> bool:
    """Approve / reject a pending voucher."""
    return role_has_permission(role, "review_expense")


def can_delete_expense(role: str) -> bool:
    return role_has_permission(role, "delete_expense")


def can_manage_users(role: str) -> bool:
    return role_has_permission(role, "manage_users")


def can_manage_settings(role: str) -> bool:
    return role_has_permission(role, "manage_settings")


def can_view_audit(role: str) -> bool:
    return role_has_permission(role, "view_audit")


def can_manage_backups(role: str) -> bool:
    return role_has_permission(role, "manage_backups")


def can_record_production(role: str) -> bool:
    return role_has_permission(role, "record_production")


def can_delete_production(role: str) -> bool:
    return role_has_permission(role, "delete_production")


def can_delete_supplier(role: str) -> bool:
    return role_has_permission(role, "delete_supplier")


def can_create_document(role: str) -> bool:
    return role_has_permission(role, "create_document")


def can_official_document(role: str) -> bool:
    return role_has_permission(role, "official_document")


def can_manage_wd_profile(role: str) -> bool:
    return role_has_permission(role, "manage_wd_profile")


def can_manage_wd_users(role: str) -> bool:
    return role_has_permission(role, "manage_wd_users")


def can_manage_wd_universal(role: str) -> bool:
    return role_has_permission(role, "manage_wd_universal")


def can_view_assets(role: str) -> bool:
    return role_has_permission(role, "view_assets")


def can_log_asset_service(role: str) -> bool:
    """Role-level gate only. Whether this person may log against *this*
    asset is a second, record-level check in
    app/programs/assets/services/service_log.py (an active assignee, or
    someone with manage_assets) - a role check alone can't express
    "this asset isn't assigned to you", the same way it couldn't express
    "this draft belongs to someone else"."""
    return role_has_permission(role, "log_asset_service")


def can_manage_assets(role: str) -> bool:
    return role_has_permission(role, "manage_assets")


def can_assign_asset(role: str) -> bool:
    return role_has_permission(role, "assign_asset")


def can_divest_asset(role: str) -> bool:
    return role_has_permission(role, "divest_asset")


# ---------------------------------------------------------------------------
# CSRF protection - a per-session token is embedded as a hidden field in every
# state-changing form and checked on every POST.
# ---------------------------------------------------------------------------

def verify_csrf(request: Request, submitted_token: str) -> bool:
    expected = request.session.get("csrf_token")
    return bool(expected) and bool(submitted_token) and secrets.compare_digest(expected, submitted_token)


# ---------------------------------------------------------------------------
# Login attempt lockout - protects against password guessing.
# ---------------------------------------------------------------------------

def is_locked_out(user_row) -> bool:
    locked_until = user_row["locked_until"] if user_row else None
    if not locked_until:
        return False
    try:
        return datetime.fromisoformat(locked_until) > _utc_now()
    except ValueError:
        return False


def register_failed_login(conn, user_row) -> None:
    attempts = user_row["failed_attempts"] + 1
    locked_until = None
    if attempts >= LOGIN_MAX_ATTEMPTS:
        locked_until = (_utc_now() + timedelta(minutes=LOGIN_LOCKOUT_MINUTES)).isoformat()
        attempts = 0  # reset counter once the lockout kicks in
    conn.execute(
        "UPDATE users SET failed_attempts = ?, locked_until = ? WHERE id = ?",
        (attempts, locked_until, user_row["id"]),
    )


def clear_failed_logins(conn, user_id) -> None:
    conn.execute(
        "UPDATE users SET failed_attempts = 0, locked_until = NULL WHERE id = ?",
        (user_id,),
    )
