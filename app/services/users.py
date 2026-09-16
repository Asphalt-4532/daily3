"""Business logic for managing user accounts.

Concern: account lifecycle (create, enable/disable, reset password).
Depends on: app.database (persistence), app.auth (password hashing - the one
primitive shared with the login flow), app.config (the list of valid roles).
Used by: app.routes.user_routes.

No FastAPI imports on purpose - see app/services/errors.py.
"""
from app.database import get_db, log_action
from app.auth import hash_password
from app.config import ROLES
from app.services.errors import ValidationError, DuplicateError

__all__ = ["list_users", "create_user", "toggle_user", "reset_password"]


def list_users():
    with get_db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM users ORDER BY created_at").fetchall()]


def create_user(*, username: str, full_name: str, password: str, role: str, acting_user_id: int) -> int:
    if role not in ROLES:
        raise ValidationError("Invalid role.")
    username = (username or "").strip()
    if not username:
        raise ValidationError("Username is required.")
    if not full_name or not full_name.strip():
        raise ValidationError("Full name is required.")
    if not password:
        raise ValidationError("Password is required.")

    with get_db() as conn:
        exists = conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone()
        if exists:
            raise DuplicateError(f"Username '{username}' is already taken.")
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, full_name, role) VALUES (?, ?, ?, ?)",
            (username, hash_password(password), full_name.strip(), role),
        )
        log_action(conn, acting_user_id, "create_user", f"{username} ({role})")
        return cur.lastrowid


def toggle_user(user_id: int, acting_user_id: int) -> None:
    """No-op if a manager tries to disable their own account - the rule
    belongs here, not in the route, so it's enforced no matter how this
    function is called."""
    if user_id == acting_user_id:
        return
    with get_db() as conn:
        conn.execute("UPDATE users SET active = 1 - active WHERE id = ?", (user_id,))
        log_action(conn, acting_user_id, "toggle_user", str(user_id))


def reset_password(user_id: int, new_password: str, acting_user_id: int) -> None:
    if not new_password:
        raise ValidationError("New password is required.")
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ?, failed_attempts = 0, locked_until = NULL WHERE id = ?",
            (hash_password(new_password), user_id),
        )
        log_action(conn, acting_user_id, "reset_password", str(user_id))
