"""Tests for app/programs/weekly_productions/services/production.py, called
directly - no HTTP involved. See tests/test_services_expenses.py's docstring
for why business-logic tests are written this way.

A line is entered as one free-text box: a description followed by a
dimensions token (widthxheightxlengthxthickness). Straight pieces get a
linear weight formula; elbows/fittings (empty length slot, angle named in
the description) get an area-based one. See production.py's module
docstring and parse_line_input()'s docstring for the exact rules.
"""
import math
import pytest
from datetime import date

from app.programs.weekly_productions.services import production as svc
from app.services.errors import ValidationError, NotFoundError


def _make_report(**overrides):
    lines = overrides.pop("lines", None)
    if lines is None:
        lines = [{
            "material_type": overrides.pop("material_type", "GI"),
            "raw": overrides.pop("raw", "cable tray 100x50x2.44mx0.7"),
            "quantity": overrides.pop("quantity", 20),
        }]
    kwargs = dict(
        report_date=date.today().isoformat(), line_name="Line 1", shift="Morning",
        notes="", recorded_by=1, lines=lines,
    )
    kwargs.update(overrides)
    return svc.create_report(**kwargs)


# --- parse_line_input() unit tests, straight pieces -----------------------

def test_parse_straight_line_splits_description_from_dims():
    factors = {"GI": 8.1, "HDG": 7.85}
    result = svc.parse_line_input("cable tray 100x50x2.44mx0.7", "GI", 20, factors)
    assert result["description"] == "cable tray"
    assert result["width"] == 100
    assert result["height"] == 50
    assert result["length"] == 2.44
    assert result["thickness"] == 0.7


def test_parse_straight_line_multi_word_description():
    factors = {"GI": 8.1, "HDG": 7.85}
    result = svc.parse_line_input("cable trunking system 100x50x2.44mx0.7", "GI", 1, factors)
    assert result["description"] == "cable trunking system"


def test_parse_straight_line_length_and_thickness_order_independent():
    factors = {"GI": 8.1, "HDG": 7.85}
    a = svc.parse_line_input("x 100x50x2.44mx0.7mm", "GI", 1, factors)
    b = svc.parse_line_input("x 100x50x0.7mmx2.44m", "GI", 1, factors)
    assert a["length"] == b["length"] == 2.44
    assert a["thickness"] == b["thickness"] == 0.7


def test_parse_straight_line_thickness_without_mm_suffix():
    factors = {"GI": 8.1, "HDG": 7.85}
    result = svc.parse_line_input("x 100x50x2.44mx0.7", "GI", 1, factors)
    assert result["thickness"] == 0.7
    assert result["length"] == 2.44


def test_parse_straight_line_rejects_ambiguous_last_two_tokens():
    factors = {"GI": 8.1, "HDG": 7.85}
    with pytest.raises(ValidationError):
        svc.parse_line_input("x 100x50x2.44mx0.7m", "GI", 1, factors)  # both end in m


def test_parse_straight_line_rejects_missing_length():
    factors = {"GI": 8.1, "HDG": 7.85}
    with pytest.raises(ValidationError):
        svc.parse_line_input("x 100x50x2.44x0.7", "GI", 1, factors)  # neither ends in m


def test_parse_straight_line_thickness_out_of_range():
    factors = {"GI": 8.1, "HDG": 7.85}
    with pytest.raises(ValidationError):
        svc.parse_line_input("x 100x50x2.44mx5", "GI", 1, factors)  # 5mm > 4mm cap


def test_parse_straight_line_rejects_zero_thickness():
    factors = {"GI": 8.1, "HDG": 7.85}
    with pytest.raises(ValidationError):
        svc.parse_line_input("x 100x50x2.44mx0", "GI", 1, factors)


def test_parse_straight_line_rejects_negative_length():
    factors = {"GI": 8.1, "HDG": 7.85}
    with pytest.raises(ValidationError):
        svc.parse_line_input("x 100x50x-1mx0.7", "GI", 1, factors)


def test_parse_straight_line_width_reducer_takes_first_number():
    factors = {"GI": 8.1, "HDG": 7.85}
    result = svc.parse_line_input("reducer 100-150x50x2.44mx0.7", "GI", 1, factors)
    assert result["width"] == 100


def test_parse_straight_line_rejects_missing_dims():
    factors = {"GI": 8.1, "HDG": 7.85}
    with pytest.raises(ValidationError):
        svc.parse_line_input("cable tray", "GI", 1, factors)


def test_parse_straight_line_rejects_wrong_part_count():
    factors = {"GI": 8.1, "HDG": 7.85}
    with pytest.raises(ValidationError):
        svc.parse_line_input("cable tray 100x50x2.44m", "GI", 1, factors)


def test_straight_weight_matches_hand_calculation():
    factors = {"GI": 8.1, "HDG": 7.85}
    result = svc.parse_line_input("cable tray 100x50x2.44mx0.7", "GI", 20, factors)
    per_piece = ((100 + 2 * 50 + 30) / 1000) * 2.44 * 0.7 * 8.1
    assert result["weight_kg"] == pytest.approx(per_piece * 20, rel=1e-6)


# --- parse_line_input() unit tests, elbows/fittings ------------------------

def test_parse_elbow_line_requires_angle_in_description():
    factors = {"GI": 8.1, "HDG": 7.85}
    with pytest.raises(ValidationError):
        svc.parse_line_input("fitting elbow 200x50xx0.7mm", "GI", 1, factors)


def test_parse_elbow_line_reads_angle_and_defaults_r_in():
    factors = {"GI": 8.1, "HDG": 7.85}
    result = svc.parse_line_input("fitting 90 degree elbow 200x50xx0.7", "GI", 1, factors)
    assert result["description"] == "fitting 90 degree elbow"
    # R_in defaults to 100mm, R_out = 100+200=300mm, length = R_out*sin(90) = 300mm = 0.3m
    assert result["length"] == pytest.approx(0.3, rel=1e-6)


def test_parse_elbow_line_width_takes_first_number_of_dash_series():
    factors = {"GI": 8.1, "HDG": 7.85}
    result = svc.parse_line_input(
        "fitting 90 degree elbow 200-100-200x50xx0.7", "GI", 1, factors,
    )
    assert result["width"] == 200


def test_elbow_weight_matches_hand_calculation_90_degrees():
    factors = {"GI": 8.1, "HDG": 7.85}
    result = svc.parse_line_input("fitting 90 degree elbow 200x50xx0.7", "HDG", 1, factors)
    r_in, r_out, h, t = 0.1, 0.3, 0.05, 0.0007
    frac = 90 / 360
    area = (frac * math.pi * (r_out**2 - r_in**2)
            + frac * 2 * math.pi * r_in * h
            + frac * 2 * math.pi * r_out * h)
    expected = area * t * 7850
    assert result["weight_kg"] == pytest.approx(expected, rel=1e-4)
    assert result["weight_kg"] == pytest.approx(0.518, abs=0.01)


def test_elbow_weight_scales_with_quantity():
    factors = {"GI": 8.1, "HDG": 7.85}
    one = svc.parse_line_input("fitting 90 degree elbow 200x50xx0.7", "HDG", 1, factors)
    five = svc.parse_line_input("fitting 90 degree elbow 200x50xx0.7", "HDG", 5, factors)
    assert five["weight_kg"] == pytest.approx(one["weight_kg"] * 5, rel=1e-6)


def test_parse_elbow_line_accepts_45_degrees():
    factors = {"GI": 8.1, "HDG": 7.85}
    result = svc.parse_line_input("fitting 45 degree elbow 200x50xx0.7", "GI", 1, factors)
    assert result["length"] == pytest.approx(0.3 * math.sin(math.radians(45)), rel=1e-6)


def test_reducer_uses_elbow_formula_at_fixed_90_degrees_no_angle_text_needed():
    factors = {"GI": 8.1, "HDG": 7.85}
    reducer = svc.parse_line_input("reducer 200x50xx0.7", "GI", 1, factors)
    elbow_90 = svc.parse_line_input("fitting 90 degree elbow 200x50xx0.7", "GI", 1, factors)
    assert reducer["length"] == pytest.approx(elbow_90["length"], rel=1e-6)
    assert reducer["weight_kg"] == pytest.approx(elbow_90["weight_kg"], rel=1e-6)


def test_reducer_explicit_angle_overrides_the_90_degree_default():
    factors = {"GI": 8.1, "HDG": 7.85}
    reducer_45 = svc.parse_line_input("reducer 45 degree 200x50xx0.7", "GI", 1, factors)
    elbow_45 = svc.parse_line_input("fitting 45 degree elbow 200x50xx0.7", "GI", 1, factors)
    assert reducer_45["length"] == pytest.approx(elbow_45["length"], rel=1e-6)


# --- parse_line_input() unit tests, tees -----------------------------------

def test_parse_tee_line_requires_no_angle():
    factors = {"GI": 8.1, "HDG": 7.85}
    result = svc.parse_line_input("cable tray tee 200-200-200x100xx0.7mm", "GI", 1, factors)
    assert result["description"] == "cable tray tee"


def test_cross_uses_the_same_formula_as_tee():
    factors = {"GI": 8.1, "HDG": 7.85}
    tee = svc.parse_line_input("cable tray tee 200-200-200x100xx0.7mm", "GI", 1, factors)
    cross = svc.parse_line_input("cable tray cross 200-200-200x100xx0.7mm", "GI", 1, factors)
    assert cross["weight_kg"] == pytest.approx(tee["weight_kg"], rel=1e-6)
    assert cross["description"] == "cable tray cross"


def test_tee_envelope_dimensions_match_hand_calculation():
    factors = {"GI": 8.1, "HDG": 7.85}
    result = svc.parse_line_input("cable tray tee 200-200-200x100xx0.7mm", "GI", 1, factors)
    # X = W + 2*R_in = 200+200=400mm; Y = 2*W + 2*R_in = 400+200=600mm (0.6m)
    assert result["width"] == pytest.approx(400, rel=1e-6)
    assert result["length"] == pytest.approx(0.6, rel=1e-6)


def test_tee_width_takes_first_number_of_dash_series():
    factors = {"GI": 8.1, "HDG": 7.85}
    result = svc.parse_line_input("cable tray tee 200-150-100x100xx0.7mm", "GI", 1, factors)
    # W (before envelope math) is the first number, 200 - envelope uses that, not 150 or 100
    assert result["width"] == pytest.approx(200 + 2 * 100, rel=1e-6)  # X = W + 2*R_in


def test_tee_weight_matches_hand_calculation_bottom_plus_side():
    factors = {"GI": 8.1, "HDG": 7.85}
    result = svc.parse_line_input("cable tray tee 200-200-200x100xx0.7mm", "GI", 1, factors)
    w, y, r_in, h, t = 0.2, 0.6, 0.1, 0.1, 0.0007
    net_base_area = (w * y) + (w * r_in) - (2 * (r_in**2 - (0.7854 * r_in**2)))
    straight_back_wall = y * h
    curved_inner_walls = 2 * (1.5708 * r_in * h)
    total_side_area = straight_back_wall + curved_inner_walls
    density = 8.1 * 1000
    expected_bottom = net_base_area * t * density
    expected_side = total_side_area * t * density
    # bottom + side aren't exposed separately - only their sum, stored as weight_kg
    assert result["weight_kg"] == pytest.approx(expected_bottom + expected_side, rel=1e-4)
    # sanity check against the worked example in the spec (~0.75kg bottom, ~0.50kg side -> ~1.25kg,
    # using GI's factor here rather than the spec's flat 7850 density, so a bit higher)
    assert result["weight_kg"] == pytest.approx(1.29, abs=0.02)


def test_tee_weight_scales_with_quantity():
    factors = {"GI": 8.1, "HDG": 7.85}
    one = svc.parse_line_input("cable tray tee 200-200-200x100xx0.7mm", "GI", 1, factors)
    four = svc.parse_line_input("cable tray tee 200-200-200x100xx0.7mm", "GI", 4, factors)
    assert four["weight_kg"] == pytest.approx(one["weight_kg"] * 4, rel=1e-6)


def test_empty_length_slot_without_angle_or_tee_keyword_raises_helpful_error():
    factors = {"GI": 8.1, "HDG": 7.85}
    with pytest.raises(ValidationError, match="elbow, reducer, tee, or cross"):
        svc.parse_line_input("fitting 200x50xx0.7", "GI", 1, factors)


def test_create_report_persists_tee_total_weight():
    rid, _no = _make_report(raw="cable tray tee 200-200-200x100xx0.7mm")
    report = svc.get_report(rid)
    assert report["lines"][0]["weight_kg"] > 0


# --- create_report()/get_report() integration with the new line shape -----

def test_create_report_persists_description_and_weight():
    rid, _no = _make_report()
    report = svc.get_report(rid)
    assert report["lines"][0]["description"] == "cable tray"
    assert report["lines"][0]["weight_kg"] > 0


def test_create_report_with_elbow_and_straight_lines():
    rid, _no = _make_report(lines=[
        {"material_type": "GI", "raw": "cable tray 100x50x2.44mx0.7", "quantity": 10},
        {"material_type": "HDG", "raw": "fitting 90 degree elbow 200x50xx0.7", "quantity": 2},
    ])
    report = svc.get_report(rid)
    assert len(report["lines"]) == 2
    assert report["total_weight"] > 0


def test_create_report_rejects_bad_line_with_line_number_context():
    with pytest.raises(ValidationError, match="Line 1"):
        _make_report(raw="cable tray 100x50x2.44x0.7")  # missing m suffix anywhere


# --- weight factors (Settings) ---------------------------------------------

def test_get_weight_factors_returns_defaults_when_unset():
    factors = svc.get_weight_factors()
    assert factors["GI"] == svc.DEFAULT_WEIGHT_FACTORS["GI"]
    assert factors["HDG"] == svc.DEFAULT_WEIGHT_FACTORS["HDG"]


def test_set_weight_factor_persists_and_is_read_back():
    svc.set_weight_factor("GI", 9.0, updated_by=1)
    factors = svc.get_weight_factors()
    assert factors["GI"] == 9.0
    assert factors["HDG"] == svc.DEFAULT_WEIGHT_FACTORS["HDG"]  # untouched


def test_set_weight_factor_affects_new_reports_not_old_ones():
    rid, _no = _make_report()
    original_weight = svc.get_report(rid)["lines"][0]["weight_kg"]
    svc.set_weight_factor("GI", 100.0, updated_by=1)
    assert svc.get_report(rid)["lines"][0]["weight_kg"] == original_weight  # stored, not recomputed
    rid2, _no2 = _make_report()
    assert svc.get_report(rid2)["lines"][0]["weight_kg"] > original_weight


def test_set_weight_factor_rejects_invalid_material():
    with pytest.raises(ValidationError):
        svc.set_weight_factor("ALUMINUM", 5.0, updated_by=1)


def test_set_weight_factor_rejects_non_positive_value():
    with pytest.raises(ValidationError):
        svc.set_weight_factor("GI", 0, updated_by=1)
    with pytest.raises(ValidationError):
        svc.set_weight_factor("GI", -1, updated_by=1)


# --- reporting / export with the new columns -------------------------------

def test_export_rows_include_description_and_weight():
    _make_report()
    rows = svc.export_rows()
    assert rows[0]["description"] == "cable tray"
    assert rows[0]["weight_kg"] > 0


def test_build_export_workbook_runs_without_error():
    _make_report()
    rows = svc.export_rows()
    buf = svc.build_export_workbook(
        rows, document_no="RPT-2026-00001", generated_by="Test Manager",
        filters_description="All records",
    )
    assert buf.getbuffer().nbytes > 0


def test_dashboard_stats_includes_week_weight():
    _make_report()
    stats = svc.dashboard_stats()
    assert stats["week_weight"] > 0


def test_weekly_summary_includes_weight_totals():
    _make_report()
    _by_week, _by_line, by_material, _grand_total, grand_weight = svc.weekly_summary()
    assert grand_weight > 0
    assert by_material[0]["total_weight"] > 0


# --- weight targets (Settings) -----------------------------------------------

def test_get_weight_targets_defaults_to_zero():
    targets = svc.get_weight_targets()
    assert targets["weekly"] == 0
    assert targets["monthly"] == 0


def test_set_weight_target_persists_and_is_read_back():
    svc.set_weight_target("weekly", 500, updated_by=1)
    svc.set_weight_target("monthly", 2000, updated_by=1)
    targets = svc.get_weight_targets()
    assert targets["weekly"] == 500
    assert targets["monthly"] == 2000


def test_set_weight_target_rejects_negative():
    with pytest.raises(ValidationError):
        svc.set_weight_target("weekly", -1, updated_by=1)


def test_set_weight_target_rejects_invalid_period():
    with pytest.raises(ValidationError):
        svc.set_weight_target("yearly", 100, updated_by=1)


def test_set_weight_target_allows_zero_to_clear_it():
    svc.set_weight_target("weekly", 500, updated_by=1)
    svc.set_weight_target("weekly", 0, updated_by=1)
    assert svc.get_weight_targets()["weekly"] == 0


def test_dashboard_stats_includes_weight_targets_and_month_weight():
    svc.set_weight_target("weekly", 200, updated_by=1)
    svc.set_weight_target("monthly", 800, updated_by=1)
    _make_report()
    stats = svc.dashboard_stats()
    assert stats["weight_targets"]["weekly"] == 200
    assert stats["weight_targets"]["monthly"] == 800
    assert stats["month_weight"] > 0


# --- month lockout ------------------------------------------------------------

def test_month_starts_unlocked():
    assert svc.is_month_locked("2026-08") is False


def test_lock_and_unlock_month():
    svc.lock_month("2026-08", locked_by=1)
    assert svc.is_month_locked("2026-08") is True
    svc.unlock_month("2026-08", unlocked_by=1)
    assert svc.is_month_locked("2026-08") is False


def test_lock_month_rejects_bad_format():
    with pytest.raises(ValidationError):
        svc.lock_month("August 2026", locked_by=1)


def test_unlock_month_not_locked_raises_not_found():
    with pytest.raises(NotFoundError):
        svc.unlock_month("2026-08", unlocked_by=1)


def test_list_locked_months_includes_computed_range_and_label():
    svc.lock_month("2026-02", locked_by=1)  # a 28-day month - exercises the leap-safe end-of-month calc
    locked = svc.list_locked_months()
    assert len(locked) == 1
    assert locked[0]["month"] == "2026-02"
    assert locked[0]["month_start"] == "2026-02-01"
    assert locked[0]["month_end"] == "2026-02-28"
    assert locked[0]["month_label"] == "February 2026"


def test_create_report_rejects_line_dated_in_a_locked_month():
    svc.lock_month("2026-08", locked_by=1)
    with pytest.raises(ValidationError, match="August 2026 is locked"):
        _make_report(report_date="2026-08-15")


def test_create_report_allowed_in_a_different_month():
    svc.lock_month("2026-08", locked_by=1)
    rid, _no = _make_report(report_date="2026-07-15")
    assert svc.get_report(rid)["report_date"] == "2026-07-15"


def test_unlocking_a_month_allows_new_reports_again():
    svc.lock_month("2026-08", locked_by=1)
    svc.unlock_month("2026-08", unlocked_by=1)
    rid, _no = _make_report(report_date="2026-08-15")
    assert svc.get_report(rid)["report_date"] == "2026-08-15"


def test_locking_a_month_does_not_block_deleting_an_existing_report_in_it():
    rid, _no = _make_report(report_date="2026-08-15")
    svc.lock_month("2026-08", locked_by=1)
    svc.delete_report(rid, deleted_by=1, reason="correction")
    assert svc.get_report(rid)["status"] == "deleted"


# --- daily summary --------------------------------------------------------------

def test_daily_summary_includes_zero_days():
    result = svc.daily_summary("2026-08-01", "2026-08-03")
    assert len(result) == 3
    assert all(r["reports"] == 0 for r in result)


def test_daily_summary_reflects_recorded_reports():
    _make_report(report_date="2026-08-02", quantity=15)
    result = svc.daily_summary("2026-08-01", "2026-08-03")
    by_date = {r["report_date"]: r for r in result}
    assert by_date["2026-08-01"]["reports"] == 0
    assert by_date["2026-08-02"]["reports"] == 1
    assert by_date["2026-08-02"]["quantity"] == 15
    assert by_date["2026-08-02"]["weight"] > 0
    assert by_date["2026-08-03"]["reports"] == 0


def test_daily_summary_caps_pathologically_large_ranges():
    result = svc.daily_summary("2020-01-01", "2030-01-01")
    assert len(result) <= 371


# --- PDF export --------------------------------------------------------------

def test_build_production_listing_pdf_runs_without_error():
    from app.programs.weekly_productions.pdf_reports import build_production_listing_pdf
    _make_report()
    rows = svc.export_rows()
    pdf_bytes = build_production_listing_pdf(
        rows, document_no="RPT-2026-00001", generated_by="Test Manager",
        filters_description="All records",
    )
    assert pdf_bytes[:4] == b"%PDF"


def test_build_production_listing_pdf_handles_empty_rows():
    from app.programs.weekly_productions.pdf_reports import build_production_listing_pdf
    pdf_bytes = build_production_listing_pdf(
        [], document_no="RPT-2026-00001", generated_by="Test Manager",
        filters_description="All records",
    )
    assert pdf_bytes[:4] == b"%PDF"
