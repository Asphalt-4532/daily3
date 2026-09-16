import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import SECRET_KEY, SESSION_HTTPS_ONLY
from app.database import init_schema
from app.seed import seed_if_needed
from app.scheduler import backup_loop
from app.network_middleware import TrustedHostFromDBMiddleware

# Shared infrastructure - used by every program, owned by none of them.
from app.routes import (
    auth_routes, user_routes, backup_routes, audit_routes, programs_routes, network_routes,
    notification_routes, undo_routes,
)

# The Expense Program - fully isolated under its own URL prefix (see
# include_router calls below) and its own app/programs/expenses/ package.
# A future program follows the same shape: its own package, its own
# prefix, registered here alongside this one and nowhere else.
from app.programs.expenses.routes import (
    expense_routes as expenses_expense_routes,
    report_routes as expenses_report_routes,
    settings_routes as expenses_settings_routes,
    cash_routes as expenses_cash_routes,
    budget_routes as expenses_budget_routes,
    vat_routes as expenses_vat_routes,
    advances_routes as expenses_advances_routes,
)

# Weekly Productions - second program, added the same additive way: its own
# package, its own prefix, registered here and nowhere else. Nothing in
# app/programs/expenses/ changed to add this.
from app.programs.weekly_productions.routes import (
    production_routes as weekly_productions_production_routes,
)

# Word Editor - third program. Fully isolated under /word-editor prefix.
from app.programs.word_editor.routes import (
    document_routes as word_editor_routes,
)

# Asset & Maintenance - fourth program. Fully isolated under /assets prefix.
# The only deliberate seam between this and the Expense Program is
# expense_lines.asset_id - see CHANGE_IMPACT_GUIDE.md. Nothing in
# app/programs/expenses/ was changed to add this program except that one
# nullable column and the _validate_lines() branch that reads
# categories.requires_asset.
from app.programs.assets.routes import (
    asset_routes as assets_asset_routes,
    custody_routes as assets_custody_routes,
    service_routes as assets_service_routes,
)

init_schema()
seed_if_needed()


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(backup_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="Expense Program", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware, secret_key=SECRET_KEY, same_site="lax", https_only=SESSION_HTTPS_ONLY
)
# Added after SessionMiddleware so it ends up outermost (Starlette applies
# middleware in reverse registration order) - a request with a disallowed
# Host header is rejected before it ever touches a session.
app.add_middleware(TrustedHostFromDBMiddleware)

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# --- shared, unprefixed: login, the program hub, user/backup/audit admin ---
app.include_router(auth_routes.router)
app.include_router(user_routes.router)
app.include_router(backup_routes.router)
app.include_router(audit_routes.router)
app.include_router(programs_routes.router)
app.include_router(network_routes.router)
app.include_router(notification_routes.router)
app.include_router(undo_routes.router)

# --- Expense Program, isolated under its own prefix ---
EXPENSE_PROGRAM_PREFIX = "/expense-program"
app.include_router(expenses_expense_routes.router, prefix=EXPENSE_PROGRAM_PREFIX)
app.include_router(expenses_report_routes.router, prefix=EXPENSE_PROGRAM_PREFIX)
app.include_router(expenses_settings_routes.router, prefix=EXPENSE_PROGRAM_PREFIX)
app.include_router(expenses_cash_routes.router, prefix=EXPENSE_PROGRAM_PREFIX)
app.include_router(expenses_budget_routes.router, prefix=EXPENSE_PROGRAM_PREFIX)
app.include_router(expenses_vat_routes.router, prefix=EXPENSE_PROGRAM_PREFIX)
app.include_router(expenses_advances_routes.router, prefix=EXPENSE_PROGRAM_PREFIX)

# --- Weekly Productions, isolated under its own prefix ---
WEEKLY_PRODUCTIONS_PREFIX = "/weekly-productions"
app.include_router(weekly_productions_production_routes.router, prefix=WEEKLY_PRODUCTIONS_PREFIX)

# --- Word Editor, isolated under its own prefix ---
# Note: routes already include the full /word-editor/... path, so no prefix here.
app.include_router(word_editor_routes.router)

# --- Asset & Maintenance Program, isolated under /assets ---
ASSETS_PREFIX = "/assets"
# custody_routes registered FIRST so /assets/reports, /assets/settings,
# /assets/employees are matched before /assets/{asset_id} in asset_routes.
app.include_router(assets_custody_routes.router, prefix=ASSETS_PREFIX)
app.include_router(assets_service_routes.router, prefix=ASSETS_PREFIX)
app.include_router(assets_asset_routes.router,   prefix=ASSETS_PREFIX)
