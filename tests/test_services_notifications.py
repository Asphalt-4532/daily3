"""In-app notifications: urgency bands, grouping, and read state.

The grouping lives in the service rather than the template on purpose (a
template deciding what counts as "urgent" is a business rule in the
presentation layer), so it is testable here without rendering anything.
"""
from app.database import get_db
from app.services import notifications as svc


def _notify(user_id=1, message="Something happened", link="", level="info"):
    with get_db() as conn:
        svc.notify(conn, user_id, message, link, level)


def _levels_of(user_id=1):
    return [n["level"] for n in svc.list_for_user(user_id)]


# ── level handling ─────────────────────────────────────────────────────────

def test_notify_defaults_to_info(reset_db):
    """Every caller written before urgency bands existed keeps working -
    that is what makes adding the column safe on an existing database."""
    with get_db() as conn:
        svc.notify(conn, 1, "Budget exceeded")
    assert _levels_of() == ["info"]


def test_notify_stores_a_declared_level(reset_db):
    _notify(level="action")
    assert _levels_of() == ["action"]


def test_unknown_level_degrades_to_info_rather_than_raising(reset_db):
    """A notification getting the wrong heading is always better than the
    thing it is about going unannounced."""
    _notify(level="catastrophe")
    assert _levels_of() == ["info"]


def test_notify_role_passes_the_level_through(reset_db):
    with get_db() as conn:
        n = svc.notify_role(conn, "manager", "Float is negative", "/x", "action")
    assert n >= 1
    assert svc.list_for_user(1)[0]["level"] == "action"


# ── grouping ───────────────────────────────────────────────────────────────

def test_list_grouped_orders_bands_most_urgent_first(reset_db):
    _notify(level="info", message="i")
    _notify(level="action", message="a")
    _notify(level="warn", message="w")
    assert [key for key, _, _ in svc.list_grouped(1)] == ["action", "warn", "info"]


def test_list_grouped_omits_empty_bands(reset_db):
    _notify(level="warn")
    groups = svc.list_grouped(1)
    assert len(groups) == 1
    assert groups[0][0] == "warn"


def test_list_grouped_headings_come_from_the_service(reset_db):
    _notify(level="action")
    _, heading, _ = svc.list_grouped(1)[0]
    assert heading == dict(svc.GROUPS)["action"]


def test_every_notification_lands_in_some_band(reset_db):
    """An unrecognised level must never make a notification invisible."""
    for level in ("action", "warn", "info", "made-up"):
        _notify(level=level)
    total = sum(len(items) for _, _, items in svc.list_grouped(1))
    assert total == 4


def test_list_grouped_is_empty_for_a_user_with_nothing(reset_db):
    assert svc.list_grouped(1) == []


def test_list_grouped_only_sees_its_own_users_notifications(reset_db):
    from app.services import users as users_service
    users_service.create_user(username="other", full_name="Other",
                              password="pass1234", role="user", acting_user_id=1)
    with get_db() as conn:
        other_id = conn.execute(
            "SELECT id FROM users WHERE username='other'").fetchone()["id"]
    _notify(user_id=other_id, level="action")
    assert svc.list_grouped(1) == []
    assert len(svc.list_grouped(other_id)) == 1


# ── read state ─────────────────────────────────────────────────────────────

def test_mark_read_marks_one_and_leaves_the_rest(reset_db):
    _notify(message="first")
    _notify(message="second")
    items = svc.list_for_user(1)
    assert svc.mark_read(items[0]["id"], 1) is True
    assert svc.count_unread(1) == 1


def test_mark_read_refuses_someone_elses_notification(reset_db):
    """Scoped in the WHERE clause, not checked-then-updated - so there is no
    window in which ownership could change underneath it."""
    from app.services import users as users_service
    users_service.create_user(username="other", full_name="Other",
                              password="pass1234", role="user", acting_user_id=1)
    with get_db() as conn:
        other_id = conn.execute(
            "SELECT id FROM users WHERE username='other'").fetchone()["id"]
    _notify(user_id=other_id)
    their_id = svc.list_for_user(other_id)[0]["id"]
    assert svc.mark_read(their_id, 1) is False
    assert svc.count_unread(other_id) == 1


def test_mark_read_of_a_missing_id_is_false_not_an_error(reset_db):
    # Indistinguishable from "someone else's", deliberately - it keeps this
    # from being an existence oracle.
    assert svc.mark_read(999999, 1) is False


def test_mark_all_read_clears_the_badge(reset_db):
    _notify()
    _notify()
    svc.mark_all_read(1)
    assert svc.count_unread(1) == 0
