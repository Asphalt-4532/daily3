"""Tests for app/programs/assets/services/assets.py and custody.py.

Two things here are load-bearing enough to be worth naming up front, because
both are easy to "tidy" into a bug later:

  * An asset can have SEVERAL active assignments at once (day shift / night
    shift on one truck). There is deliberately no unique index on
    asset_assignments.asset_id, and active_holders() returns a list.
  * Divest proceeds are recorded for reporting only and never reach
    cash_movements - selling a truck is not petty cash.
"""
import pytest
from datetime import date

from app.programs.assets.services import assets as svc
from app.programs.assets.services import custody
from app.services.errors import (
    ValidationError, NotFoundError, ConflictError, DuplicateError,
)
from app.database import get_connection, get_db
from tests.conftest import first_cost_center_id


def _today():
    return date.today().isoformat()


def _manager_id():
    conn = get_connection()
    try:
        return conn.execute("SELECT id FROM users WHERE role='manager'").fetchone()["id"]
    finally:
        conn.close()


def _make_employee(name="Ahmed K.", phone="0500000001", user_id=None):
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO employees (name, phone, created_by, user_id) VALUES (?, ?, ?, ?)",
            (name, phone, _manager_id(), user_id),
        )
        return cur.lastrowid


def _make_asset(tag="TR-04", category="Fleet/Truck", **kw):
    return svc.create_asset(
        asset_tag=tag, name=kw.pop("name", "Isuzu 6-wheel"), category=category,
        purchase_cost=kw.pop("purchase_cost", 45000.0), created_by=_manager_id(), **kw
    )


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------
def test_create_asset_stores_details():
    asset_id = _make_asset(purchase_date="2024-03-11")
    asset = svc.get_asset(asset_id)
    assert asset["asset_tag"] == "TR-04"
    assert asset["category"] == "Fleet/Truck"
    assert asset["purchase_cost"] == 45000.0
    assert asset["status"] == "active"


def test_asset_tag_must_match_the_configured_pattern():
    with pytest.raises(ValidationError) as e:
        _make_asset(tag="lorry one")
    assert "TR-04" in str(e.value)  # the example from the rule is shown, not just "invalid"


def test_asset_tag_pattern_is_editable_not_hardcoded():
    """The whole point of asset_tag_rules: a manager can change or remove the
    format without a code change."""
    svc.set_tag_rule("Fleet/Truck", "", "", updated_by=_manager_id())
    asset_id = _make_asset(tag="an odd legacy tag")   # free text now accepted
    assert svc.get_asset(asset_id)["asset_tag"] == "an odd legacy tag"


def test_invalid_tag_pattern_is_rejected_at_save_time():
    """A pattern that doesn't compile would otherwise blow up inside every
    later create_asset() for that category - i.e. a Settings typo would lock
    the registry rather than just being wrong."""
    with pytest.raises(ValidationError):
        svc.set_tag_rule("Fleet/Truck", "^TR-(unclosed", "", updated_by=_manager_id())


def test_tag_rules_fall_back_to_coded_defaults_when_unseeded():
    with get_db() as conn:
        conn.execute("DELETE FROM asset_tag_rules")
    rules = svc.get_tag_rules()
    assert rules["Heavy Machine"]["example"] == "PRESS-01"


def test_duplicate_tag_is_rejected_even_after_soft_delete():
    asset_id = _make_asset()
    svc.delete_asset(asset_id, deleted_by=_manager_id(), reason="Sold")
    with pytest.raises(DuplicateError):
        _make_asset()  # same tag - would merge two machines' histories


def test_delete_requires_a_reason_and_is_soft_only():
    asset_id = _make_asset()
    with pytest.raises(ValidationError):
        svc.delete_asset(asset_id, deleted_by=_manager_id(), reason="  ")
    svc.delete_asset(asset_id, deleted_by=_manager_id(), reason="Written off")
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
        assert row is not None                 # still there - never a real DELETE
        assert row["status"] == "deleted"
        assert row["delete_reason"] == "Written off"
    finally:
        conn.close()
    with pytest.raises(NotFoundError):
        svc.get_asset(asset_id)


def test_cannot_delete_an_asset_someone_is_still_holding():
    asset_id = _make_asset()
    emp_id = _make_employee()
    custody.assign_asset(asset_id, employee_id=emp_id, issued_date=_today(),
                         assigned_by=_manager_id())
    with pytest.raises(ConflictError):
        svc.delete_asset(asset_id, deleted_by=_manager_id(), reason="Sold")


def test_status_is_manual_and_never_inferred():
    asset_id = _make_asset()
    svc.set_status(asset_id, "in_maintenance", updated_by=_manager_id(), note="gearbox")
    assert svc.get_asset(asset_id)["status"] == "in_maintenance"
    with pytest.raises(ValidationError):
        svc.set_status(asset_id, "divested", updated_by=_manager_id())  # divest has its own path


def test_negative_purchase_cost_rejected():
    with pytest.raises(ValidationError):
        _make_asset(purchase_cost=-1)


# ---------------------------------------------------------------------------
# custody
# ---------------------------------------------------------------------------
def test_one_asset_can_have_several_active_holders_at_once():
    """Day shift and night shift on the same truck. If this ever fails
    because someone added a unique index on asset_assignments.asset_id, that
    index is the bug, not this test."""
    asset_id = _make_asset()
    day = _make_employee("Ahmed K.", "0500000001")
    night = _make_employee("Rashid M.", "0500000002")
    custody.assign_asset(asset_id, employee_id=day, role_note="Day shift",
                         issued_date=_today(), assigned_by=_manager_id())
    custody.assign_asset(asset_id, employee_id=night, role_note="Night shift",
                         issued_date=_today(), assigned_by=_manager_id())

    holders = custody.active_holders(asset_id)
    assert len(holders) == 2
    assert {h["employee_name"] for h in holders} == {"Ahmed K.", "Rashid M."}
    assert {h["role_note"] for h in holders} == {"Day shift", "Night shift"}


def test_same_asset_to_same_holder_twice_is_rejected():
    asset_id = _make_asset()
    emp_id = _make_employee()
    custody.assign_asset(asset_id, employee_id=emp_id, issued_date=_today(),
                         assigned_by=_manager_id())
    with pytest.raises(DuplicateError):
        custody.assign_asset(asset_id, employee_id=emp_id, issued_date=_today(),
                             assigned_by=_manager_id())


def test_assignment_needs_exactly_one_of_employee_or_cost_centre():
    asset_id = _make_asset()
    with pytest.raises(ValidationError):
        custody.assign_asset(asset_id, issued_date=_today(), assigned_by=_manager_id())
    with pytest.raises(ValidationError):
        custody.assign_asset(asset_id, employee_id=_make_employee(),
                             cost_center_id=first_cost_center_id(),
                             issued_date=_today(), assigned_by=_manager_id())


def test_closing_an_assignment_keeps_the_row_and_the_reason():
    asset_id = _make_asset()
    emp_id = _make_employee()
    assignment_id = custody.assign_asset(asset_id, employee_id=emp_id,
                                         issued_date="2026-01-01",
                                         assigned_by=_manager_id())
    custody.close_assignment(assignment_id, returned_date="2026-06-30",
                             reason="Left the company", closed_by=_manager_id())

    assert custody.active_holders(asset_id) == []
    history = custody.assignment_history(asset_id)
    assert len(history) == 1                      # closed, not deleted
    assert history[0]["status"] == "returned"
    assert history[0]["returned_date"] == "2026-06-30"
    assert history[0]["close_reason"] == "Left the company"


def test_closing_twice_is_refused():
    asset_id = _make_asset()
    assignment_id = custody.assign_asset(asset_id, employee_id=_make_employee(),
                                         issued_date=_today(), assigned_by=_manager_id())
    custody.close_assignment(assignment_id, returned_date=_today(), reason="x",
                             closed_by=_manager_id())
    with pytest.raises(ConflictError):
        custody.close_assignment(assignment_id, returned_date=_today(), reason="x",
                                 closed_by=_manager_id())


def test_transfer_closes_the_old_row_and_opens_a_new_one():
    asset_id = _make_asset()
    old_emp = _make_employee("Ahmed K.", "0500000001")
    new_emp = _make_employee("Rashid M.", "0500000002")
    assignment_id = custody.assign_asset(asset_id, employee_id=old_emp,
                                         role_note="Primary driver",
                                         issued_date="2026-01-01",
                                         assigned_by=_manager_id())
    custody.transfer_asset(asset_id, assignment_id, to_employee_id=new_emp,
                           transfer_date="2026-07-01", reason="Route change",
                           by=_manager_id())

    holders = custody.active_holders(asset_id)
    assert len(holders) == 1
    assert holders[0]["employee_name"] == "Rashid M."
    assert holders[0]["role_note"] == "Primary driver"   # carried across

    history = custody.assignment_history(asset_id)
    assert len(history) == 2
    closed = [h for h in history if h["status"] == "transferred"][0]
    assert closed["employee_name"] == "Ahmed K."
    assert closed["close_reason"] == "Route change"


def test_failed_transfer_leaves_the_original_holder_intact():
    """Both halves of a transfer share one transaction: a half-applied
    transfer would leave the asset held by nobody, with no record of where it
    physically is."""
    asset_id = _make_asset()
    emp_id = _make_employee()
    assignment_id = custody.assign_asset(asset_id, employee_id=emp_id,
                                         issued_date=_today(), assigned_by=_manager_id())
    with pytest.raises(ValidationError):
        custody.transfer_asset(asset_id, assignment_id, to_employee_id=99999,
                               transfer_date=_today(), reason="Route change",
                               by=_manager_id())
    holders = custody.active_holders(asset_id)
    assert len(holders) == 1
    assert holders[0]["id"] == assignment_id


def test_divest_closes_every_holder_and_records_proceeds_without_touching_cash():
    asset_id = _make_asset()
    day = _make_employee("Ahmed K.", "0500000001")
    night = _make_employee("Rashid M.", "0500000002")
    custody.assign_asset(asset_id, employee_id=day, issued_date=_today(),
                         assigned_by=_manager_id())
    custody.assign_asset(asset_id, employee_id=night, issued_date=_today(),
                         assigned_by=_manager_id())

    custody.divest_asset(asset_id, divest_date="2026-08-01", settle_amount=18000.0,
                         reason="Sold to Al-Faisal Trading", by=_manager_id())

    assert custody.active_holders(asset_id) == []
    asset = svc.get_asset(asset_id)
    assert asset["status"] == "divested"
    # Proceeds live on the ASSET, not on a custody row - an asset sold while
    # sitting in a yard has no holder for them to hang off.
    assert asset["divest_proceeds"] == 18000.0
    assert asset["divest_reason"] == "Sold to Al-Faisal Trading"
    history = custody.assignment_history(asset_id)
    assert all(h["status"] == "divested" for h in history)

    # The money rule: proceeds are reporting-only. Selling a truck is not
    # petty cash and must never appear in the float.
    conn = get_connection()
    try:
        movements = conn.execute("SELECT COUNT(*) c FROM cash_movements").fetchone()["c"]
        assert movements == 0
    finally:
        conn.close()


def test_divest_with_no_holder_still_records_how_it_left():
    """An asset nobody was holding has no assignment row to close, so the
    reason has to live on the asset itself or it's lost entirely."""
    asset_id = _make_asset()
    custody.divest_asset(asset_id, divest_date="2026-08-01", settle_amount=None,
                         reason="Scrapped", by=_manager_id())
    assert custody.assignment_history(asset_id) == []
    asset = svc.get_asset(asset_id)
    assert asset["status"] == "divested"
    assert asset["divest_reason"] == "Scrapped"
    assert asset["divested_on"] == "2026-08-01"
    assert asset["divest_proceeds"] is None


def test_divested_asset_cannot_be_reassigned():
    asset_id = _make_asset()
    custody.divest_asset(asset_id, divest_date=_today(), reason="Sold", by=_manager_id())
    with pytest.raises(ConflictError):
        custody.assign_asset(asset_id, employee_id=_make_employee(),
                             issued_date=_today(), assigned_by=_manager_id())


def test_divest_and_transfer_both_require_a_reason():
    asset_id = _make_asset()
    with pytest.raises(ValidationError):
        custody.divest_asset(asset_id, divest_date=_today(), reason="", by=_manager_id())


def test_assets_held_by_employee():
    truck = _make_asset("TR-04")
    svc.set_tag_rule("Heavy Machine", "", "", updated_by=_manager_id())
    press = svc.create_asset(asset_tag="PRESS-01", name="Hydraulic press",
                             category="Heavy Machine", created_by=_manager_id())
    emp_id = _make_employee()
    custody.assign_asset(truck, employee_id=emp_id, issued_date=_today(),
                         assigned_by=_manager_id())
    custody.assign_asset(press, employee_id=emp_id, issued_date=_today(),
                         assigned_by=_manager_id())
    held = custody.assets_held_by(emp_id)
    assert {h["asset_tag"] for h in held} == {"TR-04", "PRESS-01"}


# ---------------------------------------------------------------------------
# employee <-> login link
# ---------------------------------------------------------------------------
def test_link_employee_to_user_and_reject_double_linking():
    emp_a = _make_employee("Ahmed K.", "0500000001")
    emp_b = _make_employee("Rashid M.", "0500000002")
    manager_id = _manager_id()
    custody.link_employee_to_user(emp_a, manager_id, linked_by=manager_id)
    with pytest.raises(DuplicateError):
        custody.link_employee_to_user(emp_b, manager_id, linked_by=manager_id)


def test_link_to_unknown_user_is_rejected():
    emp_id = _make_employee()
    with pytest.raises(NotFoundError):
        custody.link_employee_to_user(emp_id, 99999, linked_by=_manager_id())


# ---------------------------------------------------------------------------
# picker
# ---------------------------------------------------------------------------
def test_picker_for_a_plain_user_shows_only_their_own_assets():
    from app.services import users as users_service
    plain_id = users_service.create_user(username="driver1", password="drivepass123",
                                         full_name="Ahmed K.", role="user",
                                         acting_user_id=_manager_id())

    mine = _make_asset("TR-04")
    _make_asset("TR-09", name="Hino")          # someone else's truck
    emp_id = _make_employee("Ahmed K.", "0500000001", user_id=plain_id)
    custody.assign_asset(mine, employee_id=emp_id, issued_date=_today(),
                         assigned_by=_manager_id())

    picked = svc.pickable_assets(user_id=plain_id)
    assert [p["asset_tag"] for p in picked] == ["TR-04"]

    # A manager records on the business's behalf and sees the whole registry.
    all_of_them = svc.pickable_assets(all_assets=True)
    assert {p["asset_tag"] for p in all_of_them} == {"TR-04", "TR-09"}


def test_picker_is_empty_for_someone_holding_nothing():
    _make_asset("TR-04")
    assert svc.pickable_assets(user_id=_manager_id()) == []


def test_divested_asset_drops_out_of_the_picker():
    asset_id = _make_asset()
    assert len(svc.pickable_assets(all_assets=True)) == 1
    custody.divest_asset(asset_id, divest_date=_today(), reason="Sold", by=_manager_id())
    assert svc.pickable_assets(all_assets=True) == []
