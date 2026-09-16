"""Business logic for the role permission matrix - which of the app's fixed
set of permissions each role currently has.

Concern: role *capabilities* (what manager/accountant/user can each do).
NOT in scope here: individual user accounts (see app/services/users.py) or
which roles exist at all (that's app/config.py's ROLES list plus a schema
change to the `users.role` CHECK constraint - see CHANGE_IMPACT_GUIDE.md's
"Roles / permissions" row - this module never adds/removes/renames a role).

No FastAPI imports on purpose - see app/services/errors.py.
"""
from app.database import get_db, log_action
from app.config import ROLES, PERMISSIONS, PERMISSION_KEYS, DEFAULT_ROLE_PERMISSIONS
from app.services.errors import ValidationError, ConflictError

__all__ = ["list_permission_matrix", "set_permission_for_roles"]


def _stored_permissions(conn):
    """{(role, permission): bool} for every row actually saved in
    role_permissions - does NOT include defaulted (never-touched) pairs."""
    rows = conn.execute("SELECT role, permission, allowed FROM role_permissions").fetchall()
    return {(r["role"], r["permission"]): bool(r["allowed"]) for r in rows}


def _effective(stored, role: str, permission: str) -> bool:
    if (role, permission) in stored:
        return stored[(role, permission)]
    return permission in DEFAULT_ROLE_PERMISSIONS.get(role, set())


def list_permission_matrix():
    """One row per permission (in PERMISSIONS order), each with every
    role's current effective allowed/not state - exactly the shape
    roles_permissions.html iterates over to render the grid of checkboxes.

    [{"key": "create_expense", "label": ..., "description": ...,
      "roles": {"manager": True, "accountant": False, "user": True}}, ...]
    """
    with get_db() as conn:
        stored = _stored_permissions(conn)
    matrix = []
    for key, label, description in PERMISSIONS:
        matrix.append({
            "key": key,
            "label": label,
            "description": description,
            "roles": {role: _effective(stored, role, key) for role in ROLES},
        })
    return matrix


def set_permission_for_roles(*, permission: str, allowed_roles, acting_user_id: int) -> None:
    """Persists which roles have `permission`, replacing the previous set for
    every role in one transaction. This is the shape of one Roles &
    Permissions form submission - the page has one row (of role checkboxes)
    per permission, so one submit always changes exactly one permission
    across all roles at once, never a single (role, permission) cell alone.

    Refuses to leave **zero** roles with "manage_users" allowed - the same
    self-lockout risk app/services/network.py's trusted-hostname allowlist
    and app/services/users.py's "can't disable your own account" rule
    already guard against elsewhere in this app. The check and every role's
    write happen in the same transaction (get_db() commits once at the end,
    or rolls back entirely on any exception - see app/database.py), so a
    partial write can never leave, say, the current manager's own role
    already stripped of the permission before a later role in the same
    submission fails the check that was supposed to prevent exactly that.
    Unknown role names (a tampered form) are silently ignored rather than
    erroring - only the checkboxes this page actually renders can ever
    reach here for a legitimate submission.
    """
    if permission not in PERMISSION_KEYS:
        raise ValidationError("Unknown permission.")
    allowed_roles = {r for r in allowed_roles if r in ROLES}

    if permission == "manage_users" and not allowed_roles:
        raise ConflictError(
            "At least one role must be able to manage users & permissions - "
            "this change would lock every account out of this page, with no "
            "way to undo it from the app."
        )

    with get_db() as conn:
        for role in ROLES:
            conn.execute(
                "INSERT INTO role_permissions (role, permission, allowed, updated_by, updated_at) "
                "VALUES (?, ?, ?, ?, datetime('now')) "
                "ON CONFLICT(role, permission) DO UPDATE SET "
                "allowed = excluded.allowed, updated_by = excluded.updated_by, updated_at = excluded.updated_at",
                (role, permission, int(role in allowed_roles), acting_user_id),
            )
        log_action(
            conn, acting_user_id, "set_role_permission",
            f"{permission} -> " + (", ".join(sorted(allowed_roles)) or "(no roles)"),
        )
