"""Tests for app/programs/expenses/services/budgets.py."""
import pytest
from datetime import date

from app.programs.expenses.services import budgets as svc
from app.programs.expenses.services import expenses as exp_svc
from app.services.errors import ValidationError, NotFoundError
from app.database import get_connection
from tests.conftest import first_category_id, first_cost_center_id


def _this_month() -> str:
    return date.today().strftime("%Y-%m")


def _approve_expense(amount=100.0):
    eid = exp_svc.create_expense(
        expense_date=date.today().isoformat(),
        cost_center_id=str(first_cost_center_id()),
        paid_to="Vendor",
        payment_mode="Cash",
        receipt_path=None,
        user_id=1,
        lines=[{
            "category_id": first_category_id(),
            "particulars": "Budget test",
            "amount": amount,
            "is_vatable": False,
            "supplier_name": "",
            "supplier_vat": "",
        }],
    )
    exp_svc.approve_expense(eid, user_id=1)
    return eid


# --- set_budget ---

def test_set_budget_happy_path():
    bid = svc.set_budget(_this_month(), 1000.0, acting_user_id=1)
    assert bid > 0


def test_set_budget_invalid_period_format():
    with pytest.raises(ValidationError):
        svc.set_budget("202501", 500.0, acting_user_id=1)


def test_set_budget_zero_amount_rejected():
    with pytest.raises(ValidationError):
        svc.set_budget(_this_month(), 0, acting_user_id=1)


def test_set_budget_negative_amount_rejected():
    with pytest.raises(ValidationError):
        svc.set_budget(_this_month(), -100.0, acting_user_id=1)


def test_set_budget_upserts_existing():
    svc.set_budget(_this_month(), 500.0, acting_user_id=1)
    svc.set_budget(_this_month(), 750.0, acting_user_id=1)
    rows = svc.list_budgets(_this_month())
    assert len(rows) == 1
    assert rows[0]["amount"] == 750.0


def test_set_budget_with_cost_center():
    cc_id = first_cost_center_id()
    bid = svc.set_budget(_this_month(), 2000.0, acting_user_id=1, cost_center_id=cc_id)
    assert bid > 0


def test_set_budget_with_unknown_cost_center_raises():
    with pytest.raises(NotFoundError):
        svc.set_budget(_this_month(), 500.0, acting_user_id=1, cost_center_id=99999)


def test_set_budget_with_unknown_category_raises():
    with pytest.raises(NotFoundError):
        svc.set_budget(_this_month(), 500.0, acting_user_id=1, category_id=99999)


# --- delete_budget ---

def test_delete_budget_removes_row():
    bid = svc.set_budget(_this_month(), 300.0, acting_user_id=1)
    svc.delete_budget(bid, acting_user_id=1)
    assert svc.list_budgets(_this_month()) == []


def test_delete_budget_not_found_raises():
    with pytest.raises(NotFoundError):
        svc.delete_budget(99999, acting_user_id=1)


# --- get_budget_vs_actual ---

def test_budget_vs_actual_no_spend():
    svc.set_budget(_this_month(), 1000.0, acting_user_id=1)
    rows = svc.get_budget_vs_actual(_this_month())
    assert len(rows) == 1
    assert rows[0]["actual"] == 0.0
    assert rows[0]["variance"] == pytest.approx(-1000.0)
    assert rows[0]["over_budget"] is False


def test_budget_vs_actual_with_approved_spend():
    svc.set_budget(_this_month(), 1000.0, acting_user_id=1)
    _approve_expense(amount=300.0)
    rows = svc.get_budget_vs_actual(_this_month())
    assert rows[0]["actual"] == pytest.approx(300.0)
    assert rows[0]["variance"] == pytest.approx(-700.0)
    assert rows[0]["utilisation_pct"] == 30.0


def test_budget_vs_actual_over_budget():
    svc.set_budget(_this_month(), 50.0, acting_user_id=1)
    _approve_expense(amount=100.0)
    rows = svc.get_budget_vs_actual(_this_month())
    assert rows[0]["over_budget"] is True
    assert rows[0]["utilisation_pct"] == 200.0


def test_budget_vs_actual_pending_not_counted():
    """Pending vouchers must not appear in actual spend."""
    svc.set_budget(_this_month(), 1000.0, acting_user_id=1)
    # Create but do NOT approve.
    exp_svc.create_expense(
        expense_date=date.today().isoformat(),
        cost_center_id=str(first_cost_center_id()),
        paid_to="Vendor", payment_mode="Cash",
        receipt_path=None, user_id=1,
        lines=[{
            "category_id": first_category_id(),
            "particulars": "Pending test",
            "amount": 500.0,
            "is_vatable": False,
            "supplier_name": "",
            "supplier_vat": "",
        }],
    )
    rows = svc.get_budget_vs_actual(_this_month())
    assert rows[0]["actual"] == 0.0


# --- check_over_budget ---

def test_check_over_budget_empty_when_under():
    svc.set_budget(_this_month(), 5000.0, acting_user_id=1)
    _approve_expense(100.0)
    over = svc.check_over_budget(date.today().isoformat())
    assert over == []


def test_check_over_budget_returns_breached_rows():
    svc.set_budget(_this_month(), 10.0, acting_user_id=1)
    _approve_expense(100.0)
    over = svc.check_over_budget(date.today().isoformat())
    assert len(over) == 1
    assert over[0]["over_budget"] is True


# --- notifications ---

def test_notify_managers_inserts_rows():
    from app.database import get_db
    with get_db() as conn:
        svc.notify_managers(conn, "Test alert", "/expense-program/budgets")
    notes = svc.get_notifications(user_id=1, unread_only=True)
    assert any(n["message"] == "Test alert" for n in notes)


def test_mark_notifications_read_clears_unread():
    from app.database import get_db
    with get_db() as conn:
        svc.notify_managers(conn, "Alert", "/expense-program/budgets")
    svc.mark_notifications_read(user_id=1)
    notes = svc.get_notifications(user_id=1, unread_only=True)
    assert notes == []


def test_approve_over_budget_fires_notification():
    """Approving a voucher that breaks a budget must generate a manager notification."""
    svc.set_budget(_this_month(), 10.0, acting_user_id=1)
    _approve_expense(amount=200.0)   # way over budget
    notes = svc.get_notifications(user_id=1, unread_only=True)
    assert len(notes) >= 1
    assert any("exceeded" in n["message"].lower() for n in notes)
