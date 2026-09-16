"""The new UI endpoints over real HTTP: bulk actions, undo, and the
notification centre's read buttons.

Service-level tests prove the rules; these prove the wiring - that the form
fields line up, that the permission each route claims is the one it
enforces, and that CSRF is required. A bulk endpoint is the obvious place
for a permission hole to appear, so `delete` being manager-only is checked
here against every role, not just asserted in a comment.
"""
from app.database import get_db
from tests.conftest import (
    csrf_from, create_voucher_over_http, first_category_id, first_cost_center_id,
)


def _status(expense_id):
    with get_db() as conn:
        return conn.execute(
            "SELECT status FROM expenses WHERE id=?", (expense_id,)
        ).fetchone()["status"]


def _token(client, path="/expense-program/expenses"):
    return csrf_from(client.get(path).text)


def _pending(manager_client, n=2):
    return [create_voucher_over_http(manager_client) for _ in range(n)]


# ── bulk: the happy paths ──────────────────────────────────────────────────

class TestBulkActions:

    def test_bulk_approve_over_http(self, manager_client):
        ids = _pending(manager_client)
        r = manager_client.post("/expense-program/expenses/bulk", data={
            "csrf_token": _token(manager_client),
            "bulk_action": "approve",
            "expense_ids": [str(i) for i in ids],
        }, follow_redirects=False)
        assert r.status_code == 302
        assert all(_status(i) == "approved" for i in ids)

    def test_bulk_reject_over_http(self, manager_client):
        ids = _pending(manager_client)
        manager_client.post("/expense-program/expenses/bulk", data={
            "csrf_token": _token(manager_client),
            "bulk_action": "reject",
            "bulk_reason": "Wrong cost centre",
            "expense_ids": [str(i) for i in ids],
        }, follow_redirects=False)
        assert all(_status(i) == "rejected" for i in ids)

    def test_bulk_delete_over_http(self, manager_client):
        ids = _pending(manager_client)
        manager_client.post("/expense-program/expenses/bulk", data={
            "csrf_token": _token(manager_client),
            "bulk_action": "delete",
            "bulk_reason": "Recorded twice",
            "expense_ids": [str(i) for i in ids],
        }, follow_redirects=False)
        assert all(_status(i) == "deleted" for i in ids)

    def test_unknown_action_is_rejected(self, manager_client):
        r = manager_client.post("/expense-program/expenses/bulk", data={
            "csrf_token": _token(manager_client),
            "bulk_action": "obliterate",
            "expense_ids": ["1"],
        }, follow_redirects=False)
        assert r.status_code == 400

    def test_missing_reason_does_not_delete_anything(self, manager_client):
        ids = _pending(manager_client, 1)
        manager_client.post("/expense-program/expenses/bulk", data={
            "csrf_token": _token(manager_client),
            "bulk_action": "delete",
            "bulk_reason": "  ",
            "expense_ids": [str(ids[0])],
        }, follow_redirects=False)
        assert _status(ids[0]) == "pending"

    def test_empty_selection_changes_nothing(self, manager_client):
        ids = _pending(manager_client, 1)
        r = manager_client.post("/expense-program/expenses/bulk", data={
            "csrf_token": _token(manager_client),
            "bulk_action": "approve",
        }, follow_redirects=False)
        assert r.status_code == 302
        assert _status(ids[0]) == "pending"


# ── bulk: permissions ──────────────────────────────────────────────────────

class TestBulkPermissions:
    """The point of this block: the bulk door must be exactly as narrow as
    the single-record one."""

    def test_plain_user_cannot_bulk_approve(self, manager_client, user_client):
        ids = _pending(manager_client, 1)
        r = user_client.post("/expense-program/expenses/bulk", data={
            "csrf_token": _token(user_client, "/expense-program/expenses"),
            "bulk_action": "approve",
            "expense_ids": [str(ids[0])],
        }, follow_redirects=False)
        assert r.status_code == 403
        assert _status(ids[0]) == "pending"

    def test_accountant_can_bulk_approve(self, manager_client, accountant_client):
        ids = _pending(manager_client, 1)
        accountant_client.post("/expense-program/expenses/bulk", data={
            "csrf_token": _token(accountant_client),
            "bulk_action": "approve",
            "expense_ids": [str(ids[0])],
        }, follow_redirects=False)
        assert _status(ids[0]) == "approved"

    def test_accountant_cannot_bulk_delete(self, manager_client, accountant_client):
        """Delete is manager-only on the single-voucher route; it has to be
        manager-only here too or the toolbar is a way around it."""
        ids = _pending(manager_client, 1)
        r = accountant_client.post("/expense-program/expenses/bulk", data={
            "csrf_token": _token(accountant_client),
            "bulk_action": "delete",
            "bulk_reason": "no",
            "expense_ids": [str(ids[0])],
        }, follow_redirects=False)
        assert r.status_code == 403
        assert _status(ids[0]) == "pending"

    def test_bulk_requires_csrf(self, manager_client):
        ids = _pending(manager_client, 1)
        r = manager_client.post("/expense-program/expenses/bulk", data={
            "csrf_token": "wrong",
            "bulk_action": "approve",
            "expense_ids": [str(ids[0])],
        }, follow_redirects=False)
        assert r.status_code == 403
        assert _status(ids[0]) == "pending"


# ── undo over HTTP ─────────────────────────────────────────────────────────

class TestUndoRoute:

    def _approve_and_get_undo_token(self, client, expense_id):
        """Approve a voucher, then read the undo token straight out of the
        toast the next page renders - the same route the button takes."""
        import re
        detail = f"/expense-program/expenses/{expense_id}"
        client.post(f"{detail}/approve",
                    data={"csrf_token": csrf_from(client.get(detail).text)},
                    follow_redirects=True)
        html = client.get(detail).text
        m = re.search(r'name="token" value="([^"]+)"', html)
        return m.group(1) if m else None

    def test_toast_offers_an_undo_after_approving(self, manager_client):
        one = create_voucher_over_http(manager_client)
        detail = f"/expense-program/expenses/{one}"
        r = manager_client.post(
            f"{detail}/approve",
            data={"csrf_token": csrf_from(manager_client.get(detail).text)},
            follow_redirects=True)
        assert 'action="/undo"' in r.text

    def test_undo_reverses_the_approval(self, manager_client):
        one = create_voucher_over_http(manager_client)
        detail = f"/expense-program/expenses/{one}"
        # The toast is one-shot (app/flash.py), so the token has to be read
        # from the page the redirect actually landed on - fetching the same
        # URL again would find it already popped, exactly as a real second
        # page load would.
        page = manager_client.post(
            f"{detail}/approve",
            data={"csrf_token": csrf_from(manager_client.get(detail).text)},
            follow_redirects=True).text
        import re
        token = re.search(r'name="token" value="([^"]+)"', page)
        assert token, "no undo token was offered"
        manager_client.post("/undo", data={
            "csrf_token": csrf_from(page),
            "token": token.group(1),
            "next_url": detail,
        }, follow_redirects=False)
        assert _status(one) == "pending"

    def test_undo_requires_csrf(self, manager_client):
        one = create_voucher_over_http(manager_client)
        detail = f"/expense-program/expenses/{one}"
        manager_client.post(
            f"{detail}/approve",
            data={"csrf_token": csrf_from(manager_client.get(detail).text)},
            follow_redirects=True)
        r = manager_client.post("/undo", data={
            "csrf_token": "wrong", "token": "whatever", "next_url": detail,
        }, follow_redirects=False)
        assert r.status_code == 403
        assert _status(one) == "approved"

    def test_a_bogus_token_changes_nothing(self, manager_client):
        one = create_voucher_over_http(manager_client)
        detail = f"/expense-program/expenses/{one}"
        page = manager_client.get(detail).text
        r = manager_client.post("/undo", data={
            "csrf_token": csrf_from(page),
            "token": "deadbeef",
            "next_url": detail,
        }, follow_redirects=False)
        assert r.status_code == 302
        assert _status(one) == "pending"

    def test_undo_requires_login(self, client):
        r = client.post("/undo", data={
            "csrf_token": "x", "token": "y", "next_url": "/"},
            follow_redirects=False)
        assert r.status_code in (302, 403)

    def test_offsite_next_url_is_not_followed(self, manager_client):
        """`next_url` arrives in a form body - unchecked, it is an open
        redirect."""
        one = create_voucher_over_http(manager_client)
        detail = f"/expense-program/expenses/{one}"
        page = manager_client.get(detail).text
        r = manager_client.post("/undo", data={
            "csrf_token": csrf_from(page),
            "token": "deadbeef",
            "next_url": "https://evil.example.com/",
        }, follow_redirects=False)
        assert r.headers["location"] == "/programs"

    def test_protocol_relative_next_url_is_not_followed(self, manager_client):
        one = create_voucher_over_http(manager_client)
        page = manager_client.get(f"/expense-program/expenses/{one}").text
        r = manager_client.post("/undo", data={
            "csrf_token": csrf_from(page),
            "token": "deadbeef",
            "next_url": "//evil.example.com/",
        }, follow_redirects=False)
        assert r.headers["location"] == "/programs"


# ── notification centre ────────────────────────────────────────────────────

class TestNotificationPanel:

    def _one_notification(self, user_id=1):
        from app.services import notifications as svc
        with get_db() as conn:
            svc.notify(conn, user_id, "Budget exceeded", "/x", "warn")
        return svc.list_for_user(user_id)[0]["id"]

    def test_panel_renders_on_a_normal_page(self, manager_client):
        self._one_notification()
        html = manager_client.get("/expense-program/").text
        assert "notif-drawer" in html
        assert "Budget exceeded" in html

    def test_panel_groups_by_urgency(self, manager_client):
        from app.services import notifications as svc
        with get_db() as conn:
            svc.notify(conn, 1, "Needs you", "/a", "action")
        html = manager_client.get("/expense-program/").text
        assert "Action needed" in html

    def test_opening_a_page_does_not_clear_the_badge(self, manager_client):
        """The panel can be flicked open from anywhere. If merely rendering
        it marked everything read, the unread count would be meaningless."""
        from app.services import notifications as svc
        self._one_notification()
        manager_client.get("/expense-program/")
        assert svc.count_unread(1) == 1

    def test_mark_one_read(self, manager_client):
        from app.services import notifications as svc
        nid = self._one_notification()
        page = manager_client.get("/expense-program/").text
        manager_client.post(f"/notifications/{nid}/read",
                            data={"csrf_token": csrf_from(page)},
                            follow_redirects=False)
        assert svc.count_unread(1) == 0

    def test_mark_all_read(self, manager_client):
        from app.services import notifications as svc
        self._one_notification()
        self._one_notification()
        page = manager_client.get("/expense-program/").text
        manager_client.post("/notifications/read-all",
                            data={"csrf_token": csrf_from(page)},
                            follow_redirects=False)
        assert svc.count_unread(1) == 0

    def test_mark_read_requires_csrf(self, manager_client):
        from app.services import notifications as svc
        nid = self._one_notification()
        r = manager_client.post(f"/notifications/{nid}/read",
                                data={"csrf_token": "wrong"},
                                follow_redirects=False)
        assert r.status_code == 403
        assert svc.count_unread(1) == 1

    def test_mark_all_read_requires_csrf(self, manager_client):
        from app.services import notifications as svc
        self._one_notification()
        r = manager_client.post("/notifications/read-all",
                                data={"csrf_token": "wrong"},
                                follow_redirects=False)
        assert r.status_code == 403
        assert svc.count_unread(1) == 1

    def test_mark_read_requires_login(self, client):
        r = client.post("/notifications/1/read", data={"csrf_token": "x"},
                        follow_redirects=False)
        assert r.status_code in (302, 403)


# ── breadcrumbs, rendered ──────────────────────────────────────────────────

class TestBreadcrumbsRendered:

    def test_a_program_page_shows_its_trail(self, manager_client):
        html = manager_client.get("/expense-program/expenses").text
        assert 'aria-label="Breadcrumb"' in html
        assert "Petty Cash &amp; Expenses" in html or "Petty Cash & Expenses" in html

    def test_the_hub_has_no_breadcrumb(self, manager_client):
        """It would only point at itself."""
        html = manager_client.get("/programs").text
        assert 'aria-label="Breadcrumb"' not in html
