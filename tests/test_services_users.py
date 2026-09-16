import pytest

from app.services import users as svc
from app.services.errors import ValidationError, DuplicateError
from app.auth import verify_password
from app.database import get_connection


def _get_user_row(user_id):
    conn = get_connection()
    try:
        return dict(conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())
    finally:
        conn.close()


def test_create_user_happy_path():
    uid = svc.create_user(username="alice", full_name="Alice A", password="secret123",
                           role="accountant", acting_user_id=1)
    row = _get_user_row(uid)
    assert row["username"] == "alice"
    assert row["role"] == "accountant"
    assert row["active"] == 1
    assert verify_password("secret123", row["password_hash"])


def test_create_user_rejects_duplicate_username():
    svc.create_user(username="bob", full_name="Bob B", password="secret123",
                     role="user", acting_user_id=1)
    with pytest.raises(DuplicateError):
        svc.create_user(username="bob", full_name="Someone Else", password="other",
                         role="user", acting_user_id=1)


def test_create_user_rejects_invalid_role():
    with pytest.raises(ValidationError):
        svc.create_user(username="carol", full_name="Carol C", password="secret123",
                         role="superadmin", acting_user_id=1)


def test_create_user_rejects_empty_username():
    with pytest.raises(ValidationError):
        svc.create_user(username="   ", full_name="X", password="secret123",
                         role="user", acting_user_id=1)


def test_toggle_user_disables_and_reenables():
    uid = svc.create_user(username="dave", full_name="Dave D", password="secret123",
                           role="user", acting_user_id=1)
    assert _get_user_row(uid)["active"] == 1
    svc.toggle_user(uid, acting_user_id=1)
    assert _get_user_row(uid)["active"] == 0
    svc.toggle_user(uid, acting_user_id=1)
    assert _get_user_row(uid)["active"] == 1


def test_toggle_user_cannot_disable_self():
    """A manager disabling their own account would lock everyone out -
    the service silently refuses rather than allowing it."""
    before = _get_user_row(1)["active"]
    svc.toggle_user(1, acting_user_id=1)
    assert _get_user_row(1)["active"] == before


def test_reset_password_changes_hash_and_clears_lockout():
    uid = svc.create_user(username="eve", full_name="Eve E", password="oldpass123",
                           role="user", acting_user_id=1)
    conn = get_connection()
    conn.execute("UPDATE users SET failed_attempts=4, locked_until=datetime('now','+1 hour') WHERE id=?", (uid,))
    conn.commit()
    conn.close()

    svc.reset_password(uid, "newpass456", acting_user_id=1)
    row = _get_user_row(uid)
    assert verify_password("newpass456", row["password_hash"])
    assert not verify_password("oldpass123", row["password_hash"])
    assert row["failed_attempts"] == 0
    assert row["locked_until"] is None


def test_list_users_includes_seeded_manager():
    users = svc.list_users()
    assert any(u["username"] == "admin" and u["role"] == "manager" for u in users)
