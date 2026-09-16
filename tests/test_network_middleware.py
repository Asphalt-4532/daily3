"""Tests for app/network_middleware.py's actual enforcement, over real HTTP
requests with a custom Host header - not just the service layer that backs
it. This is the security-critical part: proving the allowlist is genuinely
enforced, genuinely live (no restart needed), and genuinely can't lock out
local access."""
import re

from app.services import network as network_service


def csrf_from(html):
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return m.group(1) if m else None


def test_empty_allowlist_accepts_any_host(client):
    r = client.get("/login", headers={"host": "anything.at.all.example.com"})
    assert r.status_code == 200


def test_configured_hostname_is_accepted(manager_client):
    network_service.add_network_config("Tunnel", "expense.example.com", "", 1)
    r = manager_client.get("/login", headers={"host": "expense.example.com"})
    assert r.status_code == 200


def test_mismatched_host_is_rejected_once_allowlist_is_non_empty(manager_client):
    network_service.add_network_config("Tunnel", "expense.example.com", "", 1)
    r = manager_client.get("/login", headers={"host": "totally-different.example.com"})
    assert r.status_code == 400


def test_localhost_always_accepted_even_with_a_configured_allowlist():
    """The core self-lockout protection: no matter what's configured,
    localhost must keep working so a manager always has a way in."""
    network_service.add_network_config("Tunnel", "expense.example.com", "", 1)
    from starlette.testclient import TestClient
    from app.main import app as fastapi_app
    with TestClient(fastapi_app) as c:
        r = c.get("/login", headers={"host": "localhost"})
        assert r.status_code == 200
        r2 = c.get("/login", headers={"host": "127.0.0.1"})
        assert r2.status_code == 200


def test_configured_hostname_with_port_in_request_still_matches():
    """A browser includes a non-default port in the Host header
    (e.g. Host: localhost:8000) - the allowlist match must strip that
    before comparing, or a correctly-configured hostname would never
    actually match a real request."""
    network_service.add_network_config("LAN", "petty-cash.lan", "", 1)
    from starlette.testclient import TestClient
    from app.main import app as fastapi_app
    with TestClient(fastapi_app) as c:
        r = c.get("/login", headers={"host": "petty-cash.lan:8000"})
        assert r.status_code == 200


def test_disabling_an_entry_removes_it_from_enforcement(manager_client):
    """Proves the middleware is genuinely live (reads the DB per request) -
    no restart needed for a change to take effect."""
    network_service.add_network_config("Tunnel", "expense.example.com", "", 1)
    r1 = manager_client.get("/login", headers={"host": "expense.example.com"})
    assert r1.status_code == 200

    config_id = network_service.list_network_configs()[0]["id"]
    settings_html = manager_client.get("/settings/network").text
    csrf = csrf_from(settings_html)
    manager_client.post(f"/settings/network/{config_id}/toggle", data={"csrf_token": csrf})

    # now disabled - the allowlist is effectively empty again, so any host
    # is accepted (including one that was never configured at all)
    r2 = manager_client.get("/login", headers={"host": "some-other-host.example.com"})
    assert r2.status_code == 200


def test_removing_the_only_entry_restores_unrestricted_access(manager_client):
    network_service.add_network_config("Tunnel", "expense.example.com", "", 1)
    config_id = network_service.list_network_configs()[0]["id"]
    settings_html = manager_client.get("/settings/network").text
    csrf = csrf_from(settings_html)
    manager_client.post(f"/settings/network/{config_id}/delete", data={"csrf_token": csrf})

    r = manager_client.get("/login", headers={"host": "anything.example.com"})
    assert r.status_code == 200


# --- management UI: permissions and CSRF ------------------------------------

def test_network_settings_page_requires_manager(user_client, accountant_client):
    for c in (user_client, accountant_client):
        r = c.get("/settings/network", follow_redirects=False)
        assert r.status_code in (302, 303)
        assert r.headers["location"] == "/programs"


def test_network_settings_page_requires_login(client):
    r = client.get("/settings/network", follow_redirects=False)
    assert r.status_code in (302, 303)
    assert r.headers["location"] == "/login"


def test_add_network_config_rejects_invalid_csrf(manager_client):
    r = manager_client.post("/settings/network/new", data={
        "csrf_token": "this-is-not-the-real-token", "name": "Test", "hostname": "expense.example.com",
    })
    assert r.status_code == 403


def test_add_network_config_requires_csrf_field_at_all(manager_client):
    r = manager_client.post("/settings/network/new", data={
        "name": "Test", "hostname": "expense.example.com",
    })
    assert r.status_code == 422  # FastAPI's own required-field validation, before the route body runs


def test_add_network_config_over_http_end_to_end(manager_client):
    form_html = manager_client.get("/settings/network").text
    csrf = csrf_from(form_html)
    r = manager_client.post("/settings/network/new", data={
        "csrf_token": csrf, "name": "Cloudflare Tunnel", "hostname": "expense.example.com",
        "notes": "public access",
    }, follow_redirects=False)
    assert r.status_code in (302, 303)
    listing = manager_client.get("/settings/network").text
    assert "Cloudflare Tunnel" in listing
    assert "expense.example.com" in listing


def test_current_host_shown_on_settings_page(manager_client):
    network_service.add_network_config("Tunnel", "expense.example.com", "", 1)
    html = manager_client.get("/settings/network", headers={"host": "expense.example.com"}).text
    assert "expense.example.com" in html
