"""Short-lived "undo that" tokens.

A toast that says "Voucher approved" is more useful with a way back. This
stores one pending undo in the session, alongside the flash message that
advertises it, and hands out a single-use token the /undo route checks
before reversing anything.

Deliberately shared infrastructure (app/, not app/programs/): it knows
nothing about vouchers, documents or assets - a token is a `kind` string,
a list of record ids, and an expiry. app/routes/undo_routes.py owns the
mapping from `kind` to the service function that actually reverses it,
exactly the way app/routes/guards.py owns permission checks without knowing
what a voucher is.

Three properties this has to hold, and why:

* **Server-side expiry.** The countdown in the toast is a UI affordance and
  nothing more - a button that is merely hidden is still clickable by
  anyone who can craft a POST. TTL_SECONDS is the real limit, and it is
  checked here, on the server, every time.

* **Single use.** The token is popped before the reversal runs, so a
  double-clicked Undo reverses once and then finds nothing - it can never
  un-approve a voucher that somebody legitimately re-approved in between.

* **Session-scoped.** The token lives in the acting user's own session, so
  it cannot be replayed by another account. SessionMiddleware is already
  registered in main.py, so this adds no new dependency, and the session
  cookie is signed - a token cannot be forged client-side. Nothing secret
  goes in here regardless: ids and a status string, all of which the user
  who just performed the action already knows.

The server TTL is deliberately longer than the 5 seconds the button is
shown for. The extra window is latency, not generosity: a click registered
at 4.9s still has to travel to the server, and failing it there would be a
race the user cannot see or avoid.
"""
import time
import uuid

__all__ = [
    "offer", "claim", "peek", "clear", "TTL_SECONDS", "BUTTON_SECONDS",
    "MAX_RECORDS",
]

# How long the server will still honour an undo. See module docstring for
# why this is not the same number as BUTTON_SECONDS.
TTL_SECONDS = 10

# How long the Undo button stays on the toast. Sent to the template so the
# countdown and this module can never drift apart.
BUTTON_SECONDS = 5

# A bulk action can select a lot of rows; the token rides in a cookie-backed
# session, so the id list is capped. Beyond this the action still happens -
# it just does not offer an undo, rather than silently truncating the list
# and reversing only part of what was done.
MAX_RECORDS = 100

_SESSION_KEY = "_undo"


def offer(request, kind: str, record_ids, extra: dict = None) -> str:
    """Register a reversible action and return its token, or "" if it
    cannot be offered (nothing to reverse, or too many records).

    A second offer replaces the first: one undo at a time, matching the one
    toast at a time that app/flash.py allows."""
    ids = [int(i) for i in (record_ids or [])]
    if not kind or not ids or len(ids) > MAX_RECORDS:
        return ""
    token = uuid.uuid4().hex
    request.session[_SESSION_KEY] = {
        "token": token,
        "kind": kind,
        "ids": ids,
        "extra": extra or {},
        "expires_at": time.time() + TTL_SECONDS,
    }
    return token


def peek(request):
    """Return the pending undo without consuming it, or None. Used by the
    template that renders the toast - looking at the offer must not spend
    it."""
    data = _read(request)
    if data is None:
        clear(request)
    return data


def claim(request, token: str):
    """Consume the pending undo if `token` matches and has not expired.

    Returns the stored dict, or None. Always clears the slot, whether or
    not the token matched - a wrong or late token means this undo is over
    either way, and leaving it behind would let a second attempt succeed
    after the first was rejected."""
    data = _read(request)
    clear(request)
    if data is None or not token or data["token"] != token:
        return None
    return data


def clear(request) -> None:
    session = getattr(request, "session", None)
    if session is not None:
        session.pop(_SESSION_KEY, None)


def _read(request):
    """The stored undo if it is present, well-formed and unexpired."""
    session = getattr(request, "session", None)
    if not session:
        return None
    data = session.get(_SESSION_KEY)
    if not isinstance(data, dict):
        return None
    if not data.get("token") or not data.get("kind") or not data.get("ids"):
        return None
    if float(data.get("expires_at", 0)) < time.time():
        return None
    return data
