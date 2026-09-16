"""Tests for app/database.py - specifically the migration that runs when a
database is still in the old shape (one row per voucher, with category,
particulars, and amount directly on the expenses table) and needs to become
the new header + expense_lines shape. This is exercised by hand-crafting an
old-shape database, since a freshly-initialized test database is always
already in the new shape and would never take this code path."""
import sqlite3

from app.config import DATABASE_PATH
from app.database import init_schema, _migrate_single_line_vouchers, get_connection


_OLD_SCHEMA = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    full_name TEXT NOT NULL,
    role TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE cost_centers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    voucher_no TEXT UNIQUE NOT NULL,
    expense_date TEXT NOT NULL,
    category_id INTEGER NOT NULL REFERENCES categories(id),
    cost_center_id INTEGER REFERENCES cost_centers(id),
    paid_to TEXT NOT NULL,
    particulars TEXT NOT NULL,
    amount REAL NOT NULL,
    payment_mode TEXT NOT NULL DEFAULT 'Cash',
    status TEXT NOT NULL DEFAULT 'pending',
    receipt_path TEXT,
    prepared_by INTEGER NOT NULL REFERENCES users(id),
    approved_by INTEGER REFERENCES users(id),
    approved_at TEXT,
    rejection_reason TEXT,
    deleted_by INTEGER REFERENCES users(id),
    deleted_at TEXT,
    delete_reason TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _build_old_shape_database():
    """Wipes the test database and recreates it in the pre-migration shape,
    with two vouchers already in it - one approved, one pending."""
    DATABASE_PATH.unlink(missing_ok=True)
    conn = sqlite3.connect(DATABASE_PATH)
    conn.executescript(_OLD_SCHEMA)
    conn.execute(
        "INSERT INTO users (id, username, password_hash, full_name, role) "
        "VALUES (1, 'admin', 'hash', 'Manager Account', 'manager')"
    )
    conn.execute("INSERT INTO categories (id, name) VALUES (1, 'Fuel')")
    conn.execute("INSERT INTO cost_centers (id, code, name) VALUES (1, 'HQ', 'Head Office')")
    conn.execute(
        "INSERT INTO expenses (voucher_no, expense_date, category_id, cost_center_id, "
        "paid_to, particulars, amount, payment_mode, status, prepared_by, approved_by, approved_at) "
        "VALUES ('PCV-2025-00001', '2025-06-01', 1, 1, 'Old Vendor', 'Old-format entry', "
        "250.0, 'Cash', 'approved', 1, 1, '2025-06-01 10:00:00')"
    )
    conn.execute(
        "INSERT INTO expenses (voucher_no, expense_date, category_id, cost_center_id, "
        "paid_to, particulars, amount, payment_mode, status, prepared_by) "
        "VALUES ('PCV-2025-00002', '2025-06-02', 1, 1, 'Another Vendor', 'Still pending', "
        "75.5, 'Cash', 'pending', 1)"
    )
    conn.commit()
    conn.close()


def test_migration_detects_old_shape_and_converts_columns():
    _build_old_shape_database()
    conn = get_connection()
    try:
        _migrate_single_line_vouchers(conn)
        conn.commit()
    finally:
        conn.close()

    conn = get_connection()
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(expenses)").fetchall()}
        assert "category_id" not in cols
        assert "amount" not in cols
        assert "particulars" not in cols
        assert "paid_to" in cols  # header fields survive
    finally:
        conn.close()


def test_migration_preserves_every_voucher_as_a_header_plus_one_line():
    _build_old_shape_database()
    conn = get_connection()
    try:
        _migrate_single_line_vouchers(conn)
        conn.commit()
    finally:
        conn.close()

    conn = get_connection()
    try:
        vouchers = conn.execute("SELECT * FROM expenses ORDER BY voucher_no").fetchall()
        assert len(vouchers) == 2
        assert vouchers[0]["voucher_no"] == "PCV-2025-00001"
        assert vouchers[0]["paid_to"] == "Old Vendor"
        assert vouchers[0]["status"] == "approved"

        lines = conn.execute(
            "SELECT * FROM expense_lines WHERE expense_id = ?", (vouchers[0]["id"],)
        ).fetchall()
        assert len(lines) == 1
        assert lines[0]["particulars"] == "Old-format entry"
        assert lines[0]["amount"] == 250.0
        assert lines[0]["category_id"] == 1
        assert lines[0]["is_vatable"] == 0  # historical entries default to not VAT-able
    finally:
        conn.close()


def test_migration_is_idempotent_via_full_init_schema():
    """init_schema() calls the migration automatically - running it against
    an already-migrated (or freshly created) database must be a no-op, not
    an error."""
    _build_old_shape_database()
    init_schema()  # first run: migrates
    init_schema()  # second run: should just no-op cleanly

    conn = get_connection()
    try:
        count = conn.execute("SELECT COUNT(*) c FROM expenses").fetchone()["c"]
        assert count == 2
    finally:
        conn.close()


def test_migrated_database_works_through_the_real_service_layer():
    """The whole point: after migrating, the normal app code (which only
    knows about the new schema) must be able to read the old data back
    correctly through get_expense()."""
    _build_old_shape_database()
    init_schema()

    from app.programs.expenses.services import expenses as svc
    rows, total = svc.list_expenses(status="approved")
    assert len(rows) == 1
    assert rows[0]["voucher_no"] == "PCV-2025-00001"
    assert rows[0]["total_amount"] == 250.0

    full = svc.get_expense(rows[0]["id"])
    assert len(full["lines"]) == 1
    assert full["lines"][0]["particulars"] == "Old-format entry"
    assert full["lines"][0]["category_name"] == "Fuel"


def test_migration_leaves_expense_lines_foreign_key_pointing_at_the_real_table():
    """Regression test for a real bug: renaming `expenses` mid-migration
    makes SQLite silently rewrite expense_lines' FK definition to point at
    the *renamed* (soon to be dropped) table instead of the new one. That
    left expense_lines permanently unable to accept a new row after
    migrating - every INSERT failed with "no such table: main.expenses_old"
    - until expense_lines was rebuilt too, not just expenses. This creates
    a brand new line (not just reads pre-existing data) against a freshly
    migrated database to prove that's fixed and stays fixed."""
    _build_old_shape_database()
    init_schema()

    from app.database import get_db
    with get_db() as conn:
        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert fk_violations == [], f"dangling foreign keys after migration: {fk_violations}"

        expense_id = conn.execute("SELECT id FROM expenses LIMIT 1").fetchone()["id"]
        category_id = conn.execute("SELECT id FROM categories LIMIT 1").fetchone()["id"]
        conn.execute(
            "INSERT INTO expense_lines (expense_id, line_no, category_id, particulars, amount) "
            "VALUES (?, 99, ?, 'post-migration line', 10.0)",
            (expense_id, category_id),
        )
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM expense_lines WHERE particulars='post-migration line'"
        ).fetchone()
        assert row is not None


# ---------------------------------------------------------------------------
# _migrate_flat_production_entries() - Weekly Productions used to be a flat
# one-row-per-entry table (product_name/quantity/unit, no material spec);
# now it's a header (production_reports) + material-spec lines
# (production_report_lines), the same shape as the vouchers migration above.
# ---------------------------------------------------------------------------
from app.database import _migrate_flat_production_entries

_OLD_PRODUCTION_SCHEMA = """
CREATE TABLE production_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_no TEXT UNIQUE,
    entry_date TEXT NOT NULL,
    line_name TEXT NOT NULL,
    product_name TEXT NOT NULL,
    quantity REAL NOT NULL,
    unit TEXT NOT NULL DEFAULT 'pcs',
    shift TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'recorded' CHECK(status IN ('recorded','deleted')),
    recorded_by INTEGER NOT NULL REFERENCES users(id),
    deleted_by INTEGER REFERENCES users(id),
    deleted_at TEXT,
    delete_reason TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _build_old_flat_production_database():
    """Wipes the test database and recreates it with `users` plus a flat
    `production_entries` table already holding two entries - one recorded,
    one deleted - in the pre-migration shape."""
    DATABASE_PATH.unlink(missing_ok=True)
    conn = sqlite3.connect(DATABASE_PATH)
    conn.executescript(_OLD_SCHEMA)  # reuse the users table from the vouchers old-shape schema
    conn.executescript(_OLD_PRODUCTION_SCHEMA)
    conn.execute(
        "INSERT INTO users (id, username, password_hash, full_name, role) "
        "VALUES (1, 'admin', 'hash', 'Manager Account', 'manager')"
    )
    conn.execute(
        "INSERT INTO production_entries (entry_no, entry_date, line_name, product_name, "
        "quantity, unit, shift, notes, status, recorded_by) VALUES "
        "('PRD-2025-00001', '2025-06-01', 'Line 1', 'Widget-A', 120.0, 'pcs', 'Morning', "
        "'', 'recorded', 1)"
    )
    conn.execute(
        "INSERT INTO production_entries (entry_no, entry_date, line_name, product_name, "
        "quantity, unit, shift, notes, status, recorded_by, deleted_by, deleted_at, delete_reason) "
        "VALUES ('PRD-2025-00002', '2025-06-02', 'Line 2', 'Widget-B', 40.0, 'pcs', '', "
        "'', 'deleted', 1, 1, '2025-06-03 09:00:00', 'double-entry')"
    )
    conn.commit()
    conn.close()


def test_production_migration_detects_old_shape_and_creates_new_tables():
    _build_old_flat_production_database()
    conn = get_connection()
    try:
        _migrate_flat_production_entries(conn)
        conn.commit()
    finally:
        conn.close()

    conn = get_connection()
    try:
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "production_reports" in tables
        assert "production_report_lines" in tables
        assert "production_entries" not in tables
    finally:
        conn.close()


def test_production_migration_preserves_every_entry_as_a_report_plus_one_line():
    _build_old_flat_production_database()
    conn = get_connection()
    try:
        _migrate_flat_production_entries(conn)
        conn.commit()
    finally:
        conn.close()

    conn = get_connection()
    try:
        reports = conn.execute(
            "SELECT * FROM production_reports ORDER BY report_no"
        ).fetchall()
        assert len(reports) == 2
        assert reports[0]["report_no"] == "PRD-2025-00001"
        assert reports[0]["line_name"] == "Line 1"
        assert reports[0]["status"] == "recorded"
        assert "Widget-A" in reports[0]["notes"]  # legacy product/quantity preserved as text
        assert reports[1]["status"] == "deleted"
        assert reports[1]["delete_reason"] == "double-entry"

        lines = conn.execute(
            "SELECT * FROM production_report_lines WHERE report_id = ?", (reports[0]["id"],)
        ).fetchall()
        assert len(lines) == 1
        assert lines[0]["material_type"] == "GI"  # placeholder default - see migration docstring
        assert lines[0]["quantity"] == 120.0  # the one real number the old shape had
    finally:
        conn.close()


def test_production_migration_is_idempotent_via_full_init_schema():
    _build_old_flat_production_database()
    init_schema()  # first run: migrates
    init_schema()  # second run: should just no-op cleanly

    conn = get_connection()
    try:
        count = conn.execute("SELECT COUNT(*) c FROM production_reports").fetchone()["c"]
        assert count == 2
    finally:
        conn.close()


def test_migrated_production_database_works_through_the_real_service_layer():
    _build_old_flat_production_database()
    init_schema()

    from app.programs.weekly_productions.services import production as prod_svc
    rows, _total, _total_weight, _lines = prod_svc.list_reports(status="recorded")
    assert len(rows) == 1
    assert rows[0]["report_no"] == "PRD-2025-00001"

    full = prod_svc.get_report(rows[0]["id"])
    assert len(full["lines"]) == 1
    assert full["lines"][0]["material_type"] == "GI"


# ---------------------------------------------------------------------------
# _migrate_add_line_description_and_weight() - simple ADD COLUMN migration
# for a production_report_lines table created before `description` and
# `weight_kg` existed (i.e. right after the header+lines migration above,
# before this session's weight-calculation feature).
# ---------------------------------------------------------------------------
from app.database import _migrate_add_line_description_and_weight


def _build_pre_weight_production_database():
    """A production_reports/production_report_lines pair already in the
    header+lines shape, but from before `description` and `weight_kg`
    existed on the lines table."""
    DATABASE_PATH.unlink(missing_ok=True)
    conn = sqlite3.connect(DATABASE_PATH)
    conn.executescript(_OLD_SCHEMA)  # users table
    conn.executescript("""
        CREATE TABLE production_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_no TEXT UNIQUE,
            report_date TEXT NOT NULL,
            line_name TEXT NOT NULL,
            shift TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'recorded',
            recorded_by INTEGER NOT NULL REFERENCES users(id),
            deleted_by INTEGER REFERENCES users(id),
            deleted_at TEXT,
            delete_reason TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE production_report_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id INTEGER NOT NULL REFERENCES production_reports(id),
            line_no INTEGER NOT NULL,
            material_type TEXT NOT NULL,
            width REAL NOT NULL,
            height REAL NOT NULL,
            length REAL NOT NULL,
            thickness REAL NOT NULL,
            quantity REAL NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    conn.execute(
        "INSERT INTO users (id, username, password_hash, full_name, role) "
        "VALUES (1, 'admin', 'hash', 'Manager Account', 'manager')"
    )
    conn.execute(
        "INSERT INTO production_reports (report_no, report_date, line_name, recorded_by) "
        "VALUES ('PRD-2025-00001', '2025-06-01', 'Line 1', 1)"
    )
    conn.execute(
        "INSERT INTO production_report_lines (report_id, line_no, material_type, width, "
        "height, length, thickness, quantity) VALUES (1, 1, 'GI', 100, 50, 2.44, 0.7, 20)"
    )
    conn.commit()
    conn.close()


def test_add_column_migration_adds_description_and_weight_columns():
    _build_pre_weight_production_database()
    conn = get_connection()
    try:
        _migrate_add_line_description_and_weight(conn)
        conn.commit()
        cols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(production_report_lines)"
        ).fetchall()}
        assert "description" in cols
        assert "weight_kg" in cols
    finally:
        conn.close()


def test_add_column_migration_preserves_existing_rows_with_safe_defaults():
    _build_pre_weight_production_database()
    conn = get_connection()
    try:
        _migrate_add_line_description_and_weight(conn)
        conn.commit()
        row = conn.execute("SELECT * FROM production_report_lines WHERE line_no=1").fetchone()
        assert row["width"] == 100  # pre-existing data untouched
        assert row["description"] == ""  # new column, safe default
        assert row["weight_kg"] == 0  # new column, safe default - not retroactively computed
    finally:
        conn.close()


def test_add_column_migration_is_idempotent_via_full_init_schema():
    _build_pre_weight_production_database()
    init_schema()  # first run: adds the columns
    init_schema()  # second run: should just no-op cleanly

    conn = get_connection()
    try:
        count = conn.execute("SELECT COUNT(*) c FROM production_report_lines").fetchone()["c"]
        assert count == 1
    finally:
        conn.close()


def test_migrated_weight_columns_work_through_the_real_service_layer():
    _build_pre_weight_production_database()
    init_schema()

    from app.programs.weekly_productions.services import production as prod_svc
    report = prod_svc.get_report(1)
    assert report["lines"][0]["weight_kg"] == 0
    assert report["lines"][0]["description"] == ""
    # and the service can still add a real new line with weight going forward
    rid, _no = prod_svc.create_report(
        report_date="2026-01-01", line_name="Line 2", lines=[
            {"material_type": "GI", "raw": "cable tray 100x50x2.44mx0.7", "quantity": 1},
        ], recorded_by=1,
    )
    assert prod_svc.get_report(rid)["lines"][0]["weight_kg"] > 0


# ---------------------------------------------------------------------------
# _migrate_add_expense_line_supplier() - adds expense_lines.supplier_id (and
# creates the suppliers table) for a database from before the VAT supplier
# knowledge-base feature existed.
# ---------------------------------------------------------------------------
from app.database import _migrate_add_expense_line_supplier

_PRE_SUPPLIER_HEADER_LINES_SCHEMA = """
CREATE TABLE expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    voucher_no TEXT UNIQUE,
    expense_date TEXT NOT NULL,
    cost_center_id INTEGER,
    paid_to TEXT NOT NULL,
    payment_mode TEXT NOT NULL DEFAULT 'Cash',
    status TEXT NOT NULL DEFAULT 'pending',
    receipt_path TEXT,
    prepared_by INTEGER NOT NULL REFERENCES users(id),
    approved_by INTEGER REFERENCES users(id),
    approved_at TEXT,
    rejection_reason TEXT,
    deleted_by INTEGER REFERENCES users(id),
    deleted_at TEXT,
    delete_reason TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE expense_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    expense_id INTEGER NOT NULL REFERENCES expenses(id),
    line_no INTEGER NOT NULL,
    category_id INTEGER NOT NULL REFERENCES categories(id),
    particulars TEXT NOT NULL,
    amount REAL NOT NULL,
    is_vatable INTEGER NOT NULL DEFAULT 0,
    vat_rate REAL NOT NULL DEFAULT 0,
    vat_amount REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _build_pre_supplier_database():
    """users + categories + a header+lines expenses/expense_lines pair, all
    in the shape that predates expense_lines.supplier_id and the suppliers
    table entirely."""
    DATABASE_PATH.unlink(missing_ok=True)
    conn = sqlite3.connect(DATABASE_PATH)
    conn.executescript(_OLD_SCHEMA)  # users + categories (expenses table gets overwritten next)
    conn.execute("DROP TABLE expenses")
    conn.executescript(_PRE_SUPPLIER_HEADER_LINES_SCHEMA)
    conn.execute(
        "INSERT INTO users (id, username, password_hash, full_name, role) "
        "VALUES (1, 'admin', 'hash', 'Manager Account', 'manager')"
    )
    conn.execute(
        "INSERT INTO categories (id, name, active) VALUES (1, 'Fuel', 1)"
    )
    conn.execute(
        "INSERT INTO expenses (id, voucher_no, expense_date, paid_to, status, prepared_by) "
        "VALUES (1, 'PCV-2025-00001', '2025-06-01', 'Old Vendor', 'approved', 1)"
    )
    conn.execute(
        "INSERT INTO expense_lines (expense_id, line_no, category_id, particulars, amount, "
        "is_vatable, vat_rate, vat_amount) VALUES (1, 1, 1, 'Old fuel purchase', 100.0, 1, 15.0, 15.0)"
    )
    conn.commit()
    conn.close()


def test_supplier_migration_adds_column_and_creates_suppliers_table():
    _build_pre_supplier_database()
    conn = get_connection()
    try:
        _migrate_add_expense_line_supplier(conn)
        conn.commit()
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(expense_lines)").fetchall()}
        assert "supplier_id" in cols
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "suppliers" in tables
    finally:
        conn.close()


def test_supplier_migration_preserves_existing_line_with_null_supplier():
    _build_pre_supplier_database()
    conn = get_connection()
    try:
        _migrate_add_expense_line_supplier(conn)
        conn.commit()
        row = conn.execute("SELECT * FROM expense_lines WHERE expense_id=1").fetchone()
        assert row["particulars"] == "Old fuel purchase"  # pre-existing data untouched
        assert row["amount"] == 100.0
        assert row["supplier_id"] is None  # new column, safe default - not retroactively guessed
    finally:
        conn.close()


def test_supplier_migration_is_idempotent_via_full_init_schema():
    _build_pre_supplier_database()
    init_schema()  # first run: adds the column + creates suppliers
    init_schema()  # second run: should just no-op cleanly

    conn = get_connection()
    try:
        count = conn.execute("SELECT COUNT(*) c FROM expense_lines").fetchone()["c"]
        assert count == 1
    finally:
        conn.close()


def test_migrated_supplier_column_works_through_the_real_service_layer():
    _build_pre_supplier_database()
    init_schema()

    from app.programs.expenses.services import expenses as expenses_svc
    expense = expenses_svc.get_expense(1)
    assert expense["lines"][0]["supplier_name"] is None  # pre-existing line, no supplier attached

    # and the service can attach a real supplier to a new VAT-able line going forward
    new_id = expenses_svc.create_expense(
        expense_date="2026-01-01", cost_center_id=None, paid_to="New Vendor",
        payment_mode="Cash", receipt_path=None, user_id=1,
        lines=[{"category_id": 1, "particulars": "New fuel", "amount": 50.0,
                "is_vatable": True, "supplier_name": "Post-Migration Co", "supplier_vat": "VATPOSTMIG"}],
    )
    new_line = expenses_svc.get_expense(new_id)["lines"][0]
    assert new_line["supplier_name"] == "Post-Migration Co"


# ---------------------------------------------------------------------------
# Asset & Maintenance module: expense_lines.asset_id, employees.user_id, and
# categories.requires_asset / asset_category on a database that predates the
# module entirely.
#
# Every one is a plain ADD COLUMN with nothing to backfill, so what these
# tests actually prove is the boring-but-load-bearing part: the columns land,
# pre-existing rows are untouched and get safe defaults, and running
# init_schema() twice doesn't fall over. The asset module's own tables need no
# migration at all (CREATE TABLE IF NOT EXISTS), but they're asserted here too
# since a fresh install and a migrated one must end up the same shape.
# ---------------------------------------------------------------------------
from app.database import _migrate_add_asset_module_fields

_PRE_ASSET_SCHEMA = """
-- NOTE: `status` deliberately carries the draft-capable CHECK constraint.
-- The asset module shipped long after drafts did, so a database that
-- predates *this* feature is necessarily already past that one. Writing the
-- older, draft-less CHECK here instead would re-trigger
-- _migrate_add_draft_status(), which rebuilds expense_lines from its own
-- pinned 10-column historical snapshot and would then fail against the
-- 14-column shape below ("table expense_lines has 10 columns but 14 values
-- were supplied"). That is the migration functions working exactly as
-- intended - each pins the schema as it was at its own moment in history -
-- so the fixture has to describe one real point in time, not a mix.
CREATE TABLE expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    voucher_no TEXT UNIQUE,
    expense_date TEXT NOT NULL,
    cost_center_id INTEGER,
    paid_to TEXT NOT NULL DEFAULT '',
    payment_mode TEXT NOT NULL DEFAULT 'Cash',
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','pending','approved','rejected','deleted')),
    receipt_path TEXT,
    prepared_by INTEGER NOT NULL REFERENCES users(id),
    approved_by INTEGER REFERENCES users(id),
    approved_at TEXT,
    rejection_reason TEXT,
    deleted_by INTEGER REFERENCES users(id),
    deleted_at TEXT,
    delete_reason TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE expense_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    expense_id INTEGER NOT NULL REFERENCES expenses(id),
    line_no INTEGER NOT NULL,
    category_id INTEGER NOT NULL REFERENCES categories(id),
    particulars TEXT NOT NULL,
    amount REAL NOT NULL,
    is_vatable INTEGER NOT NULL DEFAULT 0,
    vat_rate REAL NOT NULL DEFAULT 0,
    vat_amount REAL NOT NULL DEFAULT 0,
    supplier_id INTEGER REFERENCES suppliers(id),
    employee_id INTEGER,
    employee_name TEXT,
    employee_phone TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE suppliers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    vat_number TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'active',
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    deleted_by INTEGER REFERENCES users(id),
    deleted_at TEXT,
    delete_reason TEXT
);
CREATE TABLE employees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    deleted_by INTEGER REFERENCES users(id),
    deleted_at TEXT,
    UNIQUE(name, phone)
);
"""


def _build_pre_asset_database():
    """users + categories + a voucher/line pair + an employee, all in the
    shape that predates the asset module: no asset_id on expense_lines, no
    user_id on employees, no requires_asset/asset_category on categories,
    and none of the asset tables."""
    DATABASE_PATH.unlink(missing_ok=True)
    conn = sqlite3.connect(DATABASE_PATH)
    conn.executescript(_OLD_SCHEMA)  # users + categories
    conn.execute("DROP TABLE expenses")
    conn.executescript(_PRE_ASSET_SCHEMA)
    conn.execute(
        "INSERT INTO users (id, username, password_hash, full_name, role) "
        "VALUES (1, 'admin', 'hash', 'Manager Account', 'manager')"
    )
    conn.execute("INSERT INTO categories (id, name, active) VALUES (1, 'Fuel', 1)")
    conn.execute(
        "INSERT INTO categories (id, name, active) "
        "VALUES (2, 'Vehicle maintenance & repairs', 1)"
    )
    conn.execute(
        "INSERT INTO expenses (id, voucher_no, expense_date, paid_to, status, prepared_by) "
        "VALUES (1, 'PCV-2025-00001', '2025-06-01', 'Old Vendor', 'approved', 1)"
    )
    conn.execute(
        "INSERT INTO expense_lines (expense_id, line_no, category_id, particulars, amount, "
        "is_vatable, vat_rate, vat_amount) "
        "VALUES (1, 1, 2, 'Truck brake pads', 200.0, 1, 15.0, 30.0)"
    )
    conn.execute(
        "INSERT INTO employees (id, name, phone, created_by) VALUES (1, 'Ahmed K.', '0500000000', 1)"
    )
    conn.commit()
    conn.close()


def test_asset_migration_adds_all_three_columns():
    _build_pre_asset_database()
    conn = get_connection()
    try:
        _migrate_add_asset_module_fields(conn)
        conn.commit()
        line_cols = {r["name"] for r in conn.execute("PRAGMA table_info(expense_lines)").fetchall()}
        assert "asset_id" in line_cols
        emp_cols = {r["name"] for r in conn.execute("PRAGMA table_info(employees)").fetchall()}
        assert "user_id" in emp_cols
        cat_cols = {r["name"] for r in conn.execute("PRAGMA table_info(categories)").fetchall()}
        assert "requires_asset" in cat_cols
        assert "asset_category" in cat_cols
    finally:
        conn.close()


def test_asset_migration_preserves_existing_rows_with_safe_defaults():
    _build_pre_asset_database()
    conn = get_connection()
    try:
        _migrate_add_asset_module_fields(conn)
        conn.commit()

        line = conn.execute("SELECT * FROM expense_lines WHERE expense_id=1").fetchone()
        assert line["particulars"] == "Truck brake pads"   # untouched
        assert line["amount"] == 200.0
        assert line["vat_amount"] == 30.0
        # A pre-existing maintenance line is NOT retroactively guessed onto an
        # asset - there is no way to know which truck it was, and inventing one
        # would poison that asset's cost history from day one.
        assert line["asset_id"] is None

        emp = conn.execute("SELECT * FROM employees WHERE id=1").fetchone()
        assert emp["name"] == "Ahmed K."
        assert emp["user_id"] is None    # linked by a manager later, never guessed

        # Every existing category defaults to "no asset needed" - so an install
        # that migrates behaves exactly as it did yesterday until a manager
        # ticks a box, rather than suddenly rejecting every voucher.
        rows = conn.execute("SELECT name, requires_asset, asset_category FROM categories").fetchall()
        assert all(r["requires_asset"] == 0 for r in rows)
        assert all(r["asset_category"] is None for r in rows)
    finally:
        conn.close()


def test_asset_migration_is_idempotent_via_full_init_schema():
    _build_pre_asset_database()
    init_schema()   # first run: adds columns + creates the asset tables
    init_schema()   # second run: no-ops cleanly

    conn = get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) c FROM expense_lines").fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) c FROM employees").fetchone()["c"] == 1
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert {"assets", "asset_assignments", "asset_service_log",
                "asset_tag_rules"} <= tables
    finally:
        conn.close()


def test_migrated_asset_column_works_through_the_real_service_layer():
    _build_pre_asset_database()
    init_schema()

    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO assets (asset_tag, name, category, purchase_cost, created_by) "
            "VALUES ('TR-04', 'Isuzu 6-wheel', 'Fleet/Truck', 45000.0, 1)"
        )
        conn.commit()
        asset_id = conn.execute("SELECT id FROM assets WHERE asset_tag='TR-04'").fetchone()["id"]
    finally:
        conn.close()

    from app.programs.expenses.services import expenses as expenses_svc

    # The pre-existing migrated line still reads back fine, with no asset.
    assert expenses_svc.get_expense(1)["lines"][0]["particulars"] == "Truck brake pads"

    # And a brand-new line can carry an asset through the real service layer.
    new_id = expenses_svc.create_expense(
        expense_date="2026-01-01", cost_center_id=None, paid_to="Al-Rajhi Motors",
        payment_mode="Cash", receipt_path=None, user_id=1,
        lines=[{"category_id": 2, "particulars": "Brake pads replaced", "amount": 640.0,
                "is_vatable": True, "supplier_name": "Al-Rajhi Motors",
                "supplier_vat": "VAT300400500", "asset_id": asset_id}],
    )
    conn = get_connection()
    try:
        stored = conn.execute(
            "SELECT asset_id FROM expense_lines WHERE expense_id=?", (new_id,)
        ).fetchone()
        assert stored["asset_id"] == asset_id
    finally:
        conn.close()


# ── WORD EDITOR: version/snapshot/search columns ───────────────────────────

def test_wd_documents_gains_version_columns_on_an_existing_database(reset_db):
    """An existing install has wd_documents without search_text/official_sha/
    version_no. init_schema() must add them without losing rows - hand-build
    that older shape, migrate, and check both the data and the new columns."""
    import sqlite3
    from app.database import DATABASE_PATH, init_schema, get_connection

    conn = sqlite3.connect(DATABASE_PATH)
    conn.execute("DROP TABLE IF EXISTS wd_document_versions")
    conn.execute("DROP TABLE IF EXISTS wd_documents")
    conn.executescript("""
        CREATE TABLE wd_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_no TEXT UNIQUE,
            title TEXT NOT NULL DEFAULT 'Untitled',
            subject TEXT NOT NULL DEFAULT '',
            receiver_name TEXT NOT NULL DEFAULT '',
            receiver_phone TEXT NOT NULL DEFAULT '',
            content_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'draft',
            created_by INTEGER NOT NULL,
            self_approved_by INTEGER, self_approved_at TEXT,
            manager_approved_by INTEGER, manager_approved_at TEXT,
            manager_note TEXT,
            deleted_by INTEGER, deleted_at TEXT, delete_reason TEXT,
            last_edited_at TEXT NOT NULL DEFAULT (datetime('now')),
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    conn.execute(
        "INSERT INTO wd_documents (id, doc_no, title, subject, content_json, "
        "status, created_by) VALUES (1,'WD-2026-00001','Old','Old subject',"
        "'<p>legacy body</p>','official',1)")
    conn.commit()
    conn.close()

    init_schema()

    conn = get_connection()
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(wd_documents)")}
        assert {"search_text", "official_sha", "version_no"} <= cols
        row = conn.execute("SELECT * FROM wd_documents WHERE id=1").fetchone()
        assert row["doc_no"] == "WD-2026-00001"
        assert row["content_json"] == "<p>legacy body</p>"
        assert row["official_sha"] == ""          # backfilled lazily, not invented
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "wd_document_versions" in tables
    finally:
        conn.close()


def test_notifications_level_column_is_added_to_an_existing_database(tmp_path):
    """A database created before urgency bands existed picks the column up
    on next start, and every row already in it becomes 'info' - which is
    exactly what those notifications were before bands existed.

    Purely additive ALTER TABLE ADD COLUMN with a DEFAULT, so unlike the
    CHECK-constraint migrations above there is no rename/copy/drop to get
    wrong - but "no rebuild needed" is a claim worth proving rather than
    asserting in a comment.
    """
    import sqlite3
    from app import database

    db_path = tmp_path / "old_shape.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT, full_name TEXT, password_hash TEXT,
            role TEXT, active INTEGER DEFAULT 1
        );
        CREATE TABLE notifications (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL REFERENCES users(id),
            message     TEXT NOT NULL,
            link        TEXT NOT NULL DEFAULT '',
            read        INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );
        INSERT INTO users (username, full_name, password_hash, role)
            VALUES ('admin', 'Admin', 'x', 'manager');
        INSERT INTO notifications (user_id, message) VALUES (1, 'Budget exceeded');
    """)
    conn.commit()
    conn.close()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    database._migrate_add_notification_level(conn)

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(notifications)")}
    assert "level" in cols
    row = conn.execute("SELECT message, level FROM notifications").fetchone()
    assert row["message"] == "Budget exceeded"      # data survived
    assert row["level"] == "info"                    # sensible default
    # Idempotent: init_schema() calls this on every start.
    database._migrate_add_notification_level(conn)
    conn.close()
