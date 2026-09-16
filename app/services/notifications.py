"""In-app notifications - the single implementation.

The `notifications` table is shared infrastructure (app/database.py's
SCHEMA, not any one program's), so the code that reads and writes it lives
here rather than inside whichever program happened to need it first. Budget
over-runs were the first caller; voucher hand-offs are the second. A third
program can use this without importing anything from Expense Program.

Nothing here knows what a voucher, a budget or a document is - a
notification is a line of text plus a link.
"""
from app.database import get_db

__all__ = [
    "notify", "notify_role", "list_for_user", "list_grouped", "count_unread",
    "mark_all_read", "mark_read", "LEVELS", "GROUPS", "MAX_LISTED",
]

# Urgency bands, most urgent first. The notification centre renders one
# section per band in this order; anything with an unrecognised level is
# treated as "info" rather than dropped, so a level written by a future
# caller can never make a notification invisible.
LEVELS = ("action", "warn", "info")
DEFAULT_LEVEL = "info"

# (key, heading) pairs - the headings live here rather than in the template
# so a second consumer (an email digest, say) gets the same wording for free.
GROUPS = (
    ("action", "Action needed"),
    ("warn", "Warnings"),
    ("info", "Information"),
)

# A notification list is a "what needs my attention" glance, not an archive.
# The audit log is the permanent record.
MAX_LISTED = 50


def _clean_level(level: str) -> str:
    """One place decides what a valid level is. An unknown value degrades to
    "info" instead of raising - a notification getting the wrong heading is
    always better than the thing it is about going unannounced."""
    return level if level in LEVELS else DEFAULT_LEVEL


def notify(conn, user_id: int, message: str, link: str = "",
           level: str = DEFAULT_LEVEL) -> None:
    """Queue one notification. Takes an open connection because callers are
    normally mid-transaction (the notification and the thing it is about
    must both land, or neither).

    `level` is optional and defaults to "info", so every caller written
    before urgency bands existed keeps working unchanged."""
    conn.execute(
        "INSERT INTO notifications (user_id, message, link, level) VALUES (?, ?, ?, ?)",
        (user_id, message, link, _clean_level(level)),
    )


def notify_role(conn, role: str, message: str, link: str = "",
                level: str = DEFAULT_LEVEL) -> int:
    """Notify every active user holding a role. Returns how many were
    notified. Same transaction rule as notify()."""
    rows = conn.execute(
        "SELECT id FROM users WHERE role = ? AND active = 1", (role,)
    ).fetchall()
    for row in rows:
        notify(conn, row["id"], message, link, level)
    return len(rows)


def list_for_user(user_id: int, unread_only: bool = False) -> list:
    with get_db() as conn:
        query = "SELECT * FROM notifications WHERE user_id = ?"
        params = [user_id]
        if unread_only:
            query += " AND read = 0"
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(MAX_LISTED)
        return [dict(r) for r in conn.execute(query, params).fetchall()]


def list_grouped(user_id: int) -> list:
    """The notification centre's view: [(key, heading, [items...]), ...] in
    LEVELS order, with empty bands omitted.

    Grouping happens here rather than in the template for the usual reason -
    a template that has to decide what "urgent" means is a business rule in
    the presentation layer. Unknown levels fold into "info" via
    _clean_level(), so a row written by an older or newer caller always
    lands somewhere visible."""
    items = list_for_user(user_id)
    buckets = {key: [] for key in LEVELS}
    for item in items:
        buckets[_clean_level(item.get("level", DEFAULT_LEVEL))].append(item)
    return [(key, heading, buckets[key]) for key, heading in GROUPS if buckets[key]]


def count_unread(user_id: int) -> int:
    """Cheap enough to call on every page render - the index on
    (user_id, read) is there for exactly this."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM notifications WHERE user_id = ? AND read = 0",
            (user_id,),
        ).fetchone()
        return row["n"] if row else 0


def mark_all_read(user_id: int) -> None:
    with get_db() as conn:
        conn.execute("UPDATE notifications SET read = 1 WHERE user_id = ?", (user_id,))


def mark_read(notification_id: int, user_id: int) -> bool:
    """Mark one notification read. Scoped to the owning user in the WHERE
    clause rather than checked first and updated second - one statement, no
    window in which another request could change ownership underneath it.
    Returns True if a row was actually updated, False if the id belongs to
    someone else or does not exist (the caller cannot tell the two apart,
    which is deliberate - it stops this being an existence oracle)."""
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE notifications SET read = 1 WHERE id = ? AND user_id = ?",
            (notification_id, user_id),
        )
        return cur.rowcount > 0
