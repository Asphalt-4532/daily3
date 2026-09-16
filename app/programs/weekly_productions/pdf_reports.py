"""Generates Weekly Productions' report PDF: a line-level listing of
production reports, same shape and convention as Expense Program's own
transaction listing PDF. A4 landscape, using the shared document
conventions from app.pdf_common (pre-numbered header, footer, text
escaping) - see that module for anything not specific to this program.

Takes a `document_no` the caller is responsible for allocating via
app.database.next_document_number() before calling in - this module only
renders, it never touches the database.
"""
import io

from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer

from app.pdf_common import safe_text, HEADCELL, CELL, META, document_header, footer, standard_table_style

__all__ = ["build_production_listing_pdf"]


def build_production_listing_pdf(rows, *, document_no, generated_by, filters_description):
    """A4 landscape table of production report lines - same line-item
    granularity as the Excel export (export_rows() in production.py), one
    row per material line with header fields repeated."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=landscape(A4), pageCompression=0,
        topMargin=15 * mm, bottomMargin=18 * mm, leftMargin=15 * mm, rightMargin=15 * mm,
    )
    story = document_header("Weekly Production Report", document_no, generated_by, filters_description)

    headers = ["Report No", "Date", "Line", "Description", "Material", "Width", "Height",
               "Length", "Thickness", "Qty", "Weight (kg)", "Status"]
    data = [[Paragraph(h, HEADCELL) for h in headers]]
    grand_qty = 0.0
    grand_weight = 0.0
    for r in rows:
        data.append([
            Paragraph(safe_text(r["report_no"]), CELL), Paragraph(safe_text(r["report_date"]), CELL),
            Paragraph(safe_text(r["line_name"]), CELL), Paragraph(safe_text(r["description"]), CELL),
            Paragraph(safe_text(r["material_type"]), CELL), Paragraph(f"{r['width']:g}", CELL),
            Paragraph(f"{r['height']:g}", CELL), Paragraph(f"{r['length']:g}", CELL),
            Paragraph(f"{r['thickness']:g}", CELL), Paragraph(f"{r['quantity']:g}", CELL),
            Paragraph(f"{r['weight_kg']:.2f}", CELL),
            Paragraph(safe_text(str(r["status"]).capitalize()), CELL),
        ])
        grand_qty += r["quantity"]
        grand_weight += r["weight_kg"]
    data.append([
        "", "", "", "", "", "", "", "", Paragraph("<b>TOTAL</b>", CELL),
        Paragraph(f"<b>{grand_qty:g}</b>", CELL), Paragraph(f"<b>{grand_weight:.2f}</b>", CELL), "",
    ])

    table = Table(data, colWidths=[26 * mm, 18 * mm, 24 * mm, 44 * mm, 16 * mm, 15 * mm,
                                    15 * mm, 15 * mm, 17 * mm, 14 * mm, 20 * mm, 18 * mm],
                   repeatRows=1)
    table.setStyle(TableStyle(standard_table_style(
        zebra_end=-2, valign="MIDDLE",
        extra=[
            ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.white),
            ("LINEBELOW", (0, -2), (-1, -2), 0.8, colors.grey),
        ],
    )))
    story.append(table)

    if not rows:
        story.append(Spacer(1, 6 * mm))
        story.append(Paragraph("No production lines match the selected filters.", META))

    doc.build(story, onFirstPage=footer(document_no, "Weekly Productions"),
              onLaterPages=footer(document_no, "Weekly Productions"))
    return buf.getvalue()
