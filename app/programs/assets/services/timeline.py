"""The unified asset timeline: four feeds merged and sorted by date.

    📦 Registration  – the asset row itself (one event, purchase cost)
    💰 Cost          – approved expense_lines where asset_id = ?
    🔧 Service       – asset_service_log (non-cost; no amount, ever)
    📋 Custody       – asset_assignments, both issue and return events

Everything is assembled as a query, never stored. Money events carry their
amount; service events carry a literal None - never 0.0, because 0.0 reads
as "cost nothing" and None reads as "not a cost event at all". The
distinction shows up in the exports: None renders as a dash ("—"), not as
"0.00".

The TCO function sums approved cost lines only (same rule as dashboard
stats) and never touches the asset tables for money.

No FastAPI imports - see app/services/errors.py.
"""

from app.database import get_db

__all__ = ["asset_timeline", "asset_tco", "asset_summary"]

# Human-readable kind labels, kept here as the single source so the
# templates, the reports, and the tests all agree.
KIND_REGISTRATION = "registration"
KIND_COST         = "cost"
KIND_SERVICE      = "service"
KIND_CUSTODY      = "custody"
ALL_KINDS = (KIND_REGISTRATION, KIND_COST, KIND_SERVICE, KIND_CUSTODY)


def asset_tco(asset_id):
    """Total cost of ownership: purchase cost + sum of all approved voucher
    lines linked to this asset. Never reads the asset tables for money -
    the single source of truth for what something cost is the
    expense_lines that were approved.

    Returns a dict with purchase_cost, maintenance_total, and tco.
    """
    with get_db() as conn:
        asset = conn.execute(
            "SELECT purchase_cost FROM assets WHERE id=?", (asset_id,)
        ).fetchone()
        if not asset:
            return {"purchase_cost": 0.0, "maintenance_total": 0.0, "tco": 0.0}
        purchase = asset["purchase_cost"] or 0.0
        row = conn.execute(
            "SELECT COALESCE(SUM(el.amount + el.vat_amount), 0) AS maint "
            "FROM expense_lines el "
            "JOIN expenses e ON e.id = el.expense_id "
            "WHERE el.asset_id = ? AND e.status = 'approved'",
            (asset_id,),
        ).fetchone()
        maintenance = row["maint"] or 0.0
        return {
            "purchase_cost": round(purchase, 2),
            "maintenance_total": round(maintenance, 2),
            "tco": round(purchase + maintenance, 2),
        }


def asset_summary(asset_id):
    """Registration facts + TCO + current holders, for the asset-detail
    page header block."""
    with get_db() as conn:
        asset = conn.execute(
            "SELECT a.*, cc.name AS cost_center_name FROM assets a "
            "LEFT JOIN cost_centers cc ON cc.id = a.cost_center_id "
            "WHERE a.id = ?", (asset_id,)
        ).fetchone()
        if not asset:
            return None
        holders = conn.execute(
            "SELECT asg.*, e.name AS employee_name, e.phone AS employee_phone, "
            "cc2.name AS cost_center_name, asg.role_note "
            "FROM asset_assignments asg "
            "LEFT JOIN employees e ON e.id = asg.employee_id "
            "LEFT JOIN cost_centers cc2 ON cc2.id = asg.cost_center_id "
            "WHERE asg.asset_id = ? AND asg.status = 'active' "
            "ORDER BY asg.issued_date, asg.id",
            (asset_id,),
        ).fetchall()
        last_service = conn.execute(
            "SELECT service_date FROM asset_service_log "
            "WHERE asset_id = ? AND status = 'recorded' "
            "ORDER BY service_date DESC, id DESC LIMIT 1",
            (asset_id,),
        ).fetchone()
    tco = asset_tco(asset_id)
    return {
        **dict(asset),
        "current_holders": [dict(h) for h in holders],
        "last_service_date": last_service["service_date"] if last_service else None,
        **tco,
    }


def asset_timeline(asset_id, *, date_from=None, date_to=None, kinds=None):
    """All events for one asset, newest first, as a flat list of dicts.

    Each dict has at minimum:
      event_date, kind, description, amount (None for non-cost events),
      vat_amount (None for non-cost), recorded_by_name, reference (voucher_no
      or assignment ID, or None).

    `kinds` is a subset of ALL_KINDS; None means all.
    """
    if kinds is None:
        kinds = set(ALL_KINDS)
    else:
        kinds = set(kinds)

    events = []

    with get_db() as conn:
        asset = conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
        if not asset:
            return []
        creator = conn.execute(
            "SELECT full_name FROM users WHERE id=?", (asset["created_by"],)
        ).fetchone()
        creator_name = creator["full_name"] if creator else "—"

        # --- Registration event (always one) ---
        if KIND_REGISTRATION in kinds:
            reg_date = asset["purchase_date"] or asset["created_at"][:10]
            if (not date_from or reg_date >= date_from) and (not date_to or reg_date <= date_to):
                events.append({
                    "event_date": reg_date,
                    "kind": KIND_REGISTRATION,
                    "description": f"Asset registered: {asset['name']} ({asset['category']})",
                    "amount": asset["purchase_cost"] or 0.0,
                    "vat_amount": None,
                    "recorded_by_name": creator_name,
                    "reference": None,
                    "detail": None,
                    "supplier_name": None,
                })

        # --- Cost events ---
        if KIND_COST in kinds:
            sql = (
                "SELECT el.amount, el.vat_amount, el.particulars, "
                "       e.expense_date, e.voucher_no, e.paid_to, "
                "       s.name AS supplier_name, c.name AS category_name, "
                "       u.full_name AS prepared_by_name "
                "FROM expense_lines el "
                "JOIN expenses e ON e.id = el.expense_id "
                "JOIN categories c ON c.id = el.category_id "
                "LEFT JOIN suppliers s ON s.id = el.supplier_id "
                "JOIN users u ON u.id = e.prepared_by "
                "WHERE el.asset_id = ? AND e.status = 'approved'"
            )
            params = [asset_id]
            if date_from:
                sql += " AND e.expense_date >= ?"
                params.append(date_from)
            if date_to:
                sql += " AND e.expense_date <= ?"
                params.append(date_to)
            for row in conn.execute(sql, tuple(params)).fetchall():
                events.append({
                    "event_date": row["expense_date"],
                    "kind": KIND_COST,
                    "description": row["particulars"],
                    "amount": row["amount"],
                    "vat_amount": row["vat_amount"],
                    "recorded_by_name": row["prepared_by_name"],
                    "reference": row["voucher_no"],
                    "detail": row["category_name"],
                    "supplier_name": row["supplier_name"],
                })

        # --- Service events ---
        if KIND_SERVICE in kinds:
            sql = (
                "SELECT sl.service_date, sl.service_type, sl.description, "
                "       sl.parts_used, sl.meter_reading, sl.meter_unit, sl.photo_path, "
                "       u.full_name AS recorded_by_name, sl.id "
                "FROM asset_service_log sl "
                "LEFT JOIN users u ON u.id = sl.recorded_by "
                "WHERE sl.asset_id = ? AND sl.status = 'recorded'"
            )
            params = [asset_id]
            if date_from:
                sql += " AND sl.service_date >= ?"
                params.append(date_from)
            if date_to:
                sql += " AND sl.service_date <= ?"
                params.append(date_to)
            for row in conn.execute(sql, tuple(params)).fetchall():
                detail_parts = [row["service_type"]]
                if row["parts_used"]:
                    detail_parts.append(f"Parts: {row['parts_used']}")
                if row["meter_reading"] is not None:
                    detail_parts.append(
                        f"Meter: {row['meter_reading']:g} {row['meter_unit']}"
                    )
                events.append({
                    "event_date": row["service_date"],
                    "kind": KIND_SERVICE,
                    "description": row["description"],
                    # None, not 0.0: in exports a dash ("—") means "not a cost
                    # event" and 0.00 means "this cost nothing". They are
                    # different facts and must not be conflated.
                    "amount": None,
                    "vat_amount": None,
                    "recorded_by_name": row["recorded_by_name"],
                    "reference": str(row["id"]),
                    "detail": " · ".join(detail_parts),
                    "supplier_name": None,
                    "photo_path": row["photo_path"],
                    "meter_reading": row["meter_reading"],
                    "meter_unit": row["meter_unit"],
                })

        # --- Custody events (each row produces up to two events) ---
        if KIND_CUSTODY in kinds:
            sql = (
                "SELECT asg.*, e.name AS employee_name, cc.name AS cc_name, "
                "       u1.full_name AS assigned_by_name, u2.full_name AS closed_by_name "
                "FROM asset_assignments asg "
                "LEFT JOIN employees e ON e.id = asg.employee_id "
                "LEFT JOIN cost_centers cc ON cc.id = asg.cost_center_id "
                "LEFT JOIN users u1 ON u1.id = asg.assigned_by "
                "LEFT JOIN users u2 ON u2.id = asg.closed_by "
                "WHERE asg.asset_id = ?"
            )
            for row in conn.execute(sql, (asset_id,)).fetchall():
                holder = row["employee_name"] or row["cc_name"] or "—"
                note = f" ({row['role_note']})" if row["role_note"] else ""
                issue_date = row["issued_date"]
                if (not date_from or issue_date >= date_from) and \
                   (not date_to or issue_date <= date_to):
                    events.append({
                        "event_date": issue_date,
                        "kind": KIND_CUSTODY,
                        "description": f"Assigned to {holder}{note}",
                        "amount": None,
                        "vat_amount": None,
                        "recorded_by_name": row["assigned_by_name"] or "—",
                        "reference": str(row["id"]),
                        "detail": row["status"],
                        "supplier_name": None,
                    })
                if row["returned_date"]:
                    ret_date = row["returned_date"]
                    if (not date_from or ret_date >= date_from) and \
                       (not date_to or ret_date <= date_to):
                        verb = {
                            "returned": "Returned by",
                            "transferred": "Transferred from",
                            "divested": "Divested from",
                        }.get(row["status"], "Returned by")
                        events.append({
                            "event_date": ret_date,
                            "kind": KIND_CUSTODY,
                            "description": f"{verb} {holder}{note}",
                            "amount": None,
                            "vat_amount": None,
                            "recorded_by_name": row["closed_by_name"] or "—",
                            "reference": str(row["id"]),
                            "detail": row["close_reason"] or "",
                            "supplier_name": None,
                        })

    events.sort(key=lambda e: e["event_date"], reverse=True)
    return events
