"""Tests for app/programs/expenses/services/sharing.py, called directly.

Sharing replaced "email it to the accountant", so what matters here is that
the hand-off is recorded, the recipient is actually told, and neither can
happen against a record where it would be meaningless (a draft, a deleted
voucher, a dead account).
"""
import pytest
from datetime import date

from app.programs.expenses.services import expenses as expenses_svc
from app.programs.expenses.services import sharing as svc
from app.services import notifications as notifications_svc
from app.services import users as users_service
from app.services.errors import ValidationError, NotFoundError, ConflictError
from app.database import get_connection
from tests.conftest import first_category_id, first_cost_center_id


def _manager():
    conn = get_connection()
    try:
        return dict(conn.execute("SELECT * FROM users WHERE role='manager' LIMIT 1").fetchone())
    finally:
        conn.close()


def _make_accountant(username="book_keeper", full_name="Book Keeper"):
    users_service.create_user(
        username=username, full_name=full_name, password="pass1234",
        role="accountant", acting_user_id=1,
    )
    conn = get_connection()
    try:
        return dict(conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone())
    finally:
        conn.close()


def _submitted_voucher(user_id=1):
    return expenses_svc.create_expense(
        expense_date=date.today().isoformat(),
        cost_center_id=str(first_cost_center_id()),
        paid_to="Corner Shop", payment_mode="Cash", receipt_path=None,
        user_id=user_id,
        lines=[{"category_id": first_category_id(), "particulars": "Tea",
                "amount": 40.0, "is_vatable": False}],
        is_draft=False,
    )


def _draft_voucher(user_id=1):
    return expenses_svc.create_expense(
        expense_date=date.today().isoformat(),
        cost_center_id=str(first_cost_center_id()),
        paid_to="", payment_mode="Cash", receipt_path=None,
        user_id=user_id, lines=[], is_draft=True,
    )


def test_share_records_the_handoff_and_notifies():
    acct = _make_accountant()
    eid = _submitted_voucher()

    count = svc.share_voucher(eid, [acct["id"]], "please book to April", _manager())

    assert count == 1
    shares = svc.list_shares(eid)
    assert len(shares) == 1
    assert shares[0]["shared_with_name"] == "Book Keeper"
    assert shares[0]["note"] == "please book to April"

    notes = notifications_svc.list_for_user(acct["id"])
    assert len(notes) == 1
    # The link is what makes this useful - the recipient lands on the
    # voucher, where the PDF and receipt already are.
    assert notes[0]["link"] == f"/expense-program/expenses/{eid}"
    assert "please book to April" in notes[0]["message"]


def test_share_with_several_people_at_once():
    a = _make_accountant("acct_a", "Acct A")
    b = _make_accountant("acct_b", "Acct B")
    eid = _submitted_voucher()

    assert svc.share_voucher(eid, [a["id"], b["id"]], "", _manager()) == 2
    assert len(svc.list_shares(eid)) == 2
    assert notifications_svc.count_unread(a["id"]) == 1
    assert notifications_svc.count_unread(b["id"]) == 1


def test_duplicate_ids_in_one_request_only_notify_once():
    acct = _make_accountant()
    eid = _submitted_voucher()
    assert svc.share_voucher(eid, [acct["id"], acct["id"]], "", _manager()) == 1
    assert notifications_svc.count_unread(acct["id"]) == 1


def test_a_draft_cannot_be_shared():
    """A draft has no voucher number and is not in anyone else's lists -
    sharing it would point the recipient at nothing they can act on."""
    acct = _make_accountant()
    did = _draft_voucher()
    with pytest.raises(ConflictError):
        svc.share_voucher(did, [acct["id"]], "", _manager())
    assert notifications_svc.count_unread(acct["id"]) == 0


def test_a_deleted_voucher_cannot_be_shared():
    acct = _make_accountant()
    eid = _submitted_voucher()
    expenses_svc.approve_expense(eid, 1)
    expenses_svc.delete_expense(eid, 1, "entered twice")
    with pytest.raises(ConflictError):
        svc.share_voucher(eid, [acct["id"]], "", _manager())


def test_sharing_with_nobody_is_rejected():
    eid = _submitted_voucher()
    with pytest.raises(ValidationError):
        svc.share_voucher(eid, [], "", _manager())


def test_sharing_with_yourself_is_rejected():
    eid = _submitted_voucher()
    mgr = _manager()
    with pytest.raises(ValidationError):
        svc.share_voucher(eid, [mgr["id"]], "", mgr)


def test_sharing_with_an_unknown_account_is_rejected_and_writes_nothing():
    acct = _make_accountant()
    eid = _submitted_voucher()
    with pytest.raises(ValidationError):
        svc.share_voucher(eid, [acct["id"], 99999], "", _manager())
    # Atomicity: the valid half of the request must not have landed either.
    assert svc.list_shares(eid) == []
    assert notifications_svc.count_unread(acct["id"]) == 0


def test_sharing_with_a_deactivated_account_is_rejected():
    acct = _make_accountant()
    users_service.toggle_user(acct["id"], acting_user_id=1)  # active -> inactive
    eid = _submitted_voucher()
    with pytest.raises(ValidationError):
        svc.share_voucher(eid, [acct["id"]], "", _manager())


def test_unknown_voucher_is_not_found():
    acct = _make_accountant()
    with pytest.raises(NotFoundError):
        svc.share_voucher(99999, [acct["id"]], "", _manager())


def test_a_note_longer_than_the_limit_is_rejected():
    acct = _make_accountant()
    eid = _submitted_voucher()
    with pytest.raises(ValidationError):
        svc.share_voucher(eid, [acct["id"]], "x" * (svc.MAX_NOTE_LENGTH + 1), _manager())


def test_non_numeric_recipient_is_rejected_not_crashed():
    """The route hands over raw form strings, so junk has to be a
    ValidationError rather than a 500."""
    eid = _submitted_voucher()
    with pytest.raises(ValidationError):
        svc.share_voucher(eid, ["not-a-number"], "", _manager())


def test_shareable_users_excludes_the_sharer_and_prefers_accountants():
    _make_accountant()
    mgr = _manager()
    people = svc.list_shareable_users(mgr["id"])
    assert all(p["id"] != mgr["id"] for p in people)
    assert people[0]["role"] == "accountant"


def test_sharing_is_written_to_the_audit_log():
    acct = _make_accountant()
    eid = _submitted_voucher()
    svc.share_voucher(eid, [acct["id"]], "", _manager())
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM audit_log WHERE action='share_expense' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert "Book Keeper" in row["details"]
