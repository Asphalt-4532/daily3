"""Employee salary-advance ledger service.

An advance is issued when a voucher line has category = ADVANCE_CATEGORY_NAME.
The advance_ledger table tracks every debit (advance) and credit (settlement)
per employee.

Lifecycle:
  1. Voucher with salary-advance line submitted → pending.
  2. Approved → advance_ledger INSERT (movement_type='advance').
     If submitted by a plain 'user' role → pending_approval=1 (settlement path
     only; the advance itself is created on approval of the expense voucher).
  3. Settlement recorded by manager/accountant → instant (pending_approval=0).
     Settlement recorded by 'user' role → pending_approval=1, manager approves.

Outstanding balance per employee:
  SUM(amount) over ALL non-pending advance_ledger rows
  (advances are positive, settlements are negative).

No FastAPI imports - see app/services/errors.py.
"""
import json
from datetime import datetime

from app.database import get_db, log_action
from app.services.errors import ValidationError, NotFoundError, ForbiddenError

__all__ = [
    "get_or_create_employee",
    "list_employees",
    "get_employee",
    "get_outstanding_balances",
    "get_employee_ledger",
    "record_advance",
    "record_settlement",
    "approve_settlement",
    "get_pending_settlements",
    "advance_rows_for_expense",
    "remove_advance_rows_for_expense",
]

SETTLEMENT_METHODS = ("Cash returned to box", "Salary deduction", "Bank transfer")


# ---------------------------------------------------------------------------
# Employee registry
# ---------------------------------------------------------------------------

def get_or_create_employee(conn, name: str, phone: str, created_by: int) -> int:
    """Return existing employee id for (name, phone) or create a new row.
    Called inside an existing transaction - uses the passed conn."""
    name = name.strip()
    phone = phone.strip()
    if not name:
        raise ValidationError("Employee name is required.")
    if not phone:
        raise ValidationError("Employee phone is required.")
    row = conn.execute(
        "SELECT id FROM employees WHERE name=? AND phone=?", (name, phone)
    ).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO employees (name, phone, created_by) VALUES (?, ?, ?)",
        (name, phone, created_by),
    )
    return cur.lastrowid


def list_employees(status: str = "active") -> list:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT e.*, u.full_name created_by_name FROM employees e "
            "JOIN users u ON u.id = e.created_by "
            "WHERE e.status=? ORDER BY e.name",
            (status,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_employee(employee_id: int) -> dict:
    with get_db() as conn:
        row = conn.execute(
            "SELECT e.*, u.full_name created_by_name FROM employees e "
            "JOIN users u ON u.id = e.created_by WHERE e.id=?",
            (employee_id,),
        ).fetchone()
        if not row:
            raise NotFoundError("Employee not found.")
        return dict(row)


# ---------------------------------------------------------------------------
# Outstanding balance summary
# ---------------------------------------------------------------------------

def get_outstanding_balances() -> list:
    """Per-employee summary: total_advanced, total_settled, outstanding.
    Only approved (non-pending) rows count toward balance."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT e.id, e.name, e.phone, e.status, "
            "COALESCE(SUM(CASE WHEN al.movement_type='advance' AND al.pending_approval=0 "
            "              THEN al.amount ELSE 0 END), 0) total_advanced, "
            "COALESCE(SUM(CASE WHEN al.movement_type='settlement' AND al.pending_approval=0 "
            "              THEN ABS(al.amount) ELSE 0 END), 0) total_settled "
            "FROM employees e "
            "LEFT JOIN advance_ledger al ON al.employee_id = e.id "
            "WHERE e.status='active' "
            "GROUP BY e.id ORDER BY e.name",
            (),
        ).fetchall()
        result = []
        for r in rows:
            adv = r["total_advanced"]
            sett = r["total_settled"]
            result.append({
                "id": r["id"],
                "name": r["name"],
                "phone": r["phone"],
                "status": r["status"],
                "total_advanced": adv,
                "total_settled": sett,
                "outstanding": round(adv - sett, 2),
            })
        return result


def get_employee_balance(conn, employee_id: int) -> float:
    """Running balance for one employee. Used inside an existing transaction."""
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) bal FROM advance_ledger "
        "WHERE employee_id=? AND pending_approval=0",
        (employee_id,),
    ).fetchone()
    return row["bal"] if row else 0.0


# ---------------------------------------------------------------------------
# Per-employee ledger
# ---------------------------------------------------------------------------

def get_employee_ledger(employee_id: int) -> dict:
    """Full movement history for one employee, newest first, with running balance."""
    with get_db() as conn:
        emp = conn.execute(
            "SELECT e.*, u.full_name created_by_name FROM employees e "
            "JOIN users u ON u.id = e.created_by WHERE e.id=?",
            (employee_id,),
        ).fetchone()
        if not emp:
            raise NotFoundError("Employee not found.")

        rows = conn.execute(
            "SELECT al.*, u.full_name created_by_name, "
            "u2.full_name approved_by_name, "
            "ex.voucher_no "
            "FROM advance_ledger al "
            "JOIN users u ON u.id = al.created_by "
            "LEFT JOIN users u2 ON u2.id = al.approved_by "
            "LEFT JOIN expenses ex ON ex.id = al.reference_expense_id "
            "WHERE al.employee_id=? "
            "ORDER BY al.created_at ASC",
            (employee_id,),
        ).fetchall()

        # Build running balance.
        movements = []
        running = 0.0
        for r in rows:
            d = dict(r)
            if not d["pending_approval"]:
                running = round(running + d["amount"], 2)
            d["running_balance"] = running if not d["pending_approval"] else None
            movements.append(d)

        total_adv = sum(m["amount"] for m in movements
                        if m["movement_type"] == "advance" and not m["pending_approval"])
        total_sett = sum(abs(m["amount"]) for m in movements
                         if m["movement_type"] == "settlement" and not m["pending_approval"])

        return {
            "employee": dict(emp),
            "movements": list(reversed(movements)),  # newest first for display
            "total_advanced": round(total_adv, 2),
            "total_settled": round(total_sett, 2),
            "outstanding": round(total_adv - total_sett, 2),
        }


# ---------------------------------------------------------------------------
# Record advance (called by expenses.py after voucher approval)
# ---------------------------------------------------------------------------

def record_advance(conn, employee_id: int, expense_line_id: int,
                   amount: float, reference_expense_id: int, created_by: int) -> None:
    """Insert an approved advance ledger entry. Called inside approve_expense()
    after the status flip commits - uses the post-commit conn2 pattern."""
    conn.execute(
        "INSERT INTO advance_ledger "
        "(employee_id, movement_type, amount, expense_line_id, "
        " reference_expense_id, pending_approval, created_by) "
        "VALUES (?, 'advance', ?, ?, ?, 0, ?)",
        (employee_id, amount, expense_line_id, reference_expense_id, created_by),
    )
    log_action(conn, created_by, "record_advance",
               json.dumps({"employee_id": employee_id, "amount": amount,
                           "expense_line_id": expense_line_id}))


# ---------------------------------------------------------------------------
# Undo support (called by expenses.py when an approval is reversed)
# ---------------------------------------------------------------------------

def advance_rows_for_expense(conn, expense_id: int) -> list:
    """Advance ledger rows that a given voucher's approval created."""
    return conn.execute(
        "SELECT * FROM advance_ledger WHERE reference_expense_id = ? "
        "AND movement_type = 'advance'",
        (expense_id,),
    ).fetchall()


def remove_advance_rows_for_expense(conn, expense_id: int, acting_user_id: int) -> bool:
    """Delete the advance rows a voucher approval created. Returns False and
    changes nothing if that is no longer safe.

    These rows are *derived* - nobody typed them, approve_expense() wrote
    them from the voucher's salary-advance lines. Undoing the approval that
    created them therefore removes them rather than booking a compensating
    settlement, which would otherwise leave a phantom pair of entries on an
    employee's ledger for a voucher that is back in `pending` and may never
    be approved at all.

    The guard: if any settlement has been recorded for that employee since
    the advance row was written, the advance is no longer standalone -
    somebody has already acted on the balance it created - and removing it
    would leave the employee's running balance wrong. In that case this
    refuses, and the caller must reject the voucher through the normal flow
    instead of undoing it. Within the ten-second undo window this is close
    to impossible, which is exactly why it must be checked rather than
    assumed."""
    rows = advance_rows_for_expense(conn, expense_id)
    if not rows:
        return True                     # nothing derived, nothing to unwind
    for row in rows:
        later_settlement = conn.execute(
            "SELECT 1 FROM advance_ledger WHERE employee_id = ? "
            "AND movement_type = 'settlement' AND id > ? LIMIT 1",
            (row["employee_id"], row["id"]),
        ).fetchone()
        if later_settlement:
            return False
    conn.execute(
        "DELETE FROM advance_ledger WHERE reference_expense_id = ? "
        "AND movement_type = 'advance'",
        (expense_id,),
    )
    log_action(conn, acting_user_id, "undo_record_advance",
               json.dumps({"reference_expense_id": expense_id,
                           "rows_removed": len(rows)}))
    return True


# ---------------------------------------------------------------------------
# Record settlement
# ---------------------------------------------------------------------------

def record_settlement(
    employee_id: int,
    amount: float,
    method: str,
    note: str,
    acting_user: dict,
    reference_expense_id: int | None = None,
) -> int:
    """Record a settlement (cash return, salary deduction, bank transfer).
    If acting_user role is 'user' -> pending_approval=1 (needs manager OK).
    Returns the new ledger row id."""
    if not amount or amount <= 0:
        raise ValidationError("Settlement amount must be positive.")
    if method not in SETTLEMENT_METHODS:
        raise ValidationError(f"Invalid settlement method: {method}")

    with get_db() as conn:
        emp = conn.execute(
            "SELECT id FROM employees WHERE id=? AND status='active'", (employee_id,)
        ).fetchone()
        if not emp:
            raise NotFoundError("Employee not found.")

        balance = get_employee_balance(conn, employee_id)
        if amount > balance:
            raise ValidationError(
                f"Settlement amount ({amount:,.2f}) exceeds outstanding balance ({balance:,.2f})."
            )

        is_user_role = acting_user["role"] == "user"
        pending = 1 if is_user_role else 0

        cur = conn.execute(
            "INSERT INTO advance_ledger "
            "(employee_id, movement_type, amount, reference_expense_id, "
            " settlement_method, note, pending_approval, created_by) "
            "VALUES (?, 'settlement', ?, ?, ?, ?, ?, ?)",
            (employee_id, -abs(amount), reference_expense_id,
             method, note.strip(), pending, acting_user["id"]),
        )
        ledger_id = cur.lastrowid
        log_action(conn, acting_user["id"], "record_settlement",
                   json.dumps({"employee_id": employee_id, "amount": amount,
                               "method": method, "pending": pending}))
        return ledger_id


# ---------------------------------------------------------------------------
# Settlement approval (manager/accountant approves user-submitted settlement)
# ---------------------------------------------------------------------------

def get_pending_settlements() -> list:
    """All settlements awaiting manager/accountant approval."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT al.*, e.name employee_name, e.phone employee_phone, "
            "u.full_name created_by_name "
            "FROM advance_ledger al "
            "JOIN employees e ON e.id = al.employee_id "
            "JOIN users u ON u.id = al.created_by "
            "WHERE al.movement_type='settlement' AND al.pending_approval=1 "
            "ORDER BY al.created_at",
        ).fetchall()
        return [dict(r) for r in rows]


def approve_settlement(ledger_id: int, acting_user: dict) -> None:
    """Approve a pending settlement. Manager or accountant only (enforced in route)."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM advance_ledger WHERE id=? AND pending_approval=1",
            (ledger_id,),
        ).fetchone()
        if not row:
            raise NotFoundError("Pending settlement not found.")
        conn.execute(
            "UPDATE advance_ledger SET pending_approval=0, approved_by=?, "
            "approved_at=datetime('now') WHERE id=?",
            (acting_user["id"], ledger_id),
        )
        log_action(conn, acting_user["id"], "approve_settlement",
                   json.dumps({"ledger_id": ledger_id}))


def reject_settlement(ledger_id: int, acting_user: dict, reason: str) -> None:
    """Reject (delete) a pending settlement with a reason."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM advance_ledger WHERE id=? AND pending_approval=1",
            (ledger_id,),
        ).fetchone()
        if not row:
            raise NotFoundError("Pending settlement not found.")
        conn.execute("DELETE FROM advance_ledger WHERE id=?", (ledger_id,))
        log_action(conn, acting_user["id"], "reject_settlement",
                   json.dumps({"ledger_id": ledger_id, "reason": reason}))


# ---------------------------------------------------------------------------
# Helpers for route access control
# ---------------------------------------------------------------------------

def get_outstanding_balances_for_user(user_id: int) -> list:
    """Balances restricted to employees this user personally advanced money to."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT e.id, e.name, e.phone, e.status, "
            "COALESCE(SUM(CASE WHEN al.movement_type='advance' AND al.pending_approval=0 "
            "              THEN al.amount ELSE 0 END), 0) total_advanced, "
            "COALESCE(SUM(CASE WHEN al.movement_type='settlement' AND al.pending_approval=0 "
            "              THEN ABS(al.amount) ELSE 0 END), 0) total_settled "
            "FROM employees e "
            "JOIN advance_ledger al ON al.employee_id = e.id "
            "WHERE e.status='active' AND al.created_by=? "
            "GROUP BY e.id ORDER BY e.name",
            (user_id,),
        ).fetchall()
        result = []
        for r in rows:
            adv = r["total_advanced"]
            sett = r["total_settled"]
            result.append({
                "id": r["id"], "name": r["name"], "phone": r["phone"],
                "status": r["status"],
                "total_advanced": adv, "total_settled": sett,
                "outstanding": round(adv - sett, 2),
            })
        return result


def user_has_advanced_employee(user_id: int, employee_id: int) -> bool:
    """True if this user has ever created an advance ledger entry for this employee."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM advance_ledger WHERE employee_id=? AND created_by=? LIMIT 1",
            (employee_id, user_id),
        ).fetchone()
        return row is not None


def get_settled_this_month() -> float:
    """Total settled amount (approved) this calendar month."""
    from datetime import date
    month = date.today().strftime("%Y-%m")
    with get_db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(ABS(amount)), 0) t FROM advance_ledger "
            "WHERE movement_type='settlement' AND pending_approval=0 "
            "AND strftime('%Y-%m', created_at)=?",
            (month,),
        ).fetchone()
        return row["t"] if row else 0.0
