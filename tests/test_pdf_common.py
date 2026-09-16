from app.pdf_common import safe_text


def test_safe_text_escapes_angle_brackets():
    assert safe_text("<b>bold</b>") == "&lt;b&gt;bold&lt;/b&gt;"


def test_safe_text_escapes_ampersand():
    assert safe_text("Tea & Coffee") == "Tea &amp; Coffee"


def test_safe_text_handles_none():
    assert safe_text(None) == ""


def test_safe_text_handles_non_string_input():
    assert safe_text(123) == "123"
    assert safe_text(45.5) == "45.5"


def test_safe_text_neutralizes_unclosed_tag_that_would_otherwise_crash_reportlab():
    """This is the exact input that raises an unhandled ValueError deep
    inside reportlab if passed to Paragraph() unescaped - proving the fix
    actually prevents the crash, not just that escape() runs without error."""
    from reportlab.platypus import Paragraph
    from reportlab.lib.styles import getSampleStyleSheet
    styles = getSampleStyleSheet()

    malicious = "<b>unclosed tag test"
    Paragraph(safe_text(malicious), styles["Normal"])  # must not raise


def test_safe_text_prevents_silent_formatting_injection():
    from reportlab.platypus import Paragraph
    from reportlab.lib.styles import getSampleStyleSheet
    styles = getSampleStyleSheet()

    injected = "<font size=200>HUGE</font>"
    p = Paragraph(safe_text(injected), styles["Normal"])
    assert "&lt;font" in p.text
    assert "<font" not in p.text
