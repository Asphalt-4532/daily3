"""Every state-changing (POST) route must reject a missing or wrong CSRF
token with 403, and never take the requested action. One representative
route per feature area is exercised here; app/auth.py's verify_csrf() itself
is unit-tested in test_auth.py."""
import re

from tests.conftest import csrf_from


def test_backup_create_rejects_forged_csrf(manager_client):
    r = manager_client.post("/backups/create", data={"csrf_token": "forged-token"})
    assert r.status_code == 403
    assert len(manager_client.get("/backups").text.split("petty_cash_backup_")) == 1


def test_user_create_rejects_forged_csrf(manager_client):
    r = manager_client.post("/users/new", data={
        "csrf_token": "forged", "username": "sneaky", "full_name": "Sneaky",
        "password": "x", "role": "user",
    })
    assert r.status_code == 403
    assert "sneaky" not in manager_client.get("/users").text


def test_category_add_rejects_forged_csrf(manager_client):
    r = manager_client.post("/expense-program/settings/categories/new", data={
        "csrf_token": "forged", "name": "Sneaky Category",
    })
    assert r.status_code == 403
    assert "Sneaky Category" not in manager_client.get("/expense-program/settings").text


def test_expense_create_rejects_missing_csrf(user_client):
    form_html = user_client.get("/expense-program/expenses/new").text
    cat_id = re.search(r'<option value="(\d+)"', form_html).group(1)
    r = user_client.post("/expense-program/expenses/new", data={
        # csrf_token omitted entirely
        "expense_date": "2026-08-22", "cost_center_id": "",
        "paid_to": "V", "line_category_id": cat_id, "line_particulars": "P",
        "line_amount": "10", "line_is_vatable": "0", "payment_mode": "Cash",
    })
    # the route parses the form manually (to support a variable number of
    # transaction lines) rather than via typed Form(...) params, so a missing
    # csrf_token no longer triggers FastAPI's automatic 422 - it flows through
    # to our own guards.require_csrf() check instead, same 403 every other
    # CSRF failure in this app returns.
    assert r.status_code == 403


def test_roles_permission_update_rejects_forged_csrf(manager_client):
    r = manager_client.post("/users/roles/update", data={
        "csrf_token": "forged", "permission": "delete_expense", "allowed_roles": ["accountant"],
    })
    assert r.status_code == 403
    from app.services import roles as roles_service
    matrix = {p["key"]: p["roles"] for p in roles_service.list_permission_matrix()}
    assert matrix["delete_expense"]["accountant"] is False  # unchanged


def test_valid_csrf_token_is_accepted(manager_client):
    html = manager_client.get("/expense-program/settings").text
    token = csrf_from(html)
    r = manager_client.post("/expense-program/settings/categories/new",
                             data={"csrf_token": token, "name": "Legit Category"},
                             follow_redirects=False)
    assert r.status_code in (302, 303)
    assert "Legit Category" in manager_client.get("/expense-program/settings").text


def test_csrf_token_not_reusable_after_relogin(client):
    """login_user() issues a fresh CSRF token every time - an old token from
    a previous session must not still validate."""
    from tests.conftest import login

    login(client, "admin", "admin123")
    old_token = csrf_from(client.get("/expense-program/settings").text)

    client.get("/logout")
    login(client, "admin", "admin123")  # new session, new csrf token

    r = client.post("/expense-program/settings/categories/new", data={"csrf_token": old_token, "name": "Should Fail"})
    assert r.status_code == 403
    assert "Should Fail" not in client.get("/expense-program/settings").text


def test_clone_requires_a_valid_csrf_token(manager_client):
    """Cloning creates a record, so it is a state-changing POST like any
    other - a forged one must not succeed."""
    from tests.conftest import create_voucher_over_http

    eid = create_voucher_over_http(manager_client, paid_to="CSRF Shop", amount="12.00")

    bad = manager_client.post(f"/expense-program/expenses/{eid}/clone",
                              data={"csrf_token": "forged"}, follow_redirects=False)
    assert bad.status_code == 403

    from app.programs.expenses.services import expenses as svc
    from app.database import get_connection
    conn = get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) c FROM expenses").fetchone()["c"] == 1
    finally:
        conn.close()


def test_share_requires_a_valid_csrf_token(manager_client):
    from tests.conftest import create_voucher_over_http
    from app.services import users as users_service
    from app.services import notifications as notifications_svc

    acct_id = users_service.create_user(
        username="csrf_acct", full_name="CSRF Accountant", password="pass1234",
        role="accountant", acting_user_id=1,
    )
    eid = create_voucher_over_http(manager_client, paid_to="CSRF Shop", amount="12.00")

    bad = manager_client.post(f"/expense-program/expenses/{eid}/share", data={
        "csrf_token": "forged", "share_with": str(acct_id), "note": "",
    }, follow_redirects=False)
    assert bad.status_code == 403
    assert notifications_svc.count_unread(acct_id) == 0
