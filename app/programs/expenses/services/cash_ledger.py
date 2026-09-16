"""Petty-cash float (imprest ledger) business logic.

The float tracks how much physical cash is in the petty-cash box.
Every approved voucher reduces it; every replenishment (top-up) increases
it; rejections restore the amount that was provisionally reserved on
approval.

Design decisions:
- Single-row `cash_float` table (id=1 singleton) stores the running
  balance. It is NEVER updated directly - only via `cash_movements`, so
  there is always a complete trail.
- balance can go negative (warn-only policy): approval still succeeds
  even when the box is short; a warning is surfaced to the caller so the
  route can flash it to the user.
- `get_balance()` returns the current float row (or a synthetic zero row
  if never initialised).
- `top_up()` adds cash; `manual_adjust()` allows a corrective entry.
- `record_approval_movement()` / `record_rejection_movement()` are called
  by expenses.py inside the same DB transaction as the status change so
  the float and voucher status never diverge.

No FastAPI imports - see app/services/errors.py.
"""
from datetime import date

from app.database import get_db
from app.services.errors import ValidationError

__all__ = [
    "get_balance",
    "get_movements",
    "record_undo_movement",
    "top_up",
    "manual_adjust",
    "record_approval_movement",
    "record_rejection_movement",
    "is_float_low",
]

_LOW_FLOAT_THRESHOLD = 0.0  # warn when balance at or below zero after movement


def _ensure_float_row(conn):
    """Seed the singleton row on first use."""
    row = conn.execute("SELECT * FROM cash_float WHERE id=1").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO cash_float (id, balance, updated_by, updated_at) VALUES (1, 0, 1, datetime('now'))"
        )
        row = conn.execute("SELECT * FROM cash_float WHERE id=1").fetchone()
    return dict(row)


def get_balance():
    """Return the current float as a dict: {balance, updated_by, updated_at}."""
    with get_db() as conn:
        return _ensure_float_row(conn)


def get_movements(limit: int = 50):
    """Most recent cash movements, newest first."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT cm.*, u.full_name created_by_name, e.voucher_no "
            "FROM cash_movements cm "
            "JOIN users u ON u.id = cm.created_by "
            "LEFT JOIN expenses e ON e.id = cm.reference_id "
            "ORDER BY cm.created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def top_up(amount: float, note: str, acting_user_id: int) -> dict:
    """Add cash to the float (replenishment). Returns updated float dict."""
    if not amount or amount <= 0:
        raise ValidationError("Top-up amount must be positive.")
    with get_db() as conn:
        _ensure_float_row(conn)
        conn.execute(
            "UPDATE cash_float SET balance = balance + ?, updated_by=?, updated_at=datetime('now') WHERE id=1",
            (amount, acting_user_id),
        )
        conn.execute(
            "INSERT INTO cash_movements (movement_type, amount, note, created_by) VALUES ('topup', ?, ?, ?)",
            (amount, note.strip() or "Top-up", acting_user_id),
        )
        return dict(conn.execute("SELECT * FROM cash_float WHERE id=1").fetchone())


def manual_adjust(amount: float, note: str, acting_user_id: int) -> dict:
    """Manual corrective adjustment (positive or negative).
    Requires a note - this is an exceptional operation."""
    if not note or not note.strip():
        raise ValidationError("A note is required for a manual adjustment.")
    if amount == 0:
        raise ValidationError("Adjustment amount cannot be zero.")
    with get_db() as conn:
        _ensure_float_row(conn)
        conn.execute(
            "UPDATE cash_float SET balance = balance + ?, updated_by=?, updated_at=datetime('now') WHERE id=1",
            (amount, acting_user_id),
        )
        conn.execute(
            "INSERT INTO cash_movements (movement_type, amount, note, created_by) "
            "VALUES ('manual_adjustment', ?, ?, ?)",
            (amount, note.strip(), acting_user_id),
        )
        return dict(conn.execute("SELECT * FROM cash_float WHERE id=1").fetchone())


def record_approval_movement(conn, expense_id: int, voucher_total: float, acting_user_id: int):
    """Deduct approved voucher total from the float.
    Called inside expenses.py's approve_expense() transaction - same conn,
    never commits independently.
    Returns True if the resulting balance is negative (warn-only)."""
    _ensure_float_row(conn)
    conn.execute(
        "UPDATE cash_float SET balance = balance - ?, updated_by=?, updated_at=datetime('now') WHERE id=1",
        (voucher_total, acting_user_id),
    )
    conn.execute(
        "INSERT INTO cash_movements (movement_type, reference_id, amount, note, created_by) "
        "VALUES ('voucher_approved', ?, ?, 'Voucher approved', ?)",
        (expense_id, -voucher_total, acting_user_id),
    )
    new_balance = conn.execute("SELECT balance FROM cash_float WHERE id=1").fetchone()["balance"]
    return new_balance < _LOW_FLOAT_THRESHOLD


def record_rejection_movement(conn, expense_id: int, voucher_total: float, acting_user_id: int):
    """Restore rejected voucher total back to the float.
    Called inside expenses.py's reject_expense() transaction."""
    _ensure_float_row(conn)
    conn.execute(
        "UPDATE cash_float SET balance = balance + ?, updated_by=?, updated_at=datetime('now') WHERE id=1",
        (voucher_total, acting_user_id),
    )
    conn.execute(
        "INSERT INTO cash_movements (movement_type, reference_id, amount, note, created_by) "
        "VALUES ('voucher_rejected', ?, ?, 'Voucher rejected - amount restored', ?)",
        (expense_id, voucher_total, acting_user_id),
    )


def is_float_low():
    """True when current balance is at or below zero (including uninitialised float)."""
    with get_db() as conn:
        row = conn.execute("SELECT balance FROM cash_float WHERE id=1").fetchone()
        if row is None:
            return True   # float never set up = effectively empty
        return row["balance"] <= _LOW_FLOAT_THRESHOLD


def record_undo_movement(conn, expense_id: int, amount: float,
                         acting_user_id: int, note: str):
    """Reverse a voucher-driven float movement after an undo.

    `amount` is signed the same way cash_movements.amount always is:
    positive puts cash back in the box, negative takes it out. Undoing an
    approval passes a positive amount (the approval had deducted it);
    undoing a rejection passes a negative one.

    Recorded as 'manual_adjustment' rather than a new movement_type on
    purpose. cash_movements.movement_type carries a SQLite CHECK
    constraint, so inventing a fourth value would turn a five-line feature
    into a table-rebuild migration (see CHANGE_IMPACT_GUIDE.md's "Schema
    changes"). The note says exactly what happened and reference_id still
    points at the voucher, so the ledger reads correctly either way - and
    crucially the original movement row is left untouched rather than
    deleted, so the float's history shows the approval *and* its reversal
    instead of quietly pretending neither happened.

    Called inside the undo transaction in expenses.py - same conn, never
    commits independently.
    """
    _ensure_float_row(conn)
    conn.execute(
        "UPDATE cash_float SET balance = balance + ?, updated_by=?, updated_at=datetime('now') WHERE id=1",
        (amount, acting_user_id),
    )
    conn.execute(
        "INSERT INTO cash_movements (movement_type, reference_id, amount, note, created_by) "
        "VALUES ('manual_adjustment', ?, ?, ?, ?)",
        (expense_id, amount, note, acting_user_id),
    )
