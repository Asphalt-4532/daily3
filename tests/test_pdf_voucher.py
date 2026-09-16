from app.programs.expenses.pdf_voucher import amount_to_words, build_voucher_pdf


def test_amount_to_words_zero():
    assert amount_to_words(0) == "Zero"


def test_amount_to_words_simple():
    assert amount_to_words(5) == "Five"
    assert amount_to_words(99) == "Ninety Nine"
    assert amount_to_words(100) == "One Hundred"


def test_amount_to_words_with_paise():
    assert amount_to_words(350.50) == "Three Hundred Fifty and 50/100"


def test_amount_to_words_thousands_and_lakhs():
    assert amount_to_words(1520) == "One Thousand Five Hundred Twenty"
    assert amount_to_words(100000) == "One Lakh"


def test_amount_to_words_large_with_paise():
    assert amount_to_words(1234567.25) == "Twelve Lakh Thirty Four Thousand Five Hundred Sixty Seven and 25/100"


def _line(category_name="Fuel", particulars="Fuel purchase", amount=100.0, is_vatable=False, vat_amount=0.0):
    return {"category_name": category_name, "particulars": particulars, "amount": amount,
            "is_vatable": is_vatable, "vat_amount": vat_amount}


def test_build_voucher_pdf_produces_valid_pdf_bytes():
    expense = {
        "voucher_no": "PCV-2026-00001", "expense_date": "2026-08-22", "status": "approved",
        "paid_to": "Test Vendor", "payment_mode": "Cash", "approved_at": "2026-08-22 10:00:00",
        "total_amount": 250.0, "total_vat": 0.0,
        "lines": [_line("Daily snacks & breakfast", "Snacks", 250.0)],
    }
    pdf_bytes = build_voucher_pdf(expense, "Factory Floor",
                                   "Test Preparer", "Test Approver", printed_by_name="Test Manager")
    assert pdf_bytes[:4] == b"%PDF"
    assert len(pdf_bytes) > 500  # a real rendered page, not an empty stub


def test_build_voucher_pdf_handles_pending_with_no_approver():
    expense = {
        "voucher_no": "PCV-2026-00002", "expense_date": "2026-08-22", "status": "pending",
        "paid_to": "Vendor", "payment_mode": "Cash", "approved_at": None,
        "total_amount": 1000.0, "total_vat": 0.0, "lines": [_line("Fuel", "Fuel", 1000.0)],
    }
    pdf_bytes = build_voucher_pdf(expense, None, "Preparer", None, printed_by_name="Some User")
    assert pdf_bytes[:4] == b"%PDF"


def test_build_voucher_pdf_renders_multiple_lines():
    """The core of this feature: a voucher with several transactions, some
    VAT-able and some not, must render every line and the VAT summary."""
    expense = {
        "voucher_no": "PCV-2026-00006", "expense_date": "2026-08-22", "status": "approved",
        "paid_to": "Multi Vendor", "payment_mode": "Cash", "approved_at": "2026-08-22 09:00:00",
        "total_amount": 155.0, "total_vat": 15.0,
        "lines": [
            _line("Fuel", "Vatable fuel", 115.0, is_vatable=True, vat_amount=15.0),
            _line("Water", "Exempt water", 40.0, is_vatable=False, vat_amount=0.0),
        ],
    }
    pdf_bytes = build_voucher_pdf(expense, "Transport", "Preparer", "Approver", printed_by_name="Tester")
    assert pdf_bytes[:4] == b"%PDF"
    text = pdf_bytes.decode("latin-1")
    assert "Vatable fuel" in text
    assert "Exempt water" in text


def test_voucher_pdf_page_size_is_a4():
    """A4 = 595.2756 x 841.8898 points - reportlab embeds the MediaBox in the
    raw PDF bytes, so this checks the actual rendered page, not just which
    constant was imported."""
    expense = {
        "voucher_no": "PCV-2026-00003", "expense_date": "2026-08-22", "status": "approved",
        "paid_to": "Vendor", "payment_mode": "Cash", "approved_at": "2026-08-22 09:00:00",
        "total_amount": 50.0, "total_vat": 0.0, "lines": [_line("Water", "Water", 50.0)],
    }
    pdf_bytes = build_voucher_pdf(expense, "Factory Floor", "Preparer", "Approver",
                                   printed_by_name="Tester")
    text = pdf_bytes.decode("latin-1")
    assert "595.27" in text and "841.88" in text, "expected an A4 MediaBox in the PDF"


def test_voucher_pdf_shows_who_printed_it_and_when():
    expense = {
        "voucher_no": "PCV-2026-00004", "expense_date": "2026-08-22", "status": "approved",
        "paid_to": "Vendor", "payment_mode": "Cash", "approved_at": "2026-08-22 09:00:00",
        "total_amount": 75.0, "total_vat": 0.0, "lines": [_line("Fuel", "Fuel", 75.0)],
    }
    pdf_bytes = build_voucher_pdf(expense, "Transport", "Preparer", "Approver",
                                   printed_by_name="Priya Sharma")
    text = pdf_bytes.decode("latin-1")
    assert "Priya Sharma" in text
    assert "Printed by" in text
    assert "PCV-2026-00004" in text  # the voucher's own pre-assigned number


def test_voucher_pdf_survives_markup_injection_in_free_text_fields():
    """particulars and paid_to are freely typed by the lowest-privilege
    role. An unclosed tag here used to raise an unhandled ValueError deep
    inside reportlab - this must now render as literal, harmless text."""
    expense = {
        "voucher_no": "PCV-2026-00005", "expense_date": "2026-08-22", "status": "pending",
        "paid_to": "<b>unclosed tag vendor", "payment_mode": "Cash", "approved_at": None,
        "total_amount": 42.0, "total_vat": 0.0,
        "lines": [_line("Fuel", "<font size=999>injected</font>", 42.0)],
    }
    pdf_bytes = build_voucher_pdf(expense, "Transport", "<b>unclosed preparer",
                                   None, printed_by_name="Manager Account")
    assert pdf_bytes[:4] == b"%PDF"
