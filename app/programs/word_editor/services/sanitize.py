"""Allowlist sanitizer for Word Editor document HTML.

The editor is a contenteditable surface, so whatever the browser (or a
pasted Word document, or a crafted POST) sends is untrusted. Document
bodies are later rendered with `| safe`, which means an unsanitised
`<script>` would execute in every reader's session.

Allowlist, never blocklist: anything not explicitly permitted is dropped.
Pure stdlib - no FastAPI, no new dependency.
"""
import re
from html.parser import HTMLParser

__all__ = ["sanitize_html", "strip_tags", "ALLOWED_TAGS"]

ALLOWED_TAGS = {
    "p", "br", "div", "span", "b", "strong", "i", "em", "u", "s", "strike",
    "sub", "sup", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre",
    "code", "ul", "ol", "li", "table", "thead", "tbody", "tfoot", "tr",
    "td", "th", "caption", "img", "a", "hr", "font",
}

VOID_TAGS = {"br", "img", "hr"}

_GLOBAL_ATTRS = {"style", "class", "dir", "lang", "align",
                 "data-tag-type", "data-tag-id", "contenteditable",
                 # page-layout markers written by the editor and read back by
                 # the print paginator (orientation, margins, print scale,
                 # manual page breaks)
                 "data-doc-settings", "data-orient", "data-margin",
                 "data-scale", "data-page-break", "id"}
_TAG_ATTRS = {
    "a": {"href", "title", "target", "rel"},
    "img": {"src", "alt", "width", "height"},
    "td": {"colspan", "rowspan", "valign", "width"},
    "th": {"colspan", "rowspan", "valign", "width"},
    "table": {"border", "cellpadding", "cellspacing", "width"},
    "font": {"color", "face", "size"},
    "div": {"data-page-break"},
    "hr": {"data-page-break"},
}

# style: only presentational properties, and no url()/expression() payloads
_ALLOWED_CSS = {
    "color", "background-color", "background", "font-weight", "font-style",
    "font-size", "font-family", "text-align", "text-decoration",
    "border", "border-collapse", "border-color", "border-width",
    "border-style", "padding", "margin", "width", "height", "max-width",
    "max-height", "min-width", "vertical-align", "line-height",
    "list-style-type", "display", "float", "page-break-before",
    "page-break-after", "break-before", "break-after", "border-radius",
}
_CSS_BAD = re.compile(r"(url\s*\(|expression\s*\(|javascript:|@import|behavior\s*:)", re.I)

_SAFE_URL = re.compile(r"^(https?:|mailto:|tel:|/|#|data:image/(png|jpeg|jpg|gif|webp);base64,)", re.I)


def _clean_style(value: str) -> str:
    out = []
    for decl in value.split(";"):
        if ":" not in decl:
            continue
        prop, _, val = decl.partition(":")
        prop = prop.strip().lower()
        val = val.strip()
        if prop in _ALLOWED_CSS and val and not _CSS_BAD.search(val):
            out.append(f"{prop}:{val}")
    return ";".join(out)


def _clean_attr(tag: str, name: str, value) -> str | None:
    name = (name or "").lower()
    value = value or ""
    if name.startswith("on"):          # every inline event handler
        return None
    allowed = _GLOBAL_ATTRS | _TAG_ATTRS.get(tag, set())
    if name not in allowed:
        return None
    if name in ("src", "href"):
        if not _SAFE_URL.match(value.strip()):
            return None
    if name == "style":
        value = _clean_style(value)
        if not value:
            return None
    return value


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self._open = []
        self._skip_depth = 0          # inside <script>/<style>: drop text too

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in ("script", "style", "iframe", "object", "embed", "form"):
            self._skip_depth += 1
            return
        if tag not in ALLOWED_TAGS:
            return
        parts = [tag]
        for name, value in attrs:
            cleaned = _clean_attr(tag, name, value)
            if cleaned is None:
                continue
            safe = str(cleaned).replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
            parts.append(f'{name.lower()}="{safe}"')
        if tag in VOID_TAGS:
            self.out.append("<" + " ".join(parts) + ">")
        else:
            self.out.append("<" + " ".join(parts) + ">")
            self._open.append(tag)

    def handle_startendtag(self, tag, attrs):
        if tag.lower() in VOID_TAGS:
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("script", "style", "iframe", "object", "embed", "form"):
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag in VOID_TAGS or tag not in ALLOWED_TAGS:
            return
        if tag in self._open:
            while self._open:
                open_tag = self._open.pop()
                self.out.append(f"</{open_tag}>")
                if open_tag == tag:
                    break

    def handle_data(self, data):
        if self._skip_depth:
            return
        self.out.append(data.replace("<", "&lt;").replace(">", "&gt;"))

    def close_all(self) -> str:
        while self._open:
            self.out.append(f"</{self._open.pop()}>")
        return "".join(self.out)


def sanitize_html(html: str) -> str:
    """Return `html` with every disallowed tag, attribute, URL scheme and
    CSS declaration removed. Always returns a string, never raises."""
    if not html:
        return ""
    parser = _Sanitizer()
    parser.feed(str(html))
    parser.close()
    return parser.close_all()


_TAG_RE = re.compile(r"<[^>]+>")


def strip_tags(html: str) -> str:
    """Plain-text form of a document body - used for full-text search."""
    if not html:
        return ""
    text = _TAG_RE.sub(" ", str(html))
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    return " ".join(text.split())
