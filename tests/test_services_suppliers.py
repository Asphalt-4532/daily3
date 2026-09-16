"""Tests for app/programs/expenses/services/suppliers.py, called directly.
resolve_supplier() runs inside an existing transaction (a real connection,
not get_db()) since that's how expenses._validate_lines() calls it - so
these tests open their own connection the same way."""
import pytest

from app.database import get_db
from app.programs.expenses.services import suppliers as svc
from app.services.errors import ValidationError, NotFoundError


def _resolve(name, vat, created_by=1):
    with get_db() as conn:
        return svc.resolve_supplier(conn, name, vat, created_by)


def test_resolve_supplier_creates_a_new_one():
    sid = _resolve("Al Noor Fuel Station", "VAT100234567")
    supplier = svc.get_supplier(sid)
    assert supplier["name"] == "Al Noor Fuel Station"
    assert supplier["vat_number"] == "VAT100234567"
    assert supplier["status"] == "active"


def test_resolve_supplier_reuses_existing_active_by_vat():
    sid1 = _resolve("Al Noor Fuel Station", "VAT100234567")
    sid2 = _resolve("Al Noor Fuel Station (typo variant)", "VAT100234567")
    assert sid1 == sid2  # VAT is the identity - reused regardless of the name typed


def test_resolve_supplier_requires_name():
    with pytest.raises(ValidationError):
        _resolve("", "VAT100234567")


def test_resolve_supplier_requires_vat():
    with pytest.raises(ValidationError):
        _resolve("Al Noor Fuel Station", "")


def test_resolve_supplier_rejects_reusing_a_deleted_suppliers_vat():
    sid = _resolve("Old Trading Co", "VAT555000900")
    svc.delete_supplier(sid, deleted_by=1, reason="duplicate, merged")
    with pytest.raises(ValidationError, match="deleted supplier"):
        _resolve("Old Trading Co", "VAT555000900")


def test_list_suppliers_defaults_to_active_only():
    sid = _resolve("Visible Supplier", "VATLIST001")
    hidden_id = _resolve("Hidden Supplier", "VATLIST002")
    svc.delete_supplier(hidden_id, deleted_by=1, reason="test")
    active = svc.list_suppliers()
    names = [s["name"] for s in active]
    assert "Visible Supplier" in names
    assert "Hidden Supplier" not in names


def test_list_suppliers_all_status_includes_deleted():
    sid = _resolve("Another Supplier", "VATLIST003")
    svc.delete_supplier(sid, deleted_by=1, reason="test")
    all_suppliers = svc.list_suppliers(status="")
    assert any(s["id"] == sid for s in all_suppliers)


def test_list_suppliers_search_matches_vat_substring():
    _resolve("Match Me Co", "VAT999888777")
    _resolve("No Match Co", "VAT111222333")
    results = svc.list_suppliers(search="999888")
    names = [s["name"] for s in results]
    assert "Match Me Co" in names
    assert "No Match Co" not in names


def test_get_supplier_missing_raises_not_found():
    with pytest.raises(NotFoundError):
        svc.get_supplier(999999)


def test_delete_supplier_requires_reason():
    sid = _resolve("Needs Reason Co", "VATREASON001")
    with pytest.raises(ValidationError):
        svc.delete_supplier(sid, deleted_by=1, reason="   ")


def test_delete_supplier_missing_raises_not_found():
    with pytest.raises(NotFoundError):
        svc.delete_supplier(999999, deleted_by=1, reason="test")


def test_delete_supplier_twice_raises_validation_error():
    sid = _resolve("Double Delete Co", "VATDOUBLE001")
    svc.delete_supplier(sid, deleted_by=1, reason="first delete")
    with pytest.raises(ValidationError, match="already deleted"):
        svc.delete_supplier(sid, deleted_by=1, reason="second delete")


def test_delete_supplier_soft_deletes_not_hard_deletes():
    sid = _resolve("Soft Delete Co", "VATSOFT001")
    svc.delete_supplier(sid, deleted_by=1, reason="no longer used")
    supplier = svc.get_supplier(sid)
    assert supplier["status"] == "deleted"
    assert supplier["delete_reason"] == "no longer used"
    assert supplier["name"] == "Soft Delete Co"  # row itself untouched
    assert supplier["vat_number"] == "VATSOFT001"


def test_deleting_a_supplier_does_not_affect_existing_expense_line_references():
    """The specific requirement: deleting a supplier must never change what
    an existing voucher line shows."""
    from app.programs.expenses.services import expenses as expenses_svc
    from tests.conftest import first_category_id, first_cost_center_id
    from datetime import date

    eid = expenses_svc.create_expense(
        expense_date=date.today().isoformat(), cost_center_id=str(first_cost_center_id()),
        paid_to="Vendor", payment_mode="Cash", receipt_path=None, user_id=1,
        lines=[{"category_id": first_category_id(), "particulars": "Fuel", "amount": 100.0,
                "is_vatable": True, "supplier_name": "Persisted Co", "supplier_vat": "VATPERSIST001"}],
    )
    expense = expenses_svc.get_expense(eid)
    line = expense["lines"][0]
    assert line["supplier_name"] == "Persisted Co"
    assert line["supplier_vat"] == "VATPERSIST001"

    supplier_id = line["supplier_id"]
    svc.delete_supplier(supplier_id, deleted_by=1, reason="cleanup")

    expense_after = expenses_svc.get_expense(eid)
    line_after = expense_after["lines"][0]
    assert line_after["supplier_name"] == "Persisted Co"
    assert line_after["supplier_vat"] == "VATPERSIST001"
