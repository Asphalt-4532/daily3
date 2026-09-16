"""Tests for app/programs/expenses/services/vat_report.py."""
import pytest
from datetime import date

from app.programs.expenses.services import vat_report as svc
from app.programs.expenses.services import expenses as exp_svc
from app.programs.expenses.services.suppliers import list_suppliers
from tests.conftest import first_category_id, first_cost_center_id


def _today() -> str:
    return date.today().isoformat()


def _make_vatable_expense(amount=100.0, supplier_name="ACME Ltd", supplier_vat="300000001"):
    return exp_svc.create_expense(
        expense_date=_today(),
        cost_center_id=str(first_cost_center_id()),
        paid_to=supplier_name,
        payment_mode="Cash",
        receipt_path=None,
        user_id=1,
        lines=[{
            "category_id": first_category_id(),
            "particulars": "VAT test item",
            "amount": amount,
            "is_vatable": True,
            "supplier_name": supplier_name,
            "supplier_vat": supplier_vat,
        }],
    )


def _make_non_vatable_expense(amount=50.0):
    return exp_svc.create_expense(
        expense_date=_today(),
        cost_center_id=str(first_cost_center_id()),
        paid_to="No-VAT Vendor",
        payment_mode="Cash",
        receipt_path=None,
        user_id=1,
        lines=[{
            "category_id": first_category_id(),
            "particulars": "Non-vatable",
            "amount": amount,
            "is_vatable": False,
            "supplier_name": "",
            "supplier_vat": "",
        }],
    )


# --- vat_summary ---

def test_vat_summary_empty_when_no_approved_expenses():
    summary = svc.vat_summary()
    assert summary["rows"] == []
    assert summary["grand"]["vat_amount"] == 0.0


def test_vat_summary_excludes_pending_vouchers():
    _make_vatable_expense()  # not approved
    summary = svc.vat_summary()
    assert summary["rows"] == []


def test_vat_summary_excludes_non_vatable_lines():
    eid = _make_non_vatable_expense()
    exp_svc.approve_expense(eid, user_id=1)
    summary = svc.vat_summary()
    assert summary["rows"] == []
    assert summary["grand"]["vat_amount"] == 0.0


def test_vat_summary_single_supplier():
    eid = _make_vatable_expense(amount=100.0, supplier_name="ACME", supplier_vat="300111")
    exp_svc.approve_expense(eid, user_id=1)
    summary = svc.vat_summary()
    assert len(summary["rows"]) == 1
    row = summary["rows"][0]
    assert row["supplier_name"] == "ACME"
    assert row["trx_count"] == 1
    assert row["voucher_count"] == 1
    assert row["net_amount"] == pytest.approx(100.0)
    assert row["vat_amount"] > 0


def test_vat_summary_grand_totals_match_sum_of_rows():
    eid1 = _make_vatable_expense(100.0, "Supplier A", "VAT001")
    eid2 = _make_vatable_expense(200.0, "Supplier B", "VAT002")
    exp_svc.approve_expense(eid1, user_id=1)
    exp_svc.approve_expense(eid2, user_id=1)
    summary = svc.vat_summary()
    assert summary["grand"]["net_amount"] == pytest.approx(
        sum(r["net_amount"] for r in summary["rows"])
    )
    assert summary["grand"]["vat_amount"] == pytest.approx(
        sum(r["vat_amount"] for r in summary["rows"])
    )


def test_vat_summary_groups_by_supplier():
    """Two approved lines to same supplier → 1 summary row, trx_count=2."""
    eid1 = _make_vatable_expense(100.0, "ACME", "VAT001")
    eid2 = _make_vatable_expense(150.0, "ACME", "VAT001")
    exp_svc.approve_expense(eid1, user_id=1)
    exp_svc.approve_expense(eid2, user_id=1)
    summary = svc.vat_summary()
    acme_rows = [r for r in summary["rows"] if r["supplier_name"] == "ACME"]
    assert len(acme_rows) == 1
    assert acme_rows[0]["trx_count"] == 2
    assert acme_rows[0]["voucher_count"] == 2  # different vouchers
    assert acme_rows[0]["net_amount"] == pytest.approx(250.0)


def test_vat_summary_voucher_count_vs_trx_count():
    """One voucher with 2 vatable lines to same supplier → voucher_count=1, trx_count=2."""
    eid = exp_svc.create_expense(
        expense_date=_today(),
        cost_center_id=str(first_cost_center_id()),
        paid_to="Multi-line supplier",
        payment_mode="Cash",
        receipt_path=None,
        user_id=1,
        lines=[
            {"category_id": first_category_id(), "particulars": "Line 1",
             "amount": 50.0, "is_vatable": True,
             "supplier_name": "Same Supplier", "supplier_vat": "VATMLT"},
            {"category_id": first_category_id(), "particulars": "Line 2",
             "amount": 75.0, "is_vatable": True,
             "supplier_name": "Same Supplier", "supplier_vat": "VATMLT"},
        ],
    )
    exp_svc.approve_expense(eid, user_id=1)
    summary = svc.vat_summary()
    supplier_rows = [r for r in summary["rows"] if r["supplier_name"] == "Same Supplier"]
    assert len(supplier_rows) == 1
    assert supplier_rows[0]["trx_count"] == 2
    assert supplier_rows[0]["voucher_count"] == 1


def test_vat_summary_date_filter_start():
    eid = _make_vatable_expense(100.0, "DateFilter", "VATDF")
    exp_svc.approve_expense(eid, user_id=1)
    # Filter future date - should exclude today's entry.
    summary = svc.vat_summary(start="2099-01-01")
    assert summary["rows"] == []


def test_vat_summary_date_filter_end():
    eid = _make_vatable_expense(100.0, "DateFilter2", "VATDF2")
    exp_svc.approve_expense(eid, user_id=1)
    # Filter past date range - should exclude today.
    summary = svc.vat_summary(end="2000-01-01")
    assert summary["rows"] == []


def test_vat_summary_no_supplier_recorded_label():
    """Vatable line with no supplier → shows (No supplier recorded) label."""
    from app.database import get_db
    # Manually insert a vatable line without supplier_id.
    eid = exp_svc.create_expense(
        expense_date=_today(),
        cost_center_id=str(first_cost_center_id()),
        paid_to="Unknown",
        payment_mode="Cash",
        receipt_path=None,
        user_id=1,
        lines=[{
            "category_id": first_category_id(),
            "particulars": "Mystery item",
            "amount": 80.0,
            "is_vatable": True,
            "supplier_name": "TempSupplier",
            "supplier_vat": "VATTEMP99",
        }],
    )
    exp_svc.approve_expense(eid, user_id=1)
    # Force supplier_id to NULL on the line to test the no-supplier path.
    with get_db() as conn:
        conn.execute(
            "UPDATE expense_lines SET supplier_id=NULL WHERE expense_id=?", (eid,)
        )
    summary = svc.vat_summary()
    labels = [r["supplier_name"] for r in summary["rows"]]
    assert "(No supplier recorded)" in labels


# --- build_vat_xlsx ---

def test_build_vat_xlsx_returns_bytes():
    eid = _make_vatable_expense()
    exp_svc.approve_expense(eid, user_id=1)
    summary = svc.vat_summary()
    buf = svc.build_vat_xlsx(
        summary, document_no="VAT-2026-00001",
        generated_by="Test User", filters_description="All",
    )
    data = buf.read()
    assert len(data) > 0
    # xlsx files start with PK (zip magic bytes)
    assert data[:2] == b"PK"


# --- build_vat_pdf ---

def test_build_vat_pdf_returns_bytes():
    eid = _make_vatable_expense()
    exp_svc.approve_expense(eid, user_id=1)
    summary = svc.vat_summary()
    pdf = svc.build_vat_pdf(
        summary, document_no="VAT-2026-00001",
        generated_by="Test User", filters_description="All",
    )
    assert isinstance(pdf, bytes)
    assert pdf[:4] == b"%PDF"


# --- allocate_document_number ---

def test_allocate_document_number_format():
    doc_no = svc.allocate_document_number("VAT")
    assert doc_no.startswith("VAT-")
    parts = doc_no.split("-")
    assert len(parts) == 3
    assert parts[2].isdigit()
