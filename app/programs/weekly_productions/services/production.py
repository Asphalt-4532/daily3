"""Business logic for Weekly Productions: a production *report* (header:
date, production line, shift, notes) holding one or more *lines* - each
line one material spec: type (GI or HDG), width, height, length, thickness,
and quantity. Mirrors Expense Program's voucher (header) + expense_lines
shape - see app/programs/expenses/services/expenses.py's docstring for the
pattern this follows.

A report gets its own pre-numbered reference (PRD-YYYY-NNNNN, via
app.database.next_document_number) the moment it's recorded. Deletion is
soft-delete only, same as expenses - a production report is a factory-floor
record of what actually happened, not a draft, so a reason is always
required and nothing is ever hard-deleted.

Concern: production-report lifecycle, line validation, and reporting
aggregates.
Depends on: app.database (persistence + document numbering).
Used by: app.programs.weekly_productions.routes.production_routes (the only caller).

No FastAPI/Starlette imports here on purpose - see app/services/errors.py.
"""
import io
import math
import re
from datetime import date, datetime, timedelta

from openpyxl import Workbook
from openpyxl.styles import Font

from app.database import get_db, get_connection, next_document_number, log_action
from app.excel_common import write_metadata_block, style_header_row
from app.services.errors import ValidationError, NotFoundError

__all__ = [
    "create_report", "list_reports", "get_report", "delete_report",
    "dashboard_stats", "weekly_summary", "daily_summary", "export_rows",
    "default_date_range", "describe_filters", "allocate_document_number",
    "build_export_workbook", "MATERIAL_TYPES", "SHIFTS", "TARGET_PERIODS",
    "get_weight_factors", "set_weight_factor", "DEFAULT_WEIGHT_FACTORS",
    "get_weight_targets", "set_weight_target",
    "is_month_locked", "list_locked_months", "lock_month", "unlock_month",
]

MATERIAL_TYPES = ["GI", "HDG"]
SHIFTS = {"", "Morning", "Afternoon", "Evening", "Night"}
TARGET_PERIODS = ["weekly", "monthly"]

# Fresh-install / never-customized weight factors, editable at Settings ->
# Weekly Productions -> Weight calculation (production_settings table). A
# material with no row yet falls back to these, same pattern as
# app.auth.role_has_permission()'s fallback to DEFAULT_ROLE_PERMISSIONS.
DEFAULT_WEIGHT_FACTORS = {"GI": 8.1, "HDG": 7.85}

_ELBOW_DEFAULT_R_IN_MM = 100.0  # never given in the input string - see _parse_elbow_dims()
_ANGLE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:degree|deg|°)", re.IGNORECASE)
_TEE_RE = re.compile(r"\b(?:tee|cross)\b", re.IGNORECASE)
_REDUCER_RE = re.compile(r"\breducer\b", re.IGNORECASE)
_REDUCER_DEFAULT_ANGLE = 90.0  # a reducer uses the elbow formula at a fixed 90 - no angle text needed


def get_weight_factors():
    """Returns {'GI': factor, 'HDG': factor}. A material with no row yet in
    production_settings falls back to DEFAULT_WEIGHT_FACTORS."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT key, value FROM production_settings WHERE key IN ('weight_factor_gi','weight_factor_hdg')"
        ).fetchall()
    finally:
        conn.close()
    saved = {r["key"]: r["value"] for r in rows}
    return {
        "GI": saved.get("weight_factor_gi", DEFAULT_WEIGHT_FACTORS["GI"]),
        "HDG": saved.get("weight_factor_hdg", DEFAULT_WEIGHT_FACTORS["HDG"]),
    }


def set_weight_factor(material_type: str, value, updated_by: int):
    material_type = (material_type or "").strip().upper()
    if material_type not in MATERIAL_TYPES:
        raise ValidationError("Material type must be GI or HDG.")
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValidationError("Factor must be a number.")
    if value <= 0:
        raise ValidationError("Factor must be greater than zero.")
    key = "weight_factor_gi" if material_type == "GI" else "weight_factor_hdg"
    with get_db() as conn:
        conn.execute(
            "INSERT INTO production_settings (key, value, updated_by, updated_at) "
            "VALUES (?, ?, ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_by=excluded.updated_by, "
            "updated_at=excluded.updated_at",
            (key, value, updated_by),
        )
        log_action(conn, updated_by, "update_weight_factor", f"{material_type}: {value}")


def get_weight_targets():
    """Returns {'weekly': kg, 'monthly': kg}, 0 meaning 'not set' - the
    dashboard only shows a progress bar for a period whose target is
    greater than 0."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT key, value FROM production_settings "
            "WHERE key IN ('weight_target_weekly_kg','weight_target_monthly_kg')"
        ).fetchall()
    finally:
        conn.close()
    saved = {r["key"]: r["value"] for r in rows}
    return {
        "weekly": saved.get("weight_target_weekly_kg", 0.0),
        "monthly": saved.get("weight_target_monthly_kg", 0.0),
    }


def set_weight_target(period: str, value, updated_by: int):
    period = (period or "").strip().lower()
    if period not in TARGET_PERIODS:
        raise ValidationError("Target period must be weekly or monthly.")
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValidationError("Weight target must be a number.")
    if value < 0:
        raise ValidationError("Weight target cannot be negative.")
    key = f"weight_target_{period}_kg"
    with get_db() as conn:
        conn.execute(
            "INSERT INTO production_settings (key, value, updated_by, updated_at) "
            "VALUES (?, ?, ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_by=excluded.updated_by, "
            "updated_at=excluded.updated_at",
            (key, value, updated_by),
        )
        log_action(conn, updated_by, "update_weight_target", f"{period}: {value} kg")


def is_month_locked(month: str) -> bool:
    """`month` is 'YYYY-MM'."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT 1 FROM production_locked_months WHERE month = ?", (month,)
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def list_locked_months():
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT plm.month, plm.locked_at, u.full_name locked_by_name "
            "FROM production_locked_months plm JOIN users u ON u.id = plm.locked_by "
            "ORDER BY plm.month DESC"
        ).fetchall()
    finally:
        conn.close()
    result = []
    for r in rows:
        d = dict(r)
        # first day of the *next* month, minus one day - avoids hardcoding
        # "-31" for a month that doesn't have one (Feb, Apr, Jun, ...)
        first_of_month = datetime.strptime(d["month"] + "-01", "%Y-%m-%d").date()
        next_month = (first_of_month.replace(day=28) + timedelta(days=4)).replace(day=1)
        d["month_end"] = (next_month - timedelta(days=1)).isoformat()
        d["month_start"] = first_of_month.isoformat()
        d["month_label"] = _format_month(d["month"])
        result.append(d)
    return result


def lock_month(month: str, locked_by: int):
    if not re.match(r"^\d{4}-\d{2}$", month or ""):
        raise ValidationError("Month must be in YYYY-MM format.")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO production_locked_months (month, locked_by, locked_at) "
            "VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(month) DO UPDATE SET locked_by=excluded.locked_by, "
            "locked_at=datetime('now')",
            (month, locked_by),
        )
        log_action(conn, locked_by, "lock_production_month", month)


def unlock_month(month: str, unlocked_by: int):
    with get_db() as conn:
        cur = conn.execute("DELETE FROM production_locked_months WHERE month = ?", (month,))
        if cur.rowcount == 0:
            raise NotFoundError("That month isn't locked.")
        log_action(conn, unlocked_by, "unlock_production_month", month)


def _validate_header(report_date, line_name, shift):
    if not report_date:
        raise ValidationError("Date is required.")
    try:
        datetime.strptime(report_date, "%Y-%m-%d")
    except ValueError:
        raise ValidationError("Date must be in YYYY-MM-DD format.")
    if not (line_name or "").strip():
        raise ValidationError("Production line / department is required.")
    if shift not in SHIFTS:
        raise ValidationError("Shift must be one of: Morning, Afternoon, Evening, Night.")


def _thickness_from_token(tok: str) -> float:
    """Thickness may be written with an explicit mm suffix or as a bare
    number - either is accepted (see the dims-box docstring on
    _parse_straight_dims/_parse_elbow_dims for why this and length are
    told apart by the length token's bare 'm' suffix, not position)."""
    tok = tok.strip()
    raw = tok[:-2] if tok.endswith("mm") else tok
    try:
        value = float(raw)
    except ValueError:
        raise ValidationError(f"Thickness '{tok}' must be a number.")
    if not (0 < value <= 4):
        raise ValidationError(f"Thickness must be over 0 and no more than 4mm - got {value}mm.")
    return value


def _first_number(tok: str) -> float:
    """A width token may be a single number, or dash-separated (a reducer
    like '100-100', or a transition fitting like '100-100-200') - only the
    first number is used for width."""
    try:
        return float(tok.split("-")[0])
    except ValueError:
        raise ValidationError(f"Width '{tok}' must start with a number.")


def _parse_straight_dims(w_tok, h_tok, l_tok, t_tok):
    """WxHxLxT where L and T are the last two x-separated tokens in either
    order - whichever ends in a bare 'm' (not 'mm') is length, the other is
    thickness (mm suffix optional there)."""
    def kind(tok):
        tok = tok.strip()
        if tok.endswith("mm"):
            return "thickness", tok
        if tok.endswith("m"):
            return "length", tok[:-1]
        return "thickness", tok

    a_kind, a_raw = kind(l_tok)
    b_kind, b_raw = kind(t_tok)
    if a_kind == b_kind:
        raise ValidationError(
            "Need exactly one length value ending in m and one thickness value, in either order."
        )
    length_raw = a_raw if a_kind == "length" else b_raw
    thickness_tok = l_tok if a_kind == "thickness" else t_tok
    try:
        length = float(length_raw)
    except ValueError:
        raise ValidationError(f"Length '{length_raw}' must be a number.")
    if length < 0:
        raise ValidationError("Length cannot be negative.")
    thickness = _thickness_from_token(thickness_tok)
    width = _first_number(w_tok)
    try:
        height = float(h_tok)
    except ValueError:
        raise ValidationError(f"Height '{h_tok}' must be a number.")
    return {"width": width, "height": height, "length": length, "thickness": thickness}


def _straight_weight_per_piece(width, height, length, thickness, factor):
    """Weight (kg) = ((width_mm + 2*height_mm + 30) / 1000) x length_m x
    thickness_mm x factor - width/height converted to metres to match
    length, so the +30mm seam/lock allowance is +0.03m in the same terms."""
    return ((width + 2 * height + 30) / 1000) * length * thickness * factor


def _parse_elbow_dims(w_tok, h_tok, t_tok, angle):
    """An elbow/reducer has no length in the dims string - the middle slot
    is left empty (WxHxxT). `angle` is resolved by the caller (parse_line_
    input()) before this is called: an explicit "N degree" phrase, or a
    fixed 90 for a reducer (see _REDUCER_DEFAULT_ANGLE). Width may be
    dash-separated (a transition fitting like '200-100-200') - only the
    first number is used. R_in is never given, so it defaults to 100mm.
    Length and the calculated 'blank' width both come from the envelope-box
    formula (R_out = R_in + width; Length = R_out x sin(theta); Width_calc
    = R_out - R_in x cos(theta))."""
    width = _first_number(w_tok)
    try:
        height = float(h_tok)
    except ValueError:
        raise ValidationError(f"Height '{h_tok}' must be a number.")
    thickness = _thickness_from_token(t_tok)
    r_in = _ELBOW_DEFAULT_R_IN_MM
    r_out = r_in + width
    rad = math.radians(angle)
    length_mm = r_out * math.sin(rad)
    width_calc_mm = r_out - (r_in * math.cos(rad))
    return {
        "width": width, "height": height, "thickness": thickness,
        "length_m": length_mm / 1000, "width_calc_m": width_calc_mm / 1000,
        "r_in": r_in, "r_out": r_out, "angle": angle,
    }


def _elbow_weight_per_piece(height, thickness, r_in, r_out, angle, factor):
    """Weight from surface area (base plate + inside wall + outside wall of
    the arced ring), not the straight-piece linear formula - an elbow is a
    hollow arc, not a solid block. All lengths in metres before this call."""
    frac = angle / 360
    base_area = frac * math.pi * (r_out ** 2 - r_in ** 2)
    inside_wall = frac * 2 * math.pi * r_in * height
    outside_wall = frac * 2 * math.pi * r_out * height
    total_area = base_area + inside_wall + outside_wall
    density = factor * 1000  # factor is in g/cm3-equivalent units (e.g. 8.1); kg/m3 = x1000
    return total_area * thickness * density


def _parse_tee_dims(w_tok, h_tok, t_tok):
    """A TEE, like an elbow, has no length in the dims string - empty
    middle slot (WxHxxT) - and no angle either, since a TEE is a straight
    three-way junction rather than a bend (see parse_line_input() for how
    a TEE is told apart from an elbow: the word "tee" in the description,
    not an angle). Width may be dash-separated (only the first number is
    used, same convention as elbows). R_in is never given, defaults to
    100mm. The envelope block this junction is cut from:
    Material Width (X) = W + 2*R_in ; Material Length (Y) = 2*W + 2*R_in."""
    width = _first_number(w_tok)
    try:
        height = float(h_tok)
    except ValueError:
        raise ValidationError(f"Height '{h_tok}' must be a number.")
    thickness = _thickness_from_token(t_tok)
    r_in = _ELBOW_DEFAULT_R_IN_MM
    x_mm = width + 2 * r_in
    y_mm = 2 * width + 2 * r_in
    return {
        "width": width, "height": height, "thickness": thickness, "r_in": r_in,
        "x_mm": x_mm, "y_mm": y_mm,
    }


def _tee_weight_per_piece(width, height, thickness, r_in, y, factor):
    """Bottom and side panel weight, kept separate rather than summed only
    - a TEE's blank isn't one flat rectangle like a straight piece, and
    isn't one arced ring like an elbow, so neither of those two weight
    functions applies. All lengths in metres before this call; y is the
    calculated envelope length (Material Length / 1000)."""
    net_base_area = (width * y) + (width * r_in) - (2 * (r_in ** 2 - (0.7854 * r_in ** 2)))
    straight_back_wall = y * height
    curved_inner_walls = 2 * (1.5708 * r_in * height)
    total_side_area = straight_back_wall + curved_inner_walls
    density = factor * 1000  # same convention as _elbow_weight_per_piece
    bottom_weight = net_base_area * thickness * density
    side_weight = total_side_area * thickness * density
    return bottom_weight, side_weight


def parse_line_input(raw: str, material_type: str, quantity, factors: dict):
    """Parses one 'description + dims' box (last whitespace-separated
    token is dims, everything before it is the description) into the
    fields stored on a production_report_lines row, including the
    computed weight. `factors` is {'GI': ..., 'HDG': ...} from
    get_weight_factors()."""
    material_type = (material_type or "").strip().upper()
    if material_type not in MATERIAL_TYPES:
        raise ValidationError("Material type must be GI or HDG.")
    try:
        quantity = float(quantity)
    except (TypeError, ValueError):
        raise ValidationError("Quantity must be a number.")
    if quantity <= 0:
        raise ValidationError("Quantity must be greater than zero.")

    words = (raw or "").strip().split()
    if len(words) < 2:
        raise ValidationError(
            "Enter a description followed by dimensions, e.g. \"cable tray 100x50x2.44mx0.7\"."
        )
    dims_tok = words[-1]
    description = " ".join(words[:-1])
    parts = dims_tok.split("x")
    if len(parts) != 4:
        raise ValidationError(
            "Dimensions need 4 x-separated parts: widthxheightxlengthxthickness "
            "(leave the length part empty for an elbow/fitting or tee)."
        )
    w_tok, h_tok, l_tok, t_tok = parts
    factor = factors[material_type]

    if l_tok.strip() == "":
        if _TEE_RE.search(description):
            d = _parse_tee_dims(w_tok, h_tok, t_tok)
            r_in_m, y_m, h_m, t_m = d["r_in"] / 1000, d["y_mm"] / 1000, d["height"] / 1000, d["thickness"] / 1000
            bottom_per_piece, side_per_piece = _tee_weight_per_piece(
                d["width"] / 1000, h_m, t_m, r_in_m, y_m, factor,
            )
            weight_per_piece = bottom_per_piece + side_per_piece
            width, height, length, thickness = d["x_mm"], d["height"], d["y_mm"] / 1000, d["thickness"]
        elif _ANGLE_RE.search(description) or _REDUCER_RE.search(description):
            angle_match = _ANGLE_RE.search(description)
            angle = float(angle_match.group(1)) if angle_match else _REDUCER_DEFAULT_ANGLE
            d = _parse_elbow_dims(w_tok, h_tok, t_tok, angle)
            weight_per_piece = _elbow_weight_per_piece(
                d["height"] / 1000, d["thickness"] / 1000, d["r_in"] / 1000, d["r_out"] / 1000,
                d["angle"], factor,
            )
            width, height, length, thickness = d["width"], d["height"], d["length_m"], d["thickness"]
        else:
            raise ValidationError(
                "An empty length part means an elbow, reducer, tee, or cross - name the bend "
                "angle (e.g. \"90 degree\"), or include the word \"reducer\" (fixed 90), "
                "\"tee\", or \"cross\" in the description."
            )
    else:
        d = _parse_straight_dims(w_tok, h_tok, l_tok, t_tok)
        weight_per_piece = _straight_weight_per_piece(
            d["width"], d["height"], d["length"], d["thickness"], factor,
        )
        width, height, length, thickness = d["width"], d["height"], d["length"], d["thickness"]

    return {
        "description": description, "material_type": material_type,
        "width": width, "height": height, "length": length, "thickness": thickness,
        "quantity": quantity, "weight_kg": weight_per_piece * quantity,
    }


def _validate_lines(lines):
    if not lines:
        raise ValidationError("A production report needs at least one line.")
    factors = get_weight_factors()
    cleaned = []
    for i, line in enumerate(lines, start=1):
        try:
            cleaned.append(parse_line_input(
                line.get("raw", ""), line.get("material_type", ""),
                line.get("quantity", ""), factors,
            ))
        except ValidationError as e:
            raise ValidationError(f"Line {i}: {e}")
    return cleaned


def _format_month(month: str) -> str:
    try:
        return datetime.strptime(month, "%Y-%m").strftime("%B %Y")
    except ValueError:
        return month


def create_report(*, report_date, line_name, shift="", notes="", lines, recorded_by):
    _validate_header(report_date, line_name, shift)
    month = report_date[:7]
    if is_month_locked(month):
        raise ValidationError(
            f"{_format_month(month)} is locked - no new production reports can be added for that month."
        )
    cleaned_lines = _validate_lines(lines)
    with get_db() as conn:
        report_no = next_document_number(conn, "PRD", report_date[:4])
        cur = conn.execute(
            "INSERT INTO production_reports (report_no, report_date, line_name, shift, notes, "
            "recorded_by) VALUES (?,?,?,?,?,?)",
            (report_no, report_date, line_name.strip(), shift, (notes or "").strip(), recorded_by),
        )
        report_id = cur.lastrowid
        for i, line in enumerate(cleaned_lines, start=1):
            conn.execute(
                "INSERT INTO production_report_lines (report_id, line_no, description, "
                "material_type, width, height, length, thickness, quantity, weight_kg) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (report_id, i, line["description"], line["material_type"], line["width"],
                 line["height"], line["length"], line["thickness"], line["quantity"],
                 line["weight_kg"]),
            )
        log_action(conn, recorded_by, "create_production_report",
                   f"{report_no}: {len(cleaned_lines)} line(s)")
        return report_id, report_no


_LIST_QUERY = (
    "SELECT pr.*, u.full_name recorded_by_name, du.full_name deleted_by_name, "
    "COUNT(prl.id) line_count, COALESCE(SUM(prl.quantity),0) total_quantity, "
    "COALESCE(SUM(prl.weight_kg),0) total_weight, "
    "GROUP_CONCAT(DISTINCT prl.material_type) material_types "
    "FROM production_reports pr "
    "JOIN users u ON u.id = pr.recorded_by "
    "LEFT JOIN users du ON du.id = pr.deleted_by "
    "LEFT JOIN production_report_lines prl ON prl.report_id = pr.id WHERE 1=1"
)


def list_reports(status="recorded", line_name="", material_type="", start="", end=""):
    query = _LIST_QUERY
    params = []
    if status:
        query += " AND pr.status = ?"
        params.append(status)
    if line_name:
        query += " AND pr.line_name = ?"
        params.append(line_name)
    if start:
        query += " AND pr.report_date >= ?"
        params.append(start)
    if end:
        query += " AND pr.report_date <= ?"
        params.append(end)
    if material_type:
        query += (" AND pr.id IN (SELECT report_id FROM production_report_lines "
                   "WHERE material_type = ?)")
        params.append(material_type)
    query += " GROUP BY pr.id ORDER BY pr.report_date DESC, pr.id DESC"
    with get_db() as conn:
        rows = [dict(r) for r in conn.execute(query, params).fetchall()]
        total_qty = sum(r["total_quantity"] for r in rows)
        total_weight = sum(r["total_weight"] for r in rows)
        lines = [r["name"] for r in conn.execute(
            "SELECT DISTINCT line_name name FROM production_reports ORDER BY line_name"
        ).fetchall()]
        return rows, total_qty, total_weight, lines


def get_report(report_id: int):
    with get_db() as conn:
        header = conn.execute(
            "SELECT pr.*, u.full_name recorded_by_name, du.full_name deleted_by_name "
            "FROM production_reports pr "
            "JOIN users u ON u.id = pr.recorded_by "
            "LEFT JOIN users du ON du.id = pr.deleted_by WHERE pr.id = ?", (report_id,)
        ).fetchone()
        if not header:
            raise NotFoundError("Production report not found.")
        report = dict(header)
        report["lines"] = [dict(r) for r in conn.execute(
            "SELECT * FROM production_report_lines WHERE report_id = ? ORDER BY line_no",
            (report_id,),
        ).fetchall()]
        report["total_quantity"] = sum(l["quantity"] for l in report["lines"])
        report["total_weight"] = sum(l["weight_kg"] for l in report["lines"])
        return report


def delete_report(report_id: int, deleted_by: int, reason: str):
    if not (reason or "").strip():
        raise ValidationError("A reason is required to delete a production report.")
    with get_db() as conn:
        row = conn.execute(
            "SELECT report_no, status FROM production_reports WHERE id = ?", (report_id,)
        ).fetchone()
        if not row:
            raise NotFoundError("Production report not found.")
        conn.execute(
            "UPDATE production_reports SET status='deleted', deleted_by=?, "
            "deleted_at=datetime('now'), delete_reason=? WHERE id=?",
            (deleted_by, reason.strip(), report_id),
        )
        log_action(conn, deleted_by, "delete_production_report",
                   f"{row['report_no']}: {reason.strip()}")


def default_date_range():
    start = (date.today() - timedelta(days=42)).isoformat()  # last 6 ISO weeks
    end = date.today().isoformat()
    return start, end


def dashboard_stats():
    """Recent reports, this week's/month's total output, and totals by
    line/material type for the last 6 weeks - the Weekly Productions
    equivalent of expenses.dashboard_stats()."""
    start, end = default_date_range()
    week_start = (date.today() - timedelta(days=date.today().weekday())).isoformat()
    month_start = date.today().replace(day=1).isoformat()
    with get_db() as conn:
        recent = [dict(r) for r in conn.execute(
            _LIST_QUERY + " AND pr.status='recorded' GROUP BY pr.id "
            "ORDER BY pr.report_date DESC, pr.id DESC LIMIT 8"
        ).fetchall()]
        week_total = conn.execute(
            "SELECT COALESCE(SUM(prl.quantity),0) t FROM production_report_lines prl "
            "JOIN production_reports pr ON pr.id = prl.report_id "
            "WHERE pr.status='recorded' AND pr.report_date >= ?", (week_start,)
        ).fetchone()["t"]
        week_weight = conn.execute(
            "SELECT COALESCE(SUM(prl.weight_kg),0) t FROM production_report_lines prl "
            "JOIN production_reports pr ON pr.id = prl.report_id "
            "WHERE pr.status='recorded' AND pr.report_date >= ?", (week_start,)
        ).fetchone()["t"]
        month_weight = conn.execute(
            "SELECT COALESCE(SUM(prl.weight_kg),0) t FROM production_report_lines prl "
            "JOIN production_reports pr ON pr.id = prl.report_id "
            "WHERE pr.status='recorded' AND pr.report_date >= ?", (month_start,)
        ).fetchone()["t"]
        report_count_week = conn.execute(
            "SELECT COUNT(*) c FROM production_reports WHERE status='recorded' AND report_date >= ?",
            (week_start,),
        ).fetchone()["c"]
        by_line = [dict(r) for r in conn.execute(
            "SELECT pr.line_name name, COALESCE(SUM(prl.quantity),0) total "
            "FROM production_reports pr "
            "LEFT JOIN production_report_lines prl ON prl.report_id = pr.id "
            "WHERE pr.status='recorded' AND pr.report_date BETWEEN ? AND ? "
            "GROUP BY pr.line_name ORDER BY total DESC", (start, end)
        ).fetchall()]
        by_material = [dict(r) for r in conn.execute(
            "SELECT prl.material_type name, COALESCE(SUM(prl.quantity),0) total "
            "FROM production_report_lines prl JOIN production_reports pr ON pr.id = prl.report_id "
            "WHERE pr.status='recorded' AND pr.report_date BETWEEN ? AND ? "
            "GROUP BY prl.material_type ORDER BY total DESC", (start, end)
        ).fetchall()]
        return {
            "recent": recent, "week_total": week_total, "week_weight": week_weight,
            "month_weight": month_weight, "report_count_week": report_count_week,
            "by_line": by_line, "by_material": by_material, "week_start": week_start,
            "month_start": month_start, "weight_targets": get_weight_targets(),
        }


def weekly_summary(start="", end=""):
    """Output grouped by ISO year-week, line, and material type - for the
    Reports page and Excel export."""
    start = start or default_date_range()[0]
    end = end or default_date_range()[1]
    with get_db() as conn:
        by_week = [dict(r) for r in conn.execute(
            "SELECT strftime('%Y-W%W', pr.report_date) yw, COALESCE(SUM(prl.quantity),0) total "
            "FROM production_report_lines prl JOIN production_reports pr ON pr.id = prl.report_id "
            "WHERE pr.status='recorded' AND pr.report_date BETWEEN ? AND ? "
            "GROUP BY yw ORDER BY yw", (start, end)
        ).fetchall()]
        by_line = [dict(r) for r in conn.execute(
            "SELECT pr.line_name name, COALESCE(SUM(prl.quantity),0) total, COUNT(DISTINCT pr.id) reports "
            "FROM production_reports pr "
            "LEFT JOIN production_report_lines prl ON prl.report_id = pr.id "
            "WHERE pr.status='recorded' AND pr.report_date BETWEEN ? AND ? "
            "GROUP BY pr.line_name ORDER BY total DESC", (start, end)
        ).fetchall()]
        by_material = [dict(r) for r in conn.execute(
            "SELECT prl.material_type name, COALESCE(SUM(prl.quantity),0) total, COUNT(*) lines, "
            "COALESCE(SUM(prl.weight_kg),0) total_weight "
            "FROM production_report_lines prl JOIN production_reports pr ON pr.id = prl.report_id "
            "WHERE pr.status='recorded' AND pr.report_date BETWEEN ? AND ? "
            "GROUP BY prl.material_type ORDER BY total DESC", (start, end)
        ).fetchall()]
        grand_total = sum(r["total"] for r in by_line)
        grand_weight = sum(r["total_weight"] for r in by_material)
        return by_week, by_line, by_material, grand_total, grand_weight


_MAX_DAILY_SUMMARY_DAYS = 370  # a generous cap - a bit over a year - against a pathological range


def daily_summary(start="", end=""):
    """Per-day report count / quantity / weight totals for the given date
    range, including days with zero production - so a locked month's daily
    report reads as a complete log, not just the days something happened.
    Used by the Reports page's "By day" table, and linked to from a locked
    month in Settings."""
    start = start or default_date_range()[0]
    end = end or default_date_range()[1]
    start_d = datetime.strptime(start, "%Y-%m-%d").date()
    end_d = datetime.strptime(end, "%Y-%m-%d").date()
    if end_d < start_d:
        start_d, end_d = end_d, start_d
    if (end_d - start_d).days > _MAX_DAILY_SUMMARY_DAYS:
        end_d = start_d + timedelta(days=_MAX_DAILY_SUMMARY_DAYS)

    with get_db() as conn:
        rows = conn.execute(
            "SELECT pr.report_date, COUNT(DISTINCT pr.id) reports, "
            "COALESCE(SUM(prl.quantity),0) quantity, COALESCE(SUM(prl.weight_kg),0) weight "
            "FROM production_reports pr "
            "LEFT JOIN production_report_lines prl ON prl.report_id = pr.id "
            "WHERE pr.status='recorded' AND pr.report_date BETWEEN ? AND ? "
            "GROUP BY pr.report_date", (start_d.isoformat(), end_d.isoformat())
        ).fetchall()
    by_date = {r["report_date"]: dict(r) for r in rows}

    result = []
    d = start_d
    while d <= end_d:
        key = d.isoformat()
        result.append(by_date.get(key, {"report_date": key, "reports": 0, "quantity": 0, "weight": 0}))
        d += timedelta(days=1)
    return result


def export_rows(status="recorded", line_name="", material_type="", start="", end=""):
    """One row per material-spec line (same granularity convention as
    Expense Program's exports - see reports.py's docstring) - a report with
    3 lines produces 3 export rows, with header fields repeated."""
    query = (
        "SELECT pr.report_no, pr.report_date, pr.line_name, pr.shift, pr.status, "
        "u.full_name recorded_by, prl.line_no, prl.description, prl.material_type, prl.width, "
        "prl.height, prl.length, prl.thickness, prl.quantity, prl.weight_kg "
        "FROM production_report_lines prl "
        "JOIN production_reports pr ON pr.id = prl.report_id "
        "JOIN users u ON u.id = pr.recorded_by WHERE 1=1"
    )
    params = []
    if status:
        query += " AND pr.status = ?"
        params.append(status)
    if line_name:
        query += " AND pr.line_name = ?"
        params.append(line_name)
    if material_type:
        query += " AND prl.material_type = ?"
        params.append(material_type)
    if start:
        query += " AND pr.report_date >= ?"
        params.append(start)
    if end:
        query += " AND pr.report_date <= ?"
        params.append(end)
    query += " ORDER BY pr.report_date, pr.id, prl.line_no"
    with get_db() as conn:
        return [dict(r) for r in conn.execute(query, params).fetchall()]


def describe_filters(start="", end="", status="", line_name="", material_type=""):
    parts = []
    if start or end:
        parts.append(f"Period: {start or 'earliest'} to {end or 'latest'}")
    if status:
        parts.append(f"Status: {status.capitalize()}")
    if line_name:
        parts.append(f"Line: {line_name}")
    if material_type:
        parts.append(f"Material: {material_type}")
    return " · ".join(parts) if parts else "All records"


def allocate_document_number(doc_type="PRD"):
    with get_db() as conn:
        return next_document_number(conn, doc_type, str(date.today().year))


def build_export_workbook(rows, *, document_no, generated_by, filters_description):
    """Same shape as expenses/services/reports.py's build_export_workbook()
    - reuses app.excel_common's shared metadata block / header styling
    rather than reinventing it (see CHANGE_IMPACT_GUIDE.md's "Reports /
    exports" row)."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Weekly Productions"
    header_row = write_metadata_block(ws, "Weekly Production Report",
                                       document_no, generated_by, filters_description)

    headers = ["Report No", "Date", "Line / Department", "Shift", "Description", "Material",
               "Width", "Height", "Length", "Thickness", "Quantity", "Weight (kg)",
               "Status", "Recorded By"]
    ws.append(headers)
    style_header_row(ws, header_row)

    total_qty = 0.0
    total_weight = 0.0
    for r in rows:
        ws.append([r["report_no"], r["report_date"], r["line_name"], r["shift"],
                   r["description"], r["material_type"], r["width"], r["height"], r["length"],
                   r["thickness"], r["quantity"], round(r["weight_kg"], 2), r["status"],
                   r["recorded_by"]])
        total_qty += r["quantity"]
        total_weight += r["weight_kg"]

    ws.append([])
    ws.append(["", "", "", "", "", "", "", "", "", "", "TOTAL", total_qty, round(total_weight, 2)])
    ws.cell(row=ws.max_row, column=11).font = Font(bold=True)
    ws.cell(row=ws.max_row, column=12).font = Font(bold=True)

    for col, width in zip("ABCDEFGHIJKLMN",
                           [16, 12, 18, 9, 22, 9, 8, 8, 8, 10, 10, 12, 11, 18]):
        ws.column_dimensions[col].width = width

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf
