"""Shared pytest fixtures.

CRITICAL: the env vars below must be set before any `app.*` module is
imported anywhere in the test session, because app/config.py reads them once
at import time. This is what keeps every test run fully isolated from your
real data/ directory - tests never touch a real database, real backups, or
real uploaded files, no matter what they do.
"""
import os
import re
import tempfile

_TEST_DATA_DIR = tempfile.mkdtemp(prefix="petty_cash_test_")
os.environ["PETTY_CASH_DATA_DIR"] = _TEST_DATA_DIR
os.environ["SECRET_KEY"] = "test-secret-key-not-for-production-use"
os.environ["BACKUP_ENABLED"] = "false"  # tests trigger backups explicitly, not on a timer
os.environ["DEFAULT_ADMIN_USERNAME"] = "admin"
os.environ["DEFAULT_ADMIN_PASSWORD"] = "admin123"
os.environ["COMPANY_NAME"] = "Test Factory"
os.environ["COMPANY_ADDRESS"] = ""  # deliberately blank - see test_excel_common.py and
                                     # test_pdf_common.py for the "address configured" case,
                                     # which explicitly monkeypatches this on rather than
                                     # relying on whatever happens to be in a real .env file

import pytest
from fastapi.testclient import TestClient

import app.config as config
from app.database import init_schema
from app.seed import seed_if_needed
from app.main import app as fastapi_app
from app.services import users as users_service


@pytest.fixture(autouse=True)
def reset_db():
    """Wipes and reseeds the test database before every single test function,
    so tests never depend on execution order or leak state into each other."""
    if config.DATABASE_PATH.exists():
        config.DATABASE_PATH.unlink()
    for f in config.BACKUPS_DIR.glob("*"):
        if f.is_file():
            f.unlink()
    for f in config.UPLOADS_DIR.glob("*"):
        if f.is_file() and f.name != ".gitkeep":
            f.unlink()
    restore_log = config.DATA_DIR / "restore_log.jsonl"
    restore_log.unlink(missing_ok=True)

    init_schema()
    seed_if_needed()
    yield


@pytest.fixture
def client():
    with TestClient(fastapi_app) as c:
        yield c


def login(client, username="admin", password="admin123"):
    """Logs a TestClient in and returns the response. The client's cookie jar
    then carries the session for subsequent requests."""
    client.get("/login")
    return client.post("/login", data={"username": username, "password": password})


def csrf_from(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "no csrf_token field found on the page - did the page actually render a form?"
    return m.group(1)


def first_category_id():
    """A real category id from the seeded defaults - every test that needs
    *a* valid category (not testing category logic itself) uses this rather
    than each hardcoding its own lookup query."""
    from app.database import get_connection
    conn = get_connection()
    try:
        return conn.execute("SELECT id FROM categories ORDER BY id LIMIT 1").fetchone()["id"]
    finally:
        conn.close()


def second_category_id():
    from app.database import get_connection
    conn = get_connection()
    try:
        return conn.execute("SELECT id FROM categories ORDER BY id LIMIT 1 OFFSET 1").fetchone()["id"]
    finally:
        conn.close()


def first_cost_center_id():
    from app.database import get_connection
    conn = get_connection()
    try:
        return conn.execute("SELECT id FROM cost_centers ORDER BY id LIMIT 1").fetchone()["id"]
    finally:
        conn.close()


@pytest.fixture
def manager_client(client):
    """A logged-in client for the seeded default manager account."""
    login(client, "admin", "admin123")
    return client


@pytest.fixture
def accountant_client():
    """A logged-in client for a freshly created accountant account."""
    users_service.create_user(
        username="test_accountant", full_name="Test Accountant",
        password="pass1234", role="accountant", acting_user_id=1,
    )
    with TestClient(fastapi_app) as c:
        login(c, "test_accountant", "pass1234")
        yield c


@pytest.fixture
def user_client():
    """A logged-in client for a freshly created 'user' (recorder) account."""
    users_service.create_user(
        username="test_user", full_name="Test User",
        password="pass1234", role="user", acting_user_id=1,
    )
    with TestClient(fastapi_app) as c:
        login(c, "test_user", "pass1234")
        yield c


@pytest.fixture
def manager_user():
    """Return the seeded admin manager row."""
    from app.database import get_connection
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM users WHERE role='manager' LIMIT 1").fetchone()
        return dict(row)
    finally:
        conn.close()


@pytest.fixture
def plain_user():
    """Create and return a plain 'user' role account for WD tests."""
    users_service.create_user(
        username="wd_user", full_name="WD Test User",
        password="password123", role="user", acting_user_id=1,
    )
    from app.database import get_connection
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM users WHERE username='wd_user'").fetchone()
        return dict(row)
    finally:
        conn.close()


@pytest.fixture
def second_user():
    """Create and return a second plain 'user' role account."""
    users_service.create_user(
        username="wd_user2", full_name="WD Test User Two",
        password="password123", role="user", acting_user_id=1,
    )
    from app.database import get_connection
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM users WHERE username='wd_user2'").fetchone()
        return dict(row)
    finally:
        conn.close()


def create_voucher_over_http(client, *, paid_to="Corner Shop", amount="45.00",
                             category_id=None):
    """Submits a real voucher through the form and returns its id.

    Lives here rather than in one test file because both the permission
    tests and the CSRF tests need a genuine submitted voucher to act on -
    same reasoning as first_category_id() above. Goes through HTTP on
    purpose: a voucher made by calling the service directly would not prove
    the form fields still line up.
    """
    from datetime import date

    html = client.get("/expense-program/expenses/new").text
    token = csrf_from(html)
    r = client.post("/expense-program/expenses/new", data={
        "csrf_token": token, "action": "submit",
        "expense_date": date.today().isoformat(),
        "cost_center_id": str(first_cost_center_id()),
        "paid_to": paid_to, "payment_mode": "Cash",
        "line_category_id": str(category_id or first_category_id()),
        "line_particulars": "Tea and sugar",
        "line_amount": amount,
        "line_is_vatable": "0",
    }, follow_redirects=False)
    assert r.status_code in (302, 303), r.text[:400]
    return int(r.headers["location"].rstrip("/").split("/")[-1])
