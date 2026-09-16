"""Word Editor export service.

Two exports, both reusing shared infrastructure rather than reinventing it:
- `build_export_html()` - a single document as a Word-openable HTML file
  (`.doc`), letterhead included. No new dependency: Word opens HTML natively,
  and the body is already HTML, so nothing is lost in translation.
- `build_register_workbook()` - the document register as .xlsx, built on
  `app/excel_common.py` exactly like the Expense Program's exports.

Sanitised at the boundary on save (`sanitize.py`), so the stored body is
already safe to embed here.
"""
import io

from openpyxl import Workbook

from app.excel_common import write_metadata_block, style_header_row
from app.programs.word_editor.services.sanitize import sanitize_html

__all__ = ["build_export_html", "build_register_workbook",
           "export_allowed", "EDITABLE_FORMATS", "OFFICIAL_EXPORT_MESSAGE"]

# Formats that hand back an editable copy of the document body. `.doc` here
# is Word-openable HTML (see build_export_html above) - Word opens it, and
# more to the point Word *edits* it, which is the whole point of the rule
# below.
EDITABLE_FORMATS = ("doc", "html")

OFFICIAL_EXPORT_MESSAGE = (
    "Official documents are issued as PDF only. Use Print / PDF - an "
    "editable copy would no longer match the official record."
)

_STATUS_LABEL = {
    "draft": "Draft",
    "self_approved": "Self-approved",
    "official": "Official",
    "deleted": "Deleted",
}


def export_allowed(doc, fmt: str) -> bool:
    """Whether `doc` may be downloaded in `fmt`.

    Once a document is **official** it is a issued record: it carries a
    document number, a verification page and a content hash
    (wd_documents.official_sha), and anyone holding it can check that what
    they have is what was issued. An editable `.doc` of that same document
    defeats all three at once - it opens in Word, it can be changed in
    thirty seconds, and it still carries the official letterhead and number
    while no longer matching the hash anyone would verify against. PDF (via
    the print view) keeps the document portable without handing out an
    editable forgery kit.

    Draft and self-approved documents are unaffected: they are working
    copies, and exporting one to keep editing is exactly what the format is
    for.

    Lives in the service layer rather than the route so the rule is
    testable without a server and cannot be bypassed by a second caller -
    the route asks this function, it does not re-implement the check.
    """
    if fmt not in EDITABLE_FORMATS:
        return False
    if doc is None:
        return False        # no document, nothing to allow - fail closed
    return doc["status"] != "official"


def _esc(value) -> str:
    text = "" if value is None else str(value)
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))


def build_export_html(doc, profile) -> str:
    """One document as standalone HTML that Word opens with formatting intact."""
    profile = profile or {}
    body = sanitize_html(doc["content_json"])
    rows = [
        ("Doc No", doc["doc_no"] or "—"),
        ("Subject", doc["subject"] or doc["title"]),
        ("To", doc["receiver_name"] or "—"),
        ("Status", _STATUS_LABEL.get(doc["status"], doc["status"])),
        ("Prepared by", doc.get("creator_name", "")),
    ]
    meta = "".join(
        f"<tr><td style='padding:2px 10px 2px 0'><b>{_esc(k)}</b></td>"
        f"<td style='padding:2px 0'>{_esc(v)}</td></tr>" for k, v in rows
    )
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>{_esc(doc['doc_no'] or doc['subject'])}</title>
<style>
 @page{{size:A4 portrait;margin:15mm 12mm;}}
 body{{font-family:"Times New Roman",serif;font-size:11pt;}}
 .lh{{border-bottom:1.5pt solid #0f1e4d;padding-bottom:4pt;margin-bottom:8pt;}}
 .lh .co{{font-size:13pt;font-weight:bold;}}
 .meta{{font-size:9pt;color:#444;margin-bottom:10pt;}}
 .disc{{font-size:8pt;font-style:italic;color:#444;margin-top:18pt;
        border-top:0.75pt solid #ccc;padding-top:3pt;}}
 table{{border-collapse:collapse;}}
</style></head><body>
<div class="lh">
  <div class="co">{_esc(profile.get('company_name'))}</div>
  <div style="font-size:9pt;color:#555;">{_esc(profile.get('document_location'))}
    &nbsp;|&nbsp; {_esc(profile.get('phone'))}</div>
</div>
<table class="meta">{meta}</table>
<div>{body}</div>
<div class="disc">This document does not constitute a purchase agreement, legally
 binding contract, or commitment of any kind unless physically signed, stamped,
 and verified by the relevant Chamber of Commerce.</div>
<div style="font-size:8pt;color:#555;margin-top:4pt;">
 {_esc(profile.get('company_location'))} &nbsp;|&nbsp;
 {_esc(profile.get('company_phone'))} &nbsp;|&nbsp;
 {_esc(profile.get('website'))}</div>
</body></html>"""


def build_register_workbook(docs, profile, *, generated_by: str = "") -> bytes:
    """The filtered document register as .xlsx bytes."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Documents"
    header_row = write_metadata_block(
        ws,
        title="Word Editor — document register",
        document_no=(profile or {}).get("company_name", "") or "—",
        generated_by=generated_by,
        filters_description=f"{len(docs)} document(s)",
    )

    headers = ["Doc No", "Subject", "To", "Status", "Created by",
               "Created", "Last edited", "Comments"]
    for col, name in enumerate(headers, start=1):
        ws.cell(row=header_row, column=col, value=name)
    style_header_row(ws, header_row)

    for i, d in enumerate(docs, start=header_row + 1):
        ws.cell(row=i, column=1, value=d.get("doc_no") or "—")
        ws.cell(row=i, column=2, value=d.get("subject") or d.get("title"))
        ws.cell(row=i, column=3, value=d.get("receiver_name") or "—")
        ws.cell(row=i, column=4, value=_STATUS_LABEL.get(d.get("status"), d.get("status")))
        ws.cell(row=i, column=5, value=d.get("creator_name"))
        ws.cell(row=i, column=6, value=(d.get("created_at") or "")[:16])
        ws.cell(row=i, column=7, value=(d.get("last_edited_at") or "")[:16])
        ws.cell(row=i, column=8, value=d.get("comment_count") or 0)

    widths = [16, 42, 22, 15, 20, 18, 18, 11]
    for col, width in enumerate(widths, start=1):
        ws.column_dimensions[ws.cell(row=header_row, column=col).column_letter].width = width

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
