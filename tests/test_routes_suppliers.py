"""Integration tests: hits real Expense Program routes over HTTP for the
supplier knowledge base - the voucher form's VAT-able fields, the Settings
page's Suppliers card, and manager-only delete."""
import re

from tests.conftest import csrf_from


def _first_two_category_ids(html):
    cat_ids = re.findall(r'<option value="(\d+)"', html)
    return cat_ids[0], cat_ids[1] if len(cat_ids) > 1 else cat_ids[0]


def test_vatable_line_without_supplier_rejected_over_http(manager_client):
    form_html = manager_client.get("/expense-program/expenses/new").text
    csrf = csrf_from(form_html)
    cat_id, _ = _first_two_category_ids(form_html)
    r = manager_client.post("/expense-program/expenses/new", data={
        "csrf_token": csrf, "expense_date": "2026-08-23", "cost_center_id": "",
        "paid_to": "Vendor", "line_category_id": [cat_id], "line_particulars": ["Fuel"],
        "line_amount": ["100"], "line_is_vatable": ["1"],
        "line_supplier_name": [""], "line_supplier_vat": [""],
        "payment_mode": "Cash",
    }, follow_redirects=False)
    assert r.status_code == 400
    assert "supplier" in r.text.lower()


def test_vatable_line_with_supplier_succeeds_over_http(manager_client):
    form_html = manager_client.get("/expense-program/expenses/new").text
    csrf = csrf_from(form_html)
    cat_id, _ = _first_two_category_ids(form_html)
    r = manager_client.post("/expense-program/expenses/new", data={
        "csrf_token": csrf, "expense_date": "2026-08-23", "cost_center_id": "",
        "paid_to": "Vendor", "line_category_id": [cat_id], "line_particulars": ["Fuel"],
        "line_amount": ["100"], "line_is_vatable": ["1"],
        "line_supplier_name": ["HTTP Fuel Co"], "line_supplier_vat": ["VATHTTP001"],
        "payment_mode": "Cash",
    }, follow_redirects=False)
    assert r.status_code in (302, 303)
    detail = manager_client.get(r.headers["location"]).text
    assert "HTTP Fuel Co" in detail
    assert "VATHTTP001" in detail


def test_known_vat_appears_in_new_voucher_form_datalist(manager_client):
    test_vatable_line_with_supplier_succeeds_over_http(manager_client)
    form_html = manager_client.get("/expense-program/expenses/new").text
    assert "VATHTTP001" in form_html


def test_manager_can_view_suppliers_settings_page(manager_client):
    r = manager_client.get("/expense-program/settings")
    assert r.status_code == 200
    assert "Suppliers" in r.text


def test_user_cannot_view_settings_page(user_client):
    r = user_client.get("/expense-program/settings", follow_redirects=False)
    assert r.status_code in (302, 303)


def test_supplier_search_by_vat_filters_results(manager_client):
    test_vatable_line_with_supplier_succeeds_over_http(manager_client)
    matching = manager_client.get("/expense-program/settings?supplier_search=VATHTTP001")
    assert "HTTP Fuel Co" in matching.text
    non_matching = manager_client.get("/expense-program/settings?supplier_search=NOMATCH")
    assert "HTTP Fuel Co" not in non_matching.text


def test_manager_can_delete_a_supplier(manager_client):
    test_vatable_line_with_supplier_succeeds_over_http(manager_client)
    page = manager_client.get("/expense-program/settings")
    m = re.search(r"/expense-program/settings/suppliers/(\d+)/delete", page.text)
    assert m, "expected a delete form action for the new supplier"
    supplier_id = m.group(1)
    csrf = csrf_from(page.text)

    r = manager_client.post(f"/expense-program/settings/suppliers/{supplier_id}/delete",
                             data={"csrf_token": csrf, "reason": "no longer used"},
                             follow_redirects=False)
    assert r.status_code == 302

    active_view = manager_client.get("/expense-program/settings")
    assert "HTTP Fuel Co" not in active_view.text  # dropped from the active list

    deleted_view = manager_client.get("/expense-program/settings?supplier_status=deleted")
    assert "HTTP Fuel Co" in deleted_view.text  # still visible when explicitly filtered


def test_delete_supplier_requires_reason_over_http(manager_client):
    test_vatable_line_with_supplier_succeeds_over_http(manager_client)
    page = manager_client.get("/expense-program/settings")
    m = re.search(r"/expense-program/settings/suppliers/(\d+)/delete", page.text)
    supplier_id = m.group(1)
    csrf = csrf_from(page.text)
    r = manager_client.post(f"/expense-program/settings/suppliers/{supplier_id}/delete",
                             data={"csrf_token": csrf, "reason": "   "}, follow_redirects=False)
    assert r.status_code == 400


def test_accountant_cannot_delete_a_supplier(manager_client, accountant_client):
    test_vatable_line_with_supplier_succeeds_over_http(manager_client)
    page = manager_client.get("/expense-program/settings")
    m = re.search(r"/expense-program/settings/suppliers/(\d+)/delete", page.text)
    supplier_id = m.group(1)
    # accountant doesn't have delete_supplier by default (only manager does)
    r = accountant_client.post(f"/expense-program/settings/suppliers/{supplier_id}/delete",
                                data={"csrf_token": "irrelevant", "reason": "test"},
                                follow_redirects=False)
    assert r.status_code == 403


def test_deleted_supplier_still_shows_on_existing_voucher_detail(manager_client):
    form_html = manager_client.get("/expense-program/expenses/new").text
    csrf = csrf_from(form_html)
    cat_id, _ = _first_two_category_ids(form_html)
    r = manager_client.post("/expense-program/expenses/new", data={
        "csrf_token": csrf, "expense_date": "2026-08-23", "cost_center_id": "",
        "paid_to": "Vendor", "line_category_id": [cat_id], "line_particulars": ["Fuel"],
        "line_amount": ["100"], "line_is_vatable": ["1"],
        "line_supplier_name": ["Persist Co"], "line_supplier_vat": ["VATPERSISTHTTP"],
        "payment_mode": "Cash",
    }, follow_redirects=False)
    voucher_url = r.headers["location"]

    settings_page = manager_client.get("/expense-program/settings")
    supplier_id = re.search(r"/expense-program/settings/suppliers/(\d+)/delete", settings_page.text).group(1)
    csrf2 = csrf_from(settings_page.text)
    manager_client.post(f"/expense-program/settings/suppliers/{supplier_id}/delete",
                         data={"csrf_token": csrf2, "reason": "cleanup"}, follow_redirects=False)

    detail_after = manager_client.get(voucher_url).text
    assert "Persist Co" in detail_after
    assert "VATPERSISTHTTP" in detail_after
