"""Login orchestration: the decision tree of "is this account locked, is the
password right, should a failure be recorded" is a business/security rule,
not an HTTP concern - so it lives here rather than in auth_routes.py.

Concern: the login attempt decision tree (lockout check, password check,
failure/success bookkeeping).
Depends on: app.database (persistence), app.auth (the primitives this
composes - password verification, lockout state, session helpers).
Used by: app.routes.auth_routes.

The route's job becomes just: call attempt_login(), then turn the result into
a redirect or an error page. The low-level primitives this composes
(verify_password, is_locked_out, register_failed_login, clear_failed_logins)
stay in app/auth.py rather than moving here, since they're used by more than
just this one flow - e.g. the users service uses hash_password directly when
creating/resetting accounts.
"""
from dataclasses import dataclass

from app.database import get_db, log_action
from app.auth import (
    verify_password, is_locked_out, register_failed_login, clear_failed_logins,
)

__all__ = ["LoginResult", "attempt_login", "record_logout"]


@dataclass
class LoginResult:
    ok: bool
    user_row: dict | None = None
    error: str | None = None


def attempt_login(username: str, password: str) -> LoginResult:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ? AND active = 1", (username.strip(),)
        ).fetchone()

        if row and is_locked_out(row):
            log_action(conn, row["id"], "login_blocked_locked", "")
            return LoginResult(ok=False, error=(
                "This account is temporarily locked after repeated failed attempts. "
                "Try again in a few minutes, or ask a manager to help."
            ))

        if not row or not verify_password(password, row["password_hash"]):
            if row:
                register_failed_login(conn, row)
                log_action(conn, row["id"], "login_failed", "")
            return LoginResult(ok=False, error="Incorrect username or password.")

        clear_failed_logins(conn, row["id"])
        log_action(conn, row["id"], "login", "")
        return LoginResult(ok=True, user_row=dict(row))


def record_logout(user_id: int) -> None:
    with get_db() as conn:
        log_action(conn, user_id, "logout", "")
