"""Breadcrumb trails (app/breadcrumbs.py).

Pure functions over a path string, so these need no database and no server -
which is the point of having put the logic in a module rather than a
template block.
"""
from app.breadcrumbs import trail_for, siblings_for


def labels(path):
    return [c["label"] for c in trail_for(path)]


def test_program_dashboard_stops_at_the_program():
    assert labels("/expense-program/") == ["Programs", "Petty Cash & Expenses"]


def test_list_page_gets_three_crumbs():
    assert labels("/expense-program/expenses") == [
        "Programs", "Petty Cash & Expenses", "Expenses"]


def test_record_id_becomes_a_hash_crumb():
    assert labels("/expense-program/expenses/12")[-1] == "#12"


def test_nested_action_under_a_record():
    assert labels("/word-editor/documents/3/history") == [
        "Programs", "Word Editor", "Documents", "#3", "History"]


def test_last_crumb_is_current_and_has_no_link():
    trail = trail_for("/expense-program/expenses/12")
    assert trail[-1]["current"] is True
    assert trail[-1]["url"] == ""
    # ...and every earlier crumb is a real link, or the trail is useless.
    assert all(c["url"] for c in trail[:-1])


def test_only_the_last_crumb_is_current():
    trail = trail_for("/assets/reports")
    assert [c["current"] for c in trail] == [False, False, True]


def test_admin_pages_hang_off_the_hub():
    assert labels("/users/roles") == ["Programs", "Users & roles", "Roles"]


def test_hub_login_and_unknown_paths_get_no_trail():
    # A breadcrumb on the hub would point at itself; on login there is no
    # trail to speak of; and a guessed trail for an unrecognised path is
    # worse than none, because it would send people somewhere wrong.
    for path in ("/programs", "/login", "/", "", "/nope/nowhere"):
        assert trail_for(path) == []


def test_query_string_is_ignored():
    assert labels("/expense-program/expenses?status=pending") == labels(
        "/expense-program/expenses")


def test_trailing_slash_does_not_add_an_empty_crumb():
    assert labels("/expense-program/expenses/") == labels(
        "/expense-program/expenses")


def test_unlisted_child_still_gets_a_readable_label():
    # Not every page is spelled out in _CHILDREN; the fallback title-cases
    # the slug rather than showing a raw URL segment.
    assert labels("/expense-program/suppliers")[-1] == "Suppliers"


def test_siblings_are_the_other_pages_of_the_same_program():
    urls = [s["url"] for s in siblings_for("/expense-program/expenses/12")]
    assert "/expense-program/reports" in urls
    assert "/expense-program/budgets" in urls
    # Never another program's pages - that is what the module drawer is for.
    assert not any(u.startswith("/word-editor") for u in urls)


def test_siblings_are_empty_outside_a_program():
    assert siblings_for("/users") == []
    assert siblings_for("/programs") == []
