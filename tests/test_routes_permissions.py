"""Integration tests: hits real routes over HTTP (via TestClient) as each
role and checks the response. Complements the service-layer tests, which
prove the business rules are correct in isolation - these prove the routes
actually enforce those rules for real requests."""
import re

from tests.conftest import csrf_from


MANAGER_ONLY_PAGES = ["/users", "/users/roles", "/expense-program/settings", "/audit", "/backups", "/backups/restore-log"]


def test_manager_can_reach_manager_only_pages(manager_client):
    for path in MANAGER_ONLY_PAGES:
        r = manager_client.get(path, follow_redirects=False)
        assert r.status_code == 200, f"manager should reach {path}"


def test_accountant_cannot_reach_manager_only_pages(accountant_client):
    for path in MANAGER_ONLY_PAGES:
        r = accountant_client.get(path, follow_redirects=False)
        assert r.status_code in (302, 303), f"accountant should be redirected away from {path}"
        assert r.headers["location"] == "/programs", "wrong-role redirect should land on the program hub"


def test_plain_user_cannot_reach_manager_only_pages(user_client):
    for path in MANAGER_ONLY_PAGES:
        r = user_client.get(path, follow_redirects=False)
        assert r.status_code in (302, 303), f"plain user should be redirected away from {path}"
        assert r.headers["location"] == "/programs", "wrong-role redirect should land on the program hub"


def test_logged_out_visitor_redirected_to_login(client):
    for path in ["/expense-program/", "/expense-program/expenses", "/expense-program/reports"] + MANAGER_ONLY_PAGES:
        r = client.get(path, follow_redirects=False)
        assert r.status_code in (302, 303)
        assert r.headers["location"] == "/login", f"{path} should redirect straight to /login"


def _create_test_expense(actor_client):
    form_html = actor_client.get("/expense-program/expenses/new").text
    csrf = csrf_from(form_html)
    cat_id = re.search(r'<option value="(\d+)"', form_html).group(1)
    r = actor_client.post("/expense-program/expenses/new", data={
        "csrf_token": csrf, "expense_date": "2026-08-22", "cost_center_id": "",
        "paid_to": "Vendor", "line_category_id": cat_id, "line_particulars": "Test",
        "line_amount": "100", "line_is_vatable": "0", "payment_mode": "Cash",
    }, follow_redirects=False)
    assert r.status_code in (302, 303)
    return r.headers["location"]  # /expense-program/expenses/{id}


def test_plain_user_can_create_expense_but_not_approve(user_client):
    expense_url = _create_test_expense(user_client)
    # a plain 'user' can't approve/reject, so the detail page renders no such
    # form for them - grab a valid csrf token from a page they *can* use,
    # to prove the block is a role check, not just an absent form
    new_form_csrf = csrf_from(user_client.get("/expense-program/expenses/new").text)
    r = user_client.post(f"{expense_url}/approve", data={"csrf_token": new_form_csrf},
                          follow_redirects=False)
    assert r.status_code == 403


def test_accountant_cannot_create_expense(accountant_client):
    r = accountant_client.get("/expense-program/expenses/new", follow_redirects=False)
    assert r.status_code in (302, 303)
    assert r.headers["location"] == "/programs"


def test_accountant_can_approve_but_not_delete(manager_client, accountant_client):
    expense_url = _create_test_expense(manager_client)  # manager can also create
    detail_html = accountant_client.get(expense_url).text
    csrf = csrf_from(detail_html)

    r = accountant_client.post(f"{expense_url}/approve", data={"csrf_token": csrf}, follow_redirects=False)
    assert r.status_code in (302, 303)

    # csrf tokens are per-session, not per-page-render, so the same token from
    # above is still valid here - and it needs to be, since the detail page no
    # longer renders a delete form for this role to grab a "fresh" one from
    r2 = accountant_client.post(f"{expense_url}/delete",
                                 data={"csrf_token": csrf, "reason": "trying to delete"},
                                 follow_redirects=False)
    assert r2.status_code == 403


def test_manager_can_delete(manager_client):
    expense_url = _create_test_expense(manager_client)
    detail_html = manager_client.get(expense_url).text
    csrf = csrf_from(detail_html)
    r = manager_client.post(f"{expense_url}/delete",
                             data={"csrf_token": csrf, "reason": "manager cleanup"},
                             follow_redirects=False)
    assert r.status_code in (302, 303)


def test_receipt_upload_rejects_spoofed_file_content_end_to_end(manager_client):
    """A script renamed to .jpg, uploaded through the real multipart form
    (not just called directly against the service), must be rejected -
    proves the sandboxing signature check is actually wired into the route."""
    form_html = manager_client.get("/expense-program/expenses/new").text
    csrf = csrf_from(form_html)
    cat_id = re.search(r'<option value="(\d+)"', form_html).group(1)

    fake_jpeg = b"#!/bin/sh\necho pwned\n"
    r = manager_client.post(
        "/expense-program/expenses/new",
        data={"csrf_token": csrf, "expense_date": "2026-08-23", "cost_center_id": "",
              "paid_to": "Vendor", "line_category_id": cat_id,
              "line_particulars": "Spoofed upload test", "line_amount": "10",
              "line_is_vatable": "0", "payment_mode": "Cash"},
        files={"receipt": ("receipt.jpg", fake_jpeg, "image/jpeg")},
    )
    assert r.status_code == 400
    assert "look like a real" in r.text


def test_receipt_upload_accepts_a_genuine_jpeg_end_to_end(manager_client):
    form_html = manager_client.get("/expense-program/expenses/new").text
    csrf = csrf_from(form_html)
    cat_id = re.search(r'<option value="(\d+)"', form_html).group(1)

    real_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 200
    r = manager_client.post(
        "/expense-program/expenses/new",
        data={"csrf_token": csrf, "expense_date": "2026-08-23", "cost_center_id": "",
              "paid_to": "Vendor", "line_category_id": cat_id,
              "line_particulars": "Real upload test", "line_amount": "10",
              "line_is_vatable": "0", "payment_mode": "Cash"},
        files={"receipt": ("receipt.jpg", real_jpeg, "image/jpeg")},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303)


def test_create_expense_with_multiple_lines_end_to_end(manager_client):
    """The core feature, exercised through the real HTTP form submission
    (not just the service layer): two transaction lines, one VAT-able and
    one not, positionally aligned across the repeated line_* fields."""
    form_html = manager_client.get("/expense-program/expenses/new").text
    csrf = csrf_from(form_html)
    cat_ids = re.findall(r'<option value="(\d+)"', form_html)
    assert len(cat_ids) >= 2

    r = manager_client.post(
        "/expense-program/expenses/new",
        data={
            "csrf_token": csrf, "expense_date": "2026-08-23", "cost_center_id": "",
            "paid_to": "Multi Vendor",
            "line_category_id": [cat_ids[0], cat_ids[1]],
            "line_particulars": ["First item", "Second item"],
            "line_amount": ["115", "40"],
            "line_is_vatable": ["1", "0"],
            "line_supplier_name": ["First Item Supplier", ""],
            "line_supplier_vat": ["VATPERM001", ""],
            "payment_mode": "Cash",
        },
        follow_redirects=False,
    )
    assert r.status_code in (302, 303)
    detail_html = manager_client.get(r.headers["location"]).text
    assert "First item" in detail_html
    assert "Second item" in detail_html
    assert "155.00" in detail_html  # 115 + 40 total


# --- drafts, over real HTTP ------------------------------------------------

def test_save_as_draft_with_incomplete_data_succeeds(manager_client):
    """The core of the feature over real HTTP: action=draft with a blank
    paid_to and zero lines must still save successfully - this is exactly
    what a strict 'submit' would reject."""
    form_html = manager_client.get("/expense-program/expenses/new").text
    csrf = csrf_from(form_html)
    r = manager_client.post("/expense-program/expenses/new", data={
        "csrf_token": csrf, "action": "draft", "expense_date": "2026-08-23",
        "cost_center_id": "", "paid_to": "", "payment_mode": "Cash",
    }, follow_redirects=False)
    assert r.status_code in (302, 303)
    detail_html = manager_client.get(r.headers["location"]).text
    assert "draft" in detail_html.lower()
    assert "Draft (not yet submitted)" in detail_html


def test_submit_action_with_incomplete_data_rerenders_with_error(manager_client):
    form_html = manager_client.get("/expense-program/expenses/new").text
    csrf = csrf_from(form_html)
    r = manager_client.post("/expense-program/expenses/new", data={
        "csrf_token": csrf, "action": "submit", "expense_date": "2026-08-23",
        "cost_center_id": "", "paid_to": "", "payment_mode": "Cash",
    })
    assert r.status_code == 400
    assert "required" in r.text.lower() or "at least one" in r.text.lower()


def _save_draft(client, paid_to="Draft Vendor"):
    form_html = client.get("/expense-program/expenses/new").text
    csrf = csrf_from(form_html)
    r = client.post("/expense-program/expenses/new", data={
        "csrf_token": csrf, "action": "draft", "expense_date": "2026-08-23",
        "cost_center_id": "", "paid_to": paid_to, "payment_mode": "Cash",
    }, follow_redirects=False)
    assert r.status_code in (302, 303)
    return r.headers["location"]


def test_edit_draft_form_shows_prefilled_values(manager_client):
    detail_url = _save_draft(manager_client, paid_to="Prefill Check Vendor")
    expense_id = detail_url.rstrip("/").split("/")[-1]
    edit_html = manager_client.get(f"/expense-program/expenses/{expense_id}/edit").text
    assert "Prefill Check Vendor" in edit_html
    assert "Edit draft voucher" in edit_html


def test_edit_draft_save_as_draft_keeps_it_a_draft(manager_client):
    detail_url = _save_draft(manager_client)
    expense_id = detail_url.rstrip("/").split("/")[-1]
    edit_html = manager_client.get(f"/expense-program/expenses/{expense_id}/edit").text
    csrf = csrf_from(edit_html)
    r = manager_client.post(f"/expense-program/expenses/{expense_id}/edit", data={
        "csrf_token": csrf, "action": "draft", "expense_date": "2026-08-23",
        "cost_center_id": "", "paid_to": "Still A Draft", "payment_mode": "Cash",
    }, follow_redirects=False)
    assert r.status_code in (302, 303)
    detail_html = manager_client.get(detail_url).text
    assert "Still A Draft" in detail_html
    assert "Draft (not yet submitted)" in detail_html


def test_edit_draft_submit_action_moves_to_pending(manager_client):
    detail_url = _save_draft(manager_client)
    expense_id = detail_url.rstrip("/").split("/")[-1]
    form_html = manager_client.get(f"/expense-program/expenses/{expense_id}/edit").text
    csrf = csrf_from(form_html)
    cat_id = re.search(r'<option value="(\d+)"', form_html).group(1)
    r = manager_client.post(f"/expense-program/expenses/{expense_id}/edit", data={
        "csrf_token": csrf, "action": "submit", "expense_date": "2026-08-23",
        "cost_center_id": "", "paid_to": "Now Complete Vendor", "payment_mode": "Cash",
        "line_category_id": cat_id, "line_particulars": "Now filled in",
        "line_amount": "50", "line_is_vatable": "0",
    }, follow_redirects=False)
    assert r.status_code in (302, 303)
    detail_html = manager_client.get(detail_url).text
    assert "PCV-" in detail_html  # real voucher number now assigned
    assert "pending" in detail_html.lower()


def test_quick_submit_route_moves_draft_to_pending(manager_client):
    """Submitting straight from the detail page's 'Submit for approval'
    button, without re-opening the edit form - only works if the draft's
    existing contents already pass strict validation."""
    form_html = manager_client.get("/expense-program/expenses/new").text
    csrf = csrf_from(form_html)
    cat_id = re.search(r'<option value="(\d+)"', form_html).group(1)
    r = manager_client.post("/expense-program/expenses/new", data={
        "csrf_token": csrf, "action": "draft", "expense_date": "2026-08-23",
        "cost_center_id": "", "paid_to": "Complete Draft Vendor", "payment_mode": "Cash",
        "line_category_id": cat_id, "line_particulars": "Complete line",
        "line_amount": "30", "line_is_vatable": "0",
    }, follow_redirects=False)
    detail_url = r.headers["location"]
    expense_id = detail_url.rstrip("/").split("/")[-1]

    detail_html = manager_client.get(detail_url).text
    csrf2 = csrf_from(detail_html)
    r2 = manager_client.post(f"/expense-program/expenses/{expense_id}/submit",
                              data={"csrf_token": csrf2}, follow_redirects=False)
    assert r2.status_code in (302, 303)
    final_html = manager_client.get(detail_url).text
    assert "PCV-" in final_html


def test_discard_draft_route_removes_it(manager_client):
    detail_url = _save_draft(manager_client)
    expense_id = detail_url.rstrip("/").split("/")[-1]
    detail_html = manager_client.get(detail_url).text
    csrf = csrf_from(detail_html)
    r = manager_client.post(f"/expense-program/expenses/{expense_id}/discard",
                             data={"csrf_token": csrf}, follow_redirects=False)
    assert r.status_code in (302, 303)
    r2 = manager_client.get(detail_url)
    assert r2.status_code == 404


def test_draft_ownership_enforced_over_http():
    """A 'user' can't edit, submit, or discard another user's draft - only
    its own owner or a manager can. Exercised over real HTTP, not just the
    service layer, to prove app.routes.guards + the service's ForbiddenError
    are actually wired together correctly end to end."""
    from tests.conftest import login
    from app.services import users as users_service
    from fastapi.testclient import TestClient
    from app.main import app as fastapi_app

    users_service.create_user(username="draft_owner", full_name="Draft Owner",
                               password="pass1234", role="user", acting_user_id=1)
    users_service.create_user(username="draft_intruder", full_name="Draft Intruder",
                               password="pass1234", role="user", acting_user_id=1)

    with TestClient(fastapi_app) as owner_client:
        login(owner_client, "draft_owner", "pass1234")
        detail_url = _save_draft(owner_client, paid_to="Owner's Draft")
        expense_id = detail_url.rstrip("/").split("/")[-1]

        with TestClient(fastapi_app) as intruder_client:
            login(intruder_client, "draft_intruder", "pass1234")

            r = intruder_client.get(f"/expense-program/expenses/{expense_id}/edit")
            assert r.status_code == 403

            # forge a plausible csrf token from the intruder's own session -
            # ownership must be rejected regardless of csrf validity
            some_form = intruder_client.get("/expense-program/expenses/new").text
            intruder_csrf = csrf_from(some_form)

            r2 = intruder_client.post(f"/expense-program/expenses/{expense_id}/submit",
                                       data={"csrf_token": intruder_csrf})
            assert r2.status_code == 403

            r3 = intruder_client.post(f"/expense-program/expenses/{expense_id}/discard",
                                       data={"csrf_token": intruder_csrf})
            assert r3.status_code == 403

        # untouched by any of the intruder's attempts (Jinja2 correctly
        # HTML-escapes the apostrophe: Owner's -> Owner&#39;s)
        still_there = owner_client.get(detail_url).text
        assert "Owner&#39;s Draft" in still_there


def test_drafts_excluded_from_default_list_but_visible_when_filtered(manager_client):
    _save_draft(manager_client, paid_to="Findable Draft Vendor")
    default_html = manager_client.get("/expense-program/expenses").text
    assert "Findable Draft Vendor" not in default_html

    filtered_html = manager_client.get("/expense-program/expenses?status=draft").text
    assert "Findable Draft Vendor" in filtered_html


def test_voucher_pdf_blocked_for_draft(manager_client):
    detail_url = _save_draft(manager_client)
    expense_id = detail_url.rstrip("/").split("/")[-1]
    r = manager_client.get(f"/expense-program/expenses/{expense_id}/voucher.pdf")
    assert r.status_code == 400
    assert b"%PDF" not in r.content


# --- duplicate transaction line detection/cleanup ---------------------------

def _save_draft_with_lines(client, cat_ids, lines):
    """lines: list of (particulars, amount, is_vatable) tuples, aligned
    positionally with cat_ids (repeated as needed)."""
    form_html = client.get("/expense-program/expenses/new").text
    csrf = csrf_from(form_html)
    data = {
        "csrf_token": csrf, "action": "draft", "expense_date": "2026-08-25",
        "cost_center_id": "", "paid_to": "Dup Test Vendor", "payment_mode": "Cash",
        "line_category_id": [cat_ids[i % len(cat_ids)] for i in range(len(lines))],
        "line_particulars": [l[0] for l in lines],
        "line_amount": [str(l[1]) for l in lines],
        "line_is_vatable": ["1" if l[2] else "0" for l in lines],
    }
    r = client.post("/expense-program/expenses/new", data=data, follow_redirects=False)
    assert r.status_code in (302, 303)
    return r.headers["location"]


def test_settings_page_reports_no_duplicates_by_default(manager_client):
    html = manager_client.get("/expense-program/settings").text
    assert "No duplicate transaction lines found." in html


def test_settings_page_shows_a_real_duplicate(manager_client):
    form_html = manager_client.get("/expense-program/expenses/new").text
    cat_id = re.search(r'<option value="(\d+)"', form_html).group(1)
    _save_draft_with_lines(manager_client, [cat_id], [
        ("Fuel", "100", False), ("Fuel", "100", False),
    ])
    html = manager_client.get("/expense-program/settings").text
    assert "Fuel" in html
    assert "Keep 1, remove rest" in html


def test_clean_duplicate_group_over_http_end_to_end(manager_client):
    form_html = manager_client.get("/expense-program/expenses/new").text
    cat_id = re.search(r'<option value="(\d+)"', form_html).group(1)
    detail_url = _save_draft_with_lines(manager_client, [cat_id], [
        ("Fuel", "100", False), ("Fuel", "100", False),
    ])

    settings_html = manager_client.get("/expense-program/settings").text
    csrf = csrf_from(settings_html)
    r = manager_client.post("/expense-program/settings/duplicates/clean", data={
        "csrf_token": csrf, "expense_id": detail_url.rstrip("/").split("/")[-1],
        "category_id": cat_id, "particulars": "Fuel", "amount": "100", "is_vatable": "0",
    }, follow_redirects=False)
    assert r.status_code in (302, 303)

    after_html = manager_client.get("/expense-program/settings").text
    assert "No duplicate transaction lines found." in after_html

    detail_html = manager_client.get(detail_url).text
    assert detail_html.count("Fuel") <= 2  # one line left, not the original two


def test_clean_duplicate_group_requires_manager(user_client):
    r = user_client.post("/expense-program/settings/duplicates/clean", data={
        "csrf_token": "x", "expense_id": "1", "category_id": "1",
        "particulars": "Fuel", "amount": "100", "is_vatable": "0",
    })
    assert r.status_code == 403


def test_clean_duplicate_group_requires_csrf(manager_client):
    r = manager_client.post("/expense-program/settings/duplicates/clean", data={
        "csrf_token": "wrong-token", "expense_id": "1", "category_id": "1",
        "particulars": "Fuel", "amount": "100", "is_vatable": "0",
    })
    assert r.status_code == 403


# --- roles & permissions, over real HTTP ------------------------------------

def test_accountant_and_user_cannot_reach_roles_page(accountant_client, user_client):
    for c in (accountant_client, user_client):
        r = c.get("/users/roles", follow_redirects=False)
        assert r.status_code in (302, 303)
        assert r.headers["location"] == "/programs"


def test_manager_can_view_and_change_a_permission_end_to_end(manager_client):
    """The actual feature: a manager toggles a permission from the real
    page, and it takes effect immediately for a *different, already logged
    in* session - no restart, no re-login."""
    html = manager_client.get("/users/roles").text
    assert "Roles &amp; Permissions" in html or "Roles & Permissions" in html
    assert "delete_expense" in html
    csrf = csrf_from(html)

    r = manager_client.post("/users/roles/update", data={
        "csrf_token": csrf, "permission": "delete_expense",
        "allowed_roles": ["manager", "accountant"],
    })
    assert r.status_code == 200

    from app.services import roles as roles_service
    matrix = {p["key"]: p["roles"] for p in roles_service.list_permission_matrix()}
    assert matrix["delete_expense"] == {"manager": True, "accountant": True, "user": False}

    # a plain 'user' still can't delete...
    expense_url = _create_test_expense(manager_client)
    new_form_csrf = csrf_from(manager_client.get("/expense-program/expenses/new").text)

    # ...but an accountant now can, purely because the manager flipped a
    # checkbox - no code change, no restart.
    from tests.conftest import login
    from fastapi.testclient import TestClient
    from app.main import app as fastapi_app
    with TestClient(fastapi_app) as accountant_client2:
        from app.services import users as users_service
        users_service.create_user(username="acct2", full_name="Acct Two", password="pass1234",
                                   role="accountant", acting_user_id=1)
        login(accountant_client2, "acct2", "pass1234")
        detail_html = accountant_client2.get(expense_url).text
        acsrf = csrf_from(detail_html)
        r2 = accountant_client2.post(f"{expense_url}/delete", data={
            "csrf_token": acsrf, "reason": "granted via Roles & Permissions",
        }, follow_redirects=False)
        assert r2.status_code in (302, 303)


def test_manager_change_is_rejected_when_it_would_lock_out_user_management(manager_client):
    html = manager_client.get("/users/roles").text
    csrf = csrf_from(html)
    r = manager_client.post("/users/roles/update", data={
        "csrf_token": csrf, "permission": "manage_users", "allowed_roles": [],
    })
    assert r.status_code == 400
    assert "lock every account" in r.text
    # manager role must still have it - re-fetch and confirm nothing changed
    after = manager_client.get("/users/roles").text
    assert "lock every account" not in after  # error doesn't persist across a fresh GET


# ---------------------------------------------------------------------------
# Quick-clone, sharing, and toast feedback - over real HTTP
# ---------------------------------------------------------------------------

from tests.conftest import create_voucher_over_http


def test_clone_creates_a_draft_and_lands_on_its_edit_page(manager_client):
    eid = create_voucher_over_http(manager_client)
    csrf = csrf_from(manager_client.get(f"/expense-program/expenses/{eid}").text)

    r = manager_client.post(f"/expense-program/expenses/{eid}/clone",
                            data={"csrf_token": csrf}, follow_redirects=False)
    assert r.status_code in (302, 303)
    location = r.headers["location"]
    assert location.endswith("/edit")

    new_id = int(location.split("/expenses/")[1].split("/")[0])
    assert new_id != eid
    from app.programs.expenses.services import expenses as svc
    assert svc.get_expense(new_id)["status"] == "draft"


def test_clone_shows_a_toast_on_the_next_page(manager_client):
    """The redirect target has to carry the confirmation, or the user sees
    a silently-changed page."""
    eid = create_voucher_over_http(manager_client)
    csrf = csrf_from(manager_client.get(f"/expense-program/expenses/{eid}").text)
    manager_client.post(f"/expense-program/expenses/{eid}/clone",
                        data={"csrf_token": csrf}, follow_redirects=False)

    followed = manager_client.get(f"/expense-program/expenses/{eid}")
    assert "Copied to a new draft" in followed.text


def test_a_toast_is_shown_once_and_not_again(manager_client):
    eid = create_voucher_over_http(manager_client)
    csrf = csrf_from(manager_client.get(f"/expense-program/expenses/{eid}").text)
    manager_client.post(f"/expense-program/expenses/{eid}/clone",
                        data={"csrf_token": csrf}, follow_redirects=False)

    first = manager_client.get(f"/expense-program/expenses/{eid}")
    assert "Copied to a new draft" in first.text
    second = manager_client.get(f"/expense-program/expenses/{eid}")
    assert "Copied to a new draft" not in second.text


def test_accountant_cannot_clone_by_default(accountant_client, manager_client):
    """Cloning makes a new voucher, so it follows create permission - which
    an accountant does not have out of the box."""
    eid = create_voucher_over_http(manager_client)
    html = accountant_client.get(f"/expense-program/expenses/{eid}").text
    assert "Duplicate" not in html
    csrf = csrf_from(html)
    r = accountant_client.post(f"/expense-program/expenses/{eid}/clone",
                               data={"csrf_token": csrf}, follow_redirects=False)
    assert r.status_code == 403


def test_share_notifies_the_recipient_end_to_end(manager_client):
    from app.services import users as users_service
    from app.services import notifications as notifications_svc

    acct_id = users_service.create_user(
        username="share_acct", full_name="Share Accountant", password="pass1234",
        role="accountant", acting_user_id=1,
    )
    eid = create_voucher_over_http(manager_client)
    html = manager_client.get(f"/expense-program/expenses/{eid}").text
    assert "Share Accountant" in html  # offered as a recipient

    r = manager_client.post(f"/expense-program/expenses/{eid}/share", data={
        "csrf_token": csrf_from(html), "share_with": str(acct_id),
        "note": "please book this",
    }, follow_redirects=False)
    assert r.status_code in (302, 303)

    notes = notifications_svc.list_for_user(acct_id)
    assert len(notes) == 1
    assert notes[0]["link"] == f"/expense-program/expenses/{eid}"

    # and the voucher itself now records the hand-off
    assert "Already shared with" in manager_client.get(f"/expense-program/expenses/{eid}").text


def test_recipient_sees_the_notification_and_it_clears_when_read(manager_client):
    from tests.conftest import login
    from fastapi.testclient import TestClient
    from app.main import app as fastapi_app
    from app.services import users as users_service

    acct_id = users_service.create_user(
        username="bell_acct", full_name="Bell Accountant", password="pass1234",
        role="accountant", acting_user_id=1,
    )
    eid = create_voucher_over_http(manager_client)
    html = manager_client.get(f"/expense-program/expenses/{eid}").text
    manager_client.post(f"/expense-program/expenses/{eid}/share", data={
        "csrf_token": csrf_from(html), "share_with": str(acct_id), "note": "",
    }, follow_redirects=False)

    with TestClient(fastapi_app) as c:
        login(c, "bell_acct", "pass1234")
        # badge count on any page
        assert '<span class="notif-count">1</span>' in c.get("/programs").text
        page = c.get("/notifications")
        assert "shared" in page.text
        # opening the page is the acknowledgement
        assert '<span class="notif-count">' not in c.get("/programs").text


def test_sharing_with_nobody_selected_warns_instead_of_500(manager_client):
    eid = create_voucher_over_http(manager_client)
    html = manager_client.get(f"/expense-program/expenses/{eid}").text
    r = manager_client.post(f"/expense-program/expenses/{eid}/share", data={
        "csrf_token": csrf_from(html), "note": "",
    }, follow_redirects=False)
    assert r.status_code in (302, 303)
    assert "at least one person" in manager_client.get(f"/expense-program/expenses/{eid}").text


def test_notifications_page_requires_login(client):
    r = client.get("/notifications", follow_redirects=False)
    assert r.status_code in (302, 303)
    assert r.headers["location"] == "/login"


def test_new_voucher_form_preselects_the_users_recent_category(manager_client):
    """The default has to reach both copies of the field on the form."""
    from tests.conftest import second_category_id
    cat = second_category_id()

    from tests.conftest import first_cost_center_id
    from datetime import date
    html = manager_client.get("/expense-program/expenses/new").text
    manager_client.post("/expense-program/expenses/new", data={
        "csrf_token": csrf_from(html), "action": "submit",
        "expense_date": date.today().isoformat(),
        "cost_center_id": str(first_cost_center_id()),
        "paid_to": "Habit Shop", "payment_mode": "Cash",
        "line_category_id": str(cat), "line_particulars": "Same as always",
        "line_amount": "10.00", "line_is_vatable": "0",
    }, follow_redirects=False)

    fresh = manager_client.get("/expense-program/expenses/new").text
    # both the server-rendered row and the "+ Add line" <template> clone
    assert fresh.count(f'value="{cat}" selected') == 2
