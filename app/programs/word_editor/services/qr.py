"""Word Editor document QR service.

Pure business logic - no FastAPI imports, no HTTP concepts. Builds the
payload string encoded into a document's QR code and renders it to PNG
bytes.

One QR is generated *per printed page*, so the payload carries `PG:i/n`
and the reader can tell which page of which document they are holding.
Page count is only known by the client-side paginator in
`document_print.html`, which is why page/pages arrive as parameters here
rather than being derived.
"""
import io

from app.services.errors import ValidationError

__all__ = ["MAX_PAGES", "build_payload", "validate_page", "render_png"]

MAX_PAGES = 500

_FIELD_MAX = 120


def _clean(value) -> str:
    """Collapse a field to a single safe line - the payload is pipe/colon
    delimited, so an embedded delimiter or newline would corrupt parsing."""
    text = "" if value is None else str(value)
    text = text.replace("|", "/").replace("\r", " ").replace("\n", " ")
    text = " ".join(text.split())
    return text[:_FIELD_MAX]


def validate_page(page, pages) -> tuple:
    """Validate the page/pages pair at the boundary. Returns (page, pages)
    as ints or raises ValidationError - never a raw ValueError."""
    try:
        page_i = int(page)
        pages_i = int(pages)
    except (TypeError, ValueError):
        raise ValidationError("Page numbers must be whole numbers.")
    if pages_i < 1 or pages_i > MAX_PAGES:
        raise ValidationError(f"Page count must be between 1 and {MAX_PAGES}.")
    if page_i < 1 or page_i > pages_i:
        raise ValidationError("Page number must be within the page count.")
    return page_i, pages_i


def build_payload(doc, profile, *, page, pages, base_url="") -> str:
    """Build the scannable payload for one page of one document."""
    page, pages = validate_page(page, pages)
    profile = profile or {}
    base = _clean(base_url).rstrip("/")
    parts = [
        f"DOC:{_clean(doc['doc_no'])}",
        f"SUBJ:{_clean(doc.get('subject') or doc.get('title'))}",
        f"CO:{_clean(profile.get('company_name'))}",
        f"TEL:{_clean(profile.get('company_phone') or profile.get('phone'))}",
        f"WEB:{_clean(profile.get('website'))}",
        f"PG:{page}/{pages}",
    ]
    if base:
        # Public, login-free verification page - a QR is only useful to
        # someone holding the paper, who has no account here.
        parts.append(f"URL:{base}/word-editor/verify/{_clean(doc['doc_no'])}")
    return "|".join(parts)


def render_png(payload: str, *, box_size: int = 6, border: int = 2) -> bytes:
    """Render a payload to PNG bytes. Raises ValidationError on empty input."""
    if not payload or not payload.strip():
        raise ValidationError("QR payload cannot be empty.")
    import qrcode

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
