"""Word Editor documents service.

Business rules:
- Draft: no doc_no, not printable, editable by creator or manage_wd_users.
- Self-approved: doc_no assigned (WD-YYYY-NNNNN), printable as Internal Document.
- Official: manager elevates self_approved -> official, printed as Official Document.
- Deleted: soft-delete, never hard-deleted.
- Visibility: creator always sees own doc. Tagged users or location members also see it.
  Manager sees all.
- Template slots: max 5 personal per user.
- Universal templates: max 2, manager-created, assigned per user.
- @ tags extracted from content_html on save.
"""
import hashlib
import json
import re
from datetime import datetime

from app.database import get_db
from app.services.errors import NotFoundError, ForbiddenError, ValidationError
from app.config import WD_MAX_PERSONAL_TEMPLATES, WD_MAX_UNIVERSAL_TEMPLATES
from app.programs.word_editor.services.sanitize import sanitize_html, strip_tags

__all__ = [
    "list_documents", "get_document", "create_document", "update_document",
    "self_approve", "make_official", "delete_document", "discard_draft",
    "list_templates", "save_to_template_slot", "delete_template_slot",
    "list_universal_templates", "create_universal_template",
    "update_universal_template", "delete_universal_template",
    "assign_universal_template", "unassign_universal_template",
    "list_subjects", "save_subject", "delete_subject",
    "list_universal_subjects", "create_universal_subject",
    "assign_universal_subject",
    "get_mention_list",
    "next_doc_no",
    "list_versions", "get_version", "restore_version",
    "content_sha", "verify_by_doc_no",
]

_LOC_NAMES = {"Factory Floor", "Transport", "Admin Office", "Warehouse"}


def _utc_now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def content_sha(content_html: str) -> str:
    """Stable fingerprint of a document body - what an official snapshot is
    checked against, and what the public verification page displays."""
    return hashlib.sha256((content_html or "").encode("utf-8")).hexdigest()


def _audit(conn, user_id: int, action: str, details: str):
    """Word Editor document actions belong in the shared audit log, same as
    every other financial-record action in this app."""
    conn.execute(
        "INSERT INTO audit_log (user_id, action, details) VALUES (?,?,?)",
        (user_id, action, details),
    )


def _save_version(conn, doc_id: int, *, title: str, subject: str,
                  content_html: str, edited_by: int,
                  is_snapshot: bool = False, note: str = "") -> int:
    """Append an immutable version row. Version rows are never updated or
    deleted - restoring an old version appends a new one instead."""
    row = conn.execute(
        "SELECT COALESCE(MAX(version_no), 0) AS v FROM wd_document_versions WHERE document_id = ?",
        (doc_id,),
    ).fetchone()
    version_no = row["v"] + 1
    sha = content_sha(content_html)
    conn.execute(
        """INSERT INTO wd_document_versions
           (document_id, version_no, title, subject, content_json,
            content_sha, is_snapshot, note, edited_by)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (doc_id, version_no, title, subject, content_html, sha,
         1 if is_snapshot else 0, note, edited_by),
    )
    conn.execute("UPDATE wd_documents SET version_no = ? WHERE id = ?", (version_no, doc_id))
    return version_no


def next_doc_no(conn) -> str:
    year = datetime.utcnow().strftime("%Y")
    conn.execute(
        "INSERT INTO document_counters (doc_type, year, seq) VALUES ('WD', ?, 1)"
        " ON CONFLICT(doc_type, year) DO UPDATE SET seq = seq + 1",
        (year,)
    )
    row = conn.execute(
        "SELECT seq FROM document_counters WHERE doc_type='WD' AND year=?", (year,)
    ).fetchone()
    return f"WD-{year}-{row['seq']:05d}"


def _require_doc(conn, doc_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM wd_documents WHERE id = ? AND status != 'deleted'", (doc_id,)
    ).fetchone()
    if not row:
        raise NotFoundError("Document not found.")
    return dict(row)


def _require_editable(doc: dict, user_id: int, role: str):
    if doc["status"] not in ("draft",):
        raise ForbiddenError("Only draft documents can be edited.")
    if doc["created_by"] != user_id and role != "manager":
        raise ForbiddenError("You can only edit your own drafts.")


def _extract_tags(content_html: str) -> list[dict]:
    """Pull @mentions from rendered HTML. Returns list of {tag_type, name}."""
    tags = []
    for m in re.finditer(r'data-tag-type="(user|location)"\s+data-tag-id="(\d+)"', content_html):
        tags.append({"tag_type": m.group(1), "id": int(m.group(2))})
    return tags


def _save_tags(conn, doc_id: int, content_html: str, tagged_by: int):
    conn.execute("DELETE FROM wd_document_tags WHERE document_id = ?", (doc_id,))
    for tag in _extract_tags(content_html):
        if tag["tag_type"] == "user":
            conn.execute(
                "INSERT OR IGNORE INTO wd_document_tags (document_id, tag_type, tagged_user_id, tagged_by) VALUES (?,?,?,?)",
                (doc_id, "user", tag["id"], tagged_by)
            )
        else:
            conn.execute(
                "INSERT OR IGNORE INTO wd_document_tags (document_id, tag_type, tagged_location_id, tagged_by) VALUES (?,?,?,?)",
                (doc_id, "location", tag["id"], tagged_by)
            )


def _visibility_clause(user_id: int, role: str, location_ids: list[int]) -> tuple[str, list]:
    """Return (WHERE clause fragment, params) for document visibility."""
    if role == "manager":
        return "1=1", []
    loc_placeholders = ",".join("?" * len(location_ids)) if location_ids else "NULL"
    params = [user_id, user_id]
    clause = f"""(
        d.created_by = ?
        OR EXISTS (
            SELECT 1 FROM wd_document_tags t
            WHERE t.document_id = d.id AND (
                (t.tag_type = 'user' AND t.tagged_user_id = ?)
                {f"OR (t.tag_type = 'location' AND t.tagged_location_id IN ({loc_placeholders}))" if location_ids else ""}
            )
        )
    )"""
    if location_ids:
        params.extend(location_ids)
    return clause, params


def _get_user_location_ids(conn, user_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT cost_center_id FROM wd_user_locations WHERE user_id = ?", (user_id,)
    ).fetchall()
    return [r["cost_center_id"] for r in rows]


# ── LIST / GET ─────────────────────────────────────────────────────────────

def list_documents(*, user_id: int, role: str,
                   status_filter: str = "", subject_filter: str = "",
                   date_from: str = "", date_to: str = "",
                   text_query: str = "") -> list[dict]:
    with get_db() as conn:
        loc_ids = _get_user_location_ids(conn, user_id)
        vis_clause, params = _visibility_clause(user_id, role, loc_ids)
        filters = [f"d.status != 'deleted'", vis_clause]
        if status_filter:
            filters.append("d.status = ?")
            params.append(status_filter)
        if subject_filter:
            filters.append("d.subject LIKE ?")
            params.append(f"%{subject_filter}%")
        if text_query and text_query.strip():
            filters.append(
                "(d.search_text LIKE ? OR d.subject LIKE ? OR d.doc_no LIKE ? "
                "OR d.receiver_name LIKE ? OR d.title LIKE ?)")
            like = f"%{text_query.strip()}%"
            params.extend([like] * 5)
        if date_from:
            filters.append("d.created_at >= ?")
            params.append(date_from)
        if date_to:
            filters.append("d.created_at <= ?")
            params.append(date_to + " 23:59:59")
        where = " AND ".join(filters)
        rows = conn.execute(
            f"""SELECT d.*, u.full_name as creator_name,
                       (SELECT COUNT(*) FROM wd_comments c WHERE c.document_id = d.id) as comment_count,
                       (SELECT 1 FROM wd_document_tags t WHERE t.document_id=d.id AND t.tag_type='user' AND t.tagged_user_id=?) as you_tagged
                FROM wd_documents d JOIN users u ON u.id = d.created_by
                WHERE {where}
                ORDER BY d.last_edited_at DESC""",
            [user_id] + params
        ).fetchall()
        return [dict(r) for r in rows]


def get_document(doc_id: int, *, user_id: int, role: str) -> dict:
    with get_db() as conn:
        loc_ids = _get_user_location_ids(conn, user_id)
        vis_clause, params = _visibility_clause(user_id, role, loc_ids)
        row = conn.execute(
            f"""SELECT d.*, u.full_name as creator_name,
                       sa.full_name as self_approver_name,
                       ma.full_name as manager_approver_name
                FROM wd_documents d
                JOIN users u ON u.id = d.created_by
                LEFT JOIN users sa ON sa.id = d.self_approved_by
                LEFT JOIN users ma ON ma.id = d.manager_approved_by
                WHERE d.id = ? AND d.status != 'deleted' AND {vis_clause}""",
            [doc_id] + params
        ).fetchone()
        if not row:
            raise NotFoundError("Document not found or you don't have access.")
        doc = dict(row)
        tags = conn.execute(
            """SELECT t.*, u.full_name as user_name, cc.name as loc_name
               FROM wd_document_tags t
               LEFT JOIN users u ON u.id = t.tagged_user_id
               LEFT JOIN cost_centers cc ON cc.id = t.tagged_location_id
               WHERE t.document_id = ?""", (doc_id,)
        ).fetchall()
        doc["tags"] = [dict(t) for t in tags]
        return doc


# ── CREATE / UPDATE ────────────────────────────────────────────────────────

def create_document(*, created_by: int, title: str = "Untitled",
                    subject: str = "", receiver_name: str = "",
                    receiver_phone: str = "", content_html: str = "",
                    is_draft: bool = True) -> int:
    content_html = sanitize_html(content_html)
    with get_db() as conn:
        conn.execute(
            """INSERT INTO wd_documents
               (title, subject, receiver_name, receiver_phone,
                content_json, search_text, status, created_by, last_edited_at)
               VALUES (?,?,?,?,?,?,?,?,datetime('now'))""",
            (title.strip() or "Untitled", subject.strip(),
             receiver_name.strip(), receiver_phone.strip(),
             content_html, strip_tags(content_html), "draft", created_by)
        )
        doc_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        _save_tags(conn, doc_id, content_html, created_by)
        _save_version(conn, doc_id, title=title.strip() or "Untitled",
                      subject=subject.strip(), content_html=content_html,
                      edited_by=created_by, note="created")
        _audit(conn, created_by, "wd_document_created", f"Document #{doc_id} created")
        return doc_id


def update_document(doc_id: int, *, user_id: int, role: str,
                    title: str, subject: str, receiver_name: str,
                    receiver_phone: str, content_html: str,
                    save_subject: bool = False):
    content_html = sanitize_html(content_html)
    with get_db() as conn:
        doc = _require_doc(conn, doc_id)
        _require_editable(doc, user_id, role)
        conn.execute(
            """UPDATE wd_documents SET title=?, subject=?, receiver_name=?,
               receiver_phone=?, content_json=?, search_text=?,
               last_edited_at=datetime('now')
               WHERE id=?""",
            (title.strip() or "Untitled", subject.strip(),
             receiver_name.strip(), receiver_phone.strip(),
             content_html, strip_tags(content_html), doc_id)
        )
        _save_tags(conn, doc_id, content_html, user_id)
        if content_sha(content_html) != content_sha(doc["content_json"]):
            _save_version(conn, doc_id, title=title.strip() or "Untitled",
                          subject=subject.strip(), content_html=content_html,
                          edited_by=user_id, note="edited")
        _audit(conn, user_id, "wd_document_edited", f"Document #{doc_id} edited")
        if save_subject and subject.strip():
            conn.execute(
                "INSERT OR IGNORE INTO wd_subject_templates (user_id, subject) VALUES (?,?)",
                (user_id, subject.strip())
            )


# ── APPROVAL ───────────────────────────────────────────────────────────────

def self_approve(doc_id: int, *, user_id: int, role: str) -> str:
    with get_db() as conn:
        doc = _require_doc(conn, doc_id)
        if doc["status"] != "draft":
            raise ForbiddenError("Only drafts can be self-approved.")
        if doc["created_by"] != user_id and role != "manager":
            raise ForbiddenError("You can only approve your own documents.")
        if not doc["subject"].strip():
            raise ValidationError("Subject is required before approving.")
        doc_no = next_doc_no(conn)
        conn.execute(
            """UPDATE wd_documents SET status='self_approved', doc_no=?,
               self_approved_by=?, self_approved_at=datetime('now'),
               last_edited_at=datetime('now') WHERE id=?""",
            (doc_no, user_id, doc_id)
        )
        _audit(conn, user_id, "wd_document_self_approved", f"{doc_no} self-approved")
        return doc_no


def make_official(doc_id: int, *, manager_id: int, note: str = ""):
    with get_db() as conn:
        doc = _require_doc(conn, doc_id)
        if doc["status"] != "self_approved":
            raise ForbiddenError("Only self-approved documents can be made official.")
        conn.execute(
            """UPDATE wd_documents SET status='official',
               manager_approved_by=?, manager_approved_at=datetime('now'),
               manager_note=?, last_edited_at=datetime('now') WHERE id=?""",
            (manager_id, note.strip(), doc_id)
        )
        sha = content_sha(doc["content_json"])
        conn.execute("UPDATE wd_documents SET official_sha=? WHERE id=?", (sha, doc_id))
        _save_version(conn, doc_id, title=doc["title"], subject=doc["subject"],
                      content_html=doc["content_json"], edited_by=manager_id,
                      is_snapshot=True, note="official snapshot")
        _audit(conn, manager_id, "wd_document_official",
               f"{doc['doc_no']} made official (sha {sha[:12]})")


def delete_document(doc_id: int, *, user_id: int, role: str, reason: str):
    if not reason.strip():
        raise ValidationError("A reason is required to delete a document.")
    with get_db() as conn:
        doc = _require_doc(conn, doc_id)
        if doc["created_by"] != user_id and role != "manager":
            raise ForbiddenError("You can only delete your own documents.")
        conn.execute(
            """UPDATE wd_documents SET status='deleted', deleted_by=?,
               deleted_at=datetime('now'), delete_reason=? WHERE id=?""",
            (user_id, reason.strip(), doc_id)
        )
        _audit(conn, user_id, "wd_document_deleted",
               f"Document #{doc_id} ({doc['doc_no'] or 'no number'}) deleted: {reason.strip()}")


def discard_draft(doc_id: int, *, user_id: int, role: str):
    """Hard-delete a draft (was never a committed record)."""
    with get_db() as conn:
        doc = _require_doc(conn, doc_id)
        if doc["status"] != "draft":
            raise ForbiddenError("Only drafts can be discarded.")
        if doc["created_by"] != user_id and role != "manager":
            raise ForbiddenError("You can only discard your own drafts.")
        conn.execute("DELETE FROM wd_document_tags WHERE document_id = ?", (doc_id,))
        conn.execute("DELETE FROM wd_document_versions WHERE document_id = ?", (doc_id,))
        conn.execute("DELETE FROM wd_documents WHERE id = ?", (doc_id,))
        _audit(conn, user_id, "wd_draft_discarded", f"Draft #{doc_id} discarded")


# ── VERSIONS / SNAPSHOTS / VERIFICATION ────────────────────────────────────

def list_versions(doc_id: int, *, user_id: int, role: str) -> list[dict]:
    """Version history for a document the caller is allowed to see."""
    get_document(doc_id, user_id=user_id, role=role)      # visibility check
    with get_db() as conn:
        rows = conn.execute(
            """SELECT v.id, v.version_no, v.title, v.subject, v.content_sha,
                      v.is_snapshot, v.note, v.created_at,
                      u.full_name AS edited_by_name
               FROM wd_document_versions v JOIN users u ON u.id = v.edited_by
               WHERE v.document_id = ? ORDER BY v.version_no DESC""",
            (doc_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_version(doc_id: int, version_no: int, *, user_id: int, role: str) -> dict:
    get_document(doc_id, user_id=user_id, role=role)
    with get_db() as conn:
        row = conn.execute(
            """SELECT v.*, u.full_name AS edited_by_name
               FROM wd_document_versions v JOIN users u ON u.id = v.edited_by
               WHERE v.document_id = ? AND v.version_no = ?""",
            (doc_id, version_no),
        ).fetchone()
        if row is None:
            raise NotFoundError("Version not found.")
        return dict(row)


def restore_version(doc_id: int, version_no: int, *, user_id: int, role: str):
    """Restore an old body onto a draft. Never rewrites history - the restored
    content is appended as a new version, so the trail stays complete."""
    version = get_version(doc_id, version_no, user_id=user_id, role=role)
    with get_db() as conn:
        doc = _require_doc(conn, doc_id)
        _require_editable(doc, user_id, role)
        body = version["content_json"]
        conn.execute(
            """UPDATE wd_documents SET content_json=?, search_text=?,
               last_edited_at=datetime('now') WHERE id=?""",
            (body, strip_tags(body), doc_id),
        )
        _save_tags(conn, doc_id, body, user_id)
        _save_version(conn, doc_id, title=doc["title"], subject=doc["subject"],
                      content_html=body, edited_by=user_id,
                      note=f"restored from v{version_no}")
        _audit(conn, user_id, "wd_document_restored",
               f"Document #{doc_id} restored to v{version_no}")


def verify_by_doc_no(doc_no: str) -> dict:
    """Public verification lookup - metadata only, never the document body.

    Returns the facts a person holding a printed page needs to confirm it is
    genuine: that the number exists, its status, who approved it, when, and
    the snapshot fingerprint.
    """
    doc_no = (doc_no or "").strip().upper()
    if not re.fullmatch(r"WD-\d{4}-\d{5}", doc_no):
        raise ValidationError("Not a valid document number.")
    with get_db() as conn:
        row = conn.execute(
            """SELECT d.doc_no, d.subject, d.receiver_name, d.status,
                      d.official_sha, d.self_approved_at, d.manager_approved_at,
                      d.created_at, u.full_name AS creator_name,
                      m.full_name AS manager_name
               FROM wd_documents d
               JOIN users u ON u.id = d.created_by
               LEFT JOIN users m ON m.id = d.manager_approved_by
               WHERE d.doc_no = ?""",
            (doc_no,),
        ).fetchone()
        if row is None:
            raise NotFoundError("No document with that number.")
        return dict(row)


# ── PERSONAL TEMPLATES ─────────────────────────────────────────────────────

def list_templates(user_id: int) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """SELECT t.slot, t.label, d.id as doc_id, d.subject, d.content_json
               FROM wd_user_templates t JOIN wd_documents d ON d.id = t.document_id
               WHERE t.user_id = ? ORDER BY t.slot""",
            (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def save_to_template_slot(doc_id: int, slot: int, label: str, *, user_id: int, role: str):
    if slot < 1 or slot > WD_MAX_PERSONAL_TEMPLATES:
        raise ValidationError(f"Slot must be 1–{WD_MAX_PERSONAL_TEMPLATES}.")
    with get_db() as conn:
        doc = _require_doc(conn, doc_id)
        if doc["created_by"] != user_id and role != "manager":
            raise ForbiddenError("You can only template your own documents.")
        conn.execute(
            """INSERT INTO wd_user_templates (user_id, document_id, slot, label)
               VALUES (?,?,?,?)
               ON CONFLICT(user_id, slot) DO UPDATE SET document_id=excluded.document_id, label=excluded.label""",
            (user_id, doc_id, slot, label.strip() or doc.get("subject", "Template"))
        )


def delete_template_slot(slot: int, *, user_id: int):
    with get_db() as conn:
        conn.execute(
            "DELETE FROM wd_user_templates WHERE user_id = ? AND slot = ?", (user_id, slot)
        )


# ── UNIVERSAL TEMPLATES ────────────────────────────────────────────────────

def list_universal_templates(*, user_id: int = None, manager: bool = False) -> list[dict]:
    with get_db() as conn:
        if manager:
            rows = conn.execute(
                """SELECT ut.*, u.full_name as creator_name,
                          (SELECT COUNT(*) FROM wd_universal_template_assignments a WHERE a.template_id=ut.id) as assigned_count
                   FROM wd_universal_templates ut JOIN users u ON u.id = ut.created_by
                   ORDER BY ut.created_at"""
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT ut.* FROM wd_universal_templates ut
                   JOIN wd_universal_template_assignments a ON a.template_id = ut.id
                   WHERE a.user_id = ? ORDER BY ut.created_at""",
                (user_id,)
            ).fetchall()
        return [dict(r) for r in rows]


def create_universal_template(*, title: str, content_html: str,
                               subject: str, created_by: int):
    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM wd_universal_templates").fetchone()[0]
        if count >= WD_MAX_UNIVERSAL_TEMPLATES:
            raise ValidationError(f"Maximum {WD_MAX_UNIVERSAL_TEMPLATES} universal templates allowed.")
        if not title.strip():
            raise ValidationError("Title is required.")
        conn.execute(
            "INSERT INTO wd_universal_templates (title, content_json, subject, created_by) VALUES (?,?,?,?)",
            (title.strip(), content_html, subject.strip(), created_by)
        )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def update_universal_template(template_id: int, *, title: str,
                               content_html: str, subject: str):
    with get_db() as conn:
        conn.execute(
            "UPDATE wd_universal_templates SET title=?, content_json=?, subject=?, updated_at=datetime('now') WHERE id=?",
            (title.strip(), content_html, subject.strip(), template_id)
        )


def delete_universal_template(template_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM wd_universal_template_assignments WHERE template_id=?", (template_id,))
        conn.execute("DELETE FROM wd_universal_templates WHERE id=?", (template_id,))


def assign_universal_template(template_id: int, user_ids: list[int], assigned_by: int):
    with get_db() as conn:
        conn.execute("DELETE FROM wd_universal_template_assignments WHERE template_id=?", (template_id,))
        for uid in user_ids:
            conn.execute(
                "INSERT OR IGNORE INTO wd_universal_template_assignments (template_id, user_id, assigned_by) VALUES (?,?,?)",
                (template_id, uid, assigned_by)
            )


def unassign_universal_template(template_id: int, user_id: int):
    with get_db() as conn:
        conn.execute(
            "DELETE FROM wd_universal_template_assignments WHERE template_id=? AND user_id=?",
            (template_id, user_id)
        )


# ── SUBJECTS ───────────────────────────────────────────────────────────────

def list_subjects(user_id: int) -> list[dict]:
    with get_db() as conn:
        personal = conn.execute(
            "SELECT id, subject, 'personal' as kind FROM wd_subject_templates WHERE user_id=? ORDER BY subject",
            (user_id,)
        ).fetchall()
        universal = conn.execute(
            """SELECT s.id, s.subject, 'universal' as kind
               FROM wd_universal_subjects s
               JOIN wd_universal_subject_assignments a ON a.subject_id=s.id
               WHERE a.user_id=? ORDER BY s.subject""",
            (user_id,)
        ).fetchall()
        return [dict(r) for r in personal] + [dict(r) for r in universal]


def save_subject(user_id: int, subject: str):
    if not subject.strip():
        raise ValidationError("Subject cannot be empty.")
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO wd_subject_templates (user_id, subject) VALUES (?,?)",
            (user_id, subject.strip())
        )


def delete_subject(subject_id: int, *, user_id: int):
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM wd_subject_templates WHERE id=? AND user_id=?", (subject_id, user_id)
        ).fetchone()
        if not row:
            raise NotFoundError("Subject template not found.")
        conn.execute("DELETE FROM wd_subject_templates WHERE id=?", (subject_id,))


def list_universal_subjects(*, manager: bool = False, user_id: int = None) -> list[dict]:
    with get_db() as conn:
        if manager:
            rows = conn.execute(
                "SELECT s.*, u.full_name as creator_name FROM wd_universal_subjects s JOIN users u ON u.id=s.created_by ORDER BY s.subject"
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT s.* FROM wd_universal_subjects s
                   JOIN wd_universal_subject_assignments a ON a.subject_id=s.id
                   WHERE a.user_id=? ORDER BY s.subject""",
                (user_id,)
            ).fetchall()
        return [dict(r) for r in rows]


def create_universal_subject(subject: str, created_by: int) -> int:
    if not subject.strip():
        raise ValidationError("Subject cannot be empty.")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO wd_universal_subjects (subject, created_by) VALUES (?,?)",
            (subject.strip(), created_by)
        )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def assign_universal_subject(subject_id: int, user_ids: list[int]):
    with get_db() as conn:
        conn.execute("DELETE FROM wd_universal_subject_assignments WHERE subject_id=?", (subject_id,))
        for uid in user_ids:
            conn.execute(
                "INSERT OR IGNORE INTO wd_universal_subject_assignments (subject_id, user_id) VALUES (?,?)",
                (subject_id, uid)
            )


# ── @ MENTION LIST ─────────────────────────────────────────────────────────

def get_mention_list() -> dict:
    """Returns {users: [...], locations: [...]} for the @ picker."""
    with get_db() as conn:
        users = conn.execute(
            """SELECT u.id, COALESCE(p.display_name, u.full_name) as name, u.role,
                      GROUP_CONCAT(cc.name, ', ') as locations
               FROM users u
               LEFT JOIN wd_user_profiles p ON p.user_id = u.id
               LEFT JOIN wd_user_locations wl ON wl.user_id = u.id
               LEFT JOIN cost_centers cc ON cc.id = wl.cost_center_id
               WHERE u.active = 1
               GROUP BY u.id ORDER BY name""",
        ).fetchall()
        locs = conn.execute(
            """SELECT cc.id, cc.name,
                      COUNT(wl.user_id) as user_count
               FROM cost_centers cc
               LEFT JOIN wd_user_locations wl ON wl.cost_center_id = cc.id
               WHERE cc.active = 1
               GROUP BY cc.id ORDER BY cc.name"""
        ).fetchall()
        return {
            "users": [dict(u) for u in users],
            "locations": [dict(l) for l in locs],
        }
