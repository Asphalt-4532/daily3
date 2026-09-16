"""Tests for app/programs/assets/services/service_log.py, plus the
voucher-side rule that an asset is only *demanded* from someone who actually
has one to pick.

The two properties worth stating outright, because both are easy to break by
"tidying":

  * asset_service_log carries NO amount, ever. Money reaches an asset only
    through an approved voucher line.
  * A driver holding no truck can still record costs. The asset requirement
    is scoped to what that person could actually choose.
"""
import io
import os
import stat
import sys
import pytest
from datetime import date

from app.programs.assets.services import assets as assets_svc
from app.programs.assets.services import custody
from app.programs.assets.services import service_log as svc
from app.programs.expenses.services import expenses as exp_svc
from app.services import users as users_service
from app.services.errors import ValidationError, NotFoundError, ForbiddenError
from app.database import get_connection, get_db
from app.config import UPLOADS_DIR
from tests.conftest import first_cost_center_id

# A minimal but genuinely valid PNG - the upload path checks real byte
# signatures, not the claimed extension, so a fake payload would be rejected.
_PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
        b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")


def _today():
    return date.today().isoformat()


def _user_row(user_id):
    conn = get_connection()
    try:
        return dict(conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())
    finally:
        conn.close()


def _manager():
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM users WHERE role='manager'").fetchone()
        return dict(row)
    finally:
        conn.close()


def _make_driver(username="driver1", name="Ahmed K.", phone="0500000001"):
    """A plain 'user' login with an employee record linked to it - the shape
    an actual driver has once a manager has given them app access."""
    user_id = users_service.create_user(
        username=username, password="drivepass123", full_name=name,
        role="user", acting_user_id=_manager()["id"],
    )
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO employees (name, phone, created_by, user_id) VALUES (?, ?, ?, ?)",
            (name, phone, _manager()["id"], user_id),
        )
        employee_id = cur.lastrowid
    return _user_row(user_id), employee_id


def _make_truck(tag="TR-04"):
    return assets_svc.create_asset(asset_tag=tag, name="Isuzu 6-wheel",
                                   category="Fleet/Truck", purchase_cost=45000.0,
                                   created_by=_manager()["id"])


def _maintenance_category_id():
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM categories WHERE requires_asset=1 AND asset_category='Fleet/Truck'"
        ).fetchone()
        assert row, "vehicle maintenance category not seeded as requiring an asset"
        return row["id"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# the table itself
# ---------------------------------------------------------------------------
def test_service_log_has_no_amount_column_at_all():
    """Not "nullable and discouraged" - absent. A second place to record
    money is a second grand total that drifts from the first."""
    conn = get_connection()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(asset_service_log)").fetchall()}
    finally:
        conn.close()
    assert not {"amount", "cost", "vat_amount", "price"} & cols


# ---------------------------------------------------------------------------
# who may log
# ---------------------------------------------------------------------------
def test_assignee_can_log_service_on_their_own_truck():
    truck = _make_truck()
    driver, employee_id = _make_driver()
    custody.assign_asset(truck, employee_id=employee_id, issued_date=_today(),
                         assigned_by=_manager()["id"])

    service_id, warnings = svc.log_service(
        truck, service_date=_today(), service_type="Greasing/Lubrication",
        description="Greased chassis points", recorded_by_user=driver,
    )
    assert service_id
    assert warnings == []
    entries = svc.list_services(truck)
    assert entries[0]["description"] == "Greased chassis points"
    assert entries[0]["recorded_by_name"] == "Ahmed K."


def test_someone_the_asset_is_not_assigned_to_is_refused():
    truck = _make_truck()
    driver, _employee_id = _make_driver()   # linked login, but holds nothing
    with pytest.raises(ForbiddenError):
        svc.log_service(truck, service_date=_today(), service_type="Inspection",
                        description="Poked at it", recorded_by_user=driver)


def test_manager_can_log_on_any_asset():
    truck = _make_truck()
    service_id, _ = svc.log_service(
        truck, service_date=_today(), service_type="Inspection",
        description="Annual check", recorded_by_user=_manager(),
    )
    assert service_id


def test_unlinked_employee_is_not_recognised_as_the_assignee():
    """employees.user_id is set by a manager and never guessed from a name -
    an employee record with no login attached can't match anyone."""
    truck = _make_truck()
    driver, _ = _make_driver()
    with get_db() as conn:
        other_employee = conn.execute(
            "INSERT INTO employees (name, phone, created_by) VALUES ('Ahmed K.', '0500000009', 1)"
        ).lastrowid
    custody.assign_asset(truck, employee_id=other_employee, issued_date=_today(),
                         assigned_by=_manager()["id"])
    with pytest.raises(ForbiddenError):
        svc.log_service(truck, service_date=_today(), service_type="Cleaning",
                        description="Washed it", recorded_by_user=driver)


def test_closing_the_assignment_ends_the_right_to_log():
    truck = _make_truck()
    driver, employee_id = _make_driver()
    assignment_id = custody.assign_asset(truck, employee_id=employee_id,
                                         issued_date=_today(),
                                         assigned_by=_manager()["id"])
    svc.log_service(truck, service_date=_today(), service_type="Cleaning",
                    description="Washed it", recorded_by_user=driver)
    custody.close_assignment(assignment_id, returned_date=_today(),
                             reason="Route change", closed_by=_manager()["id"])
    with pytest.raises(ForbiddenError):
        svc.log_service(truck, service_date=_today(), service_type="Cleaning",
                        description="Washed it again", recorded_by_user=driver)


# ---------------------------------------------------------------------------
# content rules
# ---------------------------------------------------------------------------
def test_description_and_type_are_validated():
    truck = _make_truck()
    manager = _manager()
    with pytest.raises(ValidationError):
        svc.log_service(truck, service_date=_today(), service_type="Inspection",
                        description="   ", recorded_by_user=manager)
    with pytest.raises(ValidationError):
        svc.log_service(truck, service_date=_today(), service_type="Exorcism",
                        description="Something odd", recorded_by_user=manager)


def test_meter_reading_is_stored_with_its_unit():
    truck = _make_truck()
    svc.log_service(truck, service_date=_today(), service_type="Inspection",
                    description="Service due check", meter_reading=142500,
                    meter_unit="km", recorded_by_user=_manager())
    last = svc.last_meter_reading(truck)
    assert last["meter_reading"] == 142500
    assert last["meter_unit"] == "km"


def test_meter_reading_needs_a_valid_unit():
    truck = _make_truck()
    with pytest.raises(ValidationError):
        svc.log_service(truck, service_date=_today(), service_type="Inspection",
                        description="x", meter_reading=100, meter_unit="miles",
                        recorded_by_user=_manager())


def test_backwards_meter_warns_but_does_not_block():
    """Usually a typo, sometimes a replaced meter. Refusing the note would
    just mean the maintenance never gets recorded at all."""
    truck = _make_truck()
    manager = _manager()
    svc.log_service(truck, service_date="2026-01-01", service_type="Inspection",
                    description="First", meter_reading=142500, meter_unit="km",
                    recorded_by_user=manager)
    service_id, warnings = svc.log_service(
        truck, service_date="2026-02-01", service_type="Inspection",
        description="Second", meter_reading=1000, meter_unit="km",
        recorded_by_user=manager,
    )
    assert service_id                       # saved anyway
    assert len(warnings) == 1
    assert "lower" in warnings[0]


def test_suggested_meter_unit_follows_the_asset_category():
    assert svc.suggested_meter_unit("Fleet/Truck") == "km"
    assert svc.suggested_meter_unit("Heavy Machine") == "hours"
    assert svc.suggested_meter_unit("Furniture") is None


# ---------------------------------------------------------------------------
# photos - same sandbox path as a voucher receipt
# ---------------------------------------------------------------------------
def test_photo_is_validated_by_real_bytes_not_by_extension():
    with pytest.raises(ValidationError):
        svc.save_service_photo("greasing.png", io.BytesIO(b"#!/bin/sh\nrm -rf /"))


def test_photo_extension_allowlist_is_enforced():
    with pytest.raises(ValidationError):
        svc.save_service_photo("greasing.sh", io.BytesIO(_PNG))


@pytest.mark.skipif(sys.platform == "win32",
                    reason="NTFS has no POSIX execute bit to strip")
def test_saved_photo_lands_nonexecutable():
    name = svc.save_service_photo("greasing.png", io.BytesIO(_PNG))
    path = UPLOADS_DIR / name
    assert path.exists()
    mode = os.stat(path).st_mode
    assert not mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_photo_path_is_stored_on_the_entry():
    truck = _make_truck()
    name = svc.save_service_photo("greasing.png", io.BytesIO(_PNG))
    svc.log_service(truck, service_date=_today(), service_type="Part fitted",
                    description="New spring bolt", photo_path=name,
                    recorded_by_user=_manager())
    assert svc.list_services(truck)[0]["photo_path"] == name


# ---------------------------------------------------------------------------
# ownership on edit/delete
# ---------------------------------------------------------------------------
def test_only_the_recorder_or_a_manager_can_edit_an_entry():
    truck = _make_truck()
    driver_a, emp_a = _make_driver("driver1", "Ahmed K.", "0500000001")
    driver_b, emp_b = _make_driver("driver2", "Rashid M.", "0500000002")
    custody.assign_asset(truck, employee_id=emp_a, issued_date=_today(),
                         assigned_by=_manager()["id"])
    custody.assign_asset(truck, employee_id=emp_b, issued_date=_today(),
                         assigned_by=_manager()["id"])
    service_id, _ = svc.log_service(truck, service_date=_today(),
                                    service_type="Cleaning", description="Washed it",
                                    recorded_by_user=driver_a)

    # Both hold the same truck, but the entry is still Ahmed's.
    with pytest.raises(ForbiddenError):
        svc.update_service(service_id, acting_user=driver_b, description="Actually I did it")

    svc.update_service(service_id, acting_user=driver_a, description="Washed and waxed")
    assert svc.list_services(truck)[0]["description"] == "Washed and waxed"
    svc.update_service(service_id, acting_user=_manager(), description="Manager correction")
    assert svc.list_services(truck)[0]["description"] == "Manager correction"


def test_delete_is_soft_and_needs_a_reason():
    truck = _make_truck()
    manager = _manager()
    service_id, _ = svc.log_service(truck, service_date=_today(),
                                    service_type="Cleaning", description="Washed it",
                                    recorded_by_user=manager)
    with pytest.raises(ValidationError):
        svc.delete_service(service_id, acting_user=manager, reason="")
    svc.delete_service(service_id, acting_user=manager, reason="Logged on the wrong truck")

    assert svc.list_services(truck) == []
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM asset_service_log WHERE id=?", (service_id,)).fetchone()
        assert row["status"] == "deleted"          # still there
        assert row["delete_reason"] == "Logged on the wrong truck"
    finally:
        conn.close()


def test_list_services_filters():
    truck = _make_truck()
    manager = _manager()
    svc.log_service(truck, service_date="2026-01-15", service_type="Greasing/Lubrication",
                    description="Jan grease", recorded_by_user=manager)
    svc.log_service(truck, service_date="2026-06-15", service_type="Inspection",
                    description="Jun check", recorded_by_user=manager)

    assert len(svc.list_services(truck, date_from="2026-06-01")) == 1
    assert len(svc.list_services(truck, date_to="2026-02-01")) == 1
    assert len(svc.list_services(truck, service_type="Inspection")) == 1
    assert len(svc.list_services(truck)) == 2


# ---------------------------------------------------------------------------
# the voucher-side rule: don't demand an asset from someone who has none
# ---------------------------------------------------------------------------
def test_driver_with_no_asset_assigned_can_still_record_maintenance_cost():
    """A driver holding no truck still has to be able to say what they spent.
    The voucher goes to pending exactly as normal for a manager to check."""
    driver, _employee_id = _make_driver()
    expense_id = exp_svc.create_expense(
        expense_date=_today(), cost_center_id=str(first_cost_center_id()),
        paid_to="Roadside garage", payment_mode="Cash", receipt_path=None,
        user_id=driver["id"],
        lines=[{"category_id": _maintenance_category_id(),
                "particulars": "Puncture repair", "amount": 80.0, "is_vatable": False}],
    )
    expense = exp_svc.get_expense(expense_id)
    assert expense["status"] == "pending"
    assert expense["lines"][0]["asset_id"] is None


def test_asset_is_demanded_again_once_a_truck_is_assigned():
    """Not a loophole: the moment they hold something, the field is required."""
    truck = _make_truck()
    driver, employee_id = _make_driver()
    custody.assign_asset(truck, employee_id=employee_id, issued_date=_today(),
                         assigned_by=_manager()["id"])

    with pytest.raises(ValidationError) as e:
        exp_svc.create_expense(
            expense_date=_today(), cost_center_id=str(first_cost_center_id()),
            paid_to="Al-Rajhi Motors", payment_mode="Cash", receipt_path=None,
            user_id=driver["id"],
            lines=[{"category_id": _maintenance_category_id(),
                    "particulars": "Brake pads", "amount": 640.0, "is_vatable": False}],
        )
    assert "asset is required" in str(e.value)


def test_driver_can_submit_once_they_name_their_truck():
    truck = _make_truck()
    driver, employee_id = _make_driver()
    custody.assign_asset(truck, employee_id=employee_id, issued_date=_today(),
                         assigned_by=_manager()["id"])
    expense_id = exp_svc.create_expense(
        expense_date=_today(), cost_center_id=str(first_cost_center_id()),
        paid_to="Al-Rajhi Motors", payment_mode="Cash", receipt_path=None,
        user_id=driver["id"],
        lines=[{"category_id": _maintenance_category_id(), "particulars": "Brake pads",
                "amount": 640.0, "is_vatable": False, "asset_id": truck}],
    )
    line = exp_svc.get_expense(expense_id)["lines"][0]
    assert line["asset_id"] == truck
    assert line["asset_tag"] == "TR-04"


def test_manager_is_always_asked_because_the_whole_registry_is_theirs_to_pick():
    _make_truck()
    with pytest.raises(ValidationError):
        exp_svc.create_expense(
            expense_date=_today(), cost_center_id=str(first_cost_center_id()),
            paid_to="Al-Rajhi Motors", payment_mode="Cash", receipt_path=None,
            user_id=_manager()["id"],
            lines=[{"category_id": _maintenance_category_id(), "particulars": "Brake pads",
                    "amount": 640.0, "is_vatable": False}],
        )


def test_wrong_type_of_asset_is_rejected_for_the_category():
    """A vehicle-maintenance line must not be attached to a press."""
    assets_svc.set_tag_rule("Heavy Machine", "", "", updated_by=_manager()["id"])
    press = assets_svc.create_asset(asset_tag="PRESS-01", name="Hydraulic press",
                                    category="Heavy Machine", created_by=_manager()["id"])
    with pytest.raises(ValidationError):
        exp_svc.create_expense(
            expense_date=_today(), cost_center_id=str(first_cost_center_id()),
            paid_to="Al-Rajhi Motors", payment_mode="Cash", receipt_path=None,
            user_id=_manager()["id"],
            lines=[{"category_id": _maintenance_category_id(), "particulars": "Brake pads",
                    "amount": 640.0, "is_vatable": False, "asset_id": press}],
        )


def test_divested_asset_cannot_have_new_cost_attached():
    truck = _make_truck()
    custody.divest_asset(truck, divest_date=_today(), reason="Sold",
                         by=_manager()["id"])
    with pytest.raises(ValidationError):
        exp_svc.create_expense(
            expense_date=_today(), cost_center_id=str(first_cost_center_id()),
            paid_to="Al-Rajhi Motors", payment_mode="Cash", receipt_path=None,
            user_id=_manager()["id"],
            lines=[{"category_id": _maintenance_category_id(), "particulars": "Brake pads",
                    "amount": 640.0, "is_vatable": False, "asset_id": truck}],
        )


def test_one_voucher_can_carry_lines_for_several_different_assets():
    """The multi-line/multi-asset case: one payment covering a truck, a press
    and an office fix, each line landing on its own asset."""
    manager = _manager()
    truck = _make_truck("TR-04")
    assets_svc.set_tag_rule("Heavy Machine", "", "", updated_by=manager["id"])
    press = assets_svc.create_asset(asset_tag="PRESS-01", name="Hydraulic press",
                                    category="Heavy Machine", created_by=manager["id"])
    conn = get_connection()
    try:
        machine_cat = conn.execute(
            "SELECT id FROM categories WHERE asset_category='Heavy Machine'"
        ).fetchone()["id"]
    finally:
        conn.close()

    expense_id = exp_svc.create_expense(
        expense_date=_today(), cost_center_id=str(first_cost_center_id()),
        paid_to="Al-Rajhi Motors", payment_mode="Cash", receipt_path=None,
        user_id=manager["id"],
        lines=[
            {"category_id": _maintenance_category_id(), "particulars": "Brake pads",
             "amount": 640.0, "is_vatable": False, "asset_id": truck},
            {"category_id": machine_cat, "particulars": "Drive belt",
             "amount": 220.0, "is_vatable": False, "asset_id": press},
        ],
    )
    lines = exp_svc.get_expense(expense_id)["lines"]
    assert {l["asset_tag"] for l in lines} == {"TR-04", "PRESS-01"}
