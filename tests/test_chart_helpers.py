import pytest

from app.chart_helpers import build_donut_chart, CHART_COLORS


def test_empty_list_has_no_data():
    result = build_donut_chart([])
    assert result["has_data"] is False
    assert result["gradient"] == ""
    assert result["legend"] == []


def test_all_zero_totals_has_no_data():
    result = build_donut_chart([{"name": "A", "total": 0}, {"name": "B", "total": 0}])
    assert result["has_data"] is False


def test_zero_total_categories_are_excluded_from_legend():
    result = build_donut_chart([{"name": "A", "total": 100}, {"name": "B", "total": 0}])
    assert len(result["legend"]) == 1
    assert result["legend"][0]["name"] == "A"


def test_single_category_is_100_percent():
    result = build_donut_chart([{"name": "Fuel", "total": 250}])
    assert result["has_data"] is True
    assert result["legend"][0]["percent"] == 100
    assert result["grand_total"] == 250
    assert "0.000%" in result["gradient"]
    assert "100.000%" in result["gradient"]


def test_percentages_sum_to_100():
    by_category = [{"name": "A", "total": 30}, {"name": "B", "total": 45}, {"name": "C", "total": 25}]
    result = build_donut_chart(by_category)
    assert sum(l["percent"] for l in result["legend"]) == 100


def test_legend_matches_gradient_order_and_colors():
    by_category = [{"name": "A", "total": 10}, {"name": "B", "total": 20}, {"name": "C", "total": 30}]
    result = build_donut_chart(by_category)
    for i, entry in enumerate(result["legend"]):
        assert entry["color"] == CHART_COLORS[i]
        assert entry["color"] in result["gradient"]


def test_grand_total_is_sum_of_all_categories():
    by_category = [{"name": "A", "total": 15.5}, {"name": "B", "total": 24.5}]
    result = build_donut_chart(by_category)
    assert result["grand_total"] == 40.0


def test_colors_cycle_when_more_categories_than_palette():
    by_category = [{"name": f"Cat{i}", "total": 10} for i in range(len(CHART_COLORS) + 3)]
    result = build_donut_chart(by_category)
    assert len(result["legend"]) == len(CHART_COLORS) + 3
    # the 9th category (index 8) wraps back to the first color
    assert result["legend"][len(CHART_COLORS)]["color"] == CHART_COLORS[0]
    assert result["legend"][len(CHART_COLORS) + 1]["color"] == CHART_COLORS[1]


def test_gradient_stops_are_contiguous():
    """No gaps or overlaps between consecutive slices - each stop's end
    matches the next stop's start."""
    by_category = [{"name": "A", "total": 33}, {"name": "B", "total": 33}, {"name": "C", "total": 34}]
    result = build_donut_chart(by_category)
    # extract the numeric percentages in order of appearance
    import re
    numbers = [float(x) for x in re.findall(r"(\d+\.\d+)%", result["gradient"])]
    pairs = list(zip(numbers, numbers[1:]))
    # every "end" of one slice should equal the "start" of the next
    for i in range(1, len(numbers) - 1, 2):
        assert numbers[i] == numbers[i + 1]
    assert numbers[0] == 0.0
    assert numbers[-1] == pytest.approx(100.0, abs=0.01)


def test_works_with_real_sqlite3_row_objects_not_just_dicts():
    """Regression test: dashboard_stats()'s by_category is returned as raw
    sqlite3.Row objects (never converted to dicts), and Row doesn't support
    .get() - an earlier version of build_donut_chart() used c.get("total")
    internally, which worked fine against every dict-based test here but
    raised AttributeError the moment a real request hit the dashboard route.
    Dict-only tests didn't catch it; this uses an actual Row, from an actual
    query, to make sure that specific gap can't reopen silently."""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE t (name TEXT, total REAL)")
    conn.execute("INSERT INTO t VALUES ('Fuel', 100), ('Snacks', 50), ('Water', 0)")
    rows = conn.execute("SELECT name, total FROM t").fetchall()
    conn.close()

    assert isinstance(rows[0], sqlite3.Row)
    assert not hasattr(rows[0], "get")  # confirms this test would have caught the bug

    result = build_donut_chart(rows)
    assert result["has_data"] is True
    assert len(result["legend"]) == 2  # Water (total=0) excluded
    assert result["grand_total"] == 150
