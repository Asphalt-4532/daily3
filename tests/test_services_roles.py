import pytest

from app.services import roles as svc
from app.services.errors import ValidationError, ConflictError
from app.config import ROLES, PERMISSION_KEYS
from app.database import get_connection


def _stored_row(role, permission):
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT allowed FROM role_permissions WHERE role=? AND permission=?", (role, permission)
        ).fetchone()
        return bool(row["allowed"]) if row else None
    finally:
        conn.close()


def test_list_permission_matrix_matches_seeded_defaults():
    matrix = svc.list_permission_matrix()
    assert [p["key"] for p in matrix] == PERMISSION_KEYS

    by_key = {p["key"]: p["roles"] for p in matrix}
    assert by_key["create_expense"] == {"manager": True, "accountant": False, "user": True}
    assert by_key["review_expense"] == {"manager": True, "accountant": True, "user": False}
    assert by_key["delete_expense"] == {"manager": True, "accountant": False, "user": False}
    for key in ("manage_users", "manage_settings", "view_audit", "manage_backups"):
        assert by_key[key] == {"manager": True, "accountant": False, "user": False}


def test_set_permission_for_roles_updates_and_persists():
    svc.set_permission_for_roles(permission="delete_expense", allowed_roles={"manager", "accountant"},
                                  acting_user_id=1)
    matrix = {p["key"]: p["roles"] for p in svc.list_permission_matrix()}
    assert matrix["delete_expense"] == {"manager": True, "accountant": True, "user": False}
    assert _stored_row("accountant", "delete_expense") is True
    assert _stored_row("user", "delete_expense") is False


def test_set_permission_for_roles_can_grant_a_permission_to_user_role():
    """The whole point of the feature: a manager can hand a normally
    manager/accountant-only permission to the 'user' role without touching
    any code."""
    svc.set_permission_for_roles(permission="review_expense", allowed_roles={"manager", "accountant", "user"},
                                  acting_user_id=1)
    assert svc.list_permission_matrix()[1]["roles"] == {"manager": True, "accountant": True, "user": True}


def test_set_permission_for_roles_rejects_unknown_permission():
    with pytest.raises(ValidationError):
        svc.set_permission_for_roles(permission="launch_missiles", allowed_roles={"manager"}, acting_user_id=1)


def test_set_permission_for_roles_ignores_unknown_role_names():
    """A tampered form submitting a role that doesn't exist shouldn't error
    or create a stray row - it's just silently dropped."""
    svc.set_permission_for_roles(permission="view_audit", allowed_roles={"manager", "superadmin"},
                                  acting_user_id=1)
    assert _stored_row("superadmin", "view_audit") is None
    assert set(ROLES) == {"manager", "accountant", "user"}  # unchanged


def test_set_permission_for_roles_blocks_removing_manage_users_from_every_role():
    """The self-lockout guard: zero roles left with manage_users must be refused."""
    with pytest.raises(ConflictError):
        svc.set_permission_for_roles(permission="manage_users", allowed_roles=set(), acting_user_id=1)
    # nothing was written - manager still has it
    assert _stored_row("manager", "manage_users") is True


def test_set_permission_for_roles_allows_moving_manage_users_to_a_different_role():
    """Refusing *zero* roles isn't the same as refusing to touch it at all -
    handing it to a different single role is fine."""
    svc.set_permission_for_roles(permission="manage_users", allowed_roles={"accountant"}, acting_user_id=1)
    matrix = {p["key"]: p["roles"] for p in svc.list_permission_matrix()}
    assert matrix["manage_users"] == {"manager": False, "accountant": True, "user": False}


def test_set_permission_for_roles_is_atomic_on_rejection():
    """A rejected update must leave every role's row exactly as it was -
    not just the specific role that would have caused the lockout."""
    before = {p["key"]: p["roles"] for p in svc.list_permission_matrix()}["manage_backups"]
    with pytest.raises(ConflictError):
        # manage_users only, not manage_backups - this should be a no-op for
        # every permission including manage_backups, which isn't even involved
        svc.set_permission_for_roles(permission="manage_users", allowed_roles=set(), acting_user_id=1)
    after = {p["key"]: p["roles"] for p in svc.list_permission_matrix()}["manage_backups"]
    assert before == after
