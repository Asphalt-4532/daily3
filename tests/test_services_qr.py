"""Word Editor document QR service tests.

Covers the happy path (payload shape, PNG bytes), invalid page input, and
the delimiter-injection failure case.
"""
import pytest

from app.programs.word_editor.services import qr as qr_svc
from app.services.errors import ValidationError


DOC = {
    "id": 3,
    "doc_no": "WD-2026-00003",
    "subject": "Quotation for steel sections",
    "title": "Untitled",
}
PROFILE = {
    "company_name": "Makabes Al Sharq",
    "company_phone": "0554480865",
    "phone": "0554480000",
    "website": "www.makabesalsharq.com",
}


class TestValidatePage:
    def test_accepts_valid_pair(self):
        assert qr_svc.validate_page("2", "5") == (2, 5)

    @pytest.mark.parametrize("p,n", [("0", "3"), ("4", "3"), ("-1", "3"), ("1", "0")])
    def test_rejects_out_of_range(self, p, n):
        with pytest.raises(ValidationError):
            qr_svc.validate_page(p, n)

    def test_rejects_non_numeric(self):
        with pytest.raises(ValidationError):
            qr_svc.validate_page("two", "5")

    def test_rejects_absurd_page_count(self):
        with pytest.raises(ValidationError):
            qr_svc.validate_page("1", str(qr_svc.MAX_PAGES + 1))


class TestBuildPayload:
    def test_contains_every_required_field(self):
        out = qr_svc.build_payload(DOC, PROFILE, page=2, pages=5,
                                   base_url="http://box:8000/")
        assert "DOC:WD-2026-00003" in out
        assert "SUBJ:Quotation for steel sections" in out
        assert "CO:Makabes Al Sharq" in out
        assert "TEL:0554480865" in out
        assert "WEB:www.makabesalsharq.com" in out
        assert "PG:2/5" in out
        assert "URL:http://box:8000/word-editor/verify/WD-2026-00003" in out

    def test_page_marker_differs_per_page(self):
        a = qr_svc.build_payload(DOC, PROFILE, page=1, pages=3)
        b = qr_svc.build_payload(DOC, PROFILE, page=3, pages=3)
        assert a != b
        assert "PG:1/3" in a and "PG:3/3" in b

    def test_falls_back_to_title_when_no_subject(self):
        doc = dict(DOC, subject=None)
        assert "SUBJ:Untitled" in qr_svc.build_payload(doc, PROFILE, page=1, pages=1)

    def test_pipe_and_newline_in_a_field_cannot_break_the_payload(self):
        doc = dict(DOC, subject="steel | rebar\nsecond line")
        out = qr_svc.build_payload(doc, PROFILE, page=1, pages=1)
        assert out.count("|") == len(out.split("|")) - 1
        assert "\n" not in out
        assert "SUBJ:steel / rebar second line" in out

    def test_missing_profile_does_not_crash(self):
        out = qr_svc.build_payload(DOC, None, page=1, pages=1)
        assert "CO:" in out and "PG:1/1" in out

    def test_invalid_page_rejected_here_too(self):
        with pytest.raises(ValidationError):
            qr_svc.build_payload(DOC, PROFILE, page=9, pages=2)


class TestRenderPng:
    def test_returns_png_bytes(self):
        png = qr_svc.render_png(qr_svc.build_payload(DOC, PROFILE, page=1, pages=1))
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        assert len(png) > 100

    def test_empty_payload_rejected(self):
        with pytest.raises(ValidationError):
            qr_svc.render_png("   ")
