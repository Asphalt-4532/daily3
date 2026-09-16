import pytest

from app.programs.expenses.services import settings as svc
from app.services.errors import ValidationError


def test_add_category_and_list():
    svc.add_category("Office Supplies", acting_user_id=1)
    names = [c["name"] for c in svc.list_categories()]
    assert "Office Supplies" in names


def test_add_category_rejects_empty_name():
    with pytest.raises(ValidationError):
        svc.add_category("   ", acting_user_id=1)


def test_add_category_is_idempotent_for_duplicates():
    svc.add_category("Fuel", acting_user_id=1)
    before = len(svc.list_categories())
    svc.add_category("Fuel", acting_user_id=1)  # INSERT OR IGNORE - no duplicate row
    assert len(svc.list_categories()) == before


def test_toggle_category_flips_active_flag():
    svc.add_category("Temp Category", acting_user_id=1)
    cat = next(c for c in svc.list_categories() if c["name"] == "Temp Category")
    assert cat["active"] == 1
    svc.toggle_category(cat["id"], acting_user_id=1)
    updated = next(c for c in svc.list_categories() if c["id"] == cat["id"])
    assert updated["active"] == 0


def test_list_categories_active_only_filters_disabled():
    svc.add_category("Will Disable", acting_user_id=1)
    cat = next(c for c in svc.list_categories() if c["name"] == "Will Disable")
    svc.toggle_category(cat["id"], acting_user_id=1)
    active_names = [c["name"] for c in svc.list_categories(active_only=True)]
    assert "Will Disable" not in active_names


def test_add_cost_center_uppercases_code():
    svc.add_cost_center("west", "West Branch", acting_user_id=1)
    cc = next(c for c in svc.list_cost_centers() if c["name"] == "West Branch")
    assert cc["code"] == "WEST"


def test_add_cost_center_rejects_empty_code():
    with pytest.raises(ValidationError):
        svc.add_cost_center("", "Some Name", acting_user_id=1)


def test_toggle_cost_center():
    svc.add_cost_center("TMP", "Temp Centre", acting_user_id=1)
    cc = next(c for c in svc.list_cost_centers() if c["code"] == "TMP")
    svc.toggle_cost_center(cc["id"], acting_user_id=1)
    updated = next(c for c in svc.list_cost_centers() if c["id"] == cc["id"])
    assert updated["active"] == 0
