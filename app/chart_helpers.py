"""Turns a category-breakdown list into a CSS conic-gradient donut chart
plus a matching legend.

Concern: this is presentation math (percentages -> gradient stops, colors
assigned to categories), not a business rule - it has nothing to do with
what a voucher or category IS, only how to draw one. That's why it lives
here rather than in a service module (see app/services/errors.py's
docstring convention: services hold business rules with no framework
imports; this holds rendering logic with no business rules).

The chart is a plain CSS conic-gradient rather than an SVG or a JS charting
library - this app has stayed dependency-free throughout (no JS framework,
no chart library), and a conic-gradient needs nothing but a few computed
percentages, which Python is already producing from the database.

Colors are deliberately muted/desaturated, not the bright per-category
palette a consumer budgeting app like Spendee uses - this app's base
palette is black/white/red/blue, so these are treated as a small set of
accent colors used *only* for chart segments, never for buttons, badges,
or anything else. Any number of categories works: with more categories
than colors, the palette cycles.
"""

CHART_COLORS = [
    "#3b5bdb",  # blue (close to brand)
    "#2f8a7a",  # teal
    "#c17817",  # amber / ochre
    "#a8564f",  # muted brick red (distinct from --bad's brighter red)
    "#6b5b95",  # plum
    "#4a6fa5",  # steel blue
    "#7a7a52",  # olive
    "#8b93a1",  # neutral grey (also the fallback beyond 8 categories)
]

__all__ = ["CHART_COLORS", "build_donut_chart"]


def build_donut_chart(by_category):
    """`by_category` is [{"name": ..., "total": ...}, ...] (as returned by
    dashboard_stats()/spend_breakdown()). Returns a dict:
      has_data   - False if every category is zero (nothing to draw)
      gradient   - a ready-to-use CSS conic-gradient(...) string
      legend     - [{"name", "total", "percent", "color"}, ...] in the same
                   order as the gradient stops, so a legend dot's color
                   always matches its slice
      grand_total - sum of every category's total

    Categories with a zero total are skipped entirely - a 0%-wide slice
    has nothing to draw and nothing useful to put in the legend.

    Works with plain dicts or sqlite3.Row objects (some callers convert
    query results to dicts before returning them, some don't) - uses
    bracket indexing throughout rather than .get(), since Row doesn't
    support .get().
    """
    items = [c for c in by_category if c["total"]]
    grand_total = sum(c["total"] for c in items)
    if not items or not grand_total:
        return {"has_data": False, "gradient": "", "legend": [], "grand_total": 0}

    stops = []
    legend = []
    cursor = 0.0
    for i, cat in enumerate(items):
        color = CHART_COLORS[i % len(CHART_COLORS)]
        pct = cat["total"] / grand_total * 100
        start, end = cursor, cursor + pct
        stops.append(f"{color} {start:.3f}% {end:.3f}%")
        legend.append({
            "name": cat["name"], "total": cat["total"],
            "percent": round(pct), "color": color,
        })
        cursor = end

    return {
        "has_data": True,
        "gradient": "conic-gradient(" + ", ".join(stops) + ")",
        "legend": legend,
        "grand_total": grand_total,
    }
