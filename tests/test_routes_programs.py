"""Integration tests for the program hub: the post-login landing page for
choosing which program to enter, and the coming-soon placeholder for
programs that don't exist yet."""


def test_login_redirects_to_program_hub_not_dashboard(client):
    client.get("/login")
    r = client.post("/login", data={"username": "admin", "password": "admin123"},
                     follow_redirects=False)
    assert r.status_code in (302, 303)
    assert r.headers["location"] == "/programs"


def test_already_logged_in_visiting_login_redirects_to_hub(manager_client):
    r = manager_client.get("/login", follow_redirects=False)
    assert r.status_code in (302, 303)
    assert r.headers["location"] == "/programs"


def test_program_hub_requires_login(client):
    r = client.get("/programs", follow_redirects=False)
    assert r.status_code in (302, 303)
    assert r.headers["location"] == "/login"


def test_program_hub_lists_expense_program_as_available(manager_client):
    html = manager_client.get("/programs").text
    assert "Expense Program" in html
    assert 'href="/expense-program/"' in html


def test_program_hub_lists_future_programs_as_coming_soon(manager_client):
    html = manager_client.get("/programs").text
    # Weekly Productions is a real, available program now - only these two
    # remain placeholders (see tests/test_routes_weekly_productions.py for
    # Weekly Productions' own coverage).
    for name in ["Machinery Reports", "Key Updates"]:
        assert name in html
    assert html.count("Coming in the future") >= 2


def test_program_hub_lists_weekly_productions_as_available(manager_client):
    html = manager_client.get("/programs").text
    assert "Weekly Productions" in html
    assert "/weekly-productions/" in html


def test_coming_soon_page_shows_correct_program_name(manager_client):
    html = manager_client.get("/programs/coming-soon?key=machinery-reports").text
    assert "Machinery Reports" in html
    assert "Coming in the future" in html


def test_coming_soon_page_handles_unknown_key_gracefully(manager_client):
    r = manager_client.get("/programs/coming-soon?key=nonexistent-program")
    assert r.status_code == 200
    assert "This program" in r.text


def test_coming_soon_requires_login(client):
    r = client.get("/programs/coming-soon?key=weekly-productions", follow_redirects=False)
    assert r.status_code in (302, 303)
    assert r.headers["location"] == "/login"


def test_dashboard_reachable_at_its_own_prefixed_url(manager_client):
    """The Expense Program is isolated under its own URL prefix - this is
    what makes 'a future program won't collide with this one' concrete,
    not just a file-organization convention."""
    r = manager_client.get("/expense-program/")
    assert r.status_code == 200
    assert "Expense Program" in r.text
    assert "All programs" in r.text


def test_bare_root_serves_the_login_page_for_anonymous_visitors(client):
    """/ is the default home page: the login form. It is still owned by no
    program - the hub lives at /programs and the Expense Program at
    /expense-program/ - it just no longer 404s."""
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200
    assert "login" in r.text.lower()


def test_bare_root_sends_an_already_logged_in_user_to_the_hub(manager_client):
    """Same behaviour as GET /login for a live session - no point showing the
    form again to someone already authenticated."""
    r = manager_client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/programs"
