"""Tests for app/undo.py - the short-lived undo token.

Same stand-in as tests/test_flash.py: a token is a value in a dict-like
session, so a plain object with a `session` dict is a faithful Request and
these run instantly with no HTTP and no database.

The three properties worth protecting are expiry, single use, and refusing
a token that does not match. All three are what stop "undo" from being a
ten-second hole in the rules the rest of the app enforces.
"""
import time

from app import undo


class FakeRequest:
    def __init__(self, session=None):
        self.session = {} if session is None else session


def test_offer_returns_a_token_and_stores_the_action():
    req = FakeRequest()
    token = undo.offer(req, "expense_approve", [7])
    assert token
    pending = undo.peek(req)
    assert pending["kind"] == "expense_approve"
    assert pending["ids"] == [7]


def test_claim_with_the_right_token_returns_the_action():
    req = FakeRequest()
    token = undo.offer(req, "expense_reject", [1, 2])
    claimed = undo.claim(req, token)
    assert claimed["ids"] == [1, 2]


def test_claim_is_single_use():
    """A double-clicked Undo must reverse once. If this regressed, the
    second click could un-approve a voucher somebody legitimately
    re-approved in between."""
    req = FakeRequest()
    token = undo.offer(req, "expense_approve", [3])
    assert undo.claim(req, token) is not None
    assert undo.claim(req, token) is None


def test_a_wrong_token_is_refused_and_burns_the_offer():
    """Refusing but leaving the offer behind would let a second attempt
    succeed after the first was rejected."""
    req = FakeRequest()
    undo.offer(req, "expense_approve", [3])
    assert undo.claim(req, "not-the-token") is None
    assert undo.peek(req) is None


def test_empty_token_is_refused():
    req = FakeRequest()
    undo.offer(req, "expense_approve", [3])
    assert undo.claim(req, "") is None


def test_expired_token_is_refused_server_side():
    """The countdown in the toast only hides the button. A button that is
    merely hidden is still clickable by anyone crafting a POST, so the real
    limit has to be here."""
    req = FakeRequest()
    token = undo.offer(req, "expense_approve", [4])
    req.session["_undo"]["expires_at"] = time.time() - 1
    assert undo.claim(req, token) is None


def test_peek_does_not_consume():
    req = FakeRequest()
    token = undo.offer(req, "expense_delete", [9])
    assert undo.peek(req) is not None
    assert undo.peek(req) is not None
    assert undo.claim(req, token) is not None


def test_peek_clears_an_expired_offer():
    req = FakeRequest()
    undo.offer(req, "expense_approve", [4])
    req.session["_undo"]["expires_at"] = time.time() - 1
    assert undo.peek(req) is None
    assert "_undo" not in req.session


def test_nothing_to_undo_is_not_offered():
    req = FakeRequest()
    assert undo.offer(req, "expense_approve", []) == ""
    assert undo.offer(req, "", [1]) == ""
    assert undo.peek(req) is None


def test_oversized_selection_is_not_offered():
    """The token rides in a cookie-backed session. Past the cap it declines
    outright rather than truncating the id list, which would reverse only
    part of what was done while claiming to have reversed all of it."""
    req = FakeRequest()
    too_many = list(range(undo.MAX_RECORDS + 1))
    assert undo.offer(req, "expense_approve", too_many) == ""
    assert undo.peek(req) is None


def test_exactly_at_the_cap_is_still_offered():
    req = FakeRequest()
    ids = list(range(undo.MAX_RECORDS))
    assert undo.offer(req, "expense_approve", ids) != ""


def test_a_second_offer_replaces_the_first():
    """One undo at a time, matching the one toast at a time app/flash.py
    allows - the button on screen must always mean the last thing done."""
    req = FakeRequest()
    first = undo.offer(req, "expense_approve", [1])
    second = undo.offer(req, "expense_reject", [2])
    assert undo.claim(req, first) is None
    req2 = FakeRequest()
    undo.offer(req2, "expense_approve", [1])
    token = undo.offer(req2, "expense_reject", [2])
    assert undo.claim(req2, token)["kind"] == "expense_reject"
    assert second


def test_ids_are_coerced_to_ints():
    """They arrive from a form, so they arrive as strings."""
    req = FakeRequest()
    token = undo.offer(req, "expense_approve", ["3", "4"])
    assert undo.claim(req, token)["ids"] == [3, 4]


def test_request_without_a_session_is_handled():
    class Sessionless:
        pass

    assert undo.peek(Sessionless()) is None
    assert undo.claim(Sessionless(), "x") is None


def test_button_window_is_shorter_than_the_server_window():
    """Deliberate: a click landing at 4.9s still has to reach the server.
    If these were equal, that click would lose a race the user can neither
    see nor avoid."""
    assert undo.BUTTON_SECONDS < undo.TTL_SECONDS
