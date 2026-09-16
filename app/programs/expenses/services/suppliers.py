"""The supplier "knowledge base" behind Expense Program's VAT-able
transaction lines: a VAT-able line requires a supplier company name and
VAT number, and typing a VAT number that's already known reuses that
supplier rather than requiring the name to be retyped (see
_resolve_supplier(), called from expenses._validate_lines()).

Deletion is soft-delete only, same as every other financial-adjacent
record in this app: a deleted supplier's row is never renamed or removed,
only its `status` flips - so an expense_lines.supplier_id that already
points at it keeps resolving to the exact same name/VAT it always did.
Deleting only removes the supplier from the picker offered for *new*
lines (see list_suppliers(status='active'), the default).

Concern: supplier lookup/creation/deletion.
Depends on: app.database (persistence).
Used by: app.programs.expenses.services.expenses (_resolve_supplier, at
voucher-line validation time) and
app.programs.expenses.routes.settings_routes (the Suppliers page).

No FastAPI/Starlette imports here on purpose - see app/services/errors.py.
"""
from app.database import get_db, get_connection, log_action
from app.services.errors import ValidationError, NotFoundError

__all__ = ["resolve_supplier", "list_suppliers", "get_supplier", "delete_supplier"]


def resolve_supplier(conn, name, vat_number, created_by):
    """Called inside an existing transaction (expenses._validate_lines())
    - `conn` is the caller's connection, not a fresh one. Returns a
    supplier id: reuses an active supplier whose vat_number matches
    exactly, creates a new one if the VAT number is unseen, or raises if
    the VAT number belongs to a *deleted* supplier (reusing it silently
    would look like an accidental undelete; a manager has to restore it
    first via the Suppliers page instead)."""
    name = (name or "").strip()
    vat_number = (vat_number or "").strip()
    if not name:
        raise ValidationError("Supplier name is required for a VAT-able line.")
    if not vat_number:
        raise ValidationError("Supplier VAT is required for a VAT-able line.")

    existing = conn.execute(
        "SELECT id, status FROM suppliers WHERE vat_number = ?", (vat_number,)
    ).fetchone()
    if existing:
        if existing["status"] == "deleted":
            raise ValidationError(
                f"VAT {vat_number} belongs to a deleted supplier - a manager must restore it "
                "before it can be used on a new line."
            )
        return existing["id"]

    cur = conn.execute(
        "INSERT INTO suppliers (name, vat_number, created_by) VALUES (?, ?, ?)",
        (name, vat_number, created_by),
    )
    return cur.lastrowid


def list_suppliers(status="active", search=""):
    """`search` matches against the VAT number (the "search any existing
    supplier via the VAT number" requirement) - a plain substring match,
    not a full lookup index, since the supplier list is expected to stay
    small enough for this to be instant either way."""
    query = (
        "SELECT s.*, u1.full_name created_by_name, u2.full_name deleted_by_name, "
        "(SELECT COUNT(*) FROM expense_lines WHERE supplier_id = s.id) usage_count "
        "FROM suppliers s "
        "JOIN users u1 ON u1.id = s.created_by "
        "LEFT JOIN users u2 ON u2.id = s.deleted_by WHERE 1=1"
    )
    params = []
    if status:
        query += " AND s.status = ?"
        params.append(status)
    if search:
        query += " AND s.vat_number LIKE ?"
        params.append(f"%{search}%")
    query += " ORDER BY s.name"
    conn = get_connection()
    try:
        return [dict(r) for r in conn.execute(query, params).fetchall()]
    finally:
        conn.close()


def get_supplier(supplier_id):
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM suppliers WHERE id = ?", (supplier_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise NotFoundError("Supplier not found.")
    return dict(row)


def delete_supplier(supplier_id, deleted_by, reason):
    if not (reason or "").strip():
        raise ValidationError("A reason is required to delete a supplier.")
    with get_db() as conn:
        row = conn.execute(
            "SELECT name, vat_number, status FROM suppliers WHERE id = ?", (supplier_id,)
        ).fetchone()
        if not row:
            raise NotFoundError("Supplier not found.")
        if row["status"] == "deleted":
            raise ValidationError("This supplier is already deleted.")
        conn.execute(
            "UPDATE suppliers SET status='deleted', deleted_by=?, deleted_at=datetime('now'), "
            "delete_reason=? WHERE id=?",
            (deleted_by, reason.strip(), supplier_id),
        )
        log_action(conn, deleted_by, "delete_supplier",
                   f"{row['name']} ({row['vat_number']}): {reason.strip()}")
