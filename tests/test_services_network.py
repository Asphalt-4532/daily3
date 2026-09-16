import pytest

from app.services import network as svc
from app.services.errors import ValidationError, NotFoundError


def test_add_network_config_happy_path():
    svc.add_network_config("Cloudflare Tunnel", "expense.example.com", "public access", 1)
    configs = svc.list_network_configs()
    assert len(configs) == 1
    assert configs[0]["name"] == "Cloudflare Tunnel"
    assert configs[0]["hostname"] == "expense.example.com"
    assert configs[0]["active"] == 1


def test_add_network_config_lowercases_hostname():
    svc.add_network_config("Test", "Expense.Example.COM", "", 1)
    assert svc.list_network_configs()[0]["hostname"] == "expense.example.com"


def test_add_network_config_requires_label():
    with pytest.raises(ValidationError):
        svc.add_network_config("", "expense.example.com", "", 1)


def test_add_network_config_rejects_empty_hostname():
    with pytest.raises(ValidationError):
        svc.add_network_config("Test", "", "", 1)


def test_add_network_config_rejects_full_url():
    with pytest.raises(ValidationError, match="just the hostname"):
        svc.add_network_config("Test", "https://expense.example.com/", "", 1)


def test_add_network_config_rejects_path():
    with pytest.raises(ValidationError, match="without a path"):
        svc.add_network_config("Test", "expense.example.com/login", "", 1)


def test_add_network_config_rejects_garbage():
    with pytest.raises(ValidationError):
        svc.add_network_config("Test", "not a hostname!!", "", 1)


def test_add_network_config_accepts_hostname_with_port():
    svc.add_network_config("LAN", "192.168.1.50:8000", "", 1)
    assert "192.168.1.50:8000" in svc.get_active_hostnames()


def test_add_network_config_rejects_duplicate_hostname():
    svc.add_network_config("First", "expense.example.com", "", 1)
    with pytest.raises(ValidationError, match="already"):
        svc.add_network_config("Second", "expense.example.com", "", 1)


def test_get_active_hostnames_empty_by_default():
    assert svc.get_active_hostnames() == set()


def test_get_active_hostnames_excludes_inactive():
    svc.add_network_config("A", "a.example.com", "", 1)
    svc.add_network_config("B", "b.example.com", "", 1)
    configs = svc.list_network_configs()
    b_id = next(c["id"] for c in configs if c["hostname"] == "b.example.com")
    svc.toggle_network_config(b_id, 1)
    assert svc.get_active_hostnames() == {"a.example.com"}


def test_toggle_network_config_flips_active_state():
    svc.add_network_config("A", "a.example.com", "", 1)
    config_id = svc.list_network_configs()[0]["id"]
    svc.toggle_network_config(config_id, 1)
    assert svc.list_network_configs()[0]["active"] == 0
    svc.toggle_network_config(config_id, 1)
    assert svc.list_network_configs()[0]["active"] == 1


def test_toggle_network_config_rejects_unknown_id():
    with pytest.raises(NotFoundError):
        svc.toggle_network_config(999999, 1)


def test_update_network_config_changes_fields():
    svc.add_network_config("Old Label", "old.example.com", "old note", 1)
    config_id = svc.list_network_configs()[0]["id"]
    svc.update_network_config(config_id, "New Label", "new.example.com", "new note", 1)
    row = svc.list_network_configs()[0]
    assert row["name"] == "New Label"
    assert row["hostname"] == "new.example.com"
    assert row["notes"] == "new note"


def test_update_network_config_rejects_clash_with_another_entry():
    svc.add_network_config("A", "a.example.com", "", 1)
    svc.add_network_config("B", "b.example.com", "", 1)
    a_id = next(c["id"] for c in svc.list_network_configs() if c["hostname"] == "a.example.com")
    with pytest.raises(ValidationError, match="already"):
        svc.update_network_config(a_id, "A", "b.example.com", "", 1)


def test_update_network_config_allows_keeping_same_hostname():
    """Editing other fields on an entry without changing its hostname must
    not trip the duplicate-hostname check against itself."""
    svc.add_network_config("A", "a.example.com", "old note", 1)
    config_id = svc.list_network_configs()[0]["id"]
    svc.update_network_config(config_id, "A Renamed", "a.example.com", "new note", 1)
    row = svc.list_network_configs()[0]
    assert row["name"] == "A Renamed"
    assert row["notes"] == "new note"


def test_update_network_config_rejects_unknown_id():
    with pytest.raises(NotFoundError):
        svc.update_network_config(999999, "X", "x.example.com", "", 1)


def test_delete_network_config_removes_it_completely():
    svc.add_network_config("A", "a.example.com", "", 1)
    config_id = svc.list_network_configs()[0]["id"]
    svc.delete_network_config(config_id, 1)
    assert svc.list_network_configs() == []


def test_delete_network_config_rejects_unknown_id():
    with pytest.raises(NotFoundError):
        svc.delete_network_config(999999, 1)


def test_delete_network_config_is_logged():
    from app.database import get_db
    svc.add_network_config("A", "a.example.com", "", 1)
    config_id = svc.list_network_configs()[0]["id"]
    svc.delete_network_config(config_id, 1)
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM audit_log WHERE action='delete_network_config' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    assert "a.example.com" in row["details"]
