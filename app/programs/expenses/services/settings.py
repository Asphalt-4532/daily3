"""Business logic for the Expense Program's reference data: the categories
and cost centres vouchers get tagged with.

Concern: category/cost-centre CRUD. Program-specific to the Expense Program -
a future program with its own reference data (e.g. Weekly Productions'
product lines) would get its own equivalent module in its own package, not
an addition to this one.
Depends on: app.database only.
Used by: app.programs.expenses.routes.settings_routes, and read-only by
app.programs.expenses.routes.expense_routes (for populating dropdowns).

No FastAPI imports on purpose - see app/services/errors.py.
"""
from app.database import get_db, log_action
from app.services.errors import ValidationError

__all__ = [
    "list_categories", "add_category", "toggle_category", "update_gl_code",
    "list_cost_centers", "add_cost_center", "toggle_cost_center",
]


def list_categories(active_only: bool = False):
    with get_db() as conn:
        q = "SELECT * FROM categories" + (" WHERE active=1" if active_only else "") + " ORDER BY name"
        return [dict(r) for r in conn.execute(q).fetchall()]


def add_category(name: str, acting_user_id: int) -> None:
    name = (name or "").strip()
    if not name:
        raise ValidationError("Category name is required.")
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (name,))
        log_action(conn, acting_user_id, "add_category", name)


def toggle_category(category_id: int, acting_user_id: int) -> None:
    with get_db() as conn:
        conn.execute("UPDATE categories SET active = 1 - active WHERE id = ?", (category_id,))
        log_action(conn, acting_user_id, "toggle_category", str(category_id))


def list_cost_centers(active_only: bool = False):
    with get_db() as conn:
        q = "SELECT * FROM cost_centers" + (" WHERE active=1" if active_only else "") + " ORDER BY name"
        return [dict(r) for r in conn.execute(q).fetchall()]


def add_cost_center(code: str, name: str, acting_user_id: int) -> None:
    code = (code or "").strip().upper()
    name = (name or "").strip()
    if not code:
        raise ValidationError("Cost centre code is required.")
    if not name:
        raise ValidationError("Cost centre name is required.")
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO cost_centers (code, name) VALUES (?, ?)", (code, name))
        log_action(conn, acting_user_id, "add_cost_center", code)


def toggle_cost_center(cost_center_id: int, acting_user_id: int) -> None:
    with get_db() as conn:
        conn.execute("UPDATE cost_centers SET active = 1 - active WHERE id = ?", (cost_center_id,))
        log_action(conn, acting_user_id, "toggle_cost_center", str(cost_center_id))


def update_gl_code(category_id: int, gl_code: str, acting_user_id: int) -> None:
    """Set or clear the GL account code on a category. Free-text, no format enforced."""
    from app.services.errors import NotFoundError as _NF
    with get_db() as conn:
        row = conn.execute("SELECT id FROM categories WHERE id=?", (category_id,)).fetchone()
        if not row:
            raise _NF("Category not found.")
        conn.execute("UPDATE categories SET gl_code=? WHERE id=?", (gl_code or None, category_id))
        log_action(conn, acting_user_id, "update_category_gl_code",
                   f"category_id={category_id} gl_code={gl_code!r}")
