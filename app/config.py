"""Central configuration, loaded from environment variables / .env file."""
import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.environ.get("SECRET_KEY", "insecure-dev-key-change-me")
CURRENCY_SYMBOL = os.environ.get("CURRENCY_SYMBOL", "SAR")
VAT_RATE = float(os.environ.get("VAT_RATE", "15.0"))  # percent; matches KSA VAT by default
COMPANY_NAME = os.environ.get("COMPANY_NAME", "Your Factory Name")
COMPANY_ADDRESS = os.environ.get("COMPANY_ADDRESS", "")

DEFAULT_ADMIN_USERNAME = os.environ.get("DEFAULT_ADMIN_USERNAME", "admin")
DEFAULT_ADMIN_PASSWORD = os.environ.get("DEFAULT_ADMIN_PASSWORD", "admin123")

DATA_DIR = Path(os.environ.get("PETTY_CASH_DATA_DIR", str(BASE_DIR / "data")))
DATABASE_PATH = DATA_DIR / "app.db"
UPLOADS_DIR = DATA_DIR / "uploads"

DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_CATEGORIES = [
    "Daily snacks & breakfast",
    "Fuel - Diesel/Petrol (Factory)",
    "Fuel - Vehicle/Truck",
    "Vehicle maintenance & repairs",
    "Drinking / consumption water",
    "Salary advance",
    "Miscellaneous petty cash",
]

ROLES = ["manager", "accountant", "user"]
# manager   - full access: create, approve/reject, DELETE (soft), manage users & settings, view audit log
# accountant- can view everything and approve/reject vouchers, cannot create/delete/manage users
# user      - records (creates) petty cash vouchers, cannot approve/reject/delete
#
# Adding/renaming/removing a *role* itself is a schema change (the `role`
# column has a SQLite CHECK constraint) - see CHANGE_IMPACT_GUIDE.md's
# "Roles / permissions" row and "Schema changes" section before touching
# this list. What each *existing* role is allowed to do, below, is not a
# schema change and is editable at runtime from Settings -> Users ->
# Roles & Permissions.

DEFAULT_CATEGORIES_MARKER = True  # (kept for readability in diffs)

# Category name that triggers employee advance tracking on a voucher line.
# Must match exactly one entry in DEFAULT_CATEGORIES (case-sensitive).
ADVANCE_CATEGORY_NAME = "Salary advance"

# --- Asset & Maintenance module ---
# The asset types an asset can be filed under. This IS a CHECK constraint in
# app/database.py (assets.category, asset_tag_rules.category), so adding one
# here alone is not enough - it's a schema change, the same way adding a role
# to ROLES is. See CHANGE_IMPACT_GUIDE.md's "Schema changes" section.
ASSET_CATEGORIES = [
    "Fleet/Truck",
    "Heavy Machine",
    "IT/Office",
    "Furniture",
    "Other",
]

# Fresh-install asset tag patterns, seeded into `asset_tag_rules` on first
# run and editable from Settings -> Assets thereafter. A blank pattern means
# free text with no check. A category with no row in the table at all falls
# back to this dict - the same fallback shape role_has_permission() uses
# against DEFAULT_ROLE_PERMISSIONS, so a fresh install and a
# never-customised one behave identically.
# Each entry: category -> (regex pattern, example shown in the form hint).
DEFAULT_ASSET_TAG_RULES = {
    "Fleet/Truck":   (r"^TR-\d{2,4}$",    "TR-04"),
    "Heavy Machine": (r"^PRESS-\d{2,4}$", "PRESS-01"),
    "IT/Office":     (r"^IT-\d{2,4}$",    "IT-113"),
    "Furniture":     ("",                  "any text"),
    "Other":         ("",                  "any text"),
}

# Categories ticked as requiring an asset on a fresh install, mapped to the
# asset type their picker is filtered to. Seeded ONCE (app/seed.py, first run
# only) into categories.requires_asset / categories.asset_category - after
# that it is entirely the manager's to change, and new maintenance categories
# can be added from Settings without touching this list. A name here that
# isn't in DEFAULT_CATEGORIES is simply created alongside them.
DEFAULT_ASSET_REQUIRED_CATEGORIES = {
    "Vehicle maintenance & repairs": "Fleet/Truck",
    "Machine maintenance & repairs": "Heavy Machine",
    "Office maintenance & repairs":  "IT/Office",
}

# --- role permission matrix ---
# The single source of truth for (a) which permissions exist, in what order
# and under what label/description they're shown on the Roles & Permissions
# page, and (b) what each role is allowed to do out of the box on a fresh
# install (or for a permission a manager has never touched). Both
# app/auth.py (runtime checks) and app/seed.py (first-run seeding) import
# from here rather than keeping their own copy - see the "Shared helpers
# extracted from duplication" philosophy in CHANGE_IMPACT_GUIDE.md.
#
# Each entry: (key, label, description). `key` is also the value stored in
# the `role_permissions` table and must stay a stable identifier - renaming
# one here without a data migration orphans any row already saved under the
# old key (it would silently fall back to the DEFAULT_ROLE_PERMISSIONS
# default for that permission instead of the manager's saved choice).
PERMISSIONS = [
    ("create_expense", "Create expenses",
     "Record new petty cash vouchers and save/submit drafts."),
    ("review_expense", "Approve / reject expenses",
     "Approve or reject vouchers that are pending review."),
    ("delete_expense", "Delete expenses",
     "Soft-delete a voucher (a reason is required; nothing is ever hard-deleted)."),
    ("manage_users", "Manage users & permissions",
     "Add users, reset passwords, enable/disable accounts, and edit this role permission matrix."),
    ("manage_settings", "Manage settings",
     "Edit expense categories, cost centres, and the data-integrity tools."),
    ("view_audit", "View audit log",
     "View and export the system-wide audit log."),
    ("manage_backups", "Manage backups",
     "Trigger an on-demand backup, and inspect/download/restore existing ones."),
    ("record_production", "Record production entries",
     "Log Weekly Productions output entries."),
    ("delete_production", "Delete production entries",
     "Soft-delete a production entry (a reason is required; nothing is ever hard-deleted)."),
    ("delete_supplier", "Delete supplier records",
     "Soft-delete an entry in the VAT supplier knowledge base (a reason is required); "
     "existing voucher lines that reference it are never affected."),
    ("create_document", "Create / edit documents",
     "Create, edit, and self-approve Word Editor documents."),
    ("official_document", "Make documents official",
     "Elevate a self-approved document to Official status."),
    ("manage_wd_profile", "Manage Word Editor company profile",
     "Approve company profile change requests in the Word Editor."),
    ("manage_wd_users", "Manage Word Editor user profiles",
     "Edit user display names, phones, and location assignments in the Word Editor."),
    ("view_assets", "View assets",
     "View the asset registry, an asset's cost/service history, and download asset reports."),
    ("log_asset_service", "Log asset service notes",
     "Record non-cost maintenance notes (greasing, a part fitted, an inspection) "
     "against an asset currently assigned to you."),
    ("manage_assets", "Manage assets",
     "Register, edit, and soft-delete assets, and edit asset tag rules."),
    ("assign_asset", "Assign / transfer assets",
     "Issue an asset to an employee or cost centre, and close or transfer an assignment."),
    ("divest_asset", "Divest assets",
     "Mark an asset as divested or retired (a reason is required; proceeds are recorded "
     "for reporting only, never posted to the cash float)."),
    ("manage_wd_universal", "Manage universal templates",
     "Create and assign universal templates and subjects in the Word Editor."),
]
PERMISSION_KEYS = [key for key, _label, _description in PERMISSIONS]

# Fresh-install / never-customized defaults - exactly what app/auth.py's
# can_*() functions used to hardcode before the matrix became DB-editable.
# A role/permission pair not yet present in the `role_permissions` table
# (e.g. right after `pip install`/`docker compose up`, before a manager has
# ever opened Roles & Permissions) falls back to this rather than to "deny",
# so an un-touched install behaves exactly as it always has.
DEFAULT_ROLE_PERMISSIONS = {
    "manager": set(PERMISSION_KEYS),               # full access
    "accountant": {"review_expense", "view_assets", "log_asset_service", "assign_asset"},
    "user": {"create_expense", "record_production", "create_document",
             "view_assets", "log_asset_service"},
}

# --- security settings ---
SESSION_HTTPS_ONLY = os.environ.get("SESSION_HTTPS_ONLY", "false").lower() == "true"
LOGIN_MAX_ATTEMPTS = int(os.environ.get("LOGIN_MAX_ATTEMPTS", "5"))
LOGIN_LOCKOUT_MINUTES = int(os.environ.get("LOGIN_LOCKOUT_MINUTES", "15"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "10"))
ALLOWED_UPLOAD_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".pdf"}

# --- automatic backup settings ---
BACKUPS_DIR = DATA_DIR / "backups"
BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
RESTORE_LOG_PATH = DATA_DIR / "restore_log.jsonl"

BACKUP_ENABLED = os.environ.get("BACKUP_ENABLED", "true").lower() == "true"
BACKUP_INTERVAL_HOURS = float(os.environ.get("BACKUP_INTERVAL_HOURS", "24"))
BACKUP_RETENTION_COUNT = int(os.environ.get("BACKUP_RETENTION_COUNT", "30"))
BACKUP_STARTUP_DELAY_SECONDS = int(os.environ.get("BACKUP_STARTUP_DELAY_SECONDS", "60"))

# Restoring from an uploaded (offsite) backup file is the disaster-recovery path,
# so it gets its own, stricter limits since the file wasn't created by this app.
MAX_RESTORE_UPLOAD_MB = int(os.environ.get("MAX_RESTORE_UPLOAD_MB", "100"))
MAX_BACKUP_ZIP_ENTRIES = 5000
MAX_BACKUP_UNCOMPRESSED_MB = 500

# --- Word Editor ---
WD_MAX_PERSONAL_TEMPLATES = 5
WD_MAX_UNIVERSAL_TEMPLATES = 2
