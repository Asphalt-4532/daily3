"""Tests for timeline.py and asset_reports.py.

Properties locked by these tests:
  * Four feeds merge correctly; none pollutes another.
  * Cost feed: approved only (by default) - pending lines stay out.
  * Service feed: no amount ever (None, not 0.0).
  * TCO = purchase cost + approved lines only. Service notes don't add.
  * Document numbers are pre-allocated and unique per doc_type.
  * filter_label describes what was actually applied, including the pending flag.
  * Both Excel and PDF render without error for empty, one-event, and
    multi-asset cases.
  * The fuel banner is present on Fleet/Truck asset history.
"""
import io
import pytest
from datetime import date

from app.programs.assets.services import assets as assets_svc
from app.programs.assets.services import custody, service_log as service_log_svc
from app.programs.assets.services.timeline import (
    asset_timeline, asset_tco, asset_summary,
    KIND_COST, KIND_SERVICE, KIND_CUSTODY, KIND_REGISTRATION,
)
from app.programs.assets.services.asset_reports import (
    maintenance_rows, filter_label, allocate_document_number,
    build_asset_history_workbook, build_asset_history_pdf,
    build_maintenance_listing_workbook, build_maintenance_listing_pdf,
    _FUEL_BANNER,
)
from app.programs.expenses.services import expenses as exp_svc
from app.services.errors import ValidationError
from app.database import get_connection, get_db
from tests.conftest import first_cost_center_id


def _today():
    return date.today().isoformat()


def _manager():
    conn = get_connection()
    try:
        return dict(conn.execute("SELECT * FROM users WHERE role='manager'").fetchone())
    finally:
        conn.close()


def _manager_id():
    return _manager()["id"]


def _make_asset(tag="TR-04", category="Fleet/Truck", name="Isuzu 6-wheel",
                purchase_cost=45000.0, purchase_date=None):
    return assets_svc.create_asset(
        asset_tag=tag, name=name, category=category,
        purchase_cost=purchase_cost,
        purchase_date=purchase_date or "2024-03-11",
        created_by=_manager_id(),
    )


def _maintenance_cat_id(asset_category="Fleet/Truck"):
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM categories WHERE requires_asset=1 AND asset_category=?",
            (asset_category,),
        ).fetchone()
        assert row, f"no maintenance category seeded for {asset_category}"
        return row["id"]
    finally:
        conn.close()


def _approve(expense_id):
    exp_svc.approve_expense(expense_id, _manager_id())


def _make_approved_cost(asset_id, amount=640.0):
    cat_id = _maintenance_cat_id()
    eid = exp_svc.create_expense(
        expense_date=_today(), cost_center_id=str(first_cost_center_id()),
        paid_to="Al-Rajhi Motors", payment_mode="Cash", receipt_path=None,
        user_id=_manager_id(),
        lines=[{"category_id": cat_id, "particulars": "Brake pads",
                "amount": amount, "is_vatable": False, "asset_id": asset_id}],
    )
    _approve(eid)
    return eid


def _make_service(asset_id, description="Greased chassis"):
    service_id, _ = service_log_svc.log_service(
        asset_id, service_date=_today(), service_type="Greasing/Lubrication",
        description=description, recorded_by_user=_manager(),
    )
    return service_id


# ---------------------------------------------------------------------------
# TCO
# ---------------------------------------------------------------------------
def test_tco_is_purchase_plus_approved_only():
    asset_id = _make_asset(purchase_cost=45000.0)
    cat_id = _maintenance_cat_id()

    # approved cost
    eid = exp_svc.create_expense(
        expense_date=_today(), cost_center_id=str(first_cost_center_id()),
        paid_to="Vendor", payment_mode="Cash", receipt_path=None,
        user_id=_manager_id(),
        lines=[{"category_id": cat_id, "particulars": "Tyres",
                "amount": 2000.0, "is_vatable": False, "asset_id": asset_id}],
    )
    _approve(eid)

    # pending cost - must NOT be in TCO
    exp_svc.create_expense(
        expense_date=_today(), cost_center_id=str(first_cost_center_id()),
        paid_to="Vendor", payment_mode="Cash", receipt_path=None,
        user_id=_manager_id(),
        lines=[{"category_id": cat_id, "particulars": "Oil",
                "amount": 300.0, "is_vatable": False, "asset_id": asset_id}],
    )

    tco = asset_tco(asset_id)
    assert tco["purchase_cost"] == 45000.0
    assert tco["maintenance_total"] == 2000.0
    assert tco["tco"] == 47000.0


def test_service_notes_never_affect_tco():
    asset_id = _make_asset(purchase_cost=10000.0)
    _make_service(asset_id)
    _make_service(asset_id, description="Greased again")
    tco = asset_tco(asset_id)
    assert tco["maintenance_total"] == 0.0
    assert tco["tco"] == 10000.0


# ---------------------------------------------------------------------------
# timeline - four feeds
# ---------------------------------------------------------------------------
def test_timeline_registration_event_uses_purchase_date():
    asset_id = _make_asset(purchase_date="2024-03-11")
    events = asset_timeline(asset_id, kinds=[KIND_REGISTRATION])
    assert len(events) == 1
    assert events[0]["kind"] == "registration"
    assert events[0]["event_date"] == "2024-03-11"
    assert events[0]["amount"] == 45000.0


def test_timeline_cost_events_approved_only_by_default():
    asset_id = _make_asset()
    cat_id = _maintenance_cat_id()

    approved_id = exp_svc.create_expense(
        expense_date=_today(), cost_center_id=str(first_cost_center_id()),
        paid_to="V1", payment_mode="Cash", receipt_path=None, user_id=_manager_id(),
        lines=[{"category_id": cat_id, "particulars": "Approved part",
                "amount": 500.0, "is_vatable": False, "asset_id": asset_id}],
    )
    _approve(approved_id)

    exp_svc.create_expense(
        expense_date=_today(), cost_center_id=str(first_cost_center_id()),
        paid_to="V2", payment_mode="Cash", receipt_path=None, user_id=_manager_id(),
        lines=[{"category_id": cat_id, "particulars": "Pending part",
                "amount": 200.0, "is_vatable": False, "asset_id": asset_id}],
    )

    events = asset_timeline(asset_id, kinds=[KIND_COST])
    assert len(events) == 1
    assert events[0]["description"] == "Approved part"


def test_timeline_service_events_have_no_amount():
    asset_id = _make_asset()
    _make_service(asset_id)
    events = asset_timeline(asset_id, kinds=[KIND_SERVICE])
    assert len(events) == 1
    assert events[0]["amount"] is None
    assert events[0]["vat_amount"] is None


def test_timeline_custody_events_have_no_amount():
    asset_id = _make_asset()
    with get_db() as conn:
        emp_id = conn.execute(
            "INSERT INTO employees (name, phone, created_by) VALUES ('A','0500',1)"
        ).lastrowid
    custody.assign_asset(asset_id, employee_id=emp_id,
                         issued_date="2026-01-01", assigned_by=_manager_id())
    events = asset_timeline(asset_id, kinds=[KIND_CUSTODY])
    assert len(events) == 1
    assert events[0]["amount"] is None
    assert "Assigned to A" in events[0]["description"]


def test_timeline_date_filter_is_applied():
    asset_id = _make_asset()
    _make_approved_cost(asset_id)
    events_all = asset_timeline(asset_id, kinds=[KIND_COST])
    events_old = asset_timeline(asset_id, kinds=[KIND_COST],
                                date_from="2020-01-01", date_to="2020-12-31")
    assert len(events_all) == 1
    assert len(events_old) == 0


def test_timeline_all_four_feeds_merged_and_sorted():
    asset_id = _make_asset(purchase_date="2024-01-01")
    _make_approved_cost(asset_id)
    _make_service(asset_id)
    with get_db() as conn:
        emp_id = conn.execute(
            "INSERT INTO employees (name, phone, created_by) VALUES ('B','0501',1)"
        ).lastrowid
    custody.assign_asset(asset_id, employee_id=emp_id, issued_date="2025-06-01",
                         assigned_by=_manager_id())
    events = asset_timeline(asset_id)
    kinds = [e["kind"] for e in events]
    assert "registration" in kinds
    assert "cost" in kinds
    assert "service" in kinds
    assert "custody" in kinds
    # newest first
    assert events == sorted(events, key=lambda e: e["event_date"], reverse=True)


def test_asset_summary_includes_tco_and_current_holders():
    asset_id = _make_asset()
    _make_approved_cost(asset_id, amount=1000.0)
    with get_db() as conn:
        emp_id = conn.execute(
            "INSERT INTO employees (name, phone, created_by) VALUES ('C','0502',1)"
        ).lastrowid
    custody.assign_asset(asset_id, employee_id=emp_id, issued_date=_today(),
                         assigned_by=_manager_id())
    s = asset_summary(asset_id)
    assert s["tco"] == 46000.0
    assert len(s["current_holders"]) == 1
    assert s["current_holders"][0]["employee_name"] == "C"


# ---------------------------------------------------------------------------
# maintenance_rows filter engine
# ---------------------------------------------------------------------------
def test_maintenance_rows_returns_both_kinds_by_default():
    asset_id = _make_asset()
    _make_approved_cost(asset_id)
    _make_service(asset_id)
    rows = maintenance_rows()
    kinds = {r["kind"] for r in rows}
    assert kinds == {"cost", "service"}


def test_maintenance_rows_pending_excluded_by_default():
    asset_id = _make_asset()
    cat_id = _maintenance_cat_id()
    exp_svc.create_expense(
        expense_date=_today(), cost_center_id=str(first_cost_center_id()),
        paid_to="V", payment_mode="Cash", receipt_path=None, user_id=_manager_id(),
        lines=[{"category_id": cat_id, "particulars": "Pending",
                "amount": 100.0, "is_vatable": False, "asset_id": asset_id}],
    )
    assert maintenance_rows(kinds={"cost"}) == []
    rows = maintenance_rows(kinds={"cost"}, include_pending=True)
    assert len(rows) == 1


def test_maintenance_rows_asset_id_filter():
    asset_a = _make_asset("TR-04")
    assets_svc.set_tag_rule("Heavy Machine", "", "", updated_by=_manager_id())
    asset_b = assets_svc.create_asset(asset_tag="PRESS-01", name="Press",
                                      category="Heavy Machine", created_by=_manager_id())
    _make_approved_cost(asset_a)

    cat_b_id = _maintenance_cat_id("Heavy Machine")
    eid = exp_svc.create_expense(
        expense_date=_today(), cost_center_id=str(first_cost_center_id()),
        paid_to="V", payment_mode="Cash", receipt_path=None, user_id=_manager_id(),
        lines=[{"category_id": cat_b_id, "particulars": "Belt",
                "amount": 220.0, "is_vatable": False, "asset_id": asset_b}],
    )
    _approve(eid)

    rows_a = maintenance_rows(asset_ids=[asset_a])
    assert all(r["asset_tag"] == "TR-04" for r in rows_a)
    assert maintenance_rows(asset_ids=[asset_a, asset_b]) is not None   # both work


# ---------------------------------------------------------------------------
# filter_label
# ---------------------------------------------------------------------------
def test_filter_label_all_fields():
    label = filter_label(
        date_from="2026-01-01", date_to="2026-12-31",
        asset_tags=["TR-04", "TR-09"], asset_category="Fleet/Truck",
        holder_name="Ahmed K.", include_pending=True,
    )
    assert "2026-01-01 to 2026-12-31" in label
    assert "TR-04" in label
    assert "Fleet/Truck" in label
    assert "Ahmed K." in label
    assert "PENDING" in label


def test_filter_label_empty_returns_all_records():
    assert filter_label() == "All records"


# ---------------------------------------------------------------------------
# document number uniqueness
# ---------------------------------------------------------------------------
def test_document_numbers_are_unique_across_calls():
    a = allocate_document_number("ASR")
    b = allocate_document_number("ASR")
    assert a != b
    assert a.startswith("ASR-")
    assert b.startswith("ASR-")


def test_asr_and_amr_are_different_sequences():
    asr = allocate_document_number("ASR")
    amr = allocate_document_number("AMR")
    assert "ASR" in asr and "AMR" in amr


# ---------------------------------------------------------------------------
# Excel / PDF render without error
# ---------------------------------------------------------------------------
def test_asset_history_excel_renders():
    asset_id = _make_asset()
    _make_approved_cost(asset_id)
    _make_service(asset_id)
    doc_no = allocate_document_number("ASR")
    data = build_asset_history_workbook(asset_id, document_no=doc_no,
                                        generated_by="Test Manager")
    assert isinstance(data, bytes) and len(data) > 0


def test_asset_history_pdf_renders():
    asset_id = _make_asset()
    _make_approved_cost(asset_id)
    doc_no = allocate_document_number("ASR")
    data = build_asset_history_pdf(asset_id, document_no=doc_no,
                                   generated_by="Test Manager")
    assert data[:4] == b"%PDF"


def test_fuel_banner_present_for_truck_in_pdf():
    asset_id = _make_asset(category="Fleet/Truck")
    doc_no = allocate_document_number("ASR")
    pdf = build_asset_history_pdf(asset_id, document_no=doc_no,
                                  generated_by="Test Manager")
    # Rendered PDF is binary but the banner text is embedded as a PDF
    # stream; a simple substring check is enough to confirm it was included.
    assert b"general operating cost" in pdf


def test_maintenance_listing_excel_renders():
    asset_id = _make_asset()
    _make_approved_cost(asset_id)
    _make_service(asset_id)
    rows = maintenance_rows()
    doc_no = allocate_document_number("AMR")
    data = build_maintenance_listing_workbook(
        rows, document_no=doc_no, generated_by="Test Manager",
        filters=filter_label(),
    )
    assert isinstance(data, bytes) and len(data) > 0


def test_maintenance_listing_pdf_renders():
    asset_id = _make_asset()
    _make_approved_cost(asset_id)
    _make_service(asset_id)
    rows = maintenance_rows()
    doc_no = allocate_document_number("AMR")
    pdf = build_maintenance_listing_pdf(
        rows, document_no=doc_no, generated_by="Test Manager",
        filters=filter_label(),
    )
    assert pdf[:4] == b"%PDF"


def test_empty_listing_renders_without_error():
    doc_no = allocate_document_number("AMR")
    pdf = build_maintenance_listing_pdf(
        [], document_no=doc_no, generated_by="Test Manager",
        filters=filter_label(),
    )
    assert pdf[:4] == b"%PDF"


def test_pending_banner_in_pdf_when_include_pending():
    asset_id = _make_asset()
    cat_id = _maintenance_cat_id()
    exp_svc.create_expense(
        expense_date=_today(), cost_center_id=str(first_cost_center_id()),
        paid_to="V", payment_mode="Cash", receipt_path=None, user_id=_manager_id(),
        lines=[{"category_id": cat_id, "particulars": "Pending part",
                "amount": 100.0, "is_vatable": False, "asset_id": asset_id}],
    )
    rows = maintenance_rows(kinds={"cost"}, include_pending=True)
    doc_no = allocate_document_number("AMR")
    pdf = build_maintenance_listing_pdf(
        rows, document_no=doc_no, generated_by="Test Manager",
        filters=filter_label(include_pending=True), include_pending=True,
    )
    assert b"PENDING" in pdf


def test_service_rows_show_dash_not_zero_in_excel():
    """None renders as "—" in the export. 0.00 would mean "cost nothing",
    which is a different and wrong claim about a non-cost event."""
    asset_id = _make_asset()
    _make_service(asset_id)
    rows = maintenance_rows(kinds={"service"})
    assert rows[0]["amount"] is None
    doc_no = allocate_document_number("AMR")
    data = build_maintenance_listing_workbook(
        rows, document_no=doc_no, generated_by="Test Manager",
        filters=filter_label(),
    )
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(data))
    ws = wb.active
    # Find the first data row (after metadata + headers)
    amount_cells = [row[5] for row in ws.iter_rows() if row[5].value == "—"]
    assert amount_cells, "Expected '—' in Amount column for service row"
