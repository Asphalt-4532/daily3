"""Integration tests: hits real Weekly Productions routes over HTTP (via
TestClient), mirroring tests/test_routes_permissions.py's approach for
Expense Program. Report lines are submitted as three same-named form field
lists (line_material_type, line_desc_dims, line_quantity) - see
production_routes.py's docstring."""
import re
from datetime import date

from tests.conftest import csrf_from


def _line_form_data(lines):
    data = {"line_material_type": [], "line_desc_dims": [], "line_quantity": []}
    for line in lines:
        data["line_material_type"].append(line.get("material_type", "GI"))
        data["line_desc_dims"].append(line.get("raw", "cable tray 100x50x2.44mx0.7"))
        data["line_quantity"].append(str(line.get("quantity", 20)))
    return data


def _create_report(actor_client, lines=None, **overrides):
    form = actor_client.get("/weekly-productions/reports/new")
    csrf = csrf_from(form.text)
    data = {
        "csrf_token": csrf, "report_date": date.today().isoformat(),
        "line_name": "Line 1", "shift": "Morning", "notes": "",
    }
    data.update(overrides)
    data.update(_line_form_data(lines or [{}]))
    return actor_client.post("/weekly-productions/reports/new", data=data, follow_redirects=False)


def test_program_hub_lists_weekly_productions_as_available(manager_client):
    r = manager_client.get("/programs")
    assert r.status_code == 200
    assert "Weekly Productions" in r.text
    assert "/weekly-productions/" in r.text


def test_dashboard_reachable_by_any_logged_in_role(manager_client, accountant_client, user_client):
    for c in (manager_client, accountant_client, user_client):
        r = c.get("/weekly-productions/", follow_redirects=False)
        assert r.status_code == 200


def test_logged_out_visitor_redirected_to_login(client):
    for path in ["/weekly-productions/", "/weekly-productions/reports", "/weekly-productions/reports/new"]:
        r = client.get(path, follow_redirects=False)
        assert r.status_code in (302, 303)
        assert r.headers["location"] == "/login"


def test_user_can_record_a_straight_and_an_elbow_line(user_client):
    r = _create_report(user_client, lines=[
        {"material_type": "GI", "raw": "cable tray 100x50x2.44mx0.7", "quantity": 10},
        {"material_type": "HDG", "raw": "fitting 90 degree elbow 200x50xx0.7", "quantity": 2},
    ])
    assert r.status_code == 302
    assert r.headers["location"] == "/weekly-productions/reports"

    listing = user_client.get("/weekly-productions/reports")
    assert "PRD-" in listing.text
    assert "GI" in listing.text and "HDG" in listing.text


def test_user_can_record_a_tee_line(user_client):
    r = _create_report(user_client, lines=[
        {"material_type": "GI", "raw": "cable tray tee 200-200-200x100xx0.7mm", "quantity": 1},
    ])
    assert r.status_code == 302
    listing = user_client.get("/weekly-productions/reports")
    m = re.search(r"/weekly-productions/reports/(\d+)", listing.text)
    detail = user_client.get(f"/weekly-productions/reports/{m.group(1)}")
    assert "cable tray tee" in detail.text
    assert "Weight (kg)" in detail.text


def test_accountant_cannot_reach_new_report_form(accountant_client):
    r = accountant_client.get("/weekly-productions/reports/new", follow_redirects=False)
    assert r.status_code in (302, 303)
    assert r.headers["location"] == "/programs"


def test_accountant_cannot_post_new_report(accountant_client, manager_client):
    form = manager_client.get("/weekly-productions/reports/new")
    csrf = csrf_from(form.text)
    data = {"csrf_token": csrf, "report_date": date.today().isoformat(),
            "line_name": "Line 1", "shift": "", "notes": ""}
    data.update(_line_form_data([{}]))
    r = accountant_client.post("/weekly-productions/reports/new", data=data, follow_redirects=False)
    assert r.status_code == 403


def test_create_report_rejects_bad_csrf(user_client):
    data = {"csrf_token": "not-the-real-token", "report_date": date.today().isoformat(),
            "line_name": "Line 1", "shift": "", "notes": ""}
    data.update(_line_form_data([{}]))
    r = user_client.post("/weekly-productions/reports/new", data=data, follow_redirects=False)
    assert r.status_code == 403


def test_create_report_with_missing_length_marker_rerenders_form_with_error(user_client):
    r = _create_report(user_client, lines=[{"raw": "cable tray 100x50x2.44x0.7"}])
    assert r.status_code == 400
    assert "length" in r.text.lower()


def test_create_report_elbow_without_angle_rerenders_form_with_error(user_client):
    r = _create_report(user_client, lines=[{"raw": "fitting elbow 200x50xx0.7"}])
    assert r.status_code == 400
    assert "angle" in r.text.lower()


def test_report_detail_shows_description_and_weight(user_client):
    _create_report(user_client, lines=[{"raw": "cable tray 100x50x2.44mx0.7"}])
    listing = user_client.get("/weekly-productions/reports")
    m = re.search(r"/weekly-productions/reports/(\d+)", listing.text)
    assert m
    detail = user_client.get(f"/weekly-productions/reports/{m.group(1)}")
    assert detail.status_code == 200
    assert "cable tray" in detail.text
    assert "Total weight" in detail.text


def test_manager_can_delete_report_with_reason(manager_client):
    _create_report(manager_client)
    listing = manager_client.get("/weekly-productions/reports")
    m = re.search(r"/weekly-productions/reports/(\d+)", listing.text)
    report_id = m.group(1)
    detail = manager_client.get(f"/weekly-productions/reports/{report_id}")
    csrf = csrf_from(detail.text)

    r = manager_client.post(f"/weekly-productions/reports/{report_id}/delete",
                             data={"csrf_token": csrf, "reason": "duplicate entry"},
                             follow_redirects=False)
    assert r.status_code == 302

    deleted_view = manager_client.get(f"/weekly-productions/reports/{report_id}")
    assert "deleted" in deleted_view.text.lower()


def test_user_cannot_delete_report(user_client, manager_client):
    _create_report(manager_client)
    listing = manager_client.get("/weekly-productions/reports")
    report_id = re.search(r"/weekly-productions/reports/(\d+)", listing.text).group(1)
    new_form = user_client.get("/weekly-productions/reports/new")
    csrf = csrf_from(new_form.text)
    r = user_client.post(f"/weekly-productions/reports/{report_id}/delete",
                          data={"csrf_token": csrf, "reason": "test"}, follow_redirects=False)
    assert r.status_code == 403


def test_reports_page_shows_weight_summary_and_export_link(user_client):
    _create_report(user_client)
    r = user_client.get("/weekly-productions/reports")
    assert r.status_code == 200
    assert "Total weight" in r.text
    assert "/weekly-productions/reports/export.xlsx" in r.text


def test_export_xlsx_downloads_for_any_logged_in_role(user_client):
    _create_report(user_client)
    r = user_client.get("/weekly-productions/reports/export.xlsx")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


def test_deleting_report_is_recorded_in_audit_log(manager_client):
    _create_report(manager_client)
    listing = manager_client.get("/weekly-productions/reports")
    report_id = re.search(r"/weekly-productions/reports/(\d+)", listing.text).group(1)
    detail = manager_client.get(f"/weekly-productions/reports/{report_id}")
    csrf = csrf_from(detail.text)
    manager_client.post(f"/weekly-productions/reports/{report_id}/delete",
                         data={"csrf_token": csrf, "reason": "audit-check"},
                         follow_redirects=False)
    audit = manager_client.get("/audit")
    assert "delete_production_report" in audit.text or "audit-check" in audit.text


# --- Settings (weight factors) ---------------------------------------------

def test_manager_can_reach_weight_settings(manager_client):
    r = manager_client.get("/weekly-productions/settings")
    assert r.status_code == 200
    assert "GI factor" in r.text
    assert "HDG factor" in r.text


def test_user_cannot_reach_weight_settings(user_client):
    r = user_client.get("/weekly-productions/settings", follow_redirects=False)
    assert r.status_code in (302, 303)
    assert r.headers["location"] == "/programs"


def test_manager_can_update_weight_factor(manager_client):
    page = manager_client.get("/weekly-productions/settings")
    csrf = csrf_from(page.text)
    r = manager_client.post("/weekly-productions/settings/weight-factor",
                             data={"csrf_token": csrf, "material_type": "GI", "value": "9.5"},
                             follow_redirects=False)
    assert r.status_code == 302
    updated = manager_client.get("/weekly-productions/settings")
    assert "9.5" in updated.text


def test_update_weight_factor_rejects_bad_csrf(manager_client):
    r = manager_client.post("/weekly-productions/settings/weight-factor",
                             data={"csrf_token": "bad", "material_type": "GI", "value": "9.5"},
                             follow_redirects=False)
    assert r.status_code == 403


def test_manager_can_update_weekly_and_monthly_targets(manager_client):
    page = manager_client.get("/weekly-productions/settings")
    csrf = csrf_from(page.text)
    r = manager_client.post("/weekly-productions/settings/weight-target",
                             data={"csrf_token": csrf, "period": "weekly", "value": "500"},
                             follow_redirects=False)
    assert r.status_code == 302
    r2 = manager_client.post("/weekly-productions/settings/weight-target",
                              data={"csrf_token": csrf, "period": "monthly", "value": "2000"},
                              follow_redirects=False)
    assert r2.status_code == 302
    updated = manager_client.get("/weekly-productions/settings")
    assert "500" in updated.text and "2000" in updated.text


def test_weight_target_progress_shown_on_dashboard(manager_client):
    page = manager_client.get("/weekly-productions/settings")
    csrf = csrf_from(page.text)
    manager_client.post("/weekly-productions/settings/weight-target",
                         data={"csrf_token": csrf, "period": "weekly", "value": "200"},
                         follow_redirects=False)
    _create_report(manager_client, lines=[{"raw": "cable tray 100x50x2.44mx0.7", "quantity": 10}])
    dash = manager_client.get("/weekly-productions/")
    assert "weekly target" in dash.text.lower()


def test_no_target_set_hides_progress_bar(manager_client):
    dash = manager_client.get("/weekly-productions/")
    assert "weekly target" not in dash.text.lower()
    assert "monthly target" not in dash.text.lower()


def test_user_cannot_update_weight_target(user_client):
    r = user_client.post("/weekly-productions/settings/weight-target",
                          data={"csrf_token": "irrelevant", "period": "weekly", "value": "500"},
                          follow_redirects=False)
    assert r.status_code == 403


# --- month lockout ------------------------------------------------------------

def test_manager_can_lock_and_unlock_a_month(manager_client):
    page = manager_client.get("/weekly-productions/settings")
    csrf = csrf_from(page.text)
    r = manager_client.post("/weekly-productions/settings/lock-month",
                             data={"csrf_token": csrf, "month": "2026-08"}, follow_redirects=False)
    assert r.status_code == 302
    locked_view = manager_client.get("/weekly-productions/settings")
    assert "August 2026" in locked_view.text

    csrf2 = csrf_from(locked_view.text)
    r2 = manager_client.post("/weekly-productions/settings/unlock-month",
                              data={"csrf_token": csrf2, "month": "2026-08"}, follow_redirects=False)
    assert r2.status_code == 302
    unlocked_view = manager_client.get("/weekly-productions/settings")
    assert "August 2026" not in unlocked_view.text


def test_user_cannot_lock_a_month(user_client):
    r = user_client.post("/weekly-productions/settings/lock-month",
                          data={"csrf_token": "irrelevant", "month": "2026-08"}, follow_redirects=False)
    assert r.status_code == 403


def test_new_report_rejected_for_a_locked_month(manager_client):
    page = manager_client.get("/weekly-productions/settings")
    csrf = csrf_from(page.text)
    manager_client.post("/weekly-productions/settings/lock-month",
                         data={"csrf_token": csrf, "month": "2026-08"}, follow_redirects=False)
    r = _create_report(manager_client, report_date="2026-08-15")
    assert r.status_code == 400
    assert "locked" in r.text.lower()


def test_existing_report_in_locked_month_can_still_be_deleted(manager_client):
    r = _create_report(manager_client, report_date="2026-08-15")
    assert r.status_code == 302
    listing = manager_client.get("/weekly-productions/reports")
    report_id = re.search(r"/weekly-productions/reports/(\d+)", listing.text).group(1)

    page = manager_client.get("/weekly-productions/settings")
    csrf = csrf_from(page.text)
    manager_client.post("/weekly-productions/settings/lock-month",
                         data={"csrf_token": csrf, "month": "2026-08"}, follow_redirects=False)

    detail = manager_client.get(f"/weekly-productions/reports/{report_id}")
    csrf2 = csrf_from(detail.text)
    r2 = manager_client.post(f"/weekly-productions/reports/{report_id}/delete",
                              data={"csrf_token": csrf2, "reason": "correction"},
                              follow_redirects=False)
    assert r2.status_code == 302


def test_daily_report_link_present_for_locked_month(manager_client):
    page = manager_client.get("/weekly-productions/settings")
    csrf = csrf_from(page.text)
    manager_client.post("/weekly-productions/settings/lock-month",
                         data={"csrf_token": csrf, "month": "2026-02"}, follow_redirects=False)
    settings_page = manager_client.get("/weekly-productions/settings")
    assert "/weekly-productions/reports?start=2026-02-01&end=2026-02-28" in settings_page.text


# --- daily production report --------------------------------------------------

def test_reports_page_shows_by_day_table(user_client):
    _create_report(user_client)
    r = user_client.get("/weekly-productions/reports")
    assert "By day" in r.text


# --- PDF export --------------------------------------------------------------

def test_export_pdf_downloads_for_any_logged_in_role(user_client):
    _create_report(user_client)
    r = user_client.get("/weekly-productions/reports/export.pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content[:4] == b"%PDF"


def test_export_pdf_link_present_on_reports_page(user_client):
    r = user_client.get("/weekly-productions/reports")
    assert "/weekly-productions/reports/export.pdf" in r.text


# --- reducer and cross keywords over HTTP ------------------------------------

def test_reducer_and_cross_lines_over_http(user_client):
    r = _create_report(user_client, lines=[
        {"material_type": "GI", "raw": "reducer 200x50xx0.7", "quantity": 1},
        {"material_type": "HDG", "raw": "cable tray cross 200-200-200x100xx0.7mm", "quantity": 1},
    ])
    assert r.status_code == 302
    listing = user_client.get("/weekly-productions/reports")
    m = re.search(r"/weekly-productions/reports/(\d+)", listing.text)
    detail = user_client.get(f"/weekly-productions/reports/{m.group(1)}")
    assert "reducer" in detail.text
    assert "cable tray cross" in detail.text


def _topbar_nav(html: str) -> str:
    """Just the top bar's own link row.

    The module drawer below it deliberately lists *every* program - that is
    the whole point of a drawer - so a whole-page substring search can no
    longer answer "does this program's nav show another program's links".
    Scoping to the nav asks the question the test name actually asks.
    """
    start = html.find('<div class="nav">')
    assert start != -1, "top bar nav not found"
    end = html.find("</div>", html.find("Log out", start))
    return html[start:end]


def test_weekly_productions_nav_shows_only_its_own_links(user_client):
    dash = user_client.get("/weekly-productions/")
    nav = _topbar_nav(dash.text)
    assert "Production" in nav
    # "New entry", not "+ New Entry" - the nav label lost its plus and its
    # capital E in the UI redesign, and this assertion was left behind. It
    # was failing before any of the UI/UX work in this change, which is how
    # it got noticed; the link itself has been there and working throughout.
    assert "New entry" in nav
    assert "/expense-program/" not in nav
    assert "+ New Voucher" not in nav
