"""Expense period close / month lock service.

A locked month blocks NEW vouchers dated in it (same rule as
production_locked_months). Existing vouchers in a locked month can still
be approved / rejected / deleted through the normal flow - correcting
records is not the same as backdating new entries.

Only managers can lock/unlock (enforced in the route via can_manage_settings).

No FastAPI imports - see app/services/errors.py.
"""
from app.database import get_db, log_action
from app.services.errors import ConflictError, NotFoundError

__all__ = ["is_locked", "lock_month", "unlock_month", "list_locked_months"]


def is_locked(month: str) -> bool:
    """True when the given YYYY-MM month is closed for new expense entries."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM expense_locked_months WHERE month=?", (month,)
        ).fetchone()
        return row is not None


def _is_locked_conn(conn, month: str) -> bool:
    """Same check, but uses an existing connection (for use inside a transaction)."""
    return conn.execute(
        "SELECT 1 FROM expense_locked_months WHERE month=?", (month,)
    ).fetchone() is not None


def lock_month(month: str, acting_user_id: int) -> None:
    """Lock month (YYYY-MM). Raises ConflictError if already locked."""
    with get_db() as conn:
        if _is_locked_conn(conn, month):
            raise ConflictError(f"{month} is already locked.")
        conn.execute(
            "INSERT INTO expense_locked_months (month, locked_by) VALUES (?, ?)",
            (month, acting_user_id),
        )
        log_action(conn, acting_user_id, "lock_expense_month", month)


def unlock_month(month: str, acting_user_id: int) -> None:
    """Unlock a previously closed month. Raises NotFoundError if not locked."""
    with get_db() as conn:
        if not _is_locked_conn(conn, month):
            raise NotFoundError(f"{month} is not locked.")
        conn.execute("DELETE FROM expense_locked_months WHERE month=?", (month,))
        log_action(conn, acting_user_id, "unlock_expense_month", month)


def list_locked_months() -> list:
    """Return all locked months, newest first."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT elm.*, u.full_name locked_by_name "
            "FROM expense_locked_months elm "
            "JOIN users u ON u.id = elm.locked_by "
            "ORDER BY elm.month DESC"
        ).fetchall()
        return [dict(r) for r in rows]


# Exposed for expenses.py to call inside its own connection without re-opening DB.
check_locked = _is_locked_conn
