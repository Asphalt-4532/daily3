"""Word Editor comments service.

Visibility rules:
- Top-level comment (no @): doc owner + commenter only.
- Top-level comment with @user: + that user.
- Top-level comment with @location: + all users in that location.
- Reply (no @): thread starter + replier only.
- Reply with @: thread participants + tagged.
- Manager sees all.

@ tags extracted from comment content (data-tag-type / data-tag-id attributes).
"""
import re

from app.database import get_db
from app.services.errors import NotFoundError, ForbiddenError, ValidationError

__all__ = [
    "list_threads", "get_thread", "post_comment", "post_reply",
    "edit_comment", "unread_count",
]


def _extract_comment_tags(content: str) -> list[dict]:
    tags = []
    for m in re.finditer(r'data-tag-type="(user|location)"\s+data-tag-id="(\d+)"', content):
        tags.append({"tag_type": m.group(1), "id": int(m.group(2))})
    return tags


def _save_comment_tags(conn, comment_id: int, content: str):
    conn.execute("DELETE FROM wd_comment_tags WHERE comment_id=?", (comment_id,))
    for tag in _extract_comment_tags(content):
        if tag["tag_type"] == "user":
            conn.execute(
                "INSERT OR IGNORE INTO wd_comment_tags (comment_id, tag_type, tagged_user_id) VALUES (?,?,?)",
                (comment_id, "user", tag["id"])
            )
        else:
            conn.execute(
                "INSERT OR IGNORE INTO wd_comment_tags (comment_id, tag_type, tagged_location_id) VALUES (?,?,?)",
                (comment_id, "location", tag["id"])
            )


def _can_see_comment(conn, comment: dict, user_id: int, role: str,
                     doc_owner_id: int, loc_ids: list[int]) -> bool:
    if role == "manager":
        return True
    if comment["author_id"] == user_id:
        return True
    if doc_owner_id == user_id:
        return True
    if comment["parent_id"]:
        parent = conn.execute("SELECT author_id FROM wd_comments WHERE id=?",
                              (comment["parent_id"],)).fetchone()
        if parent and parent["author_id"] == user_id:
            return True
    tags = conn.execute(
        "SELECT * FROM wd_comment_tags WHERE comment_id=?", (comment["id"],)
    ).fetchall()
    for t in tags:
        if t["tag_type"] == "user" and t["tagged_user_id"] == user_id:
            return True
        if t["tag_type"] == "location" and t["tagged_location_id"] in loc_ids:
            return True
    return False


def list_threads(doc_id: int, *, user_id: int, role: str) -> list[dict]:
    """Return top-level comments as thread summaries (inbox style)."""
    with get_db() as conn:
        doc = conn.execute("SELECT created_by FROM wd_documents WHERE id=?", (doc_id,)).fetchone()
        if not doc:
            raise NotFoundError("Document not found.")
        doc_owner = doc["created_by"]
        loc_ids_rows = conn.execute(
            "SELECT cost_center_id FROM wd_user_locations WHERE user_id=?", (user_id,)
        ).fetchall()
        loc_ids = [r["cost_center_id"] for r in loc_ids_rows]

        top_level = conn.execute(
            """SELECT c.*, u.full_name as author_name,
                      COALESCE(p.display_name, u.full_name) as display_name
               FROM wd_comments c
               JOIN users u ON u.id = c.author_id
               LEFT JOIN wd_user_profiles p ON p.user_id = c.author_id
               WHERE c.document_id=? AND c.parent_id IS NULL
               ORDER BY c.created_at DESC""",
            (doc_id,)
        ).fetchall()

        threads = []
        for c in top_level:
            c = dict(c)
            if not _can_see_comment(conn, c, user_id, role, doc_owner, loc_ids):
                continue
            replies = conn.execute(
                """SELECT c2.*, u.full_name as author_name,
                          COALESCE(p.display_name, u.full_name) as display_name
                   FROM wd_comments c2
                   JOIN users u ON u.id = c2.author_id
                   LEFT JOIN wd_user_profiles p ON p.user_id = c2.author_id
                   WHERE c2.parent_id=? ORDER BY c2.created_at""",
                (c["id"],)
            ).fetchall()
            visible_replies = [
                dict(r) for r in replies
                if _can_see_comment(conn, dict(r), user_id, role, doc_owner, loc_ids)
            ]
            tags = conn.execute(
                """SELECT ct.*, u.full_name as user_name, cc.name as loc_name
                   FROM wd_comment_tags ct
                   LEFT JOIN users u ON u.id = ct.tagged_user_id
                   LEFT JOIN cost_centers cc ON cc.id = ct.tagged_location_id
                   WHERE ct.comment_id=?""", (c["id"],)
            ).fetchall()
            c["tags"] = [dict(t) for t in tags]
            c["replies"] = visible_replies
            c["reply_count"] = len(visible_replies)
            threads.append(c)
        return threads


def get_thread(thread_id: int, *, user_id: int, role: str) -> dict:
    with get_db() as conn:
        c = conn.execute("SELECT * FROM wd_comments WHERE id=?", (thread_id,)).fetchone()
        if not c:
            raise NotFoundError("Thread not found.")
        return list_threads(c["document_id"], user_id=user_id, role=role)


def post_comment(doc_id: int, content: str, *, author_id: int) -> int:
    if not content.strip():
        raise ValidationError("Comment cannot be empty.")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO wd_comments (document_id, content, author_id) VALUES (?,?,?)",
            (doc_id, content.strip(), author_id)
        )
        cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        _save_comment_tags(conn, cid, content)
        return cid


def post_reply(parent_id: int, content: str, *, author_id: int) -> int:
    if not content.strip():
        raise ValidationError("Reply cannot be empty.")
    with get_db() as conn:
        parent = conn.execute(
            "SELECT document_id FROM wd_comments WHERE id=?", (parent_id,)
        ).fetchone()
        if not parent:
            raise NotFoundError("Parent comment not found.")
        conn.execute(
            "INSERT INTO wd_comments (document_id, parent_id, content, author_id) VALUES (?,?,?,?)",
            (parent["document_id"], parent_id, content.strip(), author_id)
        )
        cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        _save_comment_tags(conn, cid, content)
        return cid


def edit_comment(comment_id: int, content: str, *, user_id: int, role: str):
    if not content.strip():
        raise ValidationError("Comment cannot be empty.")
    with get_db() as conn:
        c = conn.execute("SELECT * FROM wd_comments WHERE id=?", (comment_id,)).fetchone()
        if not c:
            raise NotFoundError("Comment not found.")
        if c["author_id"] != user_id and role != "manager":
            raise ForbiddenError("You can only edit your own comments.")
        conn.execute(
            "UPDATE wd_comments SET content=?, edited_at=datetime('now') WHERE id=?",
            (content.strip(), comment_id)
        )
        _save_comment_tags(conn, comment_id, content)


def unread_count(doc_id: int, *, user_id: int, role: str) -> int:
    threads = list_threads(doc_id, user_id=user_id, role=role)
    return len(threads)
