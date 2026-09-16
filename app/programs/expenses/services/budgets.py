"""Budget vs Actual business logic.

Budget entries are per-period (YYYY-MM), optionally scoped to a cost
centre and/or category. NULL cost_center_id = company-wide. NULL
category_id = all categories combined.

Policy:
- Over-budget is WARN ONLY - it never blocks voucher creation/submission.
- When an approved voucher causes any cost-centre or company-wide budget
  to exceed 100% utilisation, an in-app notification is inserted for every
  manager user (via notify_managers()).
- Variance = actual - budget (positive = over budget).
- Utilisation % = (actual / budget) * 100.

No FastAPI imports - see app/services/errors.py.
"""
import json
from datetime import date

from app.database import get_db, log_action
from app.services import notifications as _notifications
from app.services.errors import ValidationError, NotFoundError, ConflictError

__all__ = [
    "set_budget",
    "delete_budget",
    "list_budgets",
    "get_budget_vs_actual",
    "check_over_budget",
    "notify_managers",
]


def set_budget(
    period: str,
    amount: float,
    acting_user_id: int,
    cost_center_id=None,
    category_id=None,
) -> int:
    """Upsert a budget for (period, cost_center_id, category_id).
    Returns the budget row id."""
    if not period or len(period) != 7 or period[4] != "-":
        raise ValidationError("Period must be in YYYY-MM format.")
    if not amount or amount <= 0:
        raise ValidationError("Budget amount must be positive.")
    with get_db() as conn:
        # Validate FK references if given.
        if cost_center_id:
            if not conn.execute("SELECT 1 FROM cost_centers WHERE id=?", (cost_center_id,)).fetchone():
                raise NotFoundError("Cost centre not found.")
        if category_id:
            if not conn.execute("SELECT 1 FROM categories WHERE id=?", (category_id,)).fetchone():
                raise NotFoundError("Category not found.")
        existing = conn.execute(
            "SELECT id FROM budgets WHERE period=? AND cost_center_id IS ? AND category_id IS ?",
            (period, cost_center_id, category_id),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE budgets SET amount=?, created_by=?, created_at=datetime('now') WHERE id=?",
                (amount, acting_user_id, existing["id"]),
            )
            budget_id = existing["id"]
        else:
            cur = conn.execute(
                "INSERT INTO budgets (period, cost_center_id, category_id, amount, created_by) "
                "VALUES (?, ?, ?, ?, ?)",
                (period, cost_center_id, category_id, amount, acting_user_id),
            )
            budget_id = cur.lastrowid
        log_action(conn, acting_user_id, "set_budget", json.dumps({
            "period": period, "cost_center_id": cost_center_id,
            "category_id": category_id, "amount": amount,
        }))
        return budget_id


def delete_budget(budget_id: int, acting_user_id: int) -> None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM budgets WHERE id=?", (budget_id,)).fetchone()
        if not row:
            raise NotFoundError("Budget not found.")
        conn.execute("DELETE FROM budgets WHERE id=?", (budget_id,))
        log_action(conn, acting_user_id, "delete_budget", json.dumps({"budget_id": budget_id}))


def list_budgets(period: str = "") -> list:
    """List all budgets, optionally filtered to a single period."""
    with get_db() as conn:
        query = (
            "SELECT b.*, cc.name cost_center_name, c.name category_name "
            "FROM budgets b "
            "LEFT JOIN cost_centers cc ON cc.id = b.cost_center_id "
            "LEFT JOIN categories c ON c.id = b.category_id"
        )
        params = []
        if period:
            query += " WHERE b.period = ?"
            params.append(period)
        query += " ORDER BY b.period DESC, cc.name, c.name"
        return [dict(r) for r in conn.execute(query, params).fetchall()]


def get_budget_vs_actual(period: str) -> list:
    """Return rows: {period, cost_center_id, cost_center_name, category_id,
    category_name, budget, actual, variance, utilisation_pct}.
    Only approved expenses count toward actual."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT b.id, b.period, b.cost_center_id, b.category_id, b.amount budget, "
            "cc.name cost_center_name, cat.name category_name, "
            "COALESCE(("
            "  SELECT SUM(el.amount + el.vat_amount) "
            "  FROM expense_lines el "
            "  JOIN expenses e ON e.id = el.expense_id "
            "  WHERE e.status='approved' "
            "  AND strftime('%Y-%m', e.expense_date) = b.period "
            "  AND (b.cost_center_id IS NULL OR e.cost_center_id = b.cost_center_id) "
            "  AND (b.category_id IS NULL OR el.category_id = b.category_id) "
            "), 0) actual "
            "FROM budgets b "
            "LEFT JOIN cost_centers cc ON cc.id = b.cost_center_id "
            "LEFT JOIN categories cat ON cat.id = b.category_id "
            "WHERE b.period = ? "
            "ORDER BY cc.name, cat.name",
            (period,),
        ).fetchall()

        result = []
        for r in rows:
            budget = r["budget"]
            actual = r["actual"]
            variance = actual - budget
            utilisation = round((actual / budget * 100), 1) if budget else 0.0
            result.append({
                "id": r["id"],
                "period": r["period"],
                "cost_center_id": r["cost_center_id"],
                "cost_center_name": r["cost_center_name"] or "Company-wide",
                "category_id": r["category_id"],
                "category_name": r["category_name"] or "All categories",
                "budget": budget,
                "actual": actual,
                "variance": variance,
                "utilisation_pct": utilisation,
                "over_budget": variance > 0,
            })
        return result


def check_over_budget(expense_date: str) -> list:
    """After an approval, return any budgets now over 100% for this period.
    Returns list of budget rows that are over budget."""
    period = expense_date[:7]
    rows = get_budget_vs_actual(period)
    return [r for r in rows if r["over_budget"]]


# The notifications table is shared infrastructure, so the SQL for it lives
# once in app/services/notifications.py. These three stay as thin wrappers
# because budget code (and its tests) already call them by these names -
# the implementation moved, the interface did not.
def notify_managers(conn, message: str, link: str = "") -> None:
    """Insert a notification row for every active manager.
    Must be called inside an existing get_db() transaction (same conn)."""
    # A blown budget is a warning, not a task with a button - it lands in
    # the "Warnings" band of the notification centre.
    _notifications.notify_role(conn, "manager", message, link, level="warn")


def get_notifications(user_id: int, unread_only: bool = False) -> list:
    """Return notifications for a user."""
    return _notifications.list_for_user(user_id, unread_only=unread_only)


def mark_notifications_read(user_id: int) -> None:
    _notifications.mark_all_read(user_id)
