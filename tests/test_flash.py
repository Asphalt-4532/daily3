"""Tests for app/flash.py.

No HTTP and no app needed - a flash is a value in a dict-like session, so a
plain object with a `session` dict is a faithful stand-in for a Request and
keeps these tests instant.
"""
from app.flash import set_flash, pop_flash, MAX_LENGTH, LEVELS


class FakeRequest:
    def __init__(self, session=None):
        self.session = {} if session is None else session


def test_set_then_pop_returns_the_message():
    req = FakeRequest()
    set_flash(req, "Voucher approved.")
    flash = pop_flash(req)
    assert flash["message"] == "Voucher approved."
    assert flash["level"] == "success"


def test_popping_twice_returns_nothing_the_second_time():
    """The whole point: a toast shows once. If this regressed, every page
    after an action would keep re-showing the same confirmation."""
    req = FakeRequest()
    set_flash(req, "Draft saved.")
    assert pop_flash(req) is not None
    assert pop_flash(req) is None


def test_pop_with_nothing_set_is_none():
    assert pop_flash(FakeRequest()) is None


def test_empty_and_whitespace_messages_are_ignored():
    req = FakeRequest()
    set_flash(req, "")
    set_flash(req, "   ")
    set_flash(req, None)
    assert pop_flash(req) is None


def test_unknown_level_falls_back_instead_of_raising():
    """A bad level must never break the page it was meant to decorate."""
    req = FakeRequest()
    set_flash(req, "Something happened.", "catastrophe")
    assert pop_flash(req)["level"] == "info"


def test_every_declared_level_is_preserved():
    for level in LEVELS:
        req = FakeRequest()
        set_flash(req, "msg", level)
        assert pop_flash(req)["level"] == level


def test_long_message_is_truncated_not_rejected():
    req = FakeRequest()
    set_flash(req, "x" * (MAX_LENGTH + 500))
    assert len(pop_flash(req)["message"]) == MAX_LENGTH


def test_second_set_replaces_the_first():
    req = FakeRequest()
    set_flash(req, "first")
    set_flash(req, "second")
    assert pop_flash(req)["message"] == "second"
    assert pop_flash(req) is None


def test_request_without_a_session_is_handled():
    """Anything rendered outside a real request cycle must not explode."""
    class Sessionless:
        pass

    assert pop_flash(Sessionless()) is None
