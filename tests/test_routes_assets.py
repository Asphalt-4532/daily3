"""Route permission and smoke tests for the Asset & Maintenance program.

Tests follow the same pattern as test_routes_permissions.py: every protected
route is exercised unauthenticated, as a 'user' role (default permissions),
and as a 'manager'. Actions that need a CSRF token get one from the session.
No mocking - routes call real services against a real test DB, so these also
serve as integration smoke tests.

Key property being locked: the role/permission boundary is enforced at the
HTTP layer via guards.py, not just by the service layer. A route that relies
only on the service raising ForbiddenError would still return a 200 with an
error body rather than a proper 403.
"""
import pytest
from datetime import date
from fastapi.testclient import TestClient

from app.main import app as fastapi_app
from app.programs.assets.services import assets as assets_svc
from app.programs.assets.services import custody
from app.database import get_connection, get_db
from tests.conftest import first_cost_center_id


@pytest.fixture
def client():
    with TestClient(fastapi_app) as c:
        yield c


def _today():
    return date.today().isoformat()


def _login(client, username="admin", password="admin123"):
    r = client.post("/login", data={"username": username, "password": password}, follow_redirects=True)
    assert r.status_code == 200, f"Login failed: {r.status_code}"
    return client


def _csrf(client):
    r = client.get("/assets/")
    from app.auth import _CSRF_KEY
    return client.cookies.get("session") and r.headers.get("set-cookie", "")


def _get_csrf(client):
    """Pull the CSRF token out of the session cookie (same as other route tests)."""
    import json, base64, itsdangerous
    from app.config import SECRET_KEY
    session_cookie = client.cookies.get("session")
    if not session_cookie:
        return ""
    signer = itsdangerous.TimestampSigner(SECRET_KEY)
    try:
        data = signer.unsign(session_cookie, max_age=None)
        payload = json.loads(base64.b64decode(data).decode())
        return payload.get("csrf_token", "")
    except Exception:
        return ""


def _manager_id():
    conn = get_connection()
    try:
        return conn.execute("SELECT id FROM users WHERE role='manager'").fetchone()["id"]
    finally:
        conn.close()


def _make_asset(tag="TR-04"):
    return assets_svc.create_asset(
        asset_tag=tag, name="Isuzu 6-wheel", category="Fleet/Truck",
        purchase_cost=45000.0, created_by=_manager_id(),
    )


# ---------------------------------------------------------------------------
# unauthenticated redirects to /login
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", [
    "/assets/",
    "/assets/new",
    "/assets/reports",
    "/assets/settings",
    "/assets/employees",
])
def test_unauthenticated_gets_redirected(client, path):
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in r.headers["location"]


# ---------------------------------------------------------------------------
# view_assets: user role can see the registry and reports
# ---------------------------------------------------------------------------
def test_user_can_view_asset_list(client):
    _make_asset()
    from app.services.users import create_user
    uid = create_user(username="viewer", password="password1",
                      full_name="Viewer", role="user", acting_user_id=_manager_id())
    _login(client, "viewer", "password1")
    r = client.get("/assets/")
    assert r.status_code == 200
    assert "TR-04" in r.text


def test_user_can_view_asset_detail(client):
    asset_id = _make_asset()
    from app.services.users import create_user
    uid = create_user(username="viewer2", password="password1",
                      full_name="Viewer2", role="user", acting_user_id=_manager_id())
    _login(client, "viewer2", "password1")
    r = client.get(f"/assets/{asset_id}")
    assert r.status_code == 200
    assert "Isuzu" in r.text


def test_user_cannot_access_create_form(client):
    from app.services.users import create_user
    create_user(username="plain1", password="password1",
                full_name="Plain", role="user", acting_user_id=_manager_id())
    _login(client, "plain1", "password1")
    r = client.get("/assets/new", follow_redirects=False)
    # Should redirect to /programs (not logged-in check passes, role check fails)
    assert r.status_code == 302
    assert "programs" in r.headers["location"]


def test_user_cannot_access_settings(client):
    from app.services.users import create_user
    create_user(username="plain2", password="password1",
                full_name="Plain2", role="user", acting_user_id=_manager_id())
    _login(client, "plain2", "password1")
    r = client.get("/assets/settings", follow_redirects=False)
    assert r.status_code == 302


# ---------------------------------------------------------------------------
# manager can create, edit, delete
# ---------------------------------------------------------------------------
def test_manager_can_load_create_form(client):
    _login(client)
    r = client.get("/assets/new")
    assert r.status_code == 200
    assert "Register" in r.text


def test_manager_can_create_asset(client):
    _login(client)
    csrf = _get_csrf(client)
    r = client.post("/assets/new", data={
        "csrf_token": csrf,
        "asset_tag": "TR-04",
        "name": "Isuzu 6-wheel",
        "category": "Fleet/Truck",
        "serial_number": "",
        "purchase_date": "2024-03-11",
        "purchase_cost": "45000",
        "cost_center_id": "",
        "notes": "",
    }, follow_redirects=False)
    assert r.status_code == 302
    # redirects to the new asset's detail page
    assert "/assets/" in r.headers["location"]


def test_create_asset_with_bad_tag_returns_400(client):
    _login(client)
    csrf = _get_csrf(client)
    r = client.post("/assets/new", data={
        "csrf_token": csrf,
        "asset_tag": "bad tag with spaces",
        "name": "Test",
        "category": "Fleet/Truck",
        "serial_number": "", "purchase_date": "", "purchase_cost": "0",
        "cost_center_id": "", "notes": "",
    })
    assert r.status_code == 400
    assert "format" in r.text.lower() or "pattern" in r.text.lower()


def test_duplicate_tag_returns_400(client):
    _make_asset("TR-04")
    _login(client)
    csrf = _get_csrf(client)
    r = client.post("/assets/new", data={
        "csrf_token": csrf,
        "asset_tag": "TR-04", "name": "Second truck",
        "category": "Fleet/Truck",
        "serial_number": "", "purchase_date": "", "purchase_cost": "0",
        "cost_center_id": "", "notes": "",
    })
    assert r.status_code == 400


def test_manager_can_set_status(client):
    asset_id = _make_asset()
    _login(client)
    csrf = _get_csrf(client)
    r = client.post(f"/assets/{asset_id}/status", data={
        "csrf_token": csrf, "status": "in_maintenance", "note": "gearbox",
    }, follow_redirects=False)
    assert r.status_code == 302
    assert assets_svc.get_asset(asset_id)["status"] == "in_maintenance"


def test_delete_requires_reason(client):
    asset_id = _make_asset()
    _login(client)
    csrf = _get_csrf(client)
    r = client.post(f"/assets/{asset_id}/delete", data={
        "csrf_token": csrf, "reason": "   ",
    })
    assert r.status_code == 400


def test_soft_delete_removes_from_list(client):
    asset_id = _make_asset()
    _login(client)
    csrf = _get_csrf(client)
    resp = client.post(f"/assets/{asset_id}/delete", data={
        "csrf_token": csrf, "reason": "Sold",
    }, follow_redirects=False)
    # If delete failed (400), the asset will still appear on the list.
    assert resp.status_code == 302, f"Delete failed: {resp.status_code} {resp.text[:200]}"
    r = client.get("/assets/")
    # TR-04 appears as a placeholder in the search field; check the table
    # body isn't present (which means either the table is empty or the tag
    # doesn't appear in a <td>).
    assert "<td><strong>TR-04</strong></td>" not in r.text


# ---------------------------------------------------------------------------
# tag rules settings
# ---------------------------------------------------------------------------
def test_manager_can_update_tag_rule(client):
    _login(client)
    csrf = _get_csrf(client)
    r = client.post("/assets/settings/tag-rule", data={
        "csrf_token": csrf,
        "category": "Fleet/Truck",
        "pattern": "",
        "example": "any text",
    }, follow_redirects=False)
    assert r.status_code == 302
    rules = assets_svc.get_tag_rules()
    assert rules["Fleet/Truck"]["pattern"] == ""


def test_bad_regex_pattern_returns_400(client):
    _login(client)
    csrf = _get_csrf(client)
    r = client.post("/assets/settings/tag-rule", data={
        "csrf_token": csrf,
        "category": "Fleet/Truck",
        "pattern": "^TR-(unclosed",
        "example": "TR-04",
    })
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# assign / custody
# ---------------------------------------------------------------------------
def test_assign_asset_route(client):
    asset_id = _make_asset()
    _login(client)
    csrf = _get_csrf(client)
    cc_id = first_cost_center_id()
    r = client.post(f"/assets/{asset_id}/assign", data={
        "csrf_token": csrf,
        "employee_id": "",
        "cost_center_id": str(cc_id),
        "role_note": "Factory floor",
        "issued_date": _today(),
    }, follow_redirects=False)
    assert r.status_code == 302
    assert len(custody.active_holders(asset_id)) == 1


def test_divest_requires_reason(client):
    asset_id = _make_asset()
    _login(client)
    csrf = _get_csrf(client)
    r = client.post(f"/assets/{asset_id}/divest", data={
        "csrf_token": csrf,
        "divest_date": _today(),
        "settle_amount": "",
        "reason": "",
    })
    assert r.status_code == 400


def test_user_role_cannot_divest(client):
    asset_id = _make_asset()
    from app.services.users import create_user
    create_user(username="plain3", password="password1",
                full_name="Plain3", role="user", acting_user_id=_manager_id())
    _login(client, "plain3", "password1")
    csrf = _get_csrf(client)
    r = client.post(f"/assets/{asset_id}/divest", data={
        "csrf_token": csrf,
        "divest_date": _today(),
        "settle_amount": "",
        "reason": "Sold",
    })
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# report downloads
# ---------------------------------------------------------------------------
def test_asset_history_pdf_download(client):
    asset_id = _make_asset()
    _login(client)
    r = client.get(f"/assets/{asset_id}/report/pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content[:4] == b"%PDF"


def test_asset_history_xlsx_download(client):
    asset_id = _make_asset()
    _login(client)
    r = client.get(f"/assets/{asset_id}/report/xlsx")
    assert r.status_code == 200
    assert "spreadsheet" in r.headers["content-type"]


def test_maintenance_report_pdf_download(client):
    _make_asset()
    _login(client)
    r = client.get("/assets/reports/export.pdf")
    assert r.status_code == 200
    assert r.content[:4] == b"%PDF"


def test_maintenance_report_xlsx_download(client):
    _make_asset()
    _login(client)
    r = client.get("/assets/reports/export.xlsx")
    assert r.status_code == 200
    assert "spreadsheet" in r.headers["content-type"]


def test_unauthenticated_cannot_download_pdf(client):
    asset_id = _make_asset()
    r = client.get(f"/assets/{asset_id}/report/pdf", follow_redirects=False)
    assert r.status_code == 302


# ---------------------------------------------------------------------------
# CSRF enforcement
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path,data", [
    ("/assets/new", {"asset_tag": "TR-04", "name": "x", "category": "Fleet/Truck",
                     "serial_number": "", "purchase_date": "", "purchase_cost": "0",
                     "cost_center_id": "", "notes": ""}),
    ("/assets/settings/tag-rule", {"category": "Fleet/Truck", "pattern": "", "example": ""}),
])
def test_csrf_enforced_on_asset_post(client, path, data):
    _login(client)
    data["csrf_token"] = "wrong-token"
    r = client.post(path, data=data)
    assert r.status_code == 403
