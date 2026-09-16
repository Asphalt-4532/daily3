"""Tests for app/programs/expenses/services/advances.py
and the advance wiring in expenses.py."""
import pytest
from datetime import date

from app.programs.expenses.services import advances as svc
from app.programs.expenses.services import expenses as exp_svc
from app.services.errors import ValidationError, NotFoundError
from app.database import get_connection, get_db
from tests.conftest import first_category_id, first_cost_center_id


def _today():
    return date.today().isoformat()


def _advance_category_id():
    """Return the id of the 'Salary advance' category."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM categories WHERE name='Salary advance'"
        ).fetchone()
        assert row, "Salary advance category not seeded"
        return row["id"]
    finally:
        conn.close()


def _make_advance_expense(name="Ahmad Ali", phone="+966501234567", amount=500.0):
    return exp_svc.create_expense(
        expense_date=_today(),
        cost_center_id=str(first_cost_center_id()),
        paid_to=name,
        payment_mode="Cash",
        receipt_path=None,
        user_id=1,
        lines=[{
            "category_id": _advance_category_id(),
            "particulars": "Salary advance",
            "amount": amount,
            "is_vatable": False,
            "supplier_name": "",
            "supplier_vat": "",
            "employee_name": name,
            "employee_phone": phone,
        }],
    )


# ---------------------------------------------------------------------------
# Employee registry
# ---------------------------------------------------------------------------

def test_get_or_create_employee_creates_new():
    with get_db() as conn:
        eid = svc.get_or_create_employee(conn, "Fatima Al-Zahra", "+966501112222", 1)
    assert eid > 0


def test_get_or_create_employee_reuses_existing():
    with get_db() as conn:
        eid1 = svc.get_or_create_employee(conn, "Same Person", "+966509998888", 1)
        eid2 = svc.get_or_create_employee(conn, "Same Person", "+966509998888", 1)
    assert eid1 == eid2


def test_get_or_create_employee_requires_name():
    with get_db() as conn:
        with pytest.raises(ValidationError):
            svc.get_or_create_employee(conn, "", "+966501234567", 1)


def test_get_or_create_employee_requires_phone():
    with get_db() as conn:
        with pytest.raises(ValidationError):
            svc.get_or_create_employee(conn, "Valid Name", "", 1)


def test_list_employees_returns_active():
    with get_db() as conn:
        svc.get_or_create_employee(conn, "Employee A", "+966500000001", 1)
        svc.get_or_create_employee(conn, "Employee B", "+966500000002", 1)
    employees = svc.list_employees()
    names = [e["name"] for e in employees]
    assert "Employee A" in names
    assert "Employee B" in names


def test_get_employee_not_found_raises():
    with pytest.raises(NotFoundError):
        svc.get_employee(99999)


# ---------------------------------------------------------------------------
# Advance recording (via voucher approval)
# ---------------------------------------------------------------------------

def test_advance_created_when_voucher_approved():
    eid = _make_advance_expense(name="Khalid Mansoor", phone="+966501111111", amount=1000.0)
    exp_svc.approve_expense(eid, user_id=1)
    balances = svc.get_outstanding_balances()
    emp = next((b for b in balances if b["name"] == "Khalid Mansoor"), None)
    assert emp is not None
    assert emp["total_advanced"] == pytest.approx(1000.0)
    assert emp["outstanding"] == pytest.approx(1000.0)


def test_advance_not_recorded_when_voucher_pending():
    """Advance is only recorded AFTER approval, not on submission."""
    _make_advance_expense(name="Sara Pending", phone="+966502222222", amount=300.0)
    balances = svc.get_outstanding_balances()
    emp = next((b for b in balances if b["name"] == "Sara Pending"), None)
    # No advance ledger entry until approved.
    if emp:
        assert emp["total_advanced"] == 0.0


def test_non_advance_category_not_recorded_in_ledger():
    exp_svc.create_expense(
        expense_date=_today(),
        cost_center_id=str(first_cost_center_id()),
        paid_to="Fuel Shop",
        payment_mode="Cash",
        receipt_path=None,
        user_id=1,
        lines=[{
            "category_id": first_category_id(),
            "particulars": "Fuel top-up",
            "amount": 150.0,
            "is_vatable": False,
            "supplier_name": "",
            "supplier_vat": "",
            "employee_name": "",
            "employee_phone": "",
        }],
    )
    # No employees created, no advance ledger entries.
    assert svc.list_employees() == []


# ---------------------------------------------------------------------------
# Validation: employee fields required for advance category in strict mode
# ---------------------------------------------------------------------------

def test_advance_line_requires_employee_name():
    with pytest.raises(ValidationError, match="employee name"):
        exp_svc.create_expense(
            expense_date=_today(),
            cost_center_id=str(first_cost_center_id()),
            paid_to="Staff",
            payment_mode="Cash",
            receipt_path=None,
            user_id=1,
            lines=[{
                "category_id": _advance_category_id(),
                "particulars": "Advance",
                "amount": 200.0,
                "is_vatable": False,
                "supplier_name": "",
                "supplier_vat": "",
                "employee_name": "",   # missing
                "employee_phone": "+966501234567",
            }],
        )


def test_advance_line_requires_employee_phone():
    with pytest.raises(ValidationError, match="employee phone"):
        exp_svc.create_expense(
            expense_date=_today(),
            cost_center_id=str(first_cost_center_id()),
            paid_to="Staff",
            payment_mode="Cash",
            receipt_path=None,
            user_id=1,
            lines=[{
                "category_id": _advance_category_id(),
                "particulars": "Advance",
                "amount": 200.0,
                "is_vatable": False,
                "supplier_name": "",
                "supplier_vat": "",
                "employee_name": "Ahmad",
                "employee_phone": "",   # missing
            }],
        )


def test_advance_line_allowed_in_draft_without_employee():
    """Draft allows missing employee fields (relaxed validation)."""
    eid = exp_svc.create_expense(
        expense_date=_today(),
        cost_center_id=str(first_cost_center_id()),
        paid_to="",
        payment_mode="Cash",
        receipt_path=None,
        user_id=1,
        lines=[{
            "category_id": _advance_category_id(),
            "particulars": "Advance placeholder",
            "amount": 100.0,
            "is_vatable": False,
            "supplier_name": "",
            "supplier_vat": "",
            "employee_name": "",
            "employee_phone": "",
        }],
        is_draft=True,
    )
    assert eid > 0


# ---------------------------------------------------------------------------
# Settlement — manager/accountant path (immediate)
# ---------------------------------------------------------------------------

def test_settlement_reduces_outstanding():
    eid = _make_advance_expense(name="Settle Test", phone="+966503333333", amount=800.0)
    exp_svc.approve_expense(eid, user_id=1)

    balances_before = svc.get_outstanding_balances()
    emp_id = next(b["id"] for b in balances_before if b["name"] == "Settle Test")

    svc.record_settlement(
        employee_id=emp_id,
        amount=300.0,
        method="Cash returned to box",
        note="Partial settlement",
        acting_user={"id": 1, "role": "manager"},
    )

    balances_after = svc.get_outstanding_balances()
    emp = next(b for b in balances_after if b["name"] == "Settle Test")
    assert emp["outstanding"] == pytest.approx(500.0)


def test_settlement_amount_cannot_exceed_balance():
    eid = _make_advance_expense(name="Exceed Test", phone="+966504444444", amount=200.0)
    exp_svc.approve_expense(eid, user_id=1)
    balances = svc.get_outstanding_balances()
    emp_id = next(b["id"] for b in balances if b["name"] == "Exceed Test")

    with pytest.raises(ValidationError, match="exceeds"):
        svc.record_settlement(
            employee_id=emp_id,
            amount=999.0,   # more than 200
            method="Cash returned to box",
            note="",
            acting_user={"id": 1, "role": "manager"},
        )


def test_settlement_rejects_zero_amount():
    eid = _make_advance_expense(name="Zero Test", phone="+966505555555", amount=100.0)
    exp_svc.approve_expense(eid, user_id=1)
    balances = svc.get_outstanding_balances()
    emp_id = next(b["id"] for b in balances if b["name"] == "Zero Test")
    with pytest.raises(ValidationError):
        svc.record_settlement(
            employee_id=emp_id, amount=0,
            method="Salary deduction", note="",
            acting_user={"id": 1, "role": "manager"},
        )


def test_settlement_invalid_method_rejected():
    eid = _make_advance_expense(name="Method Test", phone="+966506666666", amount=100.0)
    exp_svc.approve_expense(eid, user_id=1)
    balances = svc.get_outstanding_balances()
    emp_id = next(b["id"] for b in balances if b["name"] == "Method Test")
    with pytest.raises(ValidationError, match="method"):
        svc.record_settlement(
            employee_id=emp_id, amount=50.0,
            method="Crypto payment",   # invalid
            note="",
            acting_user={"id": 1, "role": "manager"},
        )


# ---------------------------------------------------------------------------
# Settlement — user role (pending approval)
# ---------------------------------------------------------------------------

def test_user_settlement_creates_pending():
    eid = _make_advance_expense(name="User Settle", phone="+966507777777", amount=600.0)
    exp_svc.approve_expense(eid, user_id=1)
    balances = svc.get_outstanding_balances()
    emp_id = next(b["id"] for b in balances if b["name"] == "User Settle")

    svc.record_settlement(
        employee_id=emp_id, amount=200.0,
        method="Cash returned to box", note="User submitted",
        acting_user={"id": 1, "role": "user"},
    )

    # Balance should NOT change yet — still pending.
    balances_after = svc.get_outstanding_balances()
    emp = next(b for b in balances_after if b["name"] == "User Settle")
    assert emp["outstanding"] == pytest.approx(600.0)

    # Pending list should have 1 entry.
    pending = svc.get_pending_settlements()
    assert len(pending) == 1


def test_approve_settlement_reduces_balance():
    eid = _make_advance_expense(name="Approve Settle", phone="+966508888888", amount=400.0)
    exp_svc.approve_expense(eid, user_id=1)
    balances = svc.get_outstanding_balances()
    emp_id = next(b["id"] for b in balances if b["name"] == "Approve Settle")

    ledger_id = svc.record_settlement(
        employee_id=emp_id, amount=100.0,
        method="Salary deduction", note="Sep deduction",
        acting_user={"id": 1, "role": "user"},
    )
    svc.approve_settlement(ledger_id, acting_user={"id": 1, "role": "manager"})

    balances_after = svc.get_outstanding_balances()
    emp = next(b for b in balances_after if b["name"] == "Approve Settle")
    assert emp["outstanding"] == pytest.approx(300.0)


def test_reject_settlement_deletes_pending():
    eid = _make_advance_expense(name="Reject Settle", phone="+966509999999", amount=200.0)
    exp_svc.approve_expense(eid, user_id=1)
    balances = svc.get_outstanding_balances()
    emp_id = next(b["id"] for b in balances if b["name"] == "Reject Settle")

    ledger_id = svc.record_settlement(
        employee_id=emp_id, amount=50.0,
        method="Bank transfer", note="",
        acting_user={"id": 1, "role": "user"},
    )
    svc.reject_settlement(ledger_id, acting_user={"id": 1, "role": "manager"}, reason="Incorrect amount")

    pending = svc.get_pending_settlements()
    assert all(p["id"] != ledger_id for p in pending)

    # Balance unchanged.
    balances_after = svc.get_outstanding_balances()
    emp = next(b for b in balances_after if b["name"] == "Reject Settle")
    assert emp["outstanding"] == pytest.approx(200.0)


# ---------------------------------------------------------------------------
# Employee ledger detail
# ---------------------------------------------------------------------------

def test_get_employee_ledger_shows_movements():
    eid = _make_advance_expense(name="Ledger Test", phone="+966510000001", amount=1000.0)
    exp_svc.approve_expense(eid, user_id=1)

    balances = svc.get_outstanding_balances()
    emp_id = next(b["id"] for b in balances if b["name"] == "Ledger Test")

    svc.record_settlement(
        employee_id=emp_id, amount=400.0,
        method="Cash returned to box", note="First return",
        acting_user={"id": 1, "role": "manager"},
    )

    ledger = svc.get_employee_ledger(emp_id)
    assert ledger["outstanding"] == pytest.approx(600.0)
    assert ledger["total_advanced"] == pytest.approx(1000.0)
    assert ledger["total_settled"] == pytest.approx(400.0)
    assert len(ledger["movements"]) == 2


def test_get_employee_ledger_running_balance():
    eid = _make_advance_expense(name="Running Bal", phone="+966510000002", amount=500.0)
    exp_svc.approve_expense(eid, user_id=1)
    balances = svc.get_outstanding_balances()
    emp_id = next(b["id"] for b in balances if b["name"] == "Running Bal")

    svc.record_settlement(
        employee_id=emp_id, amount=200.0,
        method="Salary deduction", note="",
        acting_user={"id": 1, "role": "manager"},
    )
    ledger = svc.get_employee_ledger(emp_id)
    # Movements newest first — first is settlement, second is advance.
    balances_in_ledger = [m["running_balance"] for m in ledger["movements"] if m["running_balance"] is not None]
    assert pytest.approx(300.0) in balances_in_ledger   # after settlement


# ---------------------------------------------------------------------------
# Access control helpers
# ---------------------------------------------------------------------------

def test_user_has_advanced_employee_true():
    eid = _make_advance_expense(name="Access Test", phone="+966510000003", amount=100.0)
    exp_svc.approve_expense(eid, user_id=1)
    balances = svc.get_outstanding_balances()
    emp_id = next(b["id"] for b in balances if b["name"] == "Access Test")
    assert svc.user_has_advanced_employee(1, emp_id) is True


def test_user_has_advanced_employee_false_for_other_user():
    eid = _make_advance_expense(name="Other User Test", phone="+966510000004", amount=100.0)
    exp_svc.approve_expense(eid, user_id=1)
    balances = svc.get_outstanding_balances()
    emp_id = next(b["id"] for b in balances if b["name"] == "Other User Test")
    # user_id=2 never advanced this employee
    assert svc.user_has_advanced_employee(2, emp_id) is False


def test_get_outstanding_balances_for_user_scoped():
    eid = _make_advance_expense(name="Scoped Test", phone="+966510000005", amount=100.0)
    exp_svc.approve_expense(eid, user_id=1)
    # user_id=1 advanced this employee.
    balances = svc.get_outstanding_balances_for_user(1)
    assert any(b["name"] == "Scoped Test" for b in balances)
    # user_id=2 did not.
    balances2 = svc.get_outstanding_balances_for_user(2)
    assert not any(b["name"] == "Scoped Test" for b in balances2)
