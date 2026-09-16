import pytest

from app.programs.expenses.services import expenses as expenses_svc
from app.programs.expenses.services import data_integrity as svc
from app.services.errors import ForbiddenError, ConflictError
from app.database import get_connection
from tests.conftest import first_category_id

MANAGER = {"id": 1, "role": "manager"}


def _draft_with_lines(lines):
    return expenses_svc.create_expense(
        expense_date="2026-08-25", cost_center_id="", paid_to="Test Vendor",
        payment_mode="Cash", receipt_path=None, user_id=1, lines=lines, is_draft=True,
    )


def _line(particulars="Fuel", amount=100.0, category_id=None, is_vatable=False):
    return {"category_id": category_id or first_category_id(), "particulars": particulars,
            "amount": amount, "is_vatable": is_vatable}


def test_find_duplicate_lines_returns_nothing_when_none_exist():
    _draft_with_lines([_line("A", 10), _line("B", 20)])
    assert svc.find_duplicate_lines() == []


def test_find_duplicate_lines_detects_a_real_duplicate():
    eid = _draft_with_lines([_line("Fuel", 100), _line("Fuel", 100)])
    groups = svc.find_duplicate_lines()
    assert len(groups) == 1
    g = groups[0]
    assert g["expense_id"] == eid
    assert g["particulars"] == "Fuel"
    assert g["amount"] == 100.0
    assert g["count"] == 2
    assert len(g["line_ids"]) == 2
    assert g["is_draft"] is True
    assert g["status"] == "draft"


def test_find_duplicate_lines_does_not_flag_different_lines():
    """Different particulars, different amount, or different VAT status -
    none of these should count as a duplicate of each other."""
    _draft_with_lines([
        _line("Fuel", 100, is_vatable=False),
        _line("Fuel", 100, is_vatable=True),   # different VAT status
        _line("Fuel", 200, is_vatable=False),  # different amount
        _line("Snacks", 100, is_vatable=False),  # different particulars
    ])
    assert svc.find_duplicate_lines() == []


def test_find_duplicate_lines_includes_voucher_info():
    _draft_with_lines([_line("Fuel", 50), _line("Fuel", 50)])
    g = svc.find_duplicate_lines()[0]
    assert g["paid_to"] == "Test Vendor"
    assert g["expense_date"] == "2026-08-25"
    assert g["category_name"]  # resolved from category_id, not just the raw id


def test_find_duplicate_lines_excludes_deleted_vouchers():
    eid = _draft_with_lines([_line("Fuel", 50), _line("Fuel", 50)])
    # a draft can't be soft-deleted through the normal path, so bypass
    # directly to simulate a deleted voucher that happens to have dup lines
    from app.database import get_db
    with get_db() as conn:
        conn.execute("UPDATE expenses SET status='deleted' WHERE id=?", (eid,))
    assert svc.find_duplicate_lines() == []


def test_find_duplicate_lines_flags_non_draft_voucher_as_not_cleanable():
    # submitting one valid line normally, then directly inserting a second
    # identical line to simulate a duplicate that ended up on an
    # already-submitted voucher (strict validation checks each line's own
    # shape, not whether it duplicates another line, so this is a realistic
    # gap to guard against even though it can't happen through the normal
    # multi-line submission path today).
    eid2 = expenses_svc.create_expense(
        expense_date="2026-08-25", cost_center_id="", paid_to="Vendor 2",
        payment_mode="Cash", receipt_path=None, user_id=1,
        lines=[_line("Fuel", 75)],
    )
    from app.database import get_db
    with get_db() as conn:
        row = conn.execute("SELECT * FROM expense_lines WHERE expense_id=?", (eid2,)).fetchone()
        conn.execute(
            "INSERT INTO expense_lines (expense_id, line_no, category_id, particulars, amount, "
            "is_vatable, vat_rate, vat_amount) VALUES (?, 2, ?, ?, ?, ?, ?, ?)",
            (eid2, row["category_id"], row["particulars"], row["amount"],
             row["is_vatable"], row["vat_rate"], row["vat_amount"]),
        )
    groups = svc.find_duplicate_lines()
    g = next(x for x in groups if x["expense_id"] == eid2)
    assert g["is_draft"] is False
    assert g["status"] == "pending"


def test_remove_duplicate_line_group_keeps_one_and_removes_rest():
    eid = _draft_with_lines([_line("Fuel", 100), _line("Fuel", 100), _line("Fuel", 100)])
    g = svc.find_duplicate_lines()[0]
    assert g["count"] == 3

    removed = svc.remove_duplicate_line_group(
        eid, g["category_id"], g["particulars"], g["amount"], g["is_vatable"], MANAGER,
    )
    assert removed == 2

    e = expenses_svc.get_expense(eid)
    assert len(e["lines"]) == 1
    assert svc.find_duplicate_lines() == []


def test_remove_duplicate_line_group_keeps_the_oldest():
    eid = _draft_with_lines([_line("Fuel", 100), _line("Fuel", 100)])
    e_before = expenses_svc.get_expense(eid)
    oldest_id = min(l["id"] for l in e_before["lines"])

    g = svc.find_duplicate_lines()[0]
    svc.remove_duplicate_line_group(eid, g["category_id"], g["particulars"], g["amount"], g["is_vatable"], MANAGER)

    e_after = expenses_svc.get_expense(eid)
    assert len(e_after["lines"]) == 1
    assert e_after["lines"][0]["id"] == oldest_id


def test_remove_duplicate_line_group_renumbers_remaining_lines():
    """No gaps left in line_no after a middle duplicate is removed."""
    eid = _draft_with_lines([
        _line("A", 10), _line("Fuel", 100), _line("Fuel", 100), _line("Z", 40),
    ])
    g = next(x for x in svc.find_duplicate_lines() if x["particulars"] == "Fuel")
    svc.remove_duplicate_line_group(eid, g["category_id"], g["particulars"], g["amount"], g["is_vatable"], MANAGER)

    e = expenses_svc.get_expense(eid)
    line_nos = sorted(l["line_no"] for l in e["lines"])
    assert line_nos == [1, 2, 3]


def test_remove_duplicate_line_group_preserves_voucher_total():
    eid = _draft_with_lines([_line("Fuel", 100), _line("Fuel", 100), _line("Snacks", 20)])
    g = next(x for x in svc.find_duplicate_lines() if x["particulars"] == "Fuel")
    svc.remove_duplicate_line_group(eid, g["category_id"], g["particulars"], g["amount"], g["is_vatable"], MANAGER)
    e = expenses_svc.get_expense(eid)
    assert e["total_amount"] == 120.0  # 100 (one Fuel line kept) + 20, not 220


def test_remove_duplicate_line_group_rejects_non_draft():
    eid = expenses_svc.create_expense(
        expense_date="2026-08-25", cost_center_id="", paid_to="Vendor",
        payment_mode="Cash", receipt_path=None, user_id=1,
        lines=[_line("Fuel", 75)],
    )
    from app.database import get_db
    with get_db() as conn:
        row = conn.execute("SELECT * FROM expense_lines WHERE expense_id=?", (eid,)).fetchone()
        conn.execute(
            "INSERT INTO expense_lines (expense_id, line_no, category_id, particulars, amount, "
            "is_vatable, vat_rate, vat_amount) VALUES (?, 2, ?, ?, ?, ?, ?, ?)",
            (eid, row["category_id"], row["particulars"], row["amount"],
             row["is_vatable"], row["vat_rate"], row["vat_amount"]),
        )
    with pytest.raises(ConflictError):
        svc.remove_duplicate_line_group(eid, row["category_id"], row["particulars"],
                                         row["amount"], bool(row["is_vatable"]), MANAGER)
    # untouched
    e = expenses_svc.get_expense(eid)
    assert len(e["lines"]) == 2


def test_remove_duplicate_line_group_rejects_non_owner_non_manager():
    from app.services import users as users_svc
    other_id = users_svc.create_user(username="dup_test_other", full_name="Other",
                                      password="pass1234", role="user", acting_user_id=1)
    eid = expenses_svc.create_expense(
        expense_date="2026-08-25", cost_center_id="", paid_to="Vendor", payment_mode="Cash",
        receipt_path=None, user_id=other_id,
        lines=[_line("Fuel", 100), _line("Fuel", 100)], is_draft=True,
    )
    g = svc.find_duplicate_lines()[0]
    with pytest.raises(ForbiddenError):
        svc.remove_duplicate_line_group(
            eid, g["category_id"], g["particulars"], g["amount"], g["is_vatable"],
            {"id": 999, "role": "user"},
        )


def test_remove_duplicate_line_group_is_logged():
    from app.database import get_db
    eid = _draft_with_lines([_line("Fuel", 100), _line("Fuel", 100)])
    g = svc.find_duplicate_lines()[0]
    svc.remove_duplicate_line_group(eid, g["category_id"], g["particulars"], g["amount"], g["is_vatable"], MANAGER)
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM audit_log WHERE action='clean_duplicate_lines' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    assert str(eid) in row["details"]
