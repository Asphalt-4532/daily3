"""Chain of custody: who is holding an asset, since when, and what happened
to it before that.

The shape that matters here: `asset_assignments` holds OPEN and CLOSED rows,
not a latest-row-wins ledger. One asset can legitimately have several
simultaneous active holders - a day-shift and a night-shift driver on the
same truck - so there is deliberately no unique index on asset_id and
`active_holders()` returns a list, never a single row. Anything that assumes
one holder per asset is wrong here.

A row is never deleted. Closing one sets returned_date / status / closed_by /
close_reason, which is what leaves the history readable years later.

No FastAPI imports - see app/services/errors.py.
"""
from app.database import get_db, log_action
from app.services.errors import (
    ValidationError, NotFoundError, ConflictError, DuplicateError,
)

__all__ = [
    "assign_asset",
    "close_assignment",
    "transfer_asset",
    "divest_asset",
    "active_holders",
    "assignment_history",
    "assets_held_by",
    "link_employee_to_user",
]


def _load_live_asset(conn, asset_id):
    row = conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
    if not row or row["status"] == "deleted":
        raise NotFoundError("That asset doesn't exist.")
    if row["status"] == "divested":
        raise ConflictError(f"{row['asset_tag']} has been divested.")
    return row


def _holder_label(conn, employee_id, cost_center_id):
    if employee_id:
        row = conn.execute("SELECT name FROM employees WHERE id=?", (employee_id,)).fetchone()
        return row["name"] if row else f"employee #{employee_id}"
    row = conn.execute("SELECT name FROM cost_centers WHERE id=?", (cost_center_id,)).fetchone()
    return row["name"] if row else f"cost centre #{cost_center_id}"


def link_employee_to_user(employee_id, user_id, *, linked_by):
    """Point an employee record at a login account, which is what lets an
    assignment be recognised as belonging to the person currently signed in.

    Nullable and set by hand on purpose: an employee who only ever receives
    salary advances and never signs in keeps this NULL forever, and matching
    on name+phone instead would be a guess dressed up as a fact.
    """
    with get_db() as conn:
        emp = conn.execute(
            "SELECT * FROM employees WHERE id=? AND status='active'", (employee_id,)
        ).fetchone()
        if not emp:
            raise NotFoundError("That employee doesn't exist.")
        if user_id is not None:
            user = conn.execute(
                "SELECT id, username FROM users WHERE id=? AND active=1", (user_id,)
            ).fetchone()
            if not user:
                raise NotFoundError("That user account doesn't exist.")
            taken = conn.execute(
                "SELECT id, name FROM employees WHERE user_id=? AND id<>? AND status='active'",
                (user_id, employee_id),
            ).fetchone()
            if taken:
                raise DuplicateError(
                    f"That login is already linked to employee '{taken['name']}'."
                )
        conn.execute("UPDATE employees SET user_id=? WHERE id=?", (user_id, employee_id))
        log_action(conn, linked_by, "employee_user_linked",
                   f"{emp['name']} -> user #{user_id}" if user_id
                   else f"{emp['name']} unlinked from any login")


def assign_asset(asset_id, *, employee_id=None, cost_center_id=None,
                 role_note="", issued_date, assigned_by):
    """Open an assignment. Several may be open on one asset at once - that is
    the point of this table - but not the same asset to the same holder
    twice, which is a double-click rather than a real second assignment."""
    if not employee_id and not cost_center_id:
        raise ValidationError("Assign the asset to an employee or a cost centre.")
    if employee_id and cost_center_id:
        raise ValidationError("Assign to an employee or a cost centre, not both.")
    if not issued_date:
        raise ValidationError("An issue date is required.")

    with get_db() as conn:
        asset = _load_live_asset(conn, asset_id)
        if employee_id:
            emp = conn.execute(
                "SELECT id FROM employees WHERE id=? AND status='active'", (employee_id,)
            ).fetchone()
            if not emp:
                raise ValidationError("Please choose a valid employee.")
        if cost_center_id:
            cc = conn.execute(
                "SELECT id FROM cost_centers WHERE id=? AND active=1", (cost_center_id,)
            ).fetchone()
            if not cc:
                raise ValidationError("Please choose a valid cost centre.")

        dup = conn.execute(
            "SELECT id FROM asset_assignments WHERE asset_id=? AND status='active' "
            "AND ((employee_id IS NOT NULL AND employee_id=?) "
            "  OR (cost_center_id IS NOT NULL AND cost_center_id=?))",
            (asset_id, employee_id, cost_center_id),
        ).fetchone()
        if dup:
            raise DuplicateError("That asset is already assigned to them.")

        cur = conn.execute(
            "INSERT INTO asset_assignments (asset_id, employee_id, cost_center_id, "
            "role_note, issued_date, assigned_by) VALUES (?, ?, ?, ?, ?, ?)",
            (asset_id, employee_id, cost_center_id, (role_note or "").strip(),
             issued_date, assigned_by),
        )
        who = _holder_label(conn, employee_id, cost_center_id)
        log_action(conn, assigned_by, "asset_assigned",
                   f"{asset['asset_tag']} -> {who} on {issued_date}"
                   + (f" ({role_note})" if role_note else ""))
        return cur.lastrowid


def close_assignment(assignment_id, *, returned_date, reason, closed_by,
                     status="returned"):
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("A reason is required to close an assignment.")
    if not returned_date:
        raise ValidationError("A return date is required.")
    if status not in ("returned", "transferred", "divested"):
        raise ValidationError("That isn't a valid way to close an assignment.")
    with get_db() as conn:
        row = conn.execute(
            "SELECT asg.*, a.asset_tag FROM asset_assignments asg "
            "JOIN assets a ON a.id = asg.asset_id WHERE asg.id=?", (assignment_id,)
        ).fetchone()
        if not row:
            raise NotFoundError("That assignment doesn't exist.")
        if row["status"] != "active":
            raise ConflictError("That assignment has already been closed.")
        conn.execute(
            "UPDATE asset_assignments SET status=?, returned_date=?, closed_by=?, "
            "close_reason=? WHERE id=?",
            (status, returned_date, closed_by, reason, assignment_id),
        )
        who = _holder_label(conn, row["employee_id"], row["cost_center_id"])
        log_action(conn, closed_by, "asset_assignment_closed",
                   f"{row['asset_tag']} returned from {who} on {returned_date}: {reason}")


def transfer_asset(asset_id, from_assignment_id, *, to_employee_id=None,
                   to_cost_center_id=None, transfer_date, reason, by):
    """Close one assignment and open another, as a single unit of work.

    Both halves share one `get_db()` transaction: a transfer that closed the
    old holder's row and then failed to open the new one would leave the
    asset held by nobody, with no record of where it actually is.
    """
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("A reason is required to transfer an asset.")
    if not to_employee_id and not to_cost_center_id:
        raise ValidationError("Transfer the asset to an employee or a cost centre.")
    if to_employee_id and to_cost_center_id:
        raise ValidationError("Transfer to an employee or a cost centre, not both.")
    if not transfer_date:
        raise ValidationError("A transfer date is required.")

    with get_db() as conn:
        asset = _load_live_asset(conn, asset_id)
        old = conn.execute(
            "SELECT * FROM asset_assignments WHERE id=? AND asset_id=?",
            (from_assignment_id, asset_id),
        ).fetchone()
        if not old:
            raise NotFoundError("That assignment doesn't exist for this asset.")
        if old["status"] != "active":
            raise ConflictError("That assignment has already been closed.")

        if to_employee_id:
            emp = conn.execute(
                "SELECT id FROM employees WHERE id=? AND status='active'", (to_employee_id,)
            ).fetchone()
            if not emp:
                raise ValidationError("Please choose a valid employee.")
        if to_cost_center_id:
            cc = conn.execute(
                "SELECT id FROM cost_centers WHERE id=? AND active=1", (to_cost_center_id,)
            ).fetchone()
            if not cc:
                raise ValidationError("Please choose a valid cost centre.")
        dup = conn.execute(
            "SELECT id FROM asset_assignments WHERE asset_id=? AND status='active' "
            "AND ((employee_id IS NOT NULL AND employee_id=?) "
            "  OR (cost_center_id IS NOT NULL AND cost_center_id=?))",
            (asset_id, to_employee_id, to_cost_center_id),
        ).fetchone()
        if dup:
            raise DuplicateError("That asset is already assigned to them.")

        conn.execute(
            "UPDATE asset_assignments SET status='transferred', returned_date=?, "
            "closed_by=?, close_reason=? WHERE id=?",
            (transfer_date, by, reason, from_assignment_id),
        )
        cur = conn.execute(
            "INSERT INTO asset_assignments (asset_id, employee_id, cost_center_id, "
            "role_note, issued_date, assigned_by) VALUES (?, ?, ?, ?, ?, ?)",
            (asset_id, to_employee_id, to_cost_center_id,
             old["role_note"], transfer_date, by),
        )
        old_who = _holder_label(conn, old["employee_id"], old["cost_center_id"])
        new_who = _holder_label(conn, to_employee_id, to_cost_center_id)
        log_action(conn, by, "asset_transferred",
                   f"{asset['asset_tag']}: {old_who} -> {new_who} on {transfer_date}: {reason}")
        return cur.lastrowid


def divest_asset(asset_id, *, divest_date, settle_amount=None, reason, by):
    """Mark an asset sold or scrapped: close every open assignment and set
    the asset's status, in one transaction.

    `settle_amount` lands on the ASSET (assets.divest_proceeds), not on an
    assignment row. An asset sold while sitting in a yard has no holder and
    therefore no row for the proceeds to live on - an earlier version wrote a
    placeholder assignment instead and tripped the "assigned to someone or
    somewhere" CHECK, which was the schema correctly refusing a row that
    described nothing.

    The proceeds are recorded for the asset report only and are deliberately
    NOT posted to cash_movements: money from selling a truck is not petty
    cash, and inventing a movement for it would put a number into the float
    that never physically entered the box. The accountant records the sale
    in Qoyod.
    """
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("A reason is required to divest an asset.")
    if not divest_date:
        raise ValidationError("A divest date is required.")
    if settle_amount is not None:
        try:
            settle_amount = round(float(settle_amount), 2)
        except (TypeError, ValueError):
            raise ValidationError("Proceeds must be a number.")
        if settle_amount < 0:
            raise ValidationError("Proceeds can't be negative.")

    with get_db() as conn:
        asset = _load_live_asset(conn, asset_id)
        open_rows = conn.execute(
            "SELECT id FROM asset_assignments WHERE asset_id=? AND status='active'",
            (asset_id,),
        ).fetchall()
        for row in open_rows:
            conn.execute(
                "UPDATE asset_assignments SET status='divested', returned_date=?, "
                "closed_by=?, close_reason=? WHERE id=?",
                (divest_date, by, reason, row["id"]),
            )
        conn.execute(
            "UPDATE assets SET status='divested', divested_on=?, divest_proceeds=?, "
            "divest_reason=?, divested_by=? WHERE id=?",
            (divest_date, settle_amount, reason, by, asset_id),
        )
        amount_note = f", proceeds {settle_amount:.2f}" if settle_amount is not None else ""
        log_action(conn, by, "asset_divested",
                   f"{asset['asset_tag']} divested on {divest_date}{amount_note}: {reason}")


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------
def active_holders(asset_id):
    """Every current holder - a LIST, because one asset can be held by more
    than one person at a time (day shift / night shift)."""
    with get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT asg.*, e.name AS employee_name, e.phone AS employee_phone, "
            "e.user_id AS employee_user_id, cc.name AS cost_center_name "
            "FROM asset_assignments asg "
            "LEFT JOIN employees e ON e.id = asg.employee_id "
            "LEFT JOIN cost_centers cc ON cc.id = asg.cost_center_id "
            "WHERE asg.asset_id=? AND asg.status='active' "
            "ORDER BY asg.issued_date, asg.id", (asset_id,)
        ).fetchall()]


def assignment_history(asset_id):
    with get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT asg.*, e.name AS employee_name, cc.name AS cost_center_name, "
            "u.full_name AS assigned_by_name "
            "FROM asset_assignments asg "
            "LEFT JOIN employees e ON e.id = asg.employee_id "
            "LEFT JOIN cost_centers cc ON cc.id = asg.cost_center_id "
            "LEFT JOIN users u ON u.id = asg.assigned_by "
            "WHERE asg.asset_id=? ORDER BY asg.issued_date DESC, asg.id DESC",
            (asset_id,)
        ).fetchall()]


def assets_held_by(employee_id):
    with get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT a.*, asg.issued_date, asg.role_note, asg.id AS assignment_id "
            "FROM asset_assignments asg JOIN assets a ON a.id = asg.asset_id "
            "WHERE asg.employee_id=? AND asg.status='active' AND a.status<>'deleted' "
            "ORDER BY a.asset_tag", (employee_id,)
        ).fetchall()]
