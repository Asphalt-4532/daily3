"""SQLite connection helper and schema management."""
import sqlite3
from contextlib import contextmanager
from app.config import DATABASE_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    full_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('manager','accountant','user')),
    active INTEGER NOT NULL DEFAULT 1,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    gl_code TEXT,
    -- Which categories demand an asset on a voucher line is DATA, not code:
    -- a manager ticks it at Settings -> Categories. Three states:
    --   requires_asset=1                 -> asset picker required on submit
    --   requires_asset=0 + asset_category -> picker shown, optional
    --   requires_asset=0 + NULL           -> picker hidden (Fuel, advances)
    -- asset_category NULL with requires_asset=1 means "any asset type".
    requires_asset INTEGER NOT NULL DEFAULT 0,
    asset_category TEXT
);

CREATE TABLE IF NOT EXISTS cost_centers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    voucher_no TEXT UNIQUE,
    expense_date TEXT NOT NULL,
    cost_center_id INTEGER REFERENCES cost_centers(id),
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

CREATE TABLE IF NOT EXISTS expense_lines (
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
    employee_id INTEGER REFERENCES employees(id),
    employee_name TEXT,
    employee_phone TEXT,
    -- The single seam between Expense Program and the Asset Module. Nullable,
    -- so every line ever recorded before the module existed is unaffected.
    -- An asset's whole cost history is read back out of this column; it is
    -- never copied into the asset tables. See the ASSET & MAINTENANCE MODULE
    -- block below.
    asset_id INTEGER REFERENCES assets(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_expense_lines_expense ON expense_lines(expense_id);
CREATE INDEX IF NOT EXISTS idx_expense_lines_category ON expense_lines(category_id);
CREATE INDEX IF NOT EXISTS idx_expense_lines_supplier ON expense_lines(supplier_id);
CREATE INDEX IF NOT EXISTS idx_expense_lines_employee ON expense_lines(employee_id);
CREATE INDEX IF NOT EXISTS idx_expense_lines_asset ON expense_lines(asset_id);

-- A VAT-able line's "knowledge base" supplier - see
-- app/programs/expenses/services/suppliers.py. Soft-delete only, same as
-- everything else with a financial trail: deleting a supplier never
-- touches expense_lines.supplier_id or this row's own name/vat_number, so
-- every existing voucher line keeps showing exactly what it always did -
-- only the supplier's own status flips, and it drops out of the picker for
-- *new* lines. vat_number is unique (it's how a repeat entry is found and
-- reused instead of retyped).
CREATE TABLE IF NOT EXISTS suppliers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    vat_number TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','deleted')),
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    deleted_by INTEGER REFERENCES users(id),
    deleted_at TEXT,
    delete_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_suppliers_vat ON suppliers(vat_number);
CREATE INDEX IF NOT EXISTS idx_suppliers_status ON suppliers(status);

CREATE TABLE IF NOT EXISTS voucher_counters (
    year TEXT PRIMARY KEY,
    seq INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS document_counters (
    doc_type TEXT NOT NULL,
    year TEXT NOT NULL,
    seq INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (doc_type, year)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    action TEXT NOT NULL,
    details TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS role_permissions (
    role TEXT NOT NULL,
    permission TEXT NOT NULL,
    allowed INTEGER NOT NULL DEFAULT 0,
    updated_by INTEGER REFERENCES users(id),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (role, permission)
);

CREATE TABLE IF NOT EXISTS network_configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    hostname TEXT NOT NULL UNIQUE,
    notes TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses(expense_date);
CREATE INDEX IF NOT EXISTS idx_expenses_status ON expenses(status);

-- Weekly Productions program - see app/programs/weekly_productions/. A
-- production report is a header (date, production line, shift, notes) that
-- can hold one or more lines - each line is one material spec (GI/HDG,
-- width/height/length/thickness, quantity) - mirroring Expense Program's
-- voucher (header) + expense_lines shape. This replaced an earlier flat
-- one-row-per-entry shape; see _migrate_flat_production_entries() below for
-- databases that were created under that shape.
CREATE TABLE IF NOT EXISTS production_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_no TEXT UNIQUE,
    report_date TEXT NOT NULL,
    line_name TEXT NOT NULL,
    shift TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'recorded' CHECK(status IN ('recorded','deleted')),
    recorded_by INTEGER NOT NULL REFERENCES users(id),
    deleted_by INTEGER REFERENCES users(id),
    deleted_at TEXT,
    delete_reason TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS production_report_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL REFERENCES production_reports(id),
    line_no INTEGER NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    material_type TEXT NOT NULL CHECK(material_type IN ('GI','HDG')),
    width REAL NOT NULL,
    height REAL NOT NULL,
    length REAL NOT NULL,
    thickness REAL NOT NULL,
    quantity REAL NOT NULL,
    weight_kg REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Editable weight-calculation factor per material (Settings -> Weekly
-- Productions -> Weight calculation). Purely additive, one row per material
-- key; a missing row falls back to a hardcoded default in the service
-- layer, same pattern as role_has_permission()'s fallback to
-- DEFAULT_ROLE_PERMISSIONS.
CREATE TABLE IF NOT EXISTS production_settings (
    key TEXT PRIMARY KEY,
    value REAL NOT NULL,
    updated_by INTEGER REFERENCES users(id),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- A locked month blocks *new* production reports dated in it (see
-- production.create_report()) - a manager-driven period close. Deleting an
-- existing report from a locked month is still allowed (with the usual
-- reason, same as any other deletion) - correcting a mistake isn't the
-- same as backdating new production into a closed period. Purely
-- additive, no migration needed.
CREATE TABLE IF NOT EXISTS production_locked_months (
    month TEXT PRIMARY KEY,
    locked_by INTEGER NOT NULL REFERENCES users(id),
    locked_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_production_reports_date ON production_reports(report_date);
CREATE INDEX IF NOT EXISTS idx_production_reports_status ON production_reports(status);
CREATE INDEX IF NOT EXISTS idx_production_report_lines_report ON production_report_lines(report_id);
CREATE INDEX IF NOT EXISTS idx_production_report_lines_material ON production_report_lines(material_type);

-- ============================================================
-- ERP TIER-1 ADDITIONS
-- All additive: CREATE TABLE IF NOT EXISTS / ALTER TABLE ADD COLUMN.
-- No migration function needed for any of these.
-- ============================================================

-- Cash float / imprest ledger ----------------------------------
-- Single-row table: current cash balance in the petty-cash box.
-- Never updated directly; always via cash_movements.
CREATE TABLE IF NOT EXISTS cash_float (
    id          INTEGER PRIMARY KEY CHECK(id = 1),  -- singleton guard
    balance     REAL    NOT NULL DEFAULT 0,
    updated_by  INTEGER NOT NULL REFERENCES users(id),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Every movement that touches the float (top-ups, approvals, rejections).
-- reference_id is the expenses.id for voucher-driven movements, NULL for
-- manual top-ups / adjustments.
CREATE TABLE IF NOT EXISTS cash_movements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    movement_type   TEXT NOT NULL CHECK(movement_type IN
                        ('topup','voucher_approved','voucher_rejected','manual_adjustment')),
    reference_id    INTEGER REFERENCES expenses(id),
    amount          REAL NOT NULL,   -- positive = cash in, negative = cash out
    note            TEXT NOT NULL DEFAULT '',
    created_by      INTEGER NOT NULL REFERENCES users(id),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_cash_movements_type ON cash_movements(movement_type);
CREATE INDEX IF NOT EXISTS idx_cash_movements_ref  ON cash_movements(reference_id);

-- Period close / month lock (expenses) -------------------------
-- Mirrors production_locked_months exactly.
-- Locking a month blocks new vouchers dated in it; existing vouchers
-- in locked months can still be approved/rejected/deleted via normal flow.
CREATE TABLE IF NOT EXISTS expense_locked_months (
    month       TEXT PRIMARY KEY,       -- YYYY-MM
    locked_by   INTEGER NOT NULL REFERENCES users(id),
    locked_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Budget vs Actual ---------------------------------------------
-- cost_center_id NULL = company-wide budget (not per-dept).
-- category_id    NULL = all categories combined.
-- period is YYYY-MM. UNIQUE constraint prevents duplicate budgets for the
-- same (dept, category, month) combination.
CREATE TABLE IF NOT EXISTS budgets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    cost_center_id  INTEGER REFERENCES cost_centers(id),
    category_id     INTEGER REFERENCES categories(id),
    period          TEXT NOT NULL,          -- YYYY-MM
    amount          REAL NOT NULL CHECK(amount > 0),
    created_by      INTEGER NOT NULL REFERENCES users(id),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(cost_center_id, category_id, period)
);

CREATE INDEX IF NOT EXISTS idx_budgets_period ON budgets(period);

-- Notifications (in-app, manager-facing) ----------------------
-- Budget over-run alerts land here. read=0 until manager dismisses them.
CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    message     TEXT NOT NULL,
    link        TEXT NOT NULL DEFAULT '',
    -- Urgency band the notification centre groups by. Deliberately a plain
    -- TEXT with a DEFAULT and no CHECK constraint: a CHECK would make every
    -- future band a table-rebuild migration, and the grouping code already
    -- treats an unknown value as 'info'. See app/services/notifications.py.
    level       TEXT NOT NULL DEFAULT 'info',
    read        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, read);

-- Who a voucher has been handed to for attention (the accountant, usually).
-- Purely additive: nothing else reads or writes it, so an existing database
-- picks it up with CREATE TABLE IF NOT EXISTS and needs no migration.
-- Kept as its own table rather than only firing a notification so the
-- voucher can show "shared with X on Y" permanently - a notification is
-- read once and gone, a handoff is a fact about the record.
CREATE TABLE IF NOT EXISTS expense_shares (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    expense_id  INTEGER NOT NULL REFERENCES expenses(id),
    shared_with INTEGER NOT NULL REFERENCES users(id),
    shared_by   INTEGER NOT NULL REFERENCES users(id),
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_expense_shares_expense ON expense_shares(expense_id);
CREATE INDEX IF NOT EXISTS idx_expense_shares_user ON expense_shares(shared_with);

-- ============================================================
-- EMPLOYEE ADVANCES (Tier-2 ERP)
-- All additive. expense_lines gets two new nullable columns via
-- _migrate_add_advance_line_fields() below.
-- ============================================================

-- Employee registry. Auto-created on first advance; soft-delete only.
-- name+phone is the dedup key: same pair on a new advance reuses the row.
CREATE TABLE IF NOT EXISTS employees (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    phone       TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'active' CHECK(status IN ('active','deleted')),
    created_by  INTEGER NOT NULL REFERENCES users(id),
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    deleted_by  INTEGER REFERENCES users(id),
    deleted_at  TEXT,
    -- Links this employee record to a login account, so an asset assigned to
    -- them can be recognised as belonging to the person currently logged in
    -- (see asset_service_log's assignee gate). Nullable on purpose: an
    -- employee who only ever receives salary advances and never signs into
    -- the app keeps this NULL forever.
    user_id     INTEGER REFERENCES users(id),
    UNIQUE(name, phone)
);

CREATE INDEX IF NOT EXISTS idx_employees_status ON employees(status);
CREATE INDEX IF NOT EXISTS idx_employees_user   ON employees(user_id);

-- Every debit (advance) and credit (settlement) against an employee.
-- expense_line_id links a debit to the exact voucher line that created it.
-- settlement_method only populated for settlement rows.
-- pending_approval: 1 when created by a plain user role and not yet approved.
CREATE TABLE IF NOT EXISTS advance_ledger (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id             INTEGER NOT NULL REFERENCES employees(id),
    movement_type           TEXT    NOT NULL CHECK(movement_type IN ('advance','settlement')),
    amount                  REAL    NOT NULL,
    expense_line_id         INTEGER REFERENCES expense_lines(id),
    reference_expense_id    INTEGER REFERENCES expenses(id),
    settlement_method       TEXT CHECK(settlement_method IN
                                ('Cash returned to box','Salary deduction','Bank transfer')),
    note                    TEXT    NOT NULL DEFAULT '',
    pending_approval        INTEGER NOT NULL DEFAULT 0,
    approved_by             INTEGER REFERENCES users(id),
    approved_at             TEXT,
    created_by              INTEGER NOT NULL REFERENCES users(id),
    created_at              TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_advance_ledger_employee ON advance_ledger(employee_id);
CREATE INDEX IF NOT EXISTS idx_advance_ledger_type     ON advance_ledger(movement_type);
CREATE INDEX IF NOT EXISTS idx_advance_ledger_pending  ON advance_ledger(pending_approval);

-- ============================================================
-- WORD EDITOR PROGRAM
-- All tables additive: CREATE TABLE IF NOT EXISTS only.
-- No migration function needed.
-- ============================================================

CREATE TABLE IF NOT EXISTS wd_company_profile (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name      TEXT NOT NULL DEFAULT '',
    logo_path         TEXT,
    phone             TEXT NOT NULL DEFAULT '',
    document_location TEXT NOT NULL DEFAULT '',
    website           TEXT NOT NULL DEFAULT '',
    company_location  TEXT NOT NULL DEFAULT '',
    company_phone     TEXT NOT NULL DEFAULT '',
    is_active         INTEGER NOT NULL DEFAULT 1,
    updated_by        INTEGER REFERENCES users(id),
    approved_by       INTEGER REFERENCES users(id),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS wd_profile_change_requests (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    requested_by      INTEGER NOT NULL REFERENCES users(id),
    company_name      TEXT NOT NULL DEFAULT '',
    logo_path         TEXT,
    phone             TEXT NOT NULL DEFAULT '',
    document_location TEXT NOT NULL DEFAULT '',
    website           TEXT NOT NULL DEFAULT '',
    company_location  TEXT NOT NULL DEFAULT '',
    company_phone     TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'pending'
                      CHECK(status IN ('pending','approved','rejected')),
    reviewed_by       INTEGER REFERENCES users(id),
    reviewed_at       TEXT,
    reject_reason     TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS wd_documents (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_no              TEXT UNIQUE,
    title               TEXT NOT NULL DEFAULT 'Untitled',
    subject             TEXT NOT NULL DEFAULT '',
    receiver_name       TEXT NOT NULL DEFAULT '',
    receiver_phone      TEXT NOT NULL DEFAULT '',
    content_json        TEXT NOT NULL DEFAULT '{}',
    status              TEXT NOT NULL DEFAULT 'draft'
                        CHECK(status IN ('draft','self_approved','official','deleted')),
    created_by          INTEGER NOT NULL REFERENCES users(id),
    self_approved_by    INTEGER REFERENCES users(id),
    self_approved_at    TEXT,
    manager_approved_by INTEGER REFERENCES users(id),
    manager_approved_at TEXT,
    manager_note        TEXT,
    deleted_by          INTEGER REFERENCES users(id),
    deleted_at          TEXT,
    delete_reason       TEXT,
    search_text         TEXT NOT NULL DEFAULT '',
    official_sha        TEXT NOT NULL DEFAULT '',
    version_no          INTEGER NOT NULL DEFAULT 0,
    last_edited_at      TEXT NOT NULL DEFAULT (datetime('now')),
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_wd_documents_status ON wd_documents(status);
CREATE INDEX IF NOT EXISTS idx_wd_documents_created_by ON wd_documents(created_by);

CREATE TABLE IF NOT EXISTS wd_user_templates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    document_id INTEGER NOT NULL REFERENCES wd_documents(id),
    slot        INTEGER NOT NULL CHECK(slot BETWEEN 1 AND 5),
    label       TEXT NOT NULL DEFAULT '',
    UNIQUE(user_id, slot)
);

CREATE TABLE IF NOT EXISTS wd_universal_templates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    content_json TEXT NOT NULL DEFAULT '{}',
    subject     TEXT NOT NULL DEFAULT '',
    created_by  INTEGER NOT NULL REFERENCES users(id),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS wd_universal_template_assignments (
    template_id INTEGER NOT NULL REFERENCES wd_universal_templates(id),
    user_id     INTEGER NOT NULL REFERENCES users(id),
    assigned_by INTEGER NOT NULL REFERENCES users(id),
    assigned_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (template_id, user_id)
);

CREATE TABLE IF NOT EXISTS wd_subject_templates (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    subject    TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, subject)
);

CREATE TABLE IF NOT EXISTS wd_universal_subjects (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    subject    TEXT NOT NULL,
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS wd_universal_subject_assignments (
    subject_id INTEGER NOT NULL REFERENCES wd_universal_subjects(id),
    user_id    INTEGER NOT NULL REFERENCES users(id),
    PRIMARY KEY (subject_id, user_id)
);

CREATE TABLE IF NOT EXISTS wd_document_tags (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id        INTEGER NOT NULL REFERENCES wd_documents(id),
    tag_type           TEXT NOT NULL CHECK(tag_type IN ('user','location')),
    tagged_user_id     INTEGER REFERENCES users(id),
    tagged_location_id INTEGER REFERENCES cost_centers(id),
    tagged_by          INTEGER NOT NULL REFERENCES users(id),
    tagged_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_wd_doc_tags_doc  ON wd_document_tags(document_id);
CREATE INDEX IF NOT EXISTS idx_wd_doc_tags_user ON wd_document_tags(tagged_user_id);
CREATE INDEX IF NOT EXISTS idx_wd_doc_tags_loc  ON wd_document_tags(tagged_location_id);

CREATE TABLE IF NOT EXISTS wd_comments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES wd_documents(id),
    parent_id   INTEGER REFERENCES wd_comments(id),
    author_id   INTEGER NOT NULL REFERENCES users(id),
    content     TEXT NOT NULL,
    edited_at   TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_wd_comments_doc    ON wd_comments(document_id);
CREATE INDEX IF NOT EXISTS idx_wd_comments_parent ON wd_comments(parent_id);

CREATE TABLE IF NOT EXISTS wd_comment_tags (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    comment_id         INTEGER NOT NULL REFERENCES wd_comments(id),
    tag_type           TEXT NOT NULL CHECK(tag_type IN ('user','location')),
    tagged_user_id     INTEGER REFERENCES users(id),
    tagged_location_id INTEGER REFERENCES cost_centers(id)
);

CREATE INDEX IF NOT EXISTS idx_wd_ctags_comment ON wd_comment_tags(comment_id);
CREATE INDEX IF NOT EXISTS idx_wd_ctags_user    ON wd_comment_tags(tagged_user_id);
CREATE INDEX IF NOT EXISTS idx_wd_ctags_loc     ON wd_comment_tags(tagged_location_id);

CREATE TABLE IF NOT EXISTS wd_user_profiles (
    user_id      INTEGER PRIMARY KEY REFERENCES users(id),
    display_name TEXT NOT NULL DEFAULT '',
    phone        TEXT NOT NULL DEFAULT '',
    access_note  TEXT NOT NULL DEFAULT '',
    updated_by   INTEGER REFERENCES users(id),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS wd_user_locations (
    user_id        INTEGER NOT NULL REFERENCES users(id),
    cost_center_id INTEGER NOT NULL REFERENCES cost_centers(id),
    assigned_by    INTEGER NOT NULL REFERENCES users(id),
    assigned_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, cost_center_id)
);

CREATE INDEX IF NOT EXISTS idx_wd_user_locs_user ON wd_user_locations(user_id);
CREATE INDEX IF NOT EXISTS idx_wd_user_locs_cc   ON wd_user_locations(cost_center_id);

-- Word Editor: version history, official snapshots, full-text search body.
-- All additive (new tables + ALTER TABLE ADD COLUMN via
-- _migrate_add_wd_version_fields() below) - no table is rebuilt.

CREATE TABLE IF NOT EXISTS wd_document_versions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id  INTEGER NOT NULL REFERENCES wd_documents(id),
    version_no   INTEGER NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    subject      TEXT NOT NULL DEFAULT '',
    content_json TEXT NOT NULL DEFAULT '',
    content_sha  TEXT NOT NULL DEFAULT '',
    is_snapshot  INTEGER NOT NULL DEFAULT 0,
    note         TEXT NOT NULL DEFAULT '',
    edited_by    INTEGER NOT NULL REFERENCES users(id),
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (document_id, version_no)
);

CREATE INDEX IF NOT EXISTS idx_wd_versions_doc ON wd_document_versions(document_id);

-- ============================================================
-- ASSET & MAINTENANCE MODULE
-- All additive: CREATE TABLE IF NOT EXISTS / ALTER TABLE ADD COLUMN
-- (see _migrate_add_asset_module_fields() below for existing databases).
-- No table is rebuilt, no CHECK constraint on an existing table changes,
-- so this needs no rename/copy/drop migration of the kind
-- _migrate_single_line_vouchers() does.
--
-- The money rule this module is built around: an asset's cost history is
-- ALWAYS read from approved expense_lines, never stored again here. That
-- is why asset_service_log deliberately has no amount column - a second
-- place to record money is a second grand total that drifts from the
-- first (app/backup.py has been bitten by exactly that twice already).
-- ============================================================

CREATE TABLE IF NOT EXISTS assets (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_tag      TEXT UNIQUE NOT NULL,
    name           TEXT NOT NULL,
    category       TEXT NOT NULL CHECK(category IN
                     ('Fleet/Truck','Heavy Machine','IT/Office','Furniture','Other')),
    serial_number  TEXT NOT NULL DEFAULT '',
    purchase_date  TEXT,
    purchase_cost  REAL NOT NULL DEFAULT 0,   -- VAT-EXCLUSIVE, same as every
                                              -- other amount in this app
    cost_center_id INTEGER REFERENCES cost_centers(id),
    notes          TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'active' CHECK(status IN
                     ('active','in_maintenance','retired','divested','deleted')),
    -- Divestment is a property of the ASSET, not of a custody row: an asset
    -- sold while sitting in a yard has no holder, so there is no assignment
    -- row for the proceeds to live on. Recorded here for the asset report
    -- only - deliberately never posted to cash_movements, since money from
    -- selling a truck is not petty cash and would otherwise show up as float
    -- that never physically entered the box.
    divested_on     TEXT,
    divest_proceeds REAL,
    divest_reason   TEXT,
    divested_by     INTEGER REFERENCES users(id),
    created_by     INTEGER NOT NULL REFERENCES users(id),
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    deleted_by     INTEGER REFERENCES users(id),
    deleted_at     TEXT,
    delete_reason  TEXT
);

CREATE INDEX IF NOT EXISTS idx_assets_category ON assets(category);
CREATE INDEX IF NOT EXISTS idx_assets_status   ON assets(status);

-- Per-category asset tag format. Manager-editable at Settings -> Assets,
-- deliberately NOT a hardcoded constant. An empty pattern means free text
-- and no check at all; a missing row falls back to the coded default in
-- app/config.py's DEFAULT_ASSET_TAG_RULES - the same fallback shape
-- role_has_permission() uses against DEFAULT_ROLE_PERMISSIONS, and for the
-- same reason: a fresh install and a never-customised one behave alike.
CREATE TABLE IF NOT EXISTS asset_tag_rules (
    category    TEXT PRIMARY KEY CHECK(category IN
                  ('Fleet/Truck','Heavy Machine','IT/Office','Furniture','Other')),
    pattern     TEXT NOT NULL DEFAULT '',
    example     TEXT NOT NULL DEFAULT '',
    updated_by  INTEGER REFERENCES users(id),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Chain of custody. Open/closed rows, NOT latest-row-wins: one asset can
-- legitimately have several simultaneous active holders (a day-shift and a
-- night-shift driver on the same truck), so there is deliberately NO
-- unique index on asset_id. Current holders = status='active' rows.
-- A row is never deleted; closing one sets returned_date/status/closed_by.
CREATE TABLE IF NOT EXISTS asset_assignments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id       INTEGER NOT NULL REFERENCES assets(id),
    employee_id    INTEGER REFERENCES employees(id),
    cost_center_id INTEGER REFERENCES cost_centers(id),
    role_note      TEXT NOT NULL DEFAULT '',
    issued_date    TEXT NOT NULL,
    returned_date  TEXT,
    status         TEXT NOT NULL DEFAULT 'active' CHECK(status IN
                     ('active','returned','transferred','divested')),
    assigned_by    INTEGER NOT NULL REFERENCES users(id),
    closed_by      INTEGER REFERENCES users(id),
    close_reason   TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (employee_id IS NOT NULL OR cost_center_id IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_assignments_asset    ON asset_assignments(asset_id);
CREATE INDEX IF NOT EXISTS idx_assignments_employee ON asset_assignments(employee_id);
CREATE INDEX IF NOT EXISTS idx_assignments_status   ON asset_assignments(status);

-- Non-cost maintenance notes: greasing, a nut fitted, an inspection.
-- NO amount column, by design - see the money rule at the top of this
-- block. Anything that cost money goes on a voucher line instead, which
-- reaches this asset via expense_lines.asset_id.
-- Soft-delete only, same as every other record with a history worth keeping.
CREATE TABLE IF NOT EXISTS asset_service_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id       INTEGER NOT NULL REFERENCES assets(id),
    service_date   TEXT NOT NULL,
    service_type   TEXT NOT NULL CHECK(service_type IN
                     ('Greasing/Lubrication','Part fitted','Part removed',
                      'Inspection','Cleaning','Adjustment','Breakdown note','Other')),
    description    TEXT NOT NULL,
    parts_used     TEXT NOT NULL DEFAULT '',
    meter_reading  REAL,
    meter_unit     TEXT CHECK(meter_unit IN ('km','hours')),
    photo_path     TEXT,
    status         TEXT NOT NULL DEFAULT 'recorded' CHECK(status IN
                     ('recorded','deleted')),
    recorded_by    INTEGER NOT NULL REFERENCES users(id),
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    deleted_by     INTEGER REFERENCES users(id),
    deleted_at     TEXT,
    delete_reason  TEXT
);

CREATE INDEX IF NOT EXISTS idx_service_log_asset  ON asset_service_log(asset_id);
CREATE INDEX IF NOT EXISTS idx_service_log_date   ON asset_service_log(service_date);
CREATE INDEX IF NOT EXISTS idx_service_log_status ON asset_service_log(status);
"""


def get_connection():
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def get_db():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _migrate_single_line_vouchers(conn):
    """Vouchers used to be one row = one transaction: category, particulars,
    and amount all lived directly on the expenses table. Now a voucher is a
    header with one or more expense_lines (so a single voucher can hold
    multiple transactions, some VAT-able and some not). This detects a
    database still in the old shape and migrates it in place before the
    schema script below runs - a fresh install never touches this path
    since it has no `expenses` table yet.

    Each existing voucher becomes a header row plus exactly one line item
    carrying its old category/particulars/amount, with is_vatable left at 0
    (false) since historical entries never recorded VAT status - a manager
    can edit that after the fact if needed."""
    existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='expenses'"
    ).fetchone()
    if not existing:
        return  # fresh install - nothing to migrate
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(expenses)").fetchall()}
    if "category_id" not in cols:
        return  # already migrated

    conn.execute("ALTER TABLE expenses RENAME TO expenses_old_single_line")
    conn.executescript("""
        CREATE TABLE expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            voucher_no TEXT UNIQUE NOT NULL,
            expense_date TEXT NOT NULL,
            cost_center_id INTEGER REFERENCES cost_centers(id),
            paid_to TEXT NOT NULL,
            payment_mode TEXT NOT NULL DEFAULT 'Cash',
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected','deleted')),
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
        CREATE TABLE IF NOT EXISTS expense_lines (
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
    """)

    old_rows = conn.execute("SELECT * FROM expenses_old_single_line").fetchall()
    for row in old_rows:
        conn.execute(
            "INSERT INTO expenses (id, voucher_no, expense_date, cost_center_id, paid_to, "
            "payment_mode, status, receipt_path, prepared_by, approved_by, approved_at, "
            "rejection_reason, deleted_by, deleted_at, delete_reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (row["id"], row["voucher_no"], row["expense_date"], row["cost_center_id"],
             row["paid_to"], row["payment_mode"], row["status"], row["receipt_path"],
             row["prepared_by"], row["approved_by"], row["approved_at"],
             row["rejection_reason"], row["deleted_by"], row["deleted_at"],
             row["delete_reason"], row["created_at"]),
        )
        conn.execute(
            "INSERT INTO expense_lines (expense_id, line_no, category_id, particulars, amount, "
            "is_vatable, vat_rate, vat_amount) VALUES (?, 1, ?, ?, ?, 0, 0, 0)",
            (row["id"], row["category_id"], row["particulars"], row["amount"]),
        )

    conn.execute("DROP TABLE expenses_old_single_line")


def _migrate_add_draft_status(conn):
    """Vouchers used to always be a real, numbered transaction the moment
    they were created - `voucher_no` was NOT NULL and `status` had no
    'draft' option. Now a voucher can be saved as an unfinished draft
    (no voucher number allocated yet, freely editable, hard-deletable by
    its own owner) before being submitted for approval. This detects a
    database still in the old (draft-incapable) shape and loosens those
    two constraints in place - SQLite can't ALTER a CHECK constraint or a
    NOT NULL constraint directly, so this uses the same rename/recreate/
    copy/drop pattern as `_migrate_single_line_vouchers`.

    Runs after that migration (operates on the header + expense_lines
    shape, never the ancient single-transaction shape) but before the main
    `SCHEMA` script, which would otherwise silently no-op via
    `CREATE TABLE IF NOT EXISTS` against a table that already exists in
    the old shape.

    Renaming `expenses` makes SQLite automatically rewrite expense_lines'
    FK definition to point at the *renamed* table rather than the new one
    (confirmed by inspecting sqlite_master after a rename) - dropping the
    renamed table then leaves expense_lines permanently referencing a
    table that no longer exists, silently breaking every future INSERT
    into it with "no such table". expense_lines is rebuilt too, for
    exactly this reason - not just to work around the DROP failing."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='expenses'"
    ).fetchone()
    if not row:
        return  # fresh install - nothing to migrate
    if "draft" in row["sql"]:
        return  # already migrated

    conn.commit()  # PRAGMA foreign_keys is a no-op mid-transaction - close out
                   # any pending work from _migrate_single_line_vouchers first
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("ALTER TABLE expenses RENAME TO expenses_old_no_draft")
    conn.executescript("""
        CREATE TABLE expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            voucher_no TEXT UNIQUE,
            expense_date TEXT NOT NULL,
            cost_center_id INTEGER REFERENCES cost_centers(id),
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
    """)
    old_rows = conn.execute("SELECT * FROM expenses_old_no_draft").fetchall()
    for r in old_rows:
        conn.execute(
            "INSERT INTO expenses (id, voucher_no, expense_date, cost_center_id, paid_to, "
            "payment_mode, status, receipt_path, prepared_by, approved_by, approved_at, "
            "rejection_reason, deleted_by, deleted_at, delete_reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (r["id"], r["voucher_no"], r["expense_date"], r["cost_center_id"],
             r["paid_to"], r["payment_mode"], r["status"], r["receipt_path"],
             r["prepared_by"], r["approved_by"], r["approved_at"],
             r["rejection_reason"], r["deleted_by"], r["deleted_at"],
             r["delete_reason"], r["created_at"]),
        )
    conn.execute("DROP TABLE expenses_old_no_draft")

    conn.execute("ALTER TABLE expense_lines RENAME TO expense_lines_old_no_draft")
    conn.executescript("""
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
    """)
    conn.execute("INSERT INTO expense_lines SELECT * FROM expense_lines_old_no_draft")
    conn.execute("DROP TABLE expense_lines_old_no_draft")

    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")


def _migrate_flat_production_entries(conn):
    """Weekly Productions used to be one row = one entry: product_name,
    quantity, and unit lived directly on a flat `production_entries` table,
    with no way to record a material's dimensions. Now a production report
    is a header (date, line, shift, notes) holding one or more lines, each
    a material spec (GI/HDG, width/height/length/thickness, quantity) - the
    same header + lines shape as Expense Program's vouchers. This detects a
    database still in the old flat shape and migrates it in place before
    the main SCHEMA script runs, following the same rename/recreate/copy/
    drop pattern as _migrate_single_line_vouchers().

    The old shape had no material-spec fields at all, so nothing here can
    be losslessly carried into a line's material_type/dimensions - each
    migrated report gets exactly one line defaulted to material_type='GI'
    with all dimensions at 0, and the original product_name/quantity/unit
    preserved in plain text in the header's notes so nothing is silently
    dropped. A manager should review migrated reports and correct the line
    to the real material spec.
    """
    existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='production_entries'"
    ).fetchone()
    if not existing:
        return  # fresh install, or already migrated (old table already dropped)

    conn.execute("ALTER TABLE production_entries RENAME TO production_entries_old_flat")
    conn.executescript("""
        CREATE TABLE production_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_no TEXT UNIQUE,
            report_date TEXT NOT NULL,
            line_name TEXT NOT NULL,
            shift TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'recorded' CHECK(status IN ('recorded','deleted')),
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
            material_type TEXT NOT NULL CHECK(material_type IN ('GI','HDG')),
            width REAL NOT NULL,
            height REAL NOT NULL,
            length REAL NOT NULL,
            thickness REAL NOT NULL,
            quantity REAL NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)

    old_rows = conn.execute("SELECT * FROM production_entries_old_flat").fetchall()
    for row in old_rows:
        legacy_note = (f"Migrated from legacy entry {row['entry_no']}: "
                        f"{row['quantity']:g} {row['unit']} of {row['product_name']}. "
                        f"Material spec unavailable in the old format - line defaulted to "
                        f"GI / 0x0x0x0; please correct.")
        notes = (row["notes"] + " — " + legacy_note) if row["notes"] else legacy_note
        conn.execute(
            "INSERT INTO production_reports (id, report_no, report_date, line_name, shift, "
            "notes, status, recorded_by, deleted_by, deleted_at, delete_reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (row["id"], row["entry_no"], row["entry_date"], row["line_name"], row["shift"],
             notes, row["status"], row["recorded_by"], row["deleted_by"], row["deleted_at"],
             row["delete_reason"], row["created_at"]),
        )
        conn.execute(
            "INSERT INTO production_report_lines (report_id, line_no, material_type, width, "
            "height, length, thickness, quantity) VALUES (?, 1, 'GI', 0, 0, 0, 0, ?)",
            (row["id"], row["quantity"]),
        )
    conn.execute("DROP TABLE production_entries_old_flat")


def _migrate_add_line_description_and_weight(conn):
    """Adds `description` and `weight_kg` to production_report_lines for a
    database created before those columns existed. Both are simple ADD
    COLUMNs with a constant default - no rename/copy/drop dance needed,
    unlike a NOT NULL/CHECK change (see the Schema changes procedure in
    CHANGE_IMPACT_GUIDE.md for when that's required instead). No-ops on a
    fresh install (table doesn't exist yet) or an already-migrated one.
    weight_kg is always the single total per-piece weight regardless of
    line shape (straight/elbow/tee) - see parse_line_input()."""
    existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='production_report_lines'"
    ).fetchone()
    if not existing:
        return  # fresh install - SCHEMA below creates the table in its final shape
    cols = {r["name"] for r in conn.execute(
        "PRAGMA table_info(production_report_lines)"
    ).fetchall()}
    if "description" not in cols:
        conn.execute(
            "ALTER TABLE production_report_lines ADD COLUMN description TEXT NOT NULL DEFAULT ''"
        )
    if "weight_kg" not in cols:
        conn.execute(
            "ALTER TABLE production_report_lines ADD COLUMN weight_kg REAL NOT NULL DEFAULT 0"
        )


def _migrate_add_expense_line_supplier(conn):
    """Adds `expense_lines.supplier_id` (and creates the `suppliers` table
    if it doesn't exist yet) for a database created before the supplier
    knowledge-base feature. A simple ADD COLUMN with no default data to
    backfill - every pre-existing line just gets supplier_id=NULL, exactly
    as if it had never been VAT-able as far as supplier tracking goes; only
    a manager fixing it up by hand (or a future migration, if ever needed)
    would attach a supplier retroactively. No-ops on a fresh install or an
    already-migrated one."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS suppliers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            vat_number TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','deleted')),
            created_by INTEGER NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            deleted_by INTEGER REFERENCES users(id),
            deleted_at TEXT,
            delete_reason TEXT
        )
    """)
    existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='expense_lines'"
    ).fetchone()
    if not existing:
        return  # fresh install - SCHEMA below creates expense_lines in its final shape
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(expense_lines)").fetchall()}
    if "supplier_id" not in cols:
        conn.execute("ALTER TABLE expense_lines ADD COLUMN supplier_id INTEGER REFERENCES suppliers(id)")


def _migrate_add_category_gl_code(conn):
    """Add gl_code column to categories for existing databases.
    Purely additive - new installs get it via SCHEMA automatically.
    Guard against fresh install where categories table doesn't exist yet
    (SCHEMA hasn't run) - same pattern as _migrate_add_expense_line_supplier."""
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='categories'"
    ).fetchone()
    if not table_exists:
        return  # fresh install - SCHEMA below creates the table in its final shape
    cols = [r[1] for r in conn.execute("PRAGMA table_info(categories)").fetchall()]
    if "gl_code" not in cols:
        conn.execute("ALTER TABLE categories ADD COLUMN gl_code TEXT")


def _migrate_add_advance_line_fields(conn):
    """Add employee_id, employee_name, employee_phone to expense_lines for
    existing databases. Purely additive nullable columns."""
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='expense_lines'"
    ).fetchone()
    if not table_exists:
        return
    cols = [r[1] for r in conn.execute("PRAGMA table_info(expense_lines)").fetchall()]
    if "employee_id" not in cols:
        conn.execute("ALTER TABLE expense_lines ADD COLUMN employee_id INTEGER REFERENCES employees(id)")
    if "employee_name" not in cols:
        conn.execute("ALTER TABLE expense_lines ADD COLUMN employee_name TEXT")
    if "employee_phone" not in cols:
        conn.execute("ALTER TABLE expense_lines ADD COLUMN employee_phone TEXT")


def _migrate_add_asset_module_fields(conn):
    """Adds the three additive columns the Asset & Maintenance module needs
    on tables that already exist: `expense_lines.asset_id`,
    `employees.user_id`, and `categories.requires_asset` /
    `categories.asset_category`.

    Every one is a nullable (or defaulted) ADD COLUMN with nothing to
    backfill, so there is no rename/copy/drop step here - the asset module's
    own tables are plain CREATE TABLE IF NOT EXISTS in SCHEMA and need no
    migration at all. Same shape as _migrate_add_advance_line_fields().

    Note the forward reference: `asset_id INTEGER REFERENCES assets(id)` is
    added before SCHEMA has created the `assets` table. SQLite accepts that
    (a foreign key is resolved when it's used, not when it's declared) and
    _migrate_add_advance_line_fields() already relies on exactly the same
    behaviour for employees(id). No-ops on a fresh install, where SCHEMA
    creates all three tables in their final shape, and on an
    already-migrated one.
    """
    def _columns(table):
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}

    def _table_exists(table):
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is not None

    if _table_exists("expense_lines") and "asset_id" not in _columns("expense_lines"):
        conn.execute("ALTER TABLE expense_lines ADD COLUMN asset_id INTEGER REFERENCES assets(id)")

    if _table_exists("employees") and "user_id" not in _columns("employees"):
        conn.execute("ALTER TABLE employees ADD COLUMN user_id INTEGER REFERENCES users(id)")

    if _table_exists("categories"):
        cat_cols = _columns("categories")
        if "requires_asset" not in cat_cols:
            conn.execute(
                "ALTER TABLE categories ADD COLUMN requires_asset INTEGER NOT NULL DEFAULT 0"
            )
        if "asset_category" not in cat_cols:
            conn.execute("ALTER TABLE categories ADD COLUMN asset_category TEXT")


def _migrate_add_wd_version_fields(conn):
    """Word Editor: add the search/snapshot columns to wd_documents.

    Purely additive `ALTER TABLE ... ADD COLUMN` on an existing database -
    no CHECK constraint changes, so no rename/copy/drop rebuild is needed
    (unlike _migrate_single_line_vouchers()).
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='wd_documents'"
    ).fetchone()
    if row is None:
        return                      # fresh install - SCHEMA creates it below
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(wd_documents)")}
    for name, ddl in (
        ("search_text", "ALTER TABLE wd_documents ADD COLUMN search_text TEXT NOT NULL DEFAULT ''"),
        ("official_sha", "ALTER TABLE wd_documents ADD COLUMN official_sha TEXT NOT NULL DEFAULT ''"),
        ("version_no", "ALTER TABLE wd_documents ADD COLUMN version_no INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in cols:
            conn.execute(ddl)
    conn.commit()


def _migrate_add_notification_level(conn):
    """Add notifications.level to an existing database.

    Purely additive `ALTER TABLE ... ADD COLUMN` with a DEFAULT, so every
    row already in the table becomes 'info' - which is exactly what those
    notifications were before urgency bands existed. No CHECK constraint is
    involved, so no rename/copy/drop rebuild is needed.
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='notifications'"
    ).fetchone()
    if row is None:
        return                      # fresh install - SCHEMA creates it below
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(notifications)")}
    if "level" not in cols:
        conn.execute(
            "ALTER TABLE notifications ADD COLUMN level TEXT NOT NULL DEFAULT 'info'"
        )
    conn.commit()


def init_schema():
    conn = get_connection()
    _migrate_single_line_vouchers(conn)
    _migrate_add_draft_status(conn)
    _migrate_flat_production_entries(conn)
    _migrate_add_line_description_and_weight(conn)
    _migrate_add_expense_line_supplier(conn)
    _migrate_add_category_gl_code(conn)
    _migrate_add_advance_line_fields(conn)
    _migrate_add_asset_module_fields(conn)
    _migrate_add_wd_version_fields(conn)
    _migrate_add_notification_level(conn)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def next_voucher_number(conn, year: str) -> str:
    """Atomically allocate the next voucher number for the given year."""
    cur = conn.execute("SELECT seq FROM voucher_counters WHERE year = ?", (year,))
    row = cur.fetchone()
    if row is None:
        conn.execute("INSERT INTO voucher_counters (year, seq) VALUES (?, 1)", (year,))
        seq = 1
    else:
        seq = row["seq"] + 1
        conn.execute("UPDATE voucher_counters SET seq = ? WHERE year = ?", (seq, year))
    return f"PCV-{year}-{seq:05d}"


def next_document_number(conn, doc_type: str, year: str) -> str:
    """Atomically allocates the next pre-numbered document reference for a
    generated report/export PDF, e.g. next_document_number(conn, 'RPT', '2026')
    -> 'RPT-2026-00001'. A separate counter from next_voucher_number() (and a
    separate table) - vouchers keep their own PCV-YYYY-NNNNN sequence exactly
    as before; this just gives every other kind of generated document the
    same pre-numbering guarantee."""
    cur = conn.execute(
        "SELECT seq FROM document_counters WHERE doc_type = ? AND year = ?", (doc_type, year)
    )
    row = cur.fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO document_counters (doc_type, year, seq) VALUES (?, ?, 1)", (doc_type, year)
        )
        seq = 1
    else:
        seq = row["seq"] + 1
        conn.execute(
            "UPDATE document_counters SET seq = ? WHERE doc_type = ? AND year = ?",
            (seq, doc_type, year),
        )
    return f"{doc_type}-{year}-{seq:05d}"


def log_action(conn, user_id, action, details=""):
    conn.execute(
        "INSERT INTO audit_log (user_id, action, details) VALUES (?, ?, ?)",
        (user_id, action, details),
    )
