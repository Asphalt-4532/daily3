"""Tests for app/programs/expenses/services/cash_ledger.py."""
import pytest
from app.programs.expenses.services import cash_ledger as svc
from app.programs.expenses.services import expenses as exp_svc
from app.services.errors import ValidationError
from app.database import get_connection
from tests.conftest import first_category_id, first_cost_center_id
from datetime import date


def _make_expense(**overrides):
    kwargs = dict(
        expense_date=date.today().isoformat(),
        cost_center_id=str(first_cost_center_id()),
        paid_to="Test Vendor", payment_mode="Cash",
        receipt_path=None, user_id=1,
        lines=[{
            "category_id": first_category_id(),
            "particulars": "Test item",
            "amount": 100.0,
            "is_vatable": False,
            "supplier_name": "",
            "supplier_vat": "",
        }],
    )
    kwargs.update(overrides)
    return exp_svc.create_expense(**kwargs)


# --- get_balance ---

def test_get_balance_returns_zero_before_any_movement():
    b = svc.get_balance()
    assert b["balance"] == 0.0


# --- top_up ---

def test_top_up_increases_balance():
    svc.top_up(500.0, "Initial float", acting_user_id=1)
    assert svc.get_balance()["balance"] == 500.0


def test_top_up_records_movement():
    svc.top_up(200.0, "Top-up note", acting_user_id=1)
    movements = svc.get_movements()
    assert len(movements) == 1
    assert movements[0]["movement_type"] == "topup"
    assert movements[0]["amount"] == 200.0


def test_top_up_rejects_zero_amount():
    with pytest.raises(ValidationError):
        svc.top_up(0, "", acting_user_id=1)


def test_top_up_rejects_negative_amount():
    with pytest.raises(ValidationError):
        svc.top_up(-50.0, "", acting_user_id=1)


def test_multiple_topups_accumulate():
    svc.top_up(100.0, "First", acting_user_id=1)
    svc.top_up(250.0, "Second", acting_user_id=1)
    assert svc.get_balance()["balance"] == 350.0


# --- manual_adjust ---

def test_manual_adjust_positive():
    svc.manual_adjust(75.0, "Cash found", acting_user_id=1)
    assert svc.get_balance()["balance"] == 75.0


def test_manual_adjust_negative():
    svc.top_up(200.0, "Float", acting_user_id=1)
    svc.manual_adjust(-30.0, "Cash short", acting_user_id=1)
    assert svc.get_balance()["balance"] == pytest.approx(170.0)


def test_manual_adjust_requires_note():
    with pytest.raises(ValidationError):
        svc.manual_adjust(10.0, "", acting_user_id=1)


def test_manual_adjust_rejects_zero():
    with pytest.raises(ValidationError):
        svc.manual_adjust(0, "note", acting_user_id=1)


# --- approval deducts float ---

def test_approve_expense_deducts_from_float():
    svc.top_up(500.0, "Float", acting_user_id=1)
    eid = _make_expense()
    exp_svc.approve_expense(eid, user_id=1)
    # 100.0 line amount, no VAT — should be deducted.
    assert svc.get_balance()["balance"] == pytest.approx(400.0)


def test_approve_records_voucher_movement():
    svc.top_up(500.0, "Float", acting_user_id=1)
    eid = _make_expense()
    exp_svc.approve_expense(eid, user_id=1)
    movements = svc.get_movements()
    voucher_moves = [m for m in movements if m["movement_type"] == "voucher_approved"]
    assert len(voucher_moves) == 1
    assert voucher_moves[0]["amount"] == pytest.approx(-100.0)


def test_approve_returns_true_float_warning_when_balance_goes_negative():
    # Float starts at 0 — approval should warn.
    eid = _make_expense()
    float_warning = exp_svc.approve_expense(eid, user_id=1)
    assert float_warning is True


def test_approve_returns_false_float_warning_when_balance_stays_positive():
    svc.top_up(1000.0, "Float", acting_user_id=1)
    eid = _make_expense()
    float_warning = exp_svc.approve_expense(eid, user_id=1)
    assert float_warning is False


# --- rejection restores float ---

def test_reject_expense_restores_float():
    svc.top_up(500.0, "Float", acting_user_id=1)
    eid = _make_expense()
    # Approve then check intermediate balance.
    exp_svc.approve_expense(eid, user_id=1)
    balance_after_approve = svc.get_balance()["balance"]
    # Rejection of an approved voucher is not possible - rejection only works on pending.
    # Make a fresh expense, reject it while pending.
    eid2 = _make_expense()
    exp_svc.reject_expense(eid2, user_id=1, reason="Test rejection")
    # Float should be back to original (rejection restores 100.0).
    assert svc.get_balance()["balance"] == pytest.approx(balance_after_approve + 100.0)


def test_reject_records_restoration_movement():
    svc.top_up(200.0, "Float", acting_user_id=1)
    eid = _make_expense()
    exp_svc.reject_expense(eid, user_id=1, reason="Wrong")
    movements = svc.get_movements()
    restore = [m for m in movements if m["movement_type"] == "voucher_rejected"]
    assert len(restore) == 1
    assert restore[0]["amount"] == pytest.approx(100.0)


# --- is_float_low ---

def test_is_float_low_true_when_zero():
    assert svc.is_float_low() is True


def test_is_float_low_false_when_positive():
    svc.top_up(1.0, "note", acting_user_id=1)
    assert svc.is_float_low() is False
