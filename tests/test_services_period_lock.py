"""Tests for app/programs/expenses/services/period_lock.py."""
import pytest
from datetime import date

from app.programs.expenses.services import period_lock as svc
from app.programs.expenses.services import expenses as exp_svc
from app.services.errors import ConflictError, NotFoundError, ValidationError
from tests.conftest import first_category_id, first_cost_center_id


def _this_month() -> str:
    return date.today().strftime("%Y-%m")


def _make_expense(expense_date=None):
    d = expense_date or date.today().isoformat()
    return exp_svc.create_expense(
        expense_date=d,
        cost_center_id=str(first_cost_center_id()),
        paid_to="Vendor",
        payment_mode="Cash",
        receipt_path=None,
        user_id=1,
        lines=[{
            "category_id": first_category_id(),
            "particulars": "Test",
            "amount": 50.0,
            "is_vatable": False,
            "supplier_name": "",
            "supplier_vat": "",
        }],
    )


# --- is_locked ---

def test_month_not_locked_by_default():
    assert svc.is_locked(_this_month()) is False


def test_is_locked_true_after_lock():
    svc.lock_month(_this_month(), acting_user_id=1)
    assert svc.is_locked(_this_month()) is True


# --- lock_month ---

def test_lock_month_happy_path():
    svc.lock_month("2025-01", acting_user_id=1)
    assert svc.is_locked("2025-01") is True


def test_lock_month_twice_raises_conflict():
    svc.lock_month("2025-02", acting_user_id=1)
    with pytest.raises(ConflictError):
        svc.lock_month("2025-02", acting_user_id=1)


# --- unlock_month ---

def test_unlock_month_happy_path():
    svc.lock_month("2025-03", acting_user_id=1)
    svc.unlock_month("2025-03", acting_user_id=1)
    assert svc.is_locked("2025-03") is False


def test_unlock_month_not_locked_raises_not_found():
    with pytest.raises(NotFoundError):
        svc.unlock_month("2025-04", acting_user_id=1)


# --- list_locked_months ---

def test_list_locked_months_returns_all_locked():
    svc.lock_month("2025-05", acting_user_id=1)
    svc.lock_month("2025-06", acting_user_id=1)
    locked = svc.list_locked_months()
    months = [r["month"] for r in locked]
    assert "2025-05" in months
    assert "2025-06" in months


def test_list_locked_months_empty_initially():
    assert svc.list_locked_months() == []


# --- create_expense blocked by locked period ---

def test_create_expense_blocked_in_locked_month():
    month = _this_month()
    svc.lock_month(month, acting_user_id=1)
    with pytest.raises(ConflictError, match="closed"):
        _make_expense(expense_date=date.today().isoformat())


def test_create_draft_allowed_in_locked_month():
    """Drafts bypass the lock — the lock fires only on submission."""
    month = _this_month()
    svc.lock_month(month, acting_user_id=1)
    # save as draft: no exception expected
    eid = exp_svc.create_expense(
        expense_date=date.today().isoformat(),
        cost_center_id=str(first_cost_center_id()),
        paid_to="",
        payment_mode="Cash",
        receipt_path=None,
        user_id=1,
        lines=[],
        is_draft=True,
    )
    assert eid > 0


def test_submit_draft_blocked_in_locked_month():
    """Draft saved before lock, then lock applied, then submit blocked."""
    eid = exp_svc.create_expense(
        expense_date=date.today().isoformat(),
        cost_center_id=str(first_cost_center_id()),
        paid_to="Vendor",
        payment_mode="Cash",
        receipt_path=None,
        user_id=1,
        lines=[{
            "category_id": first_category_id(),
            "particulars": "Test",
            "amount": 50.0,
            "is_vatable": False,
            "supplier_name": "",
            "supplier_vat": "",
        }],
        is_draft=True,
    )
    svc.lock_month(_this_month(), acting_user_id=1)
    acting = {"id": 1, "role": "manager"}
    with pytest.raises(ConflictError, match="closed"):
        exp_svc.submit_draft(eid, acting)


def test_expense_allowed_in_unlocked_month():
    """After unlock, new expenses go through normally."""
    svc.lock_month("2020-01", acting_user_id=1)
    svc.unlock_month("2020-01", acting_user_id=1)
    eid = exp_svc.create_expense(
        expense_date="2020-01-15",
        cost_center_id=str(first_cost_center_id()),
        paid_to="Vendor",
        payment_mode="Cash",
        receipt_path=None,
        user_id=1,
        lines=[{
            "category_id": first_category_id(),
            "particulars": "Post-unlock test",
            "amount": 25.0,
            "is_vatable": False,
            "supplier_name": "",
            "supplier_vat": "",
        }],
    )
    assert eid > 0
