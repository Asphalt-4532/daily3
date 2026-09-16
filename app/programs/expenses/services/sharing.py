"""Handing a voucher to someone for attention - usually the accountant.

This replaces "email it to the accountant". There is no mail server on a
factory LAN, and an emailed copy immediately becomes a second, divergent
record of something the app already holds. Instead the recipient gets a
notification linking to the voucher itself, where the PDF and the receipt
already live - one record, always current, and the hand-off is written into
the audit log like every other action.

Business rules only; no FastAPI imports. The route parses and the route
renders - see app/programs/expenses/routes/expense_routes.py.
"""
import json

from app.database import get_db, log_action
from app.services import notifications as _notifications
from app.services.errors import ValidationError, NotFoundError, ConflictError

__all__ = ["list_shareable_users", "share_voucher", "list_shares", "MAX_NOTE_LENGTH"]

# A note is a one-line "please check the fuel line", not correspondence.
MAX_NOTE_LENGTH = 500

# A voucher is shared to get it looked at, so only statuses where looking at
# it means something. A draft is unfinished work with no voucher number yet,
# and a deleted voucher is gone from every list the recipient could act on.
SHAREABLE_STATUSES = ("pending", "approved", "rejected")


def list_shareable_users(exclude_user_id=None) -> list:
    """Active accounts this voucher can be handed to, accountants first -
    they are who this feature exists for. Excludes the person doing the
    sharing; notifying yourself is noise."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, full_name, role FROM users "
            "WHERE active = 1 AND id != COALESCE(?, -1) "
            "ORDER BY CASE role WHEN 'accountant' THEN 0 WHEN 'manager' THEN 1 ELSE 2 END, "
            "full_name",
            (exclude_user_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def list_shares(expense_id: int) -> list:
    """Who this voucher has already been handed to, most recent first."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT s.id, s.note, s.created_at, u.full_name AS shared_with_name, "
            "u.role AS shared_with_role, b.full_name AS shared_by_name "
            "FROM expense_shares s "
            "JOIN users u ON u.id = s.shared_with "
            "JOIN users b ON b.id = s.shared_by "
            "WHERE s.expense_id = ? "
            "ORDER BY s.created_at DESC, s.id DESC",
            (expense_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def share_voucher(expense_id: int, user_ids, note: str, acting_user: dict) -> int:
    """Records the hand-off and notifies each recipient. Returns how many
    people it went to.

    Everything lands in one transaction: a notification whose share row
    failed to write would point at a hand-off that, as far as the voucher is
    concerned, never happened.
    """
    ids = []
    for raw in (user_ids or []):
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ValidationError("Pick who to share this with.")
        if value not in ids:
            ids.append(value)

    if not ids:
        raise ValidationError("Pick at least one person to share this with.")

    clean_note = (note or "").strip()
    if len(clean_note) > MAX_NOTE_LENGTH:
        raise ValidationError(f"Keep the note under {MAX_NOTE_LENGTH} characters.")

    with get_db() as conn:
        expense = conn.execute(
            "SELECT id, voucher_no, status, paid_to FROM expenses WHERE id = ?",
            (expense_id,),
        ).fetchone()
        if not expense:
            raise NotFoundError("Voucher not found.")
        if expense["status"] not in SHAREABLE_STATUSES:
            raise ConflictError(
                "Only a submitted voucher can be shared. Submit this one first."
            )

        if acting_user["id"] in ids:
            raise ValidationError("You can't share a voucher with yourself.")

        placeholders = ",".join("?" for _ in ids)
        found = conn.execute(
            f"SELECT id, full_name FROM users WHERE active = 1 AND id IN ({placeholders})",
            ids,
        ).fetchall()
        if len(found) != len(ids):
            raise ValidationError("One of those accounts no longer exists or is inactive.")

        link = f"/expense-program/expenses/{expense_id}"
        label = expense["voucher_no"] or f"voucher #{expense_id}"
        message = f"{acting_user['full_name']} shared {label} with you"
        if clean_note:
            message += f": {clean_note}"

        for row in found:
            conn.execute(
                "INSERT INTO expense_shares (expense_id, shared_with, shared_by, note) "
                "VALUES (?, ?, ?, ?)",
                (expense_id, row["id"], acting_user["id"], clean_note),
            )
            # A hand-off is someone waiting on this person - "Action needed".
            _notifications.notify(conn, row["id"], message, link, level="action")

        log_action(conn, acting_user["id"], "share_expense", json.dumps({
            "expense_id": expense_id,
            "voucher_no": expense["voucher_no"],
            "shared_with": [row["full_name"] for row in found],
            "note": clean_note,
        }))
        return len(found)
