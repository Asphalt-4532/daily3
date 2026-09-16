"""Asset maintenance reports: filter engine, Excel workbook, and PDF
generation for single-asset (ASR) and multi-asset (AMR) exports.

Both exports follow the repo conventions exactly:
  * A unique pre-numbered document reference allocated at generation time.
  * The allocating user's name and the print timestamp on every page.
  * Whatever filters were active are printed on the document - a filtered
    download taken six months later must still be self-describing.
  * A4, portrait for the single-asset detail, landscape for the listing.
  * pdf_common.safe_text() on every piece of user text before Paragraph().
  * pdf_common.standard_table_style() for every table - no fourth inline
    variant (see CHANGE_IMPACT_GUIDE.md, "Reports / exports" row).
  * Amounts VAT-exclusive in the Amount column, VAT separate, total shown.
  * Service entries carry "—" in Amount/VAT, never "0.00" - a dash means
    "not a cost event"; zero means "cost nothing". Different facts.
  * Banner: "Fuel is tracked as a general operating cost, not against this
    asset" on every truck report, because the chip system fills trucks and
    those costs never reach expense_lines.

No FastAPI imports - see app/services/errors.py.
"""
import io
from datetime import date, datetime

from openpyxl import Workbook

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable,
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

from app.database import get_db, next_document_number
from app.excel_common import write_metadata_block, style_header_row
from app.pdf_common import (
    safe_text, money, document_header, footer, standard_table_style,
    HEADCELL, CELL, META, TITLE, SUBTITLE, TOTAL,
)
from app.config import CURRENCY_SYMBOL
from app.programs.assets.services.timeline import (
    asset_timeline, asset_tco, asset_summary,
    KIND_COST, KIND_SERVICE, KIND_REGISTRATION, KIND_CUSTODY,
)

__all__ = [
    "maintenance_rows",
    "filter_label",
    "allocate_document_number",
    "build_asset_history_workbook",
    "build_asset_history_pdf",
    "build_maintenance_listing_workbook",
    "build_maintenance_listing_pdf",
]

_styles = getSampleStyleSheet()
_SECTION = ParagraphStyle("SecHead", parent=_styles["Heading4"], spaceBefore=6, spaceAfter=3)
_BANNER  = ParagraphStyle("Banner", parent=_styles["Normal"], fontSize=8,
                          textColor=colors.HexColor("#92400E"),
                          backColor=colors.HexColor("#FEF3C7"), spaceAfter=6)
_FUEL_BANNER = (
    "⚠  Fuel is tracked as a general operating cost, not against this asset. "
    "Fuel costs filled via the chip system are not included in the TCO figure below."
)

DASH = "—"   # what "no amount" looks like in a table cell


def _fmt_amount(amount):
    return money(amount) if amount is not None else DASH


def _fmt_kind(kind):
    return {
        "registration": "📦 Registration",
        "cost":         "💰 Cost",
        "service":      "🔧 Service",
        "custody":      "📋 Custody",
    }.get(kind, kind.capitalize())


# --------------------------------------------------------------------------
# filter engine
# --------------------------------------------------------------------------
def maintenance_rows(*, date_from=None, date_to=None, asset_ids=None,
                     asset_category=None, cost_center_id=None,
                     holder_employee_id=None, logged_by_user_id=None,
                     kinds=None, service_types=None, include_pending=False):
    """Flat list of event rows across zero or more assets.

    When `asset_ids` is None, all assets are included (respecting the other
    filters). Each row is one of: a cost event, a service event, or a custody
    movement. Registration events are excluded from the multi-asset listing
    (they show in the single-asset view instead) since they would appear once
    per asset and inflate the row count for what is meant to be a maintenance
    log.

    `include_pending` adds pending voucher lines alongside approved ones.
    Off by default, and when on the document says so explicitly, because
    pending lines are not yet committed costs.
    """
    status_clause = "e.status = 'approved'"
    if include_pending:
        status_clause = "e.status IN ('approved','pending')"

    sql_cost = f"""
        SELECT
            el.amount,
            el.vat_amount,
            el.particulars  AS description,
            e.expense_date  AS event_date,
            e.voucher_no    AS reference,
            e.paid_to,
            e.status        AS voucher_status,
            c.name          AS category_name,
            s.name          AS supplier_name,
            u.full_name     AS recorded_by_name,
            a.id            AS asset_id,
            a.asset_tag,
            a.name          AS asset_name,
            a.category      AS asset_category,
            'cost'          AS kind
        FROM expense_lines el
        JOIN expenses  e  ON e.id  = el.expense_id
        JOIN assets    a  ON a.id  = el.asset_id
        JOIN categories c ON c.id  = el.category_id
        LEFT JOIN suppliers s ON s.id = el.supplier_id
        JOIN users      u  ON u.id  = e.prepared_by
        WHERE el.asset_id IS NOT NULL AND {status_clause}
    """
    params_cost = []

    sql_service = """
        SELECT
            NULL            AS amount,
            NULL            AS vat_amount,
            sl.description,
            sl.service_date AS event_date,
            CAST(sl.id AS TEXT) AS reference,
            NULL            AS paid_to,
            NULL            AS voucher_status,
            sl.service_type AS category_name,
            NULL            AS supplier_name,
            u.full_name     AS recorded_by_name,
            a.id            AS asset_id,
            a.asset_tag,
            a.name          AS asset_name,
            a.category      AS asset_category,
            'service'       AS kind
        FROM asset_service_log sl
        JOIN assets a ON a.id = sl.asset_id
        LEFT JOIN users u ON u.id = sl.recorded_by
        WHERE sl.status = 'recorded'
    """
    params_service = []

    # Apply shared filters to both branches
    if date_from:
        sql_cost     += " AND e.expense_date >= ?"
        sql_service  += " AND sl.service_date >= ?"
        params_cost.append(date_from); params_service.append(date_from)
    if date_to:
        sql_cost     += " AND e.expense_date <= ?"
        sql_service  += " AND sl.service_date <= ?"
        params_cost.append(date_to); params_service.append(date_to)
    if asset_ids:
        ph = ",".join("?" for _ in asset_ids)
        sql_cost    += f" AND el.asset_id IN ({ph})"
        sql_service += f" AND sl.asset_id IN ({ph})"
        params_cost.extend(asset_ids); params_service.extend(asset_ids)
    if asset_category:
        sql_cost    += " AND a.category = ?"
        sql_service += " AND a.category = ?"
        params_cost.append(asset_category); params_service.append(asset_category)
    if cost_center_id:
        sql_cost    += " AND a.cost_center_id = ?"
        sql_service += " AND a.cost_center_id = ?"
        params_cost.append(cost_center_id); params_service.append(cost_center_id)
    if holder_employee_id:
        exists = (" AND EXISTS (SELECT 1 FROM asset_assignments asg "
                  "WHERE asg.asset_id = a.id AND asg.status='active' "
                  "AND asg.employee_id = ?)")
        sql_cost    += exists; sql_service += exists
        params_cost.append(holder_employee_id)
        params_service.append(holder_employee_id)
    if logged_by_user_id:
        sql_service += " AND sl.recorded_by = ?"
        params_service.append(logged_by_user_id)
    if service_types:
        ph = ",".join("?" for _ in service_types)
        sql_service += f" AND sl.service_type IN ({ph})"
        params_service.extend(service_types)

    # Determine which kinds to include
    if kinds is None:
        kinds = {KIND_COST, KIND_SERVICE}
    else:
        kinds = set(kinds)

    rows = []
    with get_db() as conn:
        if KIND_COST in kinds:
            rows += [dict(r) for r in conn.execute(sql_cost, tuple(params_cost)).fetchall()]
        if KIND_SERVICE in kinds:
            rows += [dict(r) for r in conn.execute(sql_service, tuple(params_service)).fetchall()]

    rows.sort(key=lambda r: (r["event_date"], r["asset_tag"]), reverse=True)
    return rows


def filter_label(*, date_from=None, date_to=None, asset_tags=None,
                 asset_category=None, cost_center_name=None,
                 holder_name=None, logged_by_name=None,
                 include_pending=False):
    parts = []
    if date_from and date_to:
        parts.append(f"{date_from} to {date_to}")
    elif date_from:
        parts.append(f"from {date_from}")
    elif date_to:
        parts.append(f"to {date_to}")
    if asset_tags:
        parts.append("Assets: " + ", ".join(asset_tags))
    if asset_category:
        parts.append(f"Category: {asset_category}")
    if cost_center_name:
        parts.append(f"Cost centre: {cost_center_name}")
    if holder_name:
        parts.append(f"Holder: {holder_name}")
    if logged_by_name:
        parts.append(f"Logged by: {logged_by_name}")
    if include_pending:
        parts.append("INCLUDES PENDING (not yet approved)")
    return " · ".join(parts) if parts else "All records"


def allocate_document_number(doc_type):
    year = str(date.today().year)
    with get_db() as conn:
        return next_document_number(conn, doc_type, year)


# --------------------------------------------------------------------------
# single-asset Excel (ASR)
# --------------------------------------------------------------------------
def build_asset_history_workbook(asset_id, *, document_no, generated_by,
                                 date_from=None, date_to=None, kinds=None):
    summary = asset_summary(asset_id)
    events  = asset_timeline(asset_id, date_from=date_from, date_to=date_to, kinds=kinds)
    is_truck = summary and summary.get("category") == "Fleet/Truck"
    filters = filter_label(date_from=date_from, date_to=date_to,
                           asset_tags=[summary["asset_tag"]] if summary else [])

    wb = Workbook()
    ws = wb.active
    ws.title = "Asset History"
    header_row = write_metadata_block(
        ws, f"Asset History — {summary['asset_tag'] if summary else asset_id}",
        document_no, generated_by, filters,
    )
    if is_truck:
        ws.append([_FUEL_BANNER])

    tco = asset_tco(asset_id)
    ws.append(["Purchase cost", tco["purchase_cost"],
               "Maintenance total", tco["maintenance_total"],
               "TCO", tco["tco"]])
    ws.append([])

    col_headers = ["Date", "Kind", "Description", "Detail",
                   f"Amount (excl. VAT) ({CURRENCY_SYMBOL})",
                   f"VAT ({CURRENCY_SYMBOL})",
                   f"Total ({CURRENCY_SYMBOL})",
                   "Voucher / Ref", "Supplier", "By"]
    ws.append(col_headers)
    style_header_row(ws, ws.max_row)

    for ev in events:
        amt  = ev["amount"]
        vat  = ev["vat_amount"]
        total = (amt + vat) if (amt is not None and vat is not None) else amt
        ws.append([
            ev["event_date"], _fmt_kind(ev["kind"]), ev["description"],
            ev.get("detail") or "",
            amt if amt is not None else DASH,
            vat if vat is not None else DASH,
            total if total is not None else DASH,
            ev.get("reference") or "",
            ev.get("supplier_name") or "",
            ev.get("recorded_by_name") or "",
        ])

    ws.column_dimensions["A"].width = 12
    ws.column_dimensions["C"].width = 40
    ws.column_dimensions["D"].width = 30

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --------------------------------------------------------------------------
# single-asset PDF (ASR)
# --------------------------------------------------------------------------
def build_asset_history_pdf(asset_id, *, document_no, generated_by,
                             date_from=None, date_to=None, kinds=None):
    summary = asset_summary(asset_id)
    events  = asset_timeline(asset_id, date_from=date_from, date_to=date_to, kinds=kinds)
    is_truck = summary and summary.get("category") == "Fleet/Truck"
    filters = filter_label(date_from=date_from, date_to=date_to,
                           asset_tags=[summary["asset_tag"]] if summary else [])
    title = f"Asset History — {summary['asset_tag']}" if summary else "Asset History"

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, pageCompression=0,
                            topMargin=15*mm, bottomMargin=18*mm,
                            leftMargin=15*mm, rightMargin=15*mm)
    story = document_header(title, document_no, generated_by, filters)

    if is_truck:
        story.append(Paragraph(safe_text(_FUEL_BANNER), _BANNER))

    # TCO summary block
    if summary:
        tco = {"purchase_cost": summary["purchase_cost"],
               "maintenance_total": summary["maintenance_total"],
               "tco": summary["tco"]}
        holders = summary.get("current_holders", [])
        holder_str = ", ".join(
            f"{h.get('employee_name') or h.get('cost_center_name')})"
            f"{(' (' + h['role_note'] + ')') if h.get('role_note') else ''}"
            for h in holders
        ) or "Unassigned"
        tco_data = [
            [Paragraph("Purchase cost", HEADCELL), Paragraph("Maintenance (approved)", HEADCELL),
             Paragraph("TCO", HEADCELL), Paragraph("Current holder(s)", HEADCELL)],
            [Paragraph(money(tco["purchase_cost"]), CELL),
             Paragraph(money(tco["maintenance_total"]), CELL),
             Paragraph(money(tco["tco"]), CELL),
             Paragraph(safe_text(holder_str), CELL)],
        ]
        tco_table = Table(tco_data, colWidths=[38*mm, 50*mm, 38*mm, None])
        tco_table.setStyle(TableStyle(standard_table_style(zebra_end=0, valign="MIDDLE")))
        story.append(tco_table)
        story.append(Spacer(1, 4*mm))

    # Timeline table
    headers = ["Date", "Kind", "Description", "Detail",
               f"Amount", "VAT", "Total", "Ref", "By"]
    data = [[Paragraph(h, HEADCELL) for h in headers]]
    cost_total = 0.0
    for ev in events:
        amt = ev["amount"]; vat = ev["vat_amount"]
        total = (amt + vat) if (amt is not None and vat is not None) else None
        if amt is not None:
            cost_total += (amt + (vat or 0))
        data.append([
            Paragraph(safe_text(ev["event_date"]), CELL),
            Paragraph(safe_text(_fmt_kind(ev["kind"])), CELL),
            Paragraph(safe_text(ev["description"]), CELL),
            Paragraph(safe_text(ev.get("detail") or ""), CELL),
            Paragraph(_fmt_amount(amt), CELL),
            Paragraph(_fmt_amount(vat), CELL),
            Paragraph(_fmt_amount(total), CELL),
            Paragraph(safe_text(ev.get("reference") or ""), CELL),
            Paragraph(safe_text(ev.get("recorded_by_name") or ""), CELL),
        ])
    data.append([
        "", "", "", Paragraph("<b>Cost total</b>", CELL), "", "",
        Paragraph(f"<b>{money(cost_total)}</b>", CELL), "", "",
    ])

    table = Table(data, colWidths=[22*mm, 26*mm, 48*mm, 28*mm,
                                    20*mm, 18*mm, 20*mm, 16*mm, 24*mm],
                  repeatRows=1)
    table.setStyle(TableStyle(standard_table_style(
        zebra_end=-2, valign="TOP",
        extra=[("LINEBELOW", (0, -2), (-1, -2), 0.8, colors.grey)],
    )))
    story.append(table)
    if not events:
        story.append(Spacer(1, 6*mm))
        story.append(Paragraph("No events in the selected range.", META))

    doc.build(story,
              onFirstPage=footer(document_no, "Asset Program"),
              onLaterPages=footer(document_no, "Asset Program"))
    return buf.getvalue()


# --------------------------------------------------------------------------
# multi-asset Excel (AMR)
# --------------------------------------------------------------------------
def build_maintenance_listing_workbook(rows, *, document_no, generated_by,
                                       filters, include_pending=False):
    wb = Workbook()
    ws = wb.active
    ws.title = "Maintenance Listing"
    header_row = write_metadata_block(ws, "Asset Maintenance Report",
                                      document_no, generated_by, filters)
    if include_pending:
        ws.append(["⚠  INCLUDES PENDING VOUCHERS - amounts not yet approved"])

    col_headers = ["Date", "Asset", "Category", "Kind", "Description",
                   f"Amount ({CURRENCY_SYMBOL})", f"VAT ({CURRENCY_SYMBOL})",
                   f"Total ({CURRENCY_SYMBOL})", "Voucher", "Supplier", "By"]
    ws.append(col_headers)
    style_header_row(ws, ws.max_row)

    cost_total = vat_total = gross_total = 0.0
    svc_count = 0
    for r in rows:
        amt = r["amount"]; vat = r["vat_amount"]
        total = (amt + vat) if (amt is not None and vat is not None) else None
        if amt is not None:
            cost_total  += amt
            vat_total   += (vat or 0)
            gross_total += (amt + (vat or 0))
        else:
            svc_count += 1
        ws.append([
            r["event_date"], r["asset_tag"], r.get("category_name") or "",
            _fmt_kind(r["kind"]), r["description"],
            amt if amt is not None else DASH,
            vat if vat is not None else DASH,
            total if total is not None else DASH,
            r.get("reference") or "",
            r.get("supplier_name") or "",
            r.get("recorded_by_name") or "",
        ])

    ws.append([])
    ws.append(["", "", "", "", "Totals (approved cost lines only)",
               round(cost_total, 2), round(vat_total, 2), round(gross_total, 2),
               f"Service entries: {svc_count} (no cost)"])

    ws.column_dimensions["A"].width = 12
    ws.column_dimensions["D"].width = 36
    ws.column_dimensions["E"].width = 32

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --------------------------------------------------------------------------
# multi-asset PDF (AMR) - A4 landscape
# --------------------------------------------------------------------------
def build_maintenance_listing_pdf(rows, *, document_no, generated_by,
                                  filters, include_pending=False):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), pageCompression=0,
                            topMargin=15*mm, bottomMargin=18*mm,
                            leftMargin=15*mm, rightMargin=15*mm)
    story = document_header("Asset Maintenance Report",
                            document_no, generated_by, filters)

    if include_pending:
        story.append(Paragraph(
            "⚠  INCLUDES PENDING VOUCHERS — figures not yet approved by a manager.",
            _BANNER,
        ))

    headers = ["Date", "Asset", "Category", "Kind", "Description",
               "Amount", "VAT", "Total", "Voucher", "By"]
    data = [[Paragraph(h, HEADCELL) for h in headers]]
    cost_total = vat_total = gross_total = 0.0
    svc_count = 0
    for r in rows:
        amt = r["amount"]; vat = r["vat_amount"]
        total = (amt + vat) if (amt is not None and vat is not None) else None
        if amt is not None:
            cost_total += amt; vat_total += (vat or 0)
            gross_total += (amt + (vat or 0))
        else:
            svc_count += 1
        data.append([
            Paragraph(safe_text(r["event_date"]), CELL),
            Paragraph(safe_text(r["asset_tag"]), CELL),
            Paragraph(safe_text(r.get("category_name") or ""), CELL),
            Paragraph(safe_text(_fmt_kind(r["kind"])), CELL),
            Paragraph(safe_text(r["description"]), CELL),
            Paragraph(_fmt_amount(r["amount"]), CELL),
            Paragraph(_fmt_amount(r["vat_amount"]), CELL),
            Paragraph(_fmt_amount(total), CELL),
            Paragraph(safe_text(r.get("reference") or ""), CELL),
            Paragraph(safe_text(r.get("recorded_by_name") or ""), CELL),
        ])

    data.append([
        "", "", "", "",
        Paragraph("<b>Totals (approved)</b>", CELL),
        Paragraph(f"<b>{money(cost_total)}</b>", CELL),
        Paragraph(f"<b>{money(vat_total)}</b>", CELL),
        Paragraph(f"<b>{money(gross_total)}</b>", CELL),
        Paragraph(f"Service entries: {svc_count} (no cost)", CELL), "",
    ])

    table = Table(
        data,
        colWidths=[22*mm, 18*mm, 28*mm, 24*mm, 62*mm,
                   20*mm, 16*mm, 20*mm, 22*mm, 26*mm],
        repeatRows=1,
    )
    table.setStyle(TableStyle(standard_table_style(
        zebra_end=-2, valign="TOP",
        extra=[("LINEBELOW", (0, -2), (-1, -2), 0.8, colors.grey)],
    )))
    story.append(table)

    if not rows:
        story.append(Spacer(1, 6*mm))
        story.append(Paragraph("No maintenance events match the selected filters.", META))

    doc.build(story,
              onFirstPage=footer(document_no, "Asset Program"),
              onLaterPages=footer(document_no, "Asset Program"))
    return buf.getvalue()
