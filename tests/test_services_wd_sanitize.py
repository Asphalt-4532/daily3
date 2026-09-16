"""Word Editor HTML sanitizer tests.

Document bodies are rendered with `| safe`, so anything that survives this
function executes in a reader's browser. Covers the happy path (formatting
is preserved), the failure paths (script/handler/URL scheme/CSS payloads),
and malformed input.
"""
import pytest

from app.programs.word_editor.services.sanitize import sanitize_html, strip_tags


class TestKeepsLegitimateFormatting:
    @pytest.mark.parametrize("html", [
        "<p>Hello <b>world</b></p>",
        "<h2>Heading</h2><ul><li>one</li><li>two</li></ul>",
        '<table border="1"><tbody><tr><td>a</td></tr></tbody></table>',
        '<span style="color:#dc2626;font-weight:700;">red</span>',
        '<a href="https://example.com">link</a>',
        '<a href="mailto:a@b.com">mail</a>',
    ])
    def test_survives(self, html):
        out = sanitize_html(html)
        assert out.strip() != ""
        assert "&lt;p&gt;" not in out

    def test_mention_tag_attributes_survive(self):
        out = sanitize_html(
            '<span class="wd-tag-user" data-tag-type="user" data-tag-id="3">@Ali</span>')
        assert 'data-tag-type="user"' in out and 'data-tag-id="3"' in out

    def test_base64_image_survives(self):
        out = sanitize_html('<img src="data:image/png;base64,iVBORw0KGgo=" alt="x">')
        assert "data:image/png;base64" in out


class TestStripsDangerousContent:
    def test_script_tag_and_its_text_removed(self):
        out = sanitize_html("<p>ok</p><script>alert(1)</script>")
        assert "script" not in out.lower()
        assert "alert(1)" not in out

    def test_inline_event_handler_removed(self):
        out = sanitize_html('<p onclick="steal()">text</p>')
        assert "onclick" not in out.lower()
        assert "text" in out

    def test_javascript_url_removed(self):
        out = sanitize_html('<a href="javascript:alert(1)">x</a>')
        assert "javascript:" not in out.lower()

    def test_iframe_and_form_removed(self):
        out = sanitize_html('<iframe src="https://evil"></iframe><form></form><p>a</p>')
        assert "iframe" not in out.lower() and "<form" not in out.lower()
        assert "<p>a</p>" in out

    def test_css_url_and_expression_removed(self):
        out = sanitize_html('<div style="background:url(javascript:1);color:red">x</div>')
        assert "url(" not in out and "javascript" not in out.lower()
        assert "color:red" in out

    def test_svg_onload_removed(self):
        out = sanitize_html('<svg onload="alert(1)"><p>hi</p></svg>')
        assert "svg" not in out.lower() and "onload" not in out.lower()

    def test_unknown_tag_dropped_but_text_kept(self):
        out = sanitize_html("<marquee>scroll</marquee>")
        assert "marquee" not in out.lower() and "scroll" in out


class TestEdgeCases:
    @pytest.mark.parametrize("value", ["", None, "   "])
    def test_empty_input_is_safe(self, value):
        assert sanitize_html(value) in ("", "   ")

    def test_unclosed_tags_are_closed(self):
        out = sanitize_html("<p><b>bold")
        assert out.count("<b>") == out.count("</b>")
        assert out.count("<p>") == out.count("</p>")

    def test_double_sanitizing_is_stable(self):
        once = sanitize_html('<p style="color:red">x</p>')
        assert sanitize_html(once) == once


class TestStripTags:
    def test_returns_plain_text(self):
        assert strip_tags("<p>Hello <b>big</b> world</p>") == "Hello big world"

    def test_handles_empty(self):
        assert strip_tags("") == ""
