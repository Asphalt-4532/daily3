"""Asset registry: register, edit, soft-delete, and look up assets.

The money rule this whole module is built around: an asset's cost history is
always READ from approved expense_lines (via expense_lines.asset_id) and is
never copied into any table here. That is why nothing in this package writes
an amount anywhere - a second place to record money is a second grand total
that drifts from the first.

Tag formats are data, not code: `asset_tag_rules` holds one editable regex
per asset category, and a category with no row falls back to
app/config.py's DEFAULT_ASSET_TAG_RULES - the same fallback shape
role_has_permission() uses against DEFAULT_ROLE_PERMISSIONS, so a fresh
install and a never-customised one behave identically.

No FastAPI imports - see app/services/errors.py.
"""
import re

from app.config import ASSET_CATEGORIES, DEFAULT_ASSET_TAG_RULES
from app.database import get_db, log_action
from app.services.errors import (
    ValidationError, NotFoundError, DuplicateError, ConflictError,
)

__all__ = [
    "ASSET_STATUSES",
    "create_asset",
    "update_asset",
    "delete_asset",
    "get_asset",
    "list_assets",
    "pickable_assets",
    "set_status",
    "get_tag_rules",
    "set_tag_rule",
]

ASSET_STATUSES = ("active", "in_maintenance", "retired", "divested", "deleted")

# Statuses an asset can still be attached to new cost on. Kept here as the
# single definition so the picker, the voucher-side validation and the
# reports can't drift apart about what "still in service" means.
_PICKABLE_STATUSES = ("active", "in_maintenance")


# --------------------------------------------------------------------------
# tag rules
# --------------------------------------------------------------------------
def get_tag_rules():
    """Every asset category's tag rule as {category: {"pattern", "example"}}.

    A category with no stored row falls back to DEFAULT_ASSET_TAG_RULES
    rather than to "no rule", so a database seeded before a new category
    existed still enforces something sensible.
    """
    with get_db() as conn:
        stored = {
            r["category"]: {"pattern": r["pattern"], "example": r["example"]}
            for r in conn.execute("SELECT * FROM asset_tag_rules").fetchall()
        }
    rules = {}
    for category in ASSET_CATEGORIES:
        if category in stored:
            rules[category] = stored[category]
        else:
            pattern, example = DEFAULT_ASSET_TAG_RULES.get(category, ("", ""))
            rules[category] = {"pattern": pattern, "example": example}
    return rules


def set_tag_rule(category, pattern, example, *, updated_by):
    """Store a tag pattern for one category. An empty pattern means free
    text with no check at all - that is a legitimate choice, not a
    misconfiguration, and is how odd legacy tags get entered.

    The regex is compiled here before it is saved. A pattern that doesn't
    compile would otherwise blow up later inside create_asset() for every
    asset of that category - i.e. a typo in Settings would lock the registry
    rather than just being wrong.
    """
    if category not in ASSET_CATEGORIES:
        raise ValidationError(f"Unknown asset category: {category}")
    pattern = (pattern or "").strip()
    if pattern:
        try:
            re.compile(pattern)
        except re.error as e:
            raise ValidationError(f"That tag pattern isn't a valid expression: {e}")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO asset_tag_rules (category, pattern, example, updated_by, updated_at) "
            "VALUES (?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(category) DO UPDATE SET pattern=excluded.pattern, "
            "example=excluded.example, updated_by=excluded.updated_by, "
            "updated_at=datetime('now')",
            (category, pattern, (example or "").strip(), updated_by),
        )
        log_action(conn, updated_by, "asset_tag_rule_updated",
                   f"{category}: pattern={pattern or '(free text)'}")
    return get_tag_rules()[category]


def _check_tag(conn, asset_tag, category):
    rule = conn.execute(
        "SELECT pattern, example FROM asset_tag_rules WHERE category=?", (category,)
    ).fetchone()
    if rule is not None:
        pattern, example = rule["pattern"], rule["example"]
    else:
        pattern, example = DEFAULT_ASSET_TAG_RULES.get(category, ("", ""))
    if not pattern:
        return  # free text by choice
    try:
        matches = re.match(pattern, asset_tag) is not None
    except re.error:
        # A bad pattern that somehow got stored anyway must not make the
        # registry unusable - fall open, the same way a missing permission
        # row falls back to its coded default rather than denying everyone.
        return
    if not matches:
        hint = f" (e.g. {example})" if example else ""
        raise ValidationError(
            f"'{asset_tag}' doesn't match the tag format set for {category}{hint}."
        )


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------
def create_asset(*, asset_tag, name, category, serial_number="", purchase_date=None,
                 purchase_cost=0.0, cost_center_id=None, notes="", created_by):
    asset_tag = (asset_tag or "").strip()
    name = (name or "").strip()
    if not asset_tag:
        raise ValidationError("An asset tag is required.")
    if not name:
        raise ValidationError("An asset name is required.")
    if category not in ASSET_CATEGORIES:
        raise ValidationError("Please choose a valid asset category.")
    try:
        purchase_cost = float(purchase_cost or 0)
    except (TypeError, ValueError):
        raise ValidationError("Purchase cost must be a number.")
    if purchase_cost < 0:
        raise ValidationError("Purchase cost can't be negative.")

    with get_db() as conn:
        _check_tag(conn, asset_tag, category)
        clash = conn.execute(
            "SELECT id, status FROM assets WHERE asset_tag = ?", (asset_tag,)
        ).fetchone()
        if clash:
            # Deliberately still a clash even when the existing asset is
            # soft-deleted: the tag is how a person identifies the physical
            # thing, and silently reusing it would merge two machines'
            # histories under one label.
            raise DuplicateError(f"Asset tag '{asset_tag}' is already in use.")
        if cost_center_id:
            cc = conn.execute(
                "SELECT id FROM cost_centers WHERE id=? AND active=1", (cost_center_id,)
            ).fetchone()
            if not cc:
                raise ValidationError("Please choose a valid cost centre.")
        cur = conn.execute(
            "INSERT INTO assets (asset_tag, name, category, serial_number, purchase_date, "
            "purchase_cost, cost_center_id, notes, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (asset_tag, name, category, (serial_number or "").strip(),
             purchase_date or None, round(purchase_cost, 2),
             cost_center_id or None, (notes or "").strip(), created_by),
        )
        asset_id = cur.lastrowid
        log_action(conn, created_by, "asset_created",
                   f"{asset_tag} ({category}) - {name}, purchase cost {purchase_cost:.2f}")
    return asset_id


def update_asset(asset_id, *, updated_by, **fields):
    """Edit an asset's own details. Status is deliberately NOT settable here -
    it moves through set_status() or the custody service, so every change of
    state carries its own audit entry and reason rather than being a silent
    field edit buried in a form post."""
    allowed = {"asset_tag", "name", "category", "serial_number", "purchase_date",
               "purchase_cost", "cost_center_id", "notes"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValidationError(f"Can't update: {', '.join(sorted(unknown))}")
    if not fields:
        return

    with get_db() as conn:
        row = conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
        if not row or row["status"] == "deleted":
            raise NotFoundError("That asset doesn't exist.")

        new_tag = (fields.get("asset_tag") or row["asset_tag"]).strip()
        new_category = fields.get("category", row["category"])
        if new_category not in ASSET_CATEGORIES:
            raise ValidationError("Please choose a valid asset category.")
        if new_tag != row["asset_tag"]:
            clash = conn.execute(
                "SELECT id FROM assets WHERE asset_tag=? AND id<>?", (new_tag, asset_id)
            ).fetchone()
            if clash:
                raise DuplicateError(f"Asset tag '{new_tag}' is already in use.")
        # Re-checked on every edit, not just at creation: changing an asset's
        # category changes which pattern applies to its existing tag.
        _check_tag(conn, new_tag, new_category)

        if "purchase_cost" in fields:
            try:
                fields["purchase_cost"] = round(float(fields["purchase_cost"] or 0), 2)
            except (TypeError, ValueError):
                raise ValidationError("Purchase cost must be a number.")
            if fields["purchase_cost"] < 0:
                raise ValidationError("Purchase cost can't be negative.")

        fields["asset_tag"] = new_tag
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE assets SET {sets} WHERE id=?",
                     (*fields.values(), asset_id))
        changed = ", ".join(f"{k}={v}" for k, v in sorted(fields.items()))
        log_action(conn, updated_by, "asset_updated", f"{new_tag}: {changed}")


def delete_asset(asset_id, *, deleted_by, reason):
    """Soft-delete only, and a reason is required - an asset with cost
    history is a financial record like any voucher. Refused while the asset
    is still assigned to someone: returning it first is what leaves an
    honest custody trail rather than an asset that simply vanished from
    someone's hands."""
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("A reason is required to delete an asset.")
    with get_db() as conn:
        row = conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
        if not row or row["status"] == "deleted":
            raise NotFoundError("That asset doesn't exist.")
        held = conn.execute(
            "SELECT COUNT(*) c FROM asset_assignments "
            "WHERE asset_id=? AND status='active'", (asset_id,)
        ).fetchone()["c"]
        if held:
            raise ConflictError(
                "This asset is still assigned. Close its assignment(s) before deleting it."
            )
        conn.execute(
            "UPDATE assets SET status='deleted', deleted_by=?, deleted_at=datetime('now'), "
            "delete_reason=? WHERE id=?",
            (deleted_by, reason, asset_id),
        )
        log_action(conn, deleted_by, "asset_deleted", f"{row['asset_tag']}: {reason}")


def set_status(asset_id, status, *, updated_by, note=""):
    """Manual status change. Deliberately not driven off maintenance
    records: a breakdown note is an observation, and inferring "this machine
    is now out of service" from it would flip real assets in and out of
    service behind a manager's back. Someone decides, and it's logged."""
    if status not in ("active", "in_maintenance", "retired"):
        raise ValidationError("That isn't a status you can set here.")
    with get_db() as conn:
        row = conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
        if not row or row["status"] == "deleted":
            raise NotFoundError("That asset doesn't exist.")
        if row["status"] == "divested":
            raise ConflictError("This asset has been divested and can't be put back in service.")
        conn.execute("UPDATE assets SET status=? WHERE id=?", (status, asset_id))
        log_action(conn, updated_by, "asset_status_changed",
                   f"{row['asset_tag']}: {row['status']} -> {status}"
                   + (f" ({note})" if note else ""))


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------
def get_asset(asset_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT a.*, cc.name AS cost_center_name FROM assets a "
            "LEFT JOIN cost_centers cc ON cc.id = a.cost_center_id "
            "WHERE a.id = ?", (asset_id,)
        ).fetchone()
        if not row or row["status"] == "deleted":
            raise NotFoundError("That asset doesn't exist.")
        return dict(row)


def list_assets(*, category=None, status=None, cost_center_id=None,
                holder_employee_id=None, q=None, include_deleted=False):
    sql = ("SELECT a.*, cc.name AS cost_center_name FROM assets a "
           "LEFT JOIN cost_centers cc ON cc.id = a.cost_center_id WHERE 1=1")
    params = []
    if not include_deleted:
        sql += " AND a.status <> 'deleted'"
    if category:
        sql += " AND a.category = ?"
        params.append(category)
    if status:
        sql += " AND a.status = ?"
        params.append(status)
    if cost_center_id:
        sql += " AND a.cost_center_id = ?"
        params.append(cost_center_id)
    if holder_employee_id:
        sql += (" AND EXISTS (SELECT 1 FROM asset_assignments asg WHERE asg.asset_id = a.id "
                "AND asg.status='active' AND asg.employee_id = ?)")
        params.append(holder_employee_id)
    if q:
        sql += " AND (a.asset_tag LIKE ? OR a.name LIKE ? OR a.serial_number LIKE ?)"
        like = f"%{q.strip()}%"
        params.extend([like, like, like])
    sql += " ORDER BY a.asset_tag"
    with get_db() as conn:
        return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]


def pickable_assets(*, asset_category=None, user_id=None, all_assets=False):
    """What the asset picker on a voucher line should offer.

    `all_assets=True` (a manager or accountant recording on the business's
    behalf) offers the whole in-service registry. Otherwise only assets
    currently assigned to this person, resolved through employees.user_id ->
    asset_assignments.

    An empty list here is meaningful, not an error: a driver holding no truck
    can still record costs, and the voucher-side validation reads exactly
    this emptiness to decide not to demand an asset (see
    _pickable_asset_count() in the expenses service, which asks the same
    question with the same rules).
    """
    placeholders = ",".join("?" for _ in _PICKABLE_STATUSES)
    sql = f"SELECT * FROM assets WHERE status IN ({placeholders})"
    params = list(_PICKABLE_STATUSES)
    if asset_category:
        sql += " AND category = ?"
        params.append(asset_category)
    if not all_assets:
        if user_id is None:
            return []
        sql += (" AND EXISTS (SELECT 1 FROM asset_assignments asg "
                "JOIN employees e ON e.id = asg.employee_id "
                "WHERE asg.asset_id = assets.id AND asg.status='active' AND e.user_id = ?)")
        params.append(user_id)
    sql += " ORDER BY asset_tag"
    with get_db() as conn:
        return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]
