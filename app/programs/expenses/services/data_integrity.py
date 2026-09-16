"""Detects and cleans up duplicate transaction lines.

Concern: a "duplicate" here means two or more lines on the *same voucher*
with identical category, particulars, amount, and VAT-able flag - most
likely an accidental double-entry (e.g. clicking "+ Add line" twice without
changing anything, or a slow double-submit). It is deliberately NOT
automatically deleted in bulk: two genuinely separate purchases can look
identical (two fuel top-ups of exactly the same amount on the same day is
plausible, not necessarily an error), so a human reviews each group before
anything is removed.

Cleanup is restricted to **draft** vouchers only, and reuses the same
ownership rule as the rest of the draft feature (the draft's own creator,
or a manager) - see app.programs.expenses.services.expenses.
_require_draft_owned_by(). A pending/approved/rejected voucher is a
submitted financial record; this module will report duplicates found in
one, but will never delete from it. Correcting a committed voucher goes
through the normal channel instead - reject/delete it (with a reason, fully
audited) and have it resubmitted.

Depends on: app.database, app.programs.expenses.services.expenses (only for
the shared ownership guard, to avoid re-implementing it).
Used by: app.programs.expenses.routes.settings_routes.

No FastAPI imports on purpose - see app/services/errors.py.
"""
import json

from app.database import get_db, log_action
from app.services.errors import ConflictError

__all__ = ["find_duplicate_lines", "remove_duplicate_line_group"]


def find_duplicate_lines():
    """Returns a list of duplicate-line groups, each with the voucher it
    belongs to and every line id in that group (oldest first). Only looks
    at vouchers that aren't already soft-deleted - a deleted voucher's data
    doesn't count toward anything and isn't worth reporting on."""
    with get_db() as conn:
        groups = conn.execute(
            "SELECT el.expense_id, el.category_id, el.particulars, el.amount, el.is_vatable, "
            "COUNT(*) AS cnt, GROUP_CONCAT(el.id) AS line_ids "
            "FROM expense_lines el "
            "JOIN expenses e ON e.id = el.expense_id "
            "WHERE e.status != 'deleted' "
            "GROUP BY el.expense_id, el.category_id, el.particulars, el.amount, el.is_vatable "
            "HAVING COUNT(*) > 1 "
            "ORDER BY el.expense_id"
        ).fetchall()

        results = []
        for g in groups:
            voucher = conn.execute(
                "SELECT e.id, e.voucher_no, e.status, e.paid_to, e.expense_date, c.name category_name "
                "FROM expenses e JOIN categories c ON c.id = ? WHERE e.id = ?",
                (g["category_id"], g["expense_id"]),
            ).fetchone()
            line_ids = [int(x) for x in g["line_ids"].split(",")]
            results.append({
                "expense_id": g["expense_id"],
                "voucher_no": voucher["voucher_no"],
                "status": voucher["status"],
                "paid_to": voucher["paid_to"],
                "expense_date": voucher["expense_date"],
                "category_id": g["category_id"],
                "category_name": voucher["category_name"],
                "particulars": g["particulars"],
                "amount": g["amount"],
                "is_vatable": bool(g["is_vatable"]),
                "count": g["cnt"],
                "line_ids": sorted(line_ids),
                "is_draft": voucher["status"] == "draft",
            })
        return results


def _renumber_lines(conn, expense_id):
    """Re-assigns line_no sequentially (1, 2, 3...) in existing order, so
    removing one duplicate out of the middle doesn't leave a gap."""
    rows = conn.execute(
        "SELECT id FROM expense_lines WHERE expense_id=? ORDER BY line_no, id", (expense_id,)
    ).fetchall()
    for i, row in enumerate(rows, start=1):
        conn.execute("UPDATE expense_lines SET line_no=? WHERE id=?", (i, row["id"]))


def remove_duplicate_line_group(expense_id, category_id, particulars, amount, is_vatable, acting_user):
    """Removes every line in the given duplicate group except the oldest
    (lowest id), on a draft only. Raises ConflictError (and changes
    nothing) if the voucher isn't a draft - use the normal reject/delete
    flow for a submitted voucher instead. Reuses the same owner-or-manager
    check as editing a draft."""
    from app.programs.expenses.services.expenses import _require_draft_owned_by

    with get_db() as conn:
        _require_draft_owned_by(conn, expense_id, acting_user)  # raises if not a draft, or not owned

        rows = conn.execute(
            "SELECT id FROM expense_lines WHERE expense_id=? AND category_id=? AND particulars=? "
            "AND amount=? AND is_vatable=? ORDER BY id",
            (expense_id, category_id, particulars, amount, int(is_vatable)),
        ).fetchall()
        if len(rows) < 2:
            raise ConflictError("This isn't a duplicate anymore - it may have already been cleaned up.")

        keep_id = rows[0]["id"]
        remove_ids = [r["id"] for r in rows[1:]]
        for line_id in remove_ids:
            conn.execute("DELETE FROM expense_lines WHERE id=?", (line_id,))
        _renumber_lines(conn, expense_id)

        log_action(conn, acting_user["id"], "clean_duplicate_lines", json.dumps({
            "expense_id": expense_id, "kept_line_id": keep_id, "removed_line_ids": remove_ids,
            "particulars": particulars, "amount": amount,
        }))
        return len(remove_ids)
