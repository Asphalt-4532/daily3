"""Business logic for petty cash vouchers: create, list, approve, reject,
soft-delete - and now drafts.

A voucher is a header (date, cost centre, paid to, payment mode, status,
approval trail) holding one or more *lines* - individual transactions, each
with its own category, particulars, and amount. Not every transaction is
VAT-able, so VAT is tracked per line, not per voucher.

A voucher can be saved as a **draft** before it's ready: a draft has no
voucher number yet (numbers are only allocated to real, submitted
transactions - see next_voucher_number()), doesn't need to pass full
validation (it can even have zero lines), and can be freely edited or
outright discarded (a real DELETE, not a soft-delete) by its own owner,
since nothing about a draft has been committed to the audit trail yet.
Once **submitted**, a voucher gets its number, must pass the same full
validation a direct submission always has, and from that point on follows
the normal pending -> approved/rejected, soft-delete-only rules.

Concern: voucher lifecycle and validation rules.
Depends on: app.database (persistence), app.config (upload limits, VAT rate).
Used by: app.programs.expenses.routes.expense_routes (the only caller -
nothing else in this app needs to know a voucher's rules).

No FastAPI/Starlette imports here on purpose - see app/services/errors.py.
Every function uses app.database.get_db(), which commits on success and
rolls back on any exception - callers never manage a connection's lifecycle
themselves, they just call a function and either get a result or a
ServiceError.
"""
import json
import secrets
from datetime import date, datetime
from pathlib import Path

from app import auth
from app.database import get_db, next_voucher_number, log_action
from app.programs.expenses.services import suppliers as suppliers_service
from app.programs.expenses.services import cash_ledger as _cash_ledger
from app.programs.expenses.services import period_lock as _period_lock
from app.programs.expenses.services import budgets as _budgets
from app.programs.expenses.services import advances as _advances
from app.config import UPLOADS_DIR, MAX_UPLOAD_MB, ALLOWED_UPLOAD_EXTENSIONS, VAT_RATE, ADVANCE_CATEGORY_NAME
from app.services.errors import ValidationError, NotFoundError, ConflictError, ForbiddenError
from app import sandbox

__all__ = [
    "get_expense", "list_expenses", "dashboard_stats", "save_receipt",
    "create_expense", "update_draft", "submit_draft", "discard_draft",
    "approve_expense", "reject_expense", "delete_expense", "compute_vat",
    "recent_category_id", "clone_expense",
    "bulk_approve", "bulk_reject", "bulk_delete", "BulkResult", "MAX_BULK_IDS",
    "revert_approval", "revert_rejection", "restore_deleted", "statuses_for",
]

_TOTALS_SQL = (
    "(SELECT COALESCE(SUM(amount + vat_amount),0) FROM expense_lines WHERE expense_id = e.id) AS total_amount, "
    "(SELECT COALESCE(SUM(vat_amount),0) FROM expense_lines WHERE expense_id = e.id) AS total_vat, "
    "(SELECT COUNT(*) FROM expense_lines WHERE expense_id = e.id) AS line_count, "
    "(SELECT GROUP_CONCAT(DISTINCT c.name) FROM expense_lines el "
    " JOIN categories c ON c.id = el.category_id WHERE el.expense_id = e.id) AS category_names"
)

_DETAIL_QUERY = (
    f"SELECT e.*, {_TOTALS_SQL}, cc.name cost_center_name, "
    "u1.full_name prepared_by_name, u2.full_name approved_by_name, "
    "u3.full_name deleted_by_name "
    "FROM expenses e "
    "LEFT JOIN cost_centers cc ON cc.id = e.cost_center_id "
    "JOIN users u1 ON u1.id = e.prepared_by "
    "LEFT JOIN users u2 ON u2.id = e.approved_by "
    "LEFT JOIN users u3 ON u3.id = e.deleted_by "
)


def compute_vat(amount, is_vatable):
    """Returns (vat_rate_applied, vat_amount) for a line. `amount` is always
    VAT-exclusive. If the line is marked VAT-able, VAT is calculated at
    VAT_RATE% of that amount and added on top to reach the line's total;
    if not VAT-able, nothing is added."""
    if not is_vatable or not amount:
        return 0.0, 0.0
    vat_amount = round(amount * VAT_RATE / 100, 2)
    return VAT_RATE, vat_amount


# How far back "what does this person usually pick" looks. Small on
# purpose: a category someone used 200 vouchers ago is history, not habit.
RECENT_CATEGORY_WINDOW = 20


def recent_category_id(user_id):
    """The category this user picks most often on their recent vouchers, or
    None if they have none yet (a brand-new account, or every candidate
    category has since been deactivated).

    Convenience only - it preselects a dropdown that the user can still
    change, and nothing downstream trusts it. Deleted vouchers are ignored
    (they were a mistake, not a habit) and drafts are counted (an unfinished
    voucher still says what this person was reaching for).

    The Salary advance category is deliberately excluded: picking it opens
    the employee fields on the form, so defaulting to it would make the
    common case noisier rather than faster.
    """
    with get_db() as conn:
        row = conn.execute(
            "WITH recent AS ("
            "  SELECT id FROM expenses "
            "  WHERE prepared_by = ? AND status != 'deleted' "
            "  ORDER BY id DESC LIMIT ?"
            ") "
            "SELECT el.category_id AS category_id, COUNT(*) AS uses, MAX(el.id) AS latest "
            "FROM expense_lines el "
            "JOIN recent r ON r.id = el.expense_id "
            "JOIN categories c ON c.id = el.category_id "
            "WHERE c.active = 1 AND c.name != ? "
            "GROUP BY el.category_id "
            "ORDER BY uses DESC, latest DESC "
            "LIMIT 1",
            (user_id, RECENT_CATEGORY_WINDOW, ADVANCE_CATEGORY_NAME),
        ).fetchone()
        return row["category_id"] if row else None


def _fetch_lines(conn, expense_id):
    return [dict(r) for r in conn.execute(
        "SELECT el.*, c.name category_name, s.name supplier_name, s.vat_number supplier_vat, el.employee_name, el.employee_phone, el.employee_id, "
        "a.asset_tag, a.name AS asset_name "
        "FROM expense_lines el "
        "JOIN categories c ON c.id = el.category_id "
        "LEFT JOIN suppliers s ON s.id = el.supplier_id "
        "LEFT JOIN assets a ON a.id = el.asset_id "
        "WHERE el.expense_id = ? ORDER BY el.line_no", (expense_id,)
    ).fetchall()]


def get_expense(expense_id):
    with get_db() as conn:
        row = conn.execute(_DETAIL_QUERY + "WHERE e.id = ?", (expense_id,)).fetchone()
        if not row:
            return None
        expense = dict(row)
        expense["lines"] = _fetch_lines(conn, expense_id)
        return expense


def list_expenses(status="", category_id="", cost_center_id="", start="", end=""):
    """Returns (rows, total_amount). By default, soft-deleted vouchers and
    drafts are both excluded unless explicitly requested via `status` -
    drafts are unfinished scratch work, not something that belongs in the
    main list until their owner submits them. Filtering by category matches
    any voucher with at least one line in that category."""
    with get_db() as conn:
        query = _DETAIL_QUERY + "WHERE 1=1"
        params = []
        if status:
            query += " AND e.status = ?"
            params.append(status)
        else:
            query += " AND e.status NOT IN ('deleted', 'draft')"
        if category_id:
            query += (" AND EXISTS (SELECT 1 FROM expense_lines el "
                       "WHERE el.expense_id = e.id AND el.category_id = ?)")
            params.append(category_id)
        if cost_center_id:
            query += " AND e.cost_center_id = ?"
            params.append(cost_center_id)
        if start:
            query += " AND e.expense_date >= ?"
            params.append(start)
        if end:
            query += " AND e.expense_date <= ?"
            params.append(end)
        query += " ORDER BY e.expense_date DESC, e.id DESC"
        rows = [dict(r) for r in conn.execute(query, params).fetchall()]
        total = sum(r["total_amount"] for r in rows if r["status"] != "deleted")
        return rows, total


def dashboard_stats():
    with get_db() as conn:
        today = date.today()
        month_start = today.replace(day=1).isoformat()
        month_total = conn.execute(
            "SELECT COALESCE(SUM(el.amount + el.vat_amount),0) t FROM expense_lines el "
            "JOIN expenses e ON e.id = el.expense_id "
            "WHERE e.status='approved' AND e.expense_date >= ?", (month_start,)
        ).fetchone()["t"]
        pending_count = conn.execute(
            "SELECT COUNT(*) c FROM expenses WHERE status='pending'"
        ).fetchone()["c"]
        pending_total = conn.execute(
            "SELECT COALESCE(SUM(el.amount + el.vat_amount),0) t FROM expense_lines el "
            "JOIN expenses e ON e.id = el.expense_id WHERE e.status='pending'"
        ).fetchone()["t"]
        draft_count = conn.execute(
            "SELECT COUNT(*) c FROM expenses WHERE status='draft'"
        ).fetchone()["c"]
        by_category = conn.execute(
            "SELECT c.name, COALESCE(SUM(el.line_total),0) total FROM categories c "
            "LEFT JOIN ("
            "  SELECT el.category_id, (el.amount + el.vat_amount) AS line_total FROM expense_lines el "
            "  JOIN expenses e ON e.id = el.expense_id "
            "  WHERE e.status='approved' AND e.expense_date >= ?"
            ") el ON el.category_id = c.id "
            "GROUP BY c.id ORDER BY total DESC", (month_start,)
        ).fetchall()
        monthly_trend = conn.execute(
            "SELECT strftime('%Y-%m', e.expense_date) ym, SUM(el.amount + el.vat_amount) total "
            "FROM expense_lines el JOIN expenses e ON e.id = el.expense_id "
            "WHERE e.status='approved' "
            "GROUP BY ym ORDER BY ym DESC LIMIT 6"
        ).fetchall()
        recent = conn.execute(
            f"SELECT e.*, {_TOTALS_SQL} FROM expenses e "
            "WHERE e.status NOT IN ('deleted', 'draft') "
            "ORDER BY e.created_at DESC LIMIT 8"
        ).fetchall()
        # Cash float balance for dashboard widget.
        float_row = conn.execute("SELECT balance FROM cash_float WHERE id=1").fetchone()
        float_balance = float_row["balance"] if float_row else None
        return {
            "month_total": month_total, "pending_count": pending_count,
            "pending_total": pending_total, "draft_count": draft_count,
            "by_category": by_category,
            "monthly_trend": list(reversed(monthly_trend)), "recent": recent,
            "float_balance": float_balance,
        }


def save_receipt(filename, file_obj):
    """Validates and saves an uploaded receipt, returning its stored filename.
    Reads `file_obj` in bounded chunks with the size limit enforced DURING
    reading, not after - an unbounded upload can't exhaust memory before the
    size check ever runs. Raises ValidationError for a disallowed extension,
    an oversized file, or content that doesn't match a real file signature
    for its claimed type."""
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        raise ValidationError(
            f"Receipt must be an image or PDF ({', '.join(sorted(ALLOWED_UPLOAD_EXTENSIONS))}).")

    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    chunks = []
    total = 0
    while True:
        chunk = file_obj.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ValidationError(f"Receipt file is too large (max {MAX_UPLOAD_MB} MB).")
        chunks.append(chunk)
    contents = b"".join(chunks)

    sandbox.validate_file_signature(ext, contents)

    safe_name = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(4)}{ext}"
    dest = sandbox.safe_join(UPLOADS_DIR, safe_name)
    dest.write_bytes(contents)
    sandbox.set_nonexecutable(dest)
    return safe_name


def _pickable_asset_count(conn, user_id, asset_category=None):
    """How many assets this person could actually choose from, which is what
    decides whether an asset is *demanded* on a maintenance line (see
    _validate_lines).

    Someone who can manage or assign assets (a manager, and an accountant by
    default) picks from the whole registry - they're recording on behalf of
    the business, not against their own truck. Everyone else picks only from
    assets currently assigned to them, resolved through
    employees.user_id -> asset_assignments. An employee record that was never
    linked to a login account, or a driver holding nothing right now, yields
    zero - and a zero here means "don't ask", not "deny".

    Written as a query here rather than imported from
    app/programs/assets/services/assets.py on purpose: expense_lines.asset_id
    is the one deliberate seam between the two programs, and a plain
    parameterised read keeps it a seam rather than a package dependency
    running the wrong way (see CHANGE_IMPACT_GUIDE.md's program-isolation
    notes). It reads the asset tables and writes nothing to them.
    """
    if user_id is None:
        return 0
    row = conn.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        return 0
    role = row["role"]

    params = []
    sql = ("SELECT COUNT(*) c FROM assets a "
           "WHERE a.status NOT IN ('deleted','divested','retired')")
    if asset_category:
        sql += " AND a.category = ?"
        params.append(asset_category)

    if not (auth.can_manage_assets(role) or auth.can_assign_asset(role)):
        sql += (" AND EXISTS (SELECT 1 FROM asset_assignments asg "
                "JOIN employees e ON e.id = asg.employee_id "
                "WHERE asg.asset_id = a.id AND asg.status = 'active' "
                "AND e.user_id = ?)")
        params.append(user_id)

    return conn.execute(sql, tuple(params)).fetchone()["c"]


def _validate_lines(conn, lines, *, strict=True, created_by=None):
    """strict=True (submitting for real): at least one line required, every
    line needs particulars, a positive amount, a valid category, and - if
    marked VAT-able - a resolved supplier (see below). strict=False
    (saving as a draft): zero lines is fine, and a present line's amount
    may be zero (a placeholder while the real figure isn't known yet) - but
    a line that's there at all still needs a valid category, since
    expense_lines.category_id has never allowed NULL and loosening that
    would be a much bigger schema change for little real benefit (the
    category dropdown always has a value, even a default one, so this is
    never a hardship in practice). A VAT-able line's supplier is only
    *required* in strict mode, but resolved (looked up/created) in either
    mode whenever a name or VAT number was actually typed - so a draft can
    be saved with the field blank, but can't be saved with a half-typed
    supplier (name with no VAT or vice versa), draft or not.

    A line may carry an already-resolved `supplier_id` instead of raw
    `supplier_name`/`supplier_vat` text - submit_draft() re-validates a
    draft's already-stored lines this way, since by then there's no raw
    text left to resolve, only whatever supplier_id (or None) was already
    attached when the draft was saved."""
    if not lines:
        if strict:
            raise ValidationError("Add at least one transaction line before submitting.")
        return []
    cleaned = []
    for i, line in enumerate(lines, start=1):
        particulars = (line.get("particulars") or "").strip()
        if strict and not particulars:
            raise ValidationError(f"Line {i}: particulars is required.")
        try:
            amount = float(line.get("amount") or 0)
        except (TypeError, ValueError):
            if strict:
                raise ValidationError(f"Line {i}: amount must be a number.")
            amount = 0.0
        if strict and amount <= 0:
            raise ValidationError(f"Line {i}: amount must be greater than zero.")
        if amount < 0:
            raise ValidationError(f"Line {i}: amount can't be negative.")
        category_id = line.get("category_id")
        cat = conn.execute(
            "SELECT id FROM categories WHERE id=? AND active=1", (category_id,)
        ).fetchone()
        if not cat:
            raise ValidationError(f"Line {i}: please choose a valid category.")
        is_vatable = bool(line.get("is_vatable"))

        supplier_id = line.get("supplier_id")
        supplier_name = (line.get("supplier_name") or "").strip()
        supplier_vat = (line.get("supplier_vat") or "").strip()
        if supplier_id is None and (supplier_name or supplier_vat):
            try:
                supplier_id = suppliers_service.resolve_supplier(
                    conn, supplier_name, supplier_vat, created_by,
                )
            except ValidationError as e:
                raise ValidationError(f"Line {i}: {e}")
        if is_vatable and strict and not supplier_id:
            raise ValidationError(
                f"Line {i}: supplier name and VAT are required for a VAT-able line."
            )

        # --- Salary advance: require employee name + phone in strict mode ---
        cat_row = conn.execute(
            "SELECT name, requires_asset, asset_category FROM categories WHERE id=?",
            (category_id,),
        ).fetchone()
        cat_name = cat_row["name"] if cat_row else ""
        is_advance_line = cat_name == ADVANCE_CATEGORY_NAME

        employee_name = (line.get("employee_name") or "").strip()
        employee_phone = (line.get("employee_phone") or "").strip()
        employee_id = line.get("employee_id")  # already resolved (submit_draft path)

        if is_advance_line and strict:
            if not employee_name:
                raise ValidationError(
                    f"Line {i}: employee name is required for a salary advance."
                )
            if not employee_phone:
                raise ValidationError(
                    f"Line {i}: employee phone is required for a salary advance."
                )
            if employee_id is None:
                employee_id = _advances.get_or_create_employee(
                    conn, employee_name, employee_phone, created_by
                )

        # --- Asset link: which categories demand one is DATA, not code ---
        # categories.requires_asset / .asset_category are ticked by a manager
        # at Settings -> Categories, so a new maintenance category needs no
        # code change here at all. Three states, all handled below:
        #   requires_asset=1                  -> required in strict mode
        #   requires_asset=0 + asset_category -> optional, but validated if given
        #   requires_asset=0 + NULL           -> picker hidden; asset ignored
        # Fuel deliberately sits in the third state: it's a general operating
        # cost here, not an asset cost, so it never reaches an asset's TCO.
        requires_asset = bool(cat_row["requires_asset"]) if cat_row else False
        wanted_asset_category = cat_row["asset_category"] if cat_row else None
        asset_id = line.get("asset_id")
        if asset_id in ("", "None"):
            asset_id = None
        if asset_id is not None:
            try:
                asset_id = int(asset_id)
            except (TypeError, ValueError):
                raise ValidationError(f"Line {i}: please choose a valid asset.")
            # Validated even when only optional - a picker that quietly accepts
            # a divested truck writes cost history onto a dead asset.
            params = [asset_id]
            sql = ("SELECT id FROM assets WHERE id=? "
                   "AND status NOT IN ('deleted','divested')")
            if wanted_asset_category:
                sql += " AND category=?"
                params.append(wanted_asset_category)
            if not conn.execute(sql, tuple(params)).fetchone():
                raise ValidationError(
                    f"Line {i}: please choose a valid asset for this category."
                )
        if requires_asset and strict and asset_id is None:
            # A driver with no truck assigned to them still has to be able to
            # record what they spent. The requirement therefore applies only
            # when this person actually HAS something to pick: if their picker
            # would be empty, the line is accepted unlinked and the voucher
            # goes to pending exactly as normal, for a manager or accountant
            # to check at approval time (they can attach the asset then).
            #
            # This is not a loophole a person can walk through at will - the
            # moment an asset is assigned to them, their picker is non-empty
            # and the field is required again. Unlinked lines are also listed
            # on the review screen and in the asset reports, so cost that
            # never reached an asset is visible rather than silently lost.
            if _pickable_asset_count(conn, created_by, wanted_asset_category):
                raise ValidationError(
                    f"Line {i}: an asset is required for {cat_name}."
                )

        cleaned.append({
            "category_id": category_id, "particulars": particulars,
            "amount": amount, "is_vatable": is_vatable, "supplier_id": supplier_id,
            "employee_id": employee_id,
            "employee_name": employee_name or None,
            "employee_phone": employee_phone or None,
            "is_advance_line": is_advance_line,
            "asset_id": asset_id,
            # True when this category wanted an asset and none was attached -
            # i.e. the person had nothing to pick. Surfaced at review so a
            # manager can attach one before approving.
            "asset_unlinked": bool(requires_asset and asset_id is None),
        })
    return cleaned


def _validate_cost_center(conn, cost_center_id):
    if not cost_center_id:
        return None
    cc = conn.execute("SELECT id FROM cost_centers WHERE id=? AND active=1", (cost_center_id,)).fetchone()
    if not cc:
        raise ValidationError("Please choose a valid cost centre.")
    return int(cost_center_id)


def _write_lines(conn, expense_id, cleaned_lines):
    """Deletes whatever lines a voucher currently has and writes the given
    set fresh - replace-the-whole-set is simpler and safer than trying to
    diff/patch individual lines, and it's what both initial creation and
    draft edits use."""
    conn.execute("DELETE FROM expense_lines WHERE expense_id = ?", (expense_id,))
    total_amount, total_vat = 0.0, 0.0
    for line_no, line in enumerate(cleaned_lines, start=1):
        vat_rate, vat_amount = compute_vat(line["amount"], line["is_vatable"])
        conn.execute(
            "INSERT INTO expense_lines (expense_id, line_no, category_id, particulars, "
            "amount, is_vatable, vat_rate, vat_amount, supplier_id, "
            "employee_id, employee_name, employee_phone, asset_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (expense_id, line_no, line["category_id"], line["particulars"],
             line["amount"], int(line["is_vatable"]), vat_rate, vat_amount, line.get("supplier_id"),
             line.get("employee_id"), line.get("employee_name"), line.get("employee_phone"),
             line.get("asset_id")),
        )
        total_amount += line["amount"]
        total_vat += vat_amount
    return round(total_amount, 2), round(total_vat, 2)


def create_expense(*, expense_date, cost_center_id, paid_to, payment_mode,
                    receipt_path, user_id, lines, is_draft=False):
    """Creates a new voucher. If is_draft, saves whatever's given with
    relaxed validation, no voucher number, status='draft'. Otherwise
    validates strictly, allocates a real voucher number, and sets
    status='pending' (submitted for approval). Returns the new expense id."""
    if not is_draft and (not paid_to or not paid_to.strip()):
        raise ValidationError("Paid to is required.")

    with get_db() as conn:
        cleaned_lines = _validate_lines(conn, lines, strict=not is_draft, created_by=user_id)
        cc_id = _validate_cost_center(conn, cost_center_id)

        voucher_no = None
        status = "draft"
        if not is_draft:
            # Block submission into a locked period.
            month = expense_date[:7]  # YYYY-MM
            if _period_lock.check_locked(conn, month):
                raise ConflictError(
                    f"Period {month} is closed. New vouchers cannot be dated in a locked month."
                )
            voucher_no = next_voucher_number(conn, expense_date[:4])
            status = "pending"

        cur = conn.execute(
            "INSERT INTO expenses (voucher_no, expense_date, cost_center_id, "
            "paid_to, payment_mode, status, receipt_path, prepared_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (voucher_no, expense_date, cc_id, (paid_to or "").strip(), payment_mode,
             status, receipt_path, user_id),
        )
        expense_id = cur.lastrowid
        total_amount, total_vat = _write_lines(conn, expense_id, cleaned_lines)

        if is_draft:
            log_action(conn, user_id, "save_draft", json.dumps({
                "expense_id": expense_id, "lines": len(cleaned_lines),
            }))
        else:
            log_action(conn, user_id, "submit_expense", json.dumps({
                "voucher_no": voucher_no, "lines": len(cleaned_lines),
                "total_amount": total_amount, "total_vat": total_vat,
            }))
        return expense_id


def _require_draft_owned_by(conn, expense_id, acting_user):
    """Shared guard for update_draft/submit_draft/discard_draft: the record
    must exist, must still be a draft, and the caller must either be the
    person who created it or a manager (oversight/cleanup)."""
    row = conn.execute("SELECT * FROM expenses WHERE id=?", (expense_id,)).fetchone()
    if not row:
        raise NotFoundError("Voucher not found.")
    if row["status"] != "draft":
        raise ConflictError("This voucher is no longer a draft.")
    if row["prepared_by"] != acting_user["id"] and acting_user.get("role") != "manager":
        raise ForbiddenError("Only the person who started this draft (or a manager) can change it.")
    return row


def update_draft(expense_id, acting_user, *, expense_date, cost_center_id,
                  paid_to, payment_mode, receipt_path, lines):
    """Replaces a draft's header fields and every line with the given
    values. Still a draft afterward - use submit_draft() to move it to
    pending. receipt_path=None leaves the existing receipt untouched (the
    route only passes a new one when the user actually uploaded a
    replacement)."""
    with get_db() as conn:
        _require_draft_owned_by(conn, expense_id, acting_user)
        cleaned_lines = _validate_lines(conn, lines, strict=False, created_by=acting_user["id"])
        cc_id = _validate_cost_center(conn, cost_center_id)

        if receipt_path is not None:
            conn.execute(
                "UPDATE expenses SET expense_date=?, cost_center_id=?, paid_to=?, "
                "payment_mode=?, receipt_path=? WHERE id=?",
                (expense_date, cc_id, (paid_to or "").strip(), payment_mode, receipt_path, expense_id),
            )
        else:
            conn.execute(
                "UPDATE expenses SET expense_date=?, cost_center_id=?, paid_to=?, "
                "payment_mode=? WHERE id=?",
                (expense_date, cc_id, (paid_to or "").strip(), payment_mode, expense_id),
            )
        _write_lines(conn, expense_id, cleaned_lines)
        log_action(conn, acting_user["id"], "save_draft", json.dumps({
            "expense_id": expense_id, "lines": len(cleaned_lines),
        }))


def submit_draft(expense_id, acting_user):
    """Validates a draft's current contents strictly and, if it passes,
    allocates a real voucher number and moves it to 'pending'. Raises
    ValidationError (naming what's missing) if the draft isn't actually
    ready yet - nothing is changed in that case."""
    with get_db() as conn:
        row = _require_draft_owned_by(conn, expense_id, acting_user)
        if not row["paid_to"] or not row["paid_to"].strip():
            raise ValidationError("Paid to is required before submitting.")
        lines = _fetch_lines(conn, expense_id)
        # re-run strict validation against what's already stored - this
        # reuses the exact same rules a fresh submission would have to pass
        _validate_lines(conn, [
            {"category_id": l["category_id"], "particulars": l["particulars"],
             "amount": l["amount"], "is_vatable": l["is_vatable"], "supplier_id": l["supplier_id"],
             "employee_id": l.get("employee_id"), "employee_name": l.get("employee_name"),
             "employee_phone": l.get("employee_phone"), "asset_id": l.get("asset_id")}
            for l in lines
        ], strict=True, created_by=acting_user["id"])

        month = row["expense_date"][:7]
        if _period_lock.check_locked(conn, month):
            raise ConflictError(
                f"Period {month} is closed. Update the voucher date to an open period before submitting."
            )
        voucher_no = next_voucher_number(conn, row["expense_date"][:4])
        conn.execute(
            "UPDATE expenses SET status='pending', voucher_no=? WHERE id=?",
            (voucher_no, expense_id),
        )
        log_action(conn, acting_user["id"], "submit_expense", json.dumps({
            "voucher_no": voucher_no, "expense_id": expense_id,
        }))
        return voucher_no


def discard_draft(expense_id, acting_user):
    """Permanently deletes a draft - a real DELETE, not the soft-delete used
    for real vouchers, since a draft was never a committed financial record
    (no voucher number was ever allocated to it)."""
    with get_db() as conn:
        row = _require_draft_owned_by(conn, expense_id, acting_user)
        conn.execute("DELETE FROM expense_lines WHERE expense_id = ?", (expense_id,))
        conn.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
        log_action(conn, acting_user["id"], "discard_draft", json.dumps({
            "expense_id": expense_id, "paid_to": row["paid_to"],
        }))


def clone_expense(expense_id, acting_user):
    """Copies an existing voucher into a **new draft** owned by whoever asked
    for it, and returns the new id. The point is recurring spend: same shop,
    same lines, different day.

    What is deliberately *not* copied:
      - `voucher_no` - a number is allocated on submit, never inherited, or
        two vouchers would claim the same reference.
      - status/approval/rejection/deletion fields - the copy starts as an
        unsubmitted draft no matter what the source was.
      - `receipt_path` - a receipt is the evidence for one specific payment.
        Sharing the file between two vouchers would attach last month's
        receipt to this month's claim.
      - the source's VAT amounts - `_write_lines()` recomputes VAT at the
        current rate, because this is a new transaction happening now, not
        a reprint of the old one. A rate change since the original does not
        silently carry forward.

    The date is reset to today. Cloning someone else's *draft* is refused
    (it is their unfinished work); cloning a submitted voucher is open to
    anyone allowed to create vouchers, since that record is already part of
    the shared trail.
    """
    with get_db() as conn:
        src = conn.execute("SELECT * FROM expenses WHERE id=?", (expense_id,)).fetchone()
        if not src:
            raise NotFoundError("Voucher not found.")
        if src["status"] == "deleted":
            raise ConflictError("A deleted voucher cannot be copied.")
        if (src["status"] == "draft"
                and src["prepared_by"] != acting_user["id"]
                and acting_user.get("role") != "manager"):
            raise ForbiddenError("Only the person who started this draft (or a manager) can copy it.")

        src_lines = conn.execute(
            "SELECT category_id, particulars, amount, is_vatable, supplier_id, "
            "employee_id, employee_name, employee_phone, asset_id "
            "FROM expense_lines WHERE expense_id = ? ORDER BY line_no",
            (expense_id,),
        ).fetchall()

        cur = conn.execute(
            "INSERT INTO expenses (voucher_no, expense_date, cost_center_id, "
            "paid_to, payment_mode, status, receipt_path, prepared_by) "
            "VALUES (NULL, ?, ?, ?, ?, 'draft', NULL, ?)",
            (date.today().isoformat(), src["cost_center_id"], src["paid_to"],
             src["payment_mode"], acting_user["id"]),
        )
        new_id = cur.lastrowid

        # Reuses the same writer as creation and draft edits, so line
        # numbering and VAT computation can never drift from those paths.
        _write_lines(conn, new_id, [{
            "category_id": l["category_id"],
            "particulars": l["particulars"],
            "amount": l["amount"],
            "is_vatable": bool(l["is_vatable"]),
            "supplier_id": l["supplier_id"],
            "employee_id": l["employee_id"],
            "employee_name": l["employee_name"],
            "employee_phone": l["employee_phone"],
            "asset_id": l["asset_id"],
        } for l in src_lines])

        log_action(conn, acting_user["id"], "clone_expense", json.dumps({
            "source_expense_id": expense_id,
            "source_voucher_no": src["voucher_no"],
            "new_expense_id": new_id,
            "lines": len(src_lines),
        }))
        return new_id


def approve_expense(expense_id, user_id):
    """Returns (None, float_warning: bool). float_warning=True when balance
    goes negative after this approval - caller surfaces a warn banner."""
    with get_db() as conn:
        row = conn.execute("SELECT status, voucher_no FROM expenses WHERE id=?", (expense_id,)).fetchone()
        if not row:
            raise NotFoundError("Voucher not found.")
        if row["status"] != "pending":
            raise ConflictError(
                "This voucher can no longer be approved (it may already have been actioned by someone else).")
        # Compute voucher total (amount + vat_amount across all lines).
        total_row = conn.execute(
            "SELECT COALESCE(SUM(amount + vat_amount), 0) t FROM expense_lines WHERE expense_id=?",
            (expense_id,),
        ).fetchone()
        voucher_total = total_row["t"] if total_row else 0.0
        conn.execute(
            "UPDATE expenses SET status='approved', approved_by=?, "
            "approved_at=datetime('now') WHERE id=? AND status='pending'",
            (user_id, expense_id),
        )
        float_warning = _cash_ledger.record_approval_movement(conn, expense_id, voucher_total, user_id)
        log_action(conn, user_id, "approve_expense", row["voucher_no"])
        expense_date_val = conn.execute(
            "SELECT expense_date FROM expenses WHERE id=?", (expense_id,)
        ).fetchone()["expense_date"]
    # Budget over-run check AFTER commit so the new approved status is visible.
    over = _budgets.check_over_budget(expense_date_val)
    if over:
        with get_db() as conn2:
            for b in over:
                msg = (
                    f"Budget exceeded: {b['cost_center_name']} / {b['category_name']} "
                    f"for {b['period']} — {b['utilisation_pct']}% utilised."
                )
                _budgets.notify_managers(conn2, msg, "/expense-program/budgets")

    # Record advance ledger entries for any salary-advance lines.
    with get_db() as conn3:
        advance_lines = conn3.execute(
            "SELECT el.id, el.amount, el.employee_id FROM expense_lines el "
            "JOIN categories c ON c.id = el.category_id "
            "WHERE el.expense_id=? AND c.name=? AND el.employee_id IS NOT NULL",
            (expense_id, ADVANCE_CATEGORY_NAME),
        ).fetchall()
        for al in advance_lines:
            _advances.record_advance(
                conn3, al["employee_id"], al["id"], al["amount"], expense_id, user_id
            )
    return float_warning


def reject_expense(expense_id, user_id, reason):
    with get_db() as conn:
        row = conn.execute("SELECT status, voucher_no FROM expenses WHERE id=?", (expense_id,)).fetchone()
        if not row:
            raise NotFoundError("Voucher not found.")
        if row["status"] != "pending":
            raise ConflictError(
                "This voucher can no longer be rejected (it may already have been actioned by someone else).")
        total_row = conn.execute(
            "SELECT COALESCE(SUM(amount + vat_amount), 0) t FROM expense_lines WHERE expense_id=?",
            (expense_id,),
        ).fetchone()
        voucher_total = total_row["t"] if total_row else 0.0
        conn.execute(
            "UPDATE expenses SET status='rejected', approved_by=?, "
            "approved_at=datetime('now'), rejection_reason=? WHERE id=? AND status='pending'",
            (user_id, reason.strip(), expense_id),
        )
        _cash_ledger.record_rejection_movement(conn, expense_id, voucher_total, user_id)
        log_action(conn, user_id, "reject_expense",
                   json.dumps({"voucher_no": row["voucher_no"], "reason": reason.strip()}))


def delete_expense(expense_id, user_id, reason):
    """Soft-delete only - see module docstring in app/backup.py for why this
    app never hard-deletes financial records. (Drafts use discard_draft()
    instead, which does hard-delete - see that function's docstring.)"""
    if not reason or not reason.strip():
        raise ValidationError("A reason is required to delete a voucher.")
    with get_db() as conn:
        row = conn.execute(_DETAIL_QUERY + "WHERE e.id = ?", (expense_id,)).fetchone()
        if not row or row["status"] == "deleted":
            raise NotFoundError("Voucher not found or already deleted.")
        lines = _fetch_lines(conn, expense_id)
        snapshot = {
            "voucher_no": row["voucher_no"], "total_amount": row["total_amount"],
            "total_vat": row["total_vat"], "paid_to": row["paid_to"],
            "lines": [{"category": l["category_name"], "particulars": l["particulars"],
                       "amount": l["amount"], "is_vatable": bool(l["is_vatable"])} for l in lines],
            "status_before_delete": row["status"], "reason": reason.strip(),
        }
        conn.execute(
            "UPDATE expenses SET status='deleted', deleted_by=?, deleted_at=datetime('now'), "
            "delete_reason=? WHERE id=?",
            (user_id, reason.strip(), expense_id),
        )
        log_action(conn, user_id, "delete_expense", json.dumps(snapshot))


# ---------------------------------------------------------------------------
# Bulk actions (the floating toolbar on the Expenses list)
# ---------------------------------------------------------------------------
#
# Every bulk function returns a BulkResult instead of raising. A batch is
# partially-successful by nature - one voucher in a selection of twenty may
# have been approved by a colleague two seconds ago - and an exception can
# only say "the whole thing failed", which would be a lie and would throw
# away the nineteen that worked. The caller renders "17 approved, 3 skipped"
# from this; see app/services/errors.py for why *single*-record operations
# still raise instead.
#
# Each id is re-checked against the same single-record rules rather than
# trusted because it arrived in a POST body: a bulk endpoint must not become
# a way around a status check that the per-record route enforces.

class BulkResult:
    """Outcome of one bulk action: which ids succeeded, and why each other
    id did not."""

    __slots__ = ("ok", "failed")

    def __init__(self):
        self.ok = []            # [expense_id, ...]
        self.failed = []        # [{"id": int, "reason": str}, ...]

    def add_ok(self, expense_id):
        self.ok.append(int(expense_id))

    def add_failure(self, expense_id, reason):
        self.failed.append({"id": int(expense_id), "reason": str(reason)})

    @property
    def ok_count(self):
        return len(self.ok)

    @property
    def failed_count(self):
        return len(self.failed)

    def summary(self, verb_past: str) -> str:
        """One-line toast text: "3 vouchers approved. 1 skipped."."""
        noun = "voucher" if self.ok_count == 1 else "vouchers"
        text = f"{self.ok_count} {noun} {verb_past}."
        if self.failed_count:
            text += f" {self.failed_count} skipped."
        return text

    def as_dict(self):
        return {"ok": list(self.ok), "failed": list(self.failed)}


MAX_BULK_IDS = 200


def _clean_bulk_ids(expense_ids):
    """De-duplicate, drop anything non-numeric, preserve order. Raises only
    for the two cases that are a caller mistake rather than a per-record
    outcome: nothing selected, and a selection too large to be a real
    review."""
    seen = []
    for raw in (expense_ids or []):
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value not in seen:
            seen.append(value)
    if not seen:
        raise ValidationError("Select at least one voucher first.")
    if len(seen) > MAX_BULK_IDS:
        raise ValidationError(
            f"Too many vouchers selected at once (limit {MAX_BULK_IDS}).")
    return seen


def bulk_approve(expense_ids, user_id):
    """Approve each selected voucher. Returns (BulkResult, float_warning).

    Delegates to approve_expense() per id rather than writing one bulk
    UPDATE: approval is not just a status flip - it moves the cash float,
    writes advance ledger rows and fires budget notifications, and a bulk
    statement would silently skip all of that."""
    ids = _clean_bulk_ids(expense_ids)
    result = BulkResult()
    float_warning = False
    for expense_id in ids:
        try:
            if approve_expense(expense_id, user_id):
                float_warning = True
            result.add_ok(expense_id)
        except (NotFoundError, ConflictError, ValidationError, ForbiddenError) as e:
            result.add_failure(expense_id, str(e))
    return result, float_warning


def bulk_reject(expense_ids, user_id, reason):
    """Reject each selected voucher with one shared reason."""
    if not reason or not reason.strip():
        raise ValidationError("A reason is required to reject vouchers.")
    ids = _clean_bulk_ids(expense_ids)
    result = BulkResult()
    for expense_id in ids:
        try:
            reject_expense(expense_id, user_id, reason)
            result.add_ok(expense_id)
        except (NotFoundError, ConflictError, ValidationError, ForbiddenError) as e:
            result.add_failure(expense_id, str(e))
    return result


def bulk_delete(expense_ids, user_id, reason):
    """Soft-delete each selected voucher with one shared reason. Manager-only
    at the route layer, same as the single-record delete - and soft-delete
    only here too, for the same financial-records reason."""
    if not reason or not reason.strip():
        raise ValidationError("A reason is required to delete vouchers.")
    ids = _clean_bulk_ids(expense_ids)
    result = BulkResult()
    for expense_id in ids:
        try:
            delete_expense(expense_id, user_id, reason)
            result.add_ok(expense_id)
        except (NotFoundError, ConflictError, ValidationError, ForbiddenError) as e:
            result.add_failure(expense_id, str(e))
    return result


# ---------------------------------------------------------------------------
# Undo (reversal of an approve / reject / delete, via app/undo.py)
# ---------------------------------------------------------------------------
#
# Three rules hold across all three reversals:
#
# 1. Nothing is erased. Each reversal writes its own audit entry next to the
#    original action, and the cash ledger gets a reversing movement rather
#    than having the original row deleted. "Undo" here means "put the record
#    back", never "pretend it did not happen".
#
# 2. The current status is re-checked. A token proves the user performed the
#    action ten seconds ago; it does not prove nobody has touched the record
#    since. Undoing an approval that somebody has already rejected would
#    quietly overwrite their decision, so it raises instead.
#
# 3. Derived side effects unwind or the undo refuses outright - it never
#    half-reverses. See advances.remove_advance_rows_for_expense().

def revert_approval(expense_id, user_id):
    """Put an approved voucher back to pending."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT status, voucher_no FROM expenses WHERE id=?", (expense_id,)
        ).fetchone()
        if not row:
            raise NotFoundError("Voucher not found.")
        if row["status"] != "approved":
            raise ConflictError(
                "This approval can no longer be undone - the voucher has changed since.")
        if not _advances.remove_advance_rows_for_expense(conn, expense_id, user_id):
            raise ConflictError(
                "This voucher created a salary advance that has already been settled - "
                "undo is no longer safe. Reject the voucher instead.")
        total_row = conn.execute(
            "SELECT COALESCE(SUM(amount + vat_amount), 0) t FROM expense_lines WHERE expense_id=?",
            (expense_id,),
        ).fetchone()
        voucher_total = total_row["t"] if total_row else 0.0
        conn.execute(
            "UPDATE expenses SET status='pending', approved_by=NULL, approved_at=NULL "
            "WHERE id=? AND status='approved'",
            (expense_id,),
        )
        _cash_ledger.record_undo_movement(
            conn, expense_id, voucher_total, user_id,
            f"Approval undone for {row['voucher_no'] or 'voucher'} - amount restored",
        )
        log_action(conn, user_id, "undo_approve_expense", row["voucher_no"])


def revert_rejection(expense_id, user_id):
    """Put a rejected voucher back to pending."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT status, voucher_no FROM expenses WHERE id=?", (expense_id,)
        ).fetchone()
        if not row:
            raise NotFoundError("Voucher not found.")
        if row["status"] != "rejected":
            raise ConflictError(
                "This rejection can no longer be undone - the voucher has changed since.")
        total_row = conn.execute(
            "SELECT COALESCE(SUM(amount + vat_amount), 0) t FROM expense_lines WHERE expense_id=?",
            (expense_id,),
        ).fetchone()
        voucher_total = total_row["t"] if total_row else 0.0
        conn.execute(
            "UPDATE expenses SET status='pending', approved_by=NULL, approved_at=NULL, "
            "rejection_reason=NULL WHERE id=? AND status='rejected'",
            (expense_id,),
        )
        # The rejection had put the amount back in the float; undoing it takes
        # that back out, leaving the float exactly where it was pre-rejection.
        _cash_ledger.record_undo_movement(
            conn, expense_id, -voucher_total, user_id,
            f"Rejection undone for {row['voucher_no'] or 'voucher'}",
        )
        log_action(conn, user_id, "undo_reject_expense", row["voucher_no"])


def restore_deleted(expense_id, user_id, previous_status="pending"):
    """Bring a soft-deleted voucher back to the status it held before.

    `previous_status` comes from the undo token, which delete_expense()'s
    caller captured at the moment of deletion - the row itself does not keep
    it. Anything unexpected falls back to 'pending', the one status that is
    always safe to land in (it re-enters the approval queue rather than
    quietly counting toward totals)."""
    if previous_status not in ("pending", "approved", "rejected"):
        previous_status = "pending"
    with get_db() as conn:
        row = conn.execute(
            "SELECT status, voucher_no FROM expenses WHERE id=?", (expense_id,)
        ).fetchone()
        if not row:
            raise NotFoundError("Voucher not found.")
        if row["status"] != "deleted":
            raise ConflictError("This voucher is not deleted.")
        conn.execute(
            "UPDATE expenses SET status=?, deleted_by=NULL, deleted_at=NULL, "
            "delete_reason=NULL WHERE id=? AND status='deleted'",
            (previous_status, expense_id),
        )
        log_action(conn, user_id, "undo_delete_expense",
                   json.dumps({"voucher_no": row["voucher_no"],
                               "restored_to": previous_status}))


def statuses_for(expense_ids):
    """{id: status} for a set of vouchers. Used by the route layer to capture
    what a record was *before* a destructive action, so the undo token can
    put it back."""
    ids = [int(i) for i in (expense_ids or [])]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT id, status FROM expenses WHERE id IN ({placeholders})", ids
        ).fetchall()
    return {r["id"]: r["status"] for r in rows}
