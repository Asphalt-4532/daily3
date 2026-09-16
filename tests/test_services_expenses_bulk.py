"""Bulk voucher actions and their reversals.

Kept in its own file rather than appended to the already-large
tests/test_services_expenses.py, for the same reason the services live in
their own section of expenses.py: these are a different shape of operation -
partially-successful by nature, and returning a result object instead of
raising.

Two things these are really protecting:

* A batch must never be all-or-nothing. One voucher somebody else actioned
  two seconds ago cannot be allowed to fail the other nineteen.
* A bulk endpoint must not be a softer door than the single-record one. Every
  id is re-checked against the same rules, and the tests below prove a
  non-pending voucher is skipped rather than force-approved.
"""
import pytest

from app.database import get_db
from app.programs.expenses.services import expenses as svc
from app.services.errors import ValidationError, ConflictError, NotFoundError
from tests.conftest import first_category_id, first_cost_center_id


def _voucher(created_by=1, amount=50.0, draft=False):
    """A real submitted voucher, via the service so these stay fast."""
    from datetime import date
    return svc.create_expense(
        expense_date=date.today().isoformat(),
        cost_center_id=first_cost_center_id(),
        paid_to="Corner Shop",
        payment_mode="Cash",
        lines=[{"category_id": first_category_id(),
                "particulars": "Tea and sugar",
                "amount": amount, "is_vatable": False}],
        user_id=created_by,
        receipt_path=None,
        is_draft=draft,
    )


def _status(expense_id):
    with get_db() as conn:
        return conn.execute(
            "SELECT status FROM expenses WHERE id=?", (expense_id,)
        ).fetchone()["status"]


# ── bulk approve ───────────────────────────────────────────────────────────

def test_bulk_approve_approves_every_pending_voucher(reset_db):
    ids = [_voucher(), _voucher(), _voucher()]
    result, _ = svc.bulk_approve(ids, 1)
    assert result.ok_count == 3
    assert result.failed_count == 0
    assert all(_status(i) == "approved" for i in ids)


def test_bulk_approve_skips_what_it_cannot_approve_and_keeps_going(reset_db):
    """The partial-success case. An already-approved voucher is reported as
    skipped; the rest still go through."""
    already = _voucher()
    svc.approve_expense(already, 1)
    fresh = [_voucher(), _voucher()]
    result, _ = svc.bulk_approve([already] + fresh, 1)
    assert result.ok_count == 2
    assert result.failed_count == 1
    assert result.failed[0]["id"] == already
    assert all(_status(i) == "approved" for i in fresh)


def test_bulk_approve_reports_a_missing_id_rather_than_raising(reset_db):
    result, _ = svc.bulk_approve([_voucher(), 999999], 1)
    assert result.ok_count == 1
    assert result.failed_count == 1


def test_bulk_approve_does_not_touch_a_draft(reset_db):
    """A draft was never submitted for approval. Approving one through the
    bulk door would create an approved voucher with no voucher number."""
    draft = _voucher(draft=True)
    result, _ = svc.bulk_approve([draft], 1)
    assert result.ok_count == 0
    assert _status(draft) == "draft"


def test_bulk_approve_moves_the_cash_float_once_per_voucher(reset_db):
    from app.programs.expenses.services import cash_ledger
    cash_ledger.top_up(1000.0, "float", 1)
    before = cash_ledger.get_balance()["balance"]
    ids = [_voucher(amount=50.0), _voucher(amount=50.0)]
    svc.bulk_approve(ids, 1)
    after = cash_ledger.get_balance()["balance"]
    assert round(before - after, 2) == 100.00


# ── bulk reject / delete ───────────────────────────────────────────────────

def test_bulk_reject_rejects_with_the_shared_reason(reset_db):
    ids = [_voucher(), _voucher()]
    result = svc.bulk_reject(ids, 1, "Wrong cost centre")
    assert result.ok_count == 2
    assert all(_status(i) == "rejected" for i in ids)


def test_bulk_reject_requires_a_reason(reset_db):
    """A caller mistake, not a per-record outcome - so this raises rather
    than coming back as twenty identical failures."""
    with pytest.raises(ValidationError):
        svc.bulk_reject([_voucher()], 1, "   ")


def test_bulk_delete_soft_deletes_only(reset_db):
    ids = [_voucher(), _voucher()]
    svc.bulk_delete(ids, 1, "Duplicated entry")
    assert all(_status(i) == "deleted" for i in ids)
    # Still there. Soft-delete is a financial-records decision; a bulk path
    # must not quietly become the hard-delete this app refuses to have.
    with get_db() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM expenses WHERE id IN (?, ?)",
                         ids).fetchone()["c"]
    assert n == 2


def test_bulk_delete_requires_a_reason(reset_db):
    with pytest.raises(ValidationError):
        svc.bulk_delete([_voucher()], 1, "")


# ── id handling ────────────────────────────────────────────────────────────

def test_empty_selection_raises(reset_db):
    with pytest.raises(ValidationError):
        svc.bulk_approve([], 1)


def test_duplicate_ids_are_collapsed(reset_db):
    """Otherwise the second copy would come back as a spurious failure -
    "1 approved, 1 skipped" for one voucher."""
    one = _voucher()
    result, _ = svc.bulk_approve([one, one, str(one)], 1)
    assert result.ok_count == 1
    assert result.failed_count == 0


def test_non_numeric_ids_are_dropped(reset_db):
    one = _voucher()
    result, _ = svc.bulk_approve([one, "banana", None], 1)
    assert result.ok_count == 1


def test_too_many_ids_raises(reset_db):
    with pytest.raises(ValidationError):
        svc.bulk_approve(list(range(svc.MAX_BULK_IDS + 1)), 1)


def test_summary_wording(reset_db):
    result = svc.BulkResult()
    result.add_ok(1)
    assert result.summary("approved") == "1 voucher approved."
    result.add_ok(2)
    result.add_failure(3, "already actioned")
    assert result.summary("approved") == "2 vouchers approved. 1 skipped."


# ── undo: revert approval ──────────────────────────────────────────────────

def test_revert_approval_puts_it_back_to_pending(reset_db):
    one = _voucher()
    svc.approve_expense(one, 1)
    svc.revert_approval(one, 1)
    assert _status(one) == "pending"
    with get_db() as conn:
        row = conn.execute("SELECT approved_by, approved_at FROM expenses WHERE id=?",
                           (one,)).fetchone()
    assert row["approved_by"] is None and row["approved_at"] is None


def test_revert_approval_restores_the_cash_float(reset_db):
    from app.programs.expenses.services import cash_ledger
    cash_ledger.top_up(1000.0, "float", 1)
    before = cash_ledger.get_balance()["balance"]
    one = _voucher(amount=120.0)
    svc.approve_expense(one, 1)
    svc.revert_approval(one, 1)
    assert round(cash_ledger.get_balance()["balance"], 2) == round(before, 2)


def test_revert_approval_leaves_the_original_movement_in_the_ledger(reset_db):
    """Undo means "put the record back", never "pretend it did not happen".
    The approval and its reversal both stay visible."""
    from app.programs.expenses.services import cash_ledger
    one = _voucher()
    svc.approve_expense(one, 1)
    svc.revert_approval(one, 1)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT movement_type FROM cash_movements WHERE reference_id=?", (one,)
        ).fetchall()
    types = [r["movement_type"] for r in rows]
    assert "voucher_approved" in types
    assert "manual_adjustment" in types


def test_revert_approval_writes_its_own_audit_entry(reset_db):
    one = _voucher()
    svc.approve_expense(one, 1)
    svc.revert_approval(one, 1)
    with get_db() as conn:
        actions = [r["action"] for r in conn.execute(
            "SELECT action FROM audit_log").fetchall()]
    assert "approve_expense" in actions
    assert "undo_approve_expense" in actions


def test_revert_approval_refuses_if_the_voucher_moved_on(reset_db):
    """A token proves who acted ten seconds ago. It does not prove nobody
    has touched the record since - undoing over somebody else's decision
    would be worse than refusing."""
    one = _voucher()
    svc.approve_expense(one, 1)
    svc.revert_approval(one, 1)          # back to pending
    svc.reject_expense(one, 1, "no")     # someone else acts
    with pytest.raises(ConflictError):
        svc.revert_approval(one, 1)


def test_revert_approval_on_a_missing_voucher_raises_not_found(reset_db):
    with pytest.raises(NotFoundError):
        svc.revert_approval(999999, 1)


# ── undo: revert rejection ─────────────────────────────────────────────────

def test_revert_rejection_puts_it_back_to_pending_and_clears_the_reason(reset_db):
    one = _voucher()
    svc.reject_expense(one, 1, "Wrong cost centre")
    svc.revert_rejection(one, 1)
    assert _status(one) == "pending"
    with get_db() as conn:
        row = conn.execute("SELECT rejection_reason FROM expenses WHERE id=?",
                           (one,)).fetchone()
    assert not row["rejection_reason"]


def test_revert_rejection_leaves_the_float_where_it_started(reset_db):
    """Rejecting put the amount back; undoing the rejection takes it out
    again, landing exactly where the voucher was pre-rejection."""
    from app.programs.expenses.services import cash_ledger
    cash_ledger.top_up(1000.0, "float", 1)
    one = _voucher(amount=75.0)
    before = cash_ledger.get_balance()["balance"]
    svc.reject_expense(one, 1, "nope")
    svc.revert_rejection(one, 1)
    assert round(cash_ledger.get_balance()["balance"], 2) == round(before, 2)


def test_revert_rejection_refuses_an_approved_voucher(reset_db):
    one = _voucher()
    svc.approve_expense(one, 1)
    with pytest.raises(ConflictError):
        svc.revert_rejection(one, 1)


# ── undo: restore a deleted voucher ────────────────────────────────────────

def test_restore_deleted_returns_it_to_its_previous_status(reset_db):
    one = _voucher()
    svc.approve_expense(one, 1)
    svc.delete_expense(one, 1, "Recorded twice")
    svc.restore_deleted(one, 1, "approved")
    assert _status(one) == "approved"


def test_restore_deleted_clears_the_deletion_trail_on_the_row(reset_db):
    one = _voucher()
    svc.delete_expense(one, 1, "oops")
    svc.restore_deleted(one, 1, "pending")
    with get_db() as conn:
        row = conn.execute(
            "SELECT deleted_by, deleted_at, delete_reason FROM expenses WHERE id=?",
            (one,)).fetchone()
    assert row["deleted_by"] is None
    assert row["deleted_at"] is None
    assert not row["delete_reason"]


def test_restore_deleted_keeps_the_deletion_in_the_audit_log(reset_db):
    """The row is clean again, but what happened is not erased."""
    one = _voucher()
    svc.delete_expense(one, 1, "oops")
    svc.restore_deleted(one, 1, "pending")
    with get_db() as conn:
        actions = [r["action"] for r in conn.execute(
            "SELECT action FROM audit_log").fetchall()]
    assert "delete_expense" in actions
    assert "undo_delete_expense" in actions


def test_restore_deleted_falls_back_to_pending_for_an_odd_status(reset_db):
    """Pending is the one status always safe to land in - it re-enters the
    approval queue rather than quietly counting toward totals."""
    one = _voucher()
    svc.delete_expense(one, 1, "oops")
    svc.restore_deleted(one, 1, "nonsense")
    assert _status(one) == "pending"


def test_restore_deleted_refuses_a_voucher_that_is_not_deleted(reset_db):
    one = _voucher()
    with pytest.raises(ConflictError):
        svc.restore_deleted(one, 1, "pending")


def test_statuses_for_captures_what_a_record_was(reset_db):
    """The row does not keep its pre-delete status, so the route has to
    capture it first - this is what makes "restore to approved" possible."""
    one = _voucher()
    svc.approve_expense(one, 1)
    assert svc.statuses_for([one]) == {one: "approved"}
    assert svc.statuses_for([]) == {}
