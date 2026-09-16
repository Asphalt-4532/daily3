from datetime import datetime, timedelta, timezone

from app import auth


def test_password_hash_roundtrip():
    h = auth.hash_password("correct horse battery staple")
    assert auth.verify_password("correct horse battery staple", h)
    assert not auth.verify_password("wrong password", h)


def test_verify_password_handles_garbage_hash_safely():
    # a corrupt/foreign hash should fail closed, not raise
    assert auth.verify_password("anything", "not-a-real-bcrypt-hash") is False


def test_role_permissions_matrix():
    assert auth.can_create_expense("user") is True
    assert auth.can_create_expense("manager") is True
    assert auth.can_create_expense("accountant") is False

    assert auth.can_review_expense("accountant") is True
    assert auth.can_review_expense("manager") is True
    assert auth.can_review_expense("user") is False

    assert auth.can_delete_expense("manager") is True
    assert auth.can_delete_expense("accountant") is False
    assert auth.can_delete_expense("user") is False

    for perm in (auth.can_manage_users, auth.can_manage_settings, auth.can_view_audit):
        assert perm("manager") is True
        assert perm("accountant") is False
        assert perm("user") is False


def test_verify_csrf_requires_exact_match():
    class FakeRequest:
        session = {"csrf_token": "abc123"}
    assert auth.verify_csrf(FakeRequest(), "abc123") is True
    assert auth.verify_csrf(FakeRequest(), "wrong") is False
    assert auth.verify_csrf(FakeRequest(), "") is False


def test_verify_csrf_fails_when_no_session_token():
    class FakeRequest:
        session = {}
    assert auth.verify_csrf(FakeRequest(), "anything") is False


def test_is_locked_out_false_when_no_lock():
    assert auth.is_locked_out({"locked_until": None}) is False


def test_is_locked_out_true_for_future_timestamp():
    future = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=5)).isoformat()
    assert auth.is_locked_out({"locked_until": future}) is True


def test_is_locked_out_false_for_past_timestamp():
    past = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)).isoformat()
    assert auth.is_locked_out({"locked_until": past}) is False


def test_register_failed_login_locks_after_max_attempts(client):
    """Integration-ish: exercises the real DB update path used by the login route."""
    from app.services import users as users_svc
    from app.database import get_connection

    uid = users_svc.create_user(username="locktest", full_name="Lock Test",
                                 password="pass1234", role="user", acting_user_id=1)
    conn = get_connection()
    try:
        for _ in range(5):
            row = dict(conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())
            auth.register_failed_login(conn, row)
        conn.commit()
        final = dict(conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())
    finally:
        conn.close()
    assert auth.is_locked_out(final) is True
