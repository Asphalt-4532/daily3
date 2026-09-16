"""Non-cost maintenance notes against an asset: greasing, a nut fitted, an
inspection, a breakdown observed.

There is deliberately NO amount anywhere in this module, and no amount column
on `asset_service_log`. Anything that cost money goes on a voucher line
instead and reaches the asset through expense_lines.asset_id. Two places to
record money means two grand totals that drift; app/backup.py has already
been bitten by exactly that twice, so the field simply doesn't exist here
rather than existing and being discouraged.

Who may write one is a RECORD-level question, not just a role one: an active
assignee of that specific asset, or someone with manage_assets. A role check
alone can't express "that machine isn't yours", the same way it couldn't
express "that draft belongs to someone else" - so this raises ForbiddenError
from the service layer, exactly like the drafts feature does.

No FastAPI imports - see app/services/errors.py.
"""
import secrets
from datetime import datetime
from pathlib import Path

from app import auth, sandbox
from app.config import UPLOADS_DIR, MAX_UPLOAD_MB, ALLOWED_UPLOAD_EXTENSIONS
from app.database import get_db, log_action
from app.services.errors import (
    ValidationError, NotFoundError, ForbiddenError,
)

__all__ = [
    "SERVICE_TYPES",
    "METER_UNITS",
    "may_log_service",
    "save_service_photo",
    "log_service",
    "update_service",
    "delete_service",
    "list_services",
    "last_meter_reading",
    "suggested_meter_unit",
]

SERVICE_TYPES = (
    "Greasing/Lubrication", "Part fitted", "Part removed", "Inspection",
    "Cleaning", "Adjustment", "Breakdown note", "Other",
)
METER_UNITS = ("km", "hours")

# Which meter a category of asset is normally read in. A suggestion for the
# form only - the person can always override it, because a hired machine
# clocked in km is not worth blocking a maintenance note over.
_DEFAULT_METER_UNIT = {
    "Fleet/Truck": "km",
    "Heavy Machine": "hours",
}


def suggested_meter_unit(asset_category):
    return _DEFAULT_METER_UNIT.get(asset_category)


def _load_asset(conn, asset_id):
    row = conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
    if not row or row["status"] == "deleted":
        raise NotFoundError("That asset doesn't exist.")
    return row


def may_log_service(conn, asset_id, user):
    """True when this person may record a service note against THIS asset.

    Two ways in: they can manage assets (a manager), or the asset is
    currently assigned to them, resolved employees.user_id ->
    asset_assignments. An employee record never linked to a login account
    can't match - that link is set by a manager and never guessed from a
    name, so an unlinked driver simply isn't recognised here yet.
    """
    if user is None:
        return False
    if auth.can_manage_assets(user["role"]):
        return True
    return conn.execute(
        "SELECT 1 FROM asset_assignments asg JOIN employees e ON e.id = asg.employee_id "
        "WHERE asg.asset_id=? AND asg.status='active' AND e.user_id=?",
        (asset_id, user["id"]),
    ).fetchone() is not None


def save_service_photo(filename, file_obj):
    """Same validation path as a voucher receipt: allowed extension, size
    enforced DURING reading rather than after, real byte-signature check, a
    generated name, and the execute bit stripped on the way out. Reused
    rather than reimplemented so a new upload surface can't quietly be the
    weak one."""
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        raise ValidationError(
            f"Photo must be an image or PDF ({', '.join(sorted(ALLOWED_UPLOAD_EXTENSIONS))}).")

    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    chunks, total = [], 0
    while True:
        chunk = file_obj.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ValidationError(f"Photo is too large (max {MAX_UPLOAD_MB} MB).")
        chunks.append(chunk)
    contents = b"".join(chunks)

    sandbox.validate_file_signature(ext, contents)

    safe_name = f"svc_{datetime.now().strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(4)}{ext}"
    dest = sandbox.safe_join(UPLOADS_DIR, safe_name)
    dest.write_bytes(contents)
    sandbox.set_nonexecutable(dest)
    return safe_name


def _clean_meter(meter_reading, meter_unit):
    if meter_reading in (None, ""):
        return None, None
    try:
        meter_reading = float(meter_reading)
    except (TypeError, ValueError):
        raise ValidationError("Meter reading must be a number.")
    if meter_reading < 0:
        raise ValidationError("Meter reading can't be negative.")
    if meter_unit not in METER_UNITS:
        raise ValidationError("Meter unit must be km or hours.")
    return round(meter_reading, 2), meter_unit


def last_meter_reading(asset_id):
    """The most recent reading recorded for this asset, or None. Used to warn
    (never to block) when a new reading goes backwards."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT meter_reading, meter_unit, service_date FROM asset_service_log "
            "WHERE asset_id=? AND status='recorded' AND meter_reading IS NOT NULL "
            "ORDER BY service_date DESC, id DESC LIMIT 1", (asset_id,)
        ).fetchone()
        return dict(row) if row else None


def log_service(asset_id, *, service_date, service_type, description,
                parts_used="", meter_reading=None, meter_unit=None,
                photo_path=None, recorded_by_user):
    """Record a note. Returns (service_id, warnings) - `warnings` is a list of
    strings the route can show alongside a successful save. A meter reading
    that goes backwards is a warning rather than an error: it's usually a
    typo, but a replaced meter is real and refusing the note would just mean
    the maintenance never gets recorded at all."""
    description = (description or "").strip()
    if not description:
        raise ValidationError("Describe what was done.")
    if service_type not in SERVICE_TYPES:
        raise ValidationError("Please choose a valid service type.")
    if not service_date:
        raise ValidationError("A service date is required.")
    meter_reading, meter_unit = _clean_meter(meter_reading, meter_unit)

    warnings = []
    with get_db() as conn:
        asset = _load_asset(conn, asset_id)
        if not may_log_service(conn, asset_id, recorded_by_user):
            raise ForbiddenError(
                f"{asset['asset_tag']} isn't assigned to you, so you can't log service on it."
            )
        if meter_reading is not None:
            prev = conn.execute(
                "SELECT meter_reading, meter_unit FROM asset_service_log "
                "WHERE asset_id=? AND status='recorded' AND meter_reading IS NOT NULL "
                "ORDER BY service_date DESC, id DESC LIMIT 1", (asset_id,)
            ).fetchone()
            if prev and prev["meter_unit"] == meter_unit and meter_reading < prev["meter_reading"]:
                warnings.append(
                    f"Meter reading {meter_reading:g} {meter_unit} is lower than the last "
                    f"recorded {prev['meter_reading']:g} {meter_unit} - check it's not a typo."
                )
        cur = conn.execute(
            "INSERT INTO asset_service_log (asset_id, service_date, service_type, "
            "description, parts_used, meter_reading, meter_unit, photo_path, recorded_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (asset_id, service_date, service_type, description,
             (parts_used or "").strip(), meter_reading, meter_unit, photo_path,
             recorded_by_user["id"]),
        )
        log_action(conn, recorded_by_user["id"], "asset_service_logged",
                   f"{asset['asset_tag']} - {service_type} on {service_date}: {description}")
        return cur.lastrowid, warnings


def _load_own_or_manager(conn, service_id, acting_user):
    row = conn.execute(
        "SELECT sl.*, a.asset_tag FROM asset_service_log sl "
        "JOIN assets a ON a.id = sl.asset_id WHERE sl.id=?", (service_id,)
    ).fetchone()
    if not row or row["status"] == "deleted":
        raise NotFoundError("That service entry doesn't exist.")
    if row["recorded_by"] != acting_user["id"] and not auth.can_manage_assets(acting_user["role"]):
        raise ForbiddenError("That service entry was recorded by someone else.")
    return row


def update_service(service_id, *, acting_user, **fields):
    allowed = {"service_date", "service_type", "description", "parts_used",
               "meter_reading", "meter_unit", "photo_path"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValidationError(f"Can't update: {', '.join(sorted(unknown))}")
    if not fields:
        return
    if "service_type" in fields and fields["service_type"] not in SERVICE_TYPES:
        raise ValidationError("Please choose a valid service type.")
    if "description" in fields:
        fields["description"] = (fields["description"] or "").strip()
        if not fields["description"]:
            raise ValidationError("Describe what was done.")
    if "meter_reading" in fields or "meter_unit" in fields:
        fields["meter_reading"], fields["meter_unit"] = _clean_meter(
            fields.get("meter_reading"), fields.get("meter_unit")
        )

    with get_db() as conn:
        row = _load_own_or_manager(conn, service_id, acting_user)
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE asset_service_log SET {sets} WHERE id=?",
                     (*fields.values(), service_id))
        log_action(conn, acting_user["id"], "asset_service_updated",
                   f"{row['asset_tag']} service #{service_id} edited")


def delete_service(service_id, *, acting_user, reason):
    """Soft-delete, reason required. A service note is a maintenance record
    even though it carries no money - a machine's history is worth as much as
    its cost history when something fails later."""
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("A reason is required to delete a service entry.")
    with get_db() as conn:
        row = _load_own_or_manager(conn, service_id, acting_user)
        conn.execute(
            "UPDATE asset_service_log SET status='deleted', deleted_by=?, "
            "deleted_at=datetime('now'), delete_reason=? WHERE id=?",
            (acting_user["id"], reason, service_id),
        )
        log_action(conn, acting_user["id"], "asset_service_deleted",
                   f"{row['asset_tag']} service #{service_id}: {reason}")


def list_services(asset_id=None, *, date_from=None, date_to=None,
                  service_type=None, recorded_by=None, include_deleted=False):
    sql = ("SELECT sl.*, a.asset_tag, a.name AS asset_name, a.category AS asset_category, "
           "u.full_name AS recorded_by_name FROM asset_service_log sl "
           "JOIN assets a ON a.id = sl.asset_id "
           "LEFT JOIN users u ON u.id = sl.recorded_by WHERE 1=1")
    params = []
    if not include_deleted:
        sql += " AND sl.status='recorded'"
    if asset_id:
        sql += " AND sl.asset_id = ?"
        params.append(asset_id)
    if date_from:
        sql += " AND sl.service_date >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND sl.service_date <= ?"
        params.append(date_to)
    if service_type:
        sql += " AND sl.service_type = ?"
        params.append(service_type)
    if recorded_by:
        sql += " AND sl.recorded_by = ?"
        params.append(recorded_by)
    sql += " ORDER BY sl.service_date DESC, sl.id DESC"
    with get_db() as conn:
        return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]
