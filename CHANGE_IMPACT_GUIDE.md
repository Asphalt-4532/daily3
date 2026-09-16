# Change Impact Guide

**Upload this file first when asking for a change.** It maps common change
requests to the exact files they touch, the tests that must pass afterward,
and specific mistakes already made once during development - so they aren't
repeated. `README.md` explains how the app works today; this document is
about *changing* it safely.

Every row below has actually happened in this codebase - this isn't a
theoretical map, it's a record of real changes and the files that turned out
to be involved each time.

---

## How to use this document

1. Find the row(s) closest to the requested change.
2. Open every file listed - most changes touch a **service** (business
   logic), the **route** that calls it, one or more **templates**, and the
   **tests** for all of the above. Missing one of these is the most common
   way a change looks done but isn't.
3. Check "Watch out for" *before* writing code - these are specific bugs
   that already happened once.
4. After changing code, run the test command listed, then the full suite:
   `pytest`. Fix any failure before considering the change finished -
   failures here have repeatedly caught real bugs, not just stale
   assertions (see "Watch out for" entries below for examples).
5. If the database schema changed, see **Schema changes** below before
   doing anything else - it has its own procedure.
6. Do a live smoke test for anything touching money math or PDFs: start the
   app, perform the action for real, and open the resulting PDF/Excel file -
   several bugs in this app were only caught by actually looking at
   rendered output, not by tests alone.
7. Update `README.md` if the change affects anything a user or admin would
   need to know (a new `.env` setting, a new page, changed behavior).
   Update *this* file if the change affects which files a *future* change
   would touch (e.g. a new service module, a new table).

---

## Change impact map

### Money / VAT logic

| | |
|---|---|
| **Files** | `app/programs/expenses/services/expenses.py` (`compute_vat()`, `_TOTALS_SQL`, `dashboard_stats()`) · `app/programs/expenses/services/reports.py` (`spend_breakdown()`, `export_rows()`, `build_export_workbook()`) · `app/programs/expenses/pdf_voucher.py` (line table + summary block) · `app/programs/expenses/pdf_reports.py` (`build_transaction_listing_pdf()`) · `app/programs/expenses/templates/expenses/expense_form.html` (hint text **and** the live-total JS, which duplicates the VAT math client-side) · `app/programs/expenses/templates/expenses/expense_detail.html` (line table) · `app/config.py` (`VAT_RATE`) · `app/templates_env.py` (exposes `VAT_RATE` to templates) · `app/backup.py` (`_snapshot_counts()`'s `total_approved_amount`, `inspect_backup()`'s per-record amount - see the Drafts row above, this has already broken once). |
| **Tests** | `tests/test_services_expenses.py` (`compute_vat` tests, multi-line total tests) · `tests/test_services_reports.py` (VAT column tests) · `tests/test_pdf_voucher.py` |
| **Watch out for** | The VAT math direction has flipped once already (extraction from a VAT-inclusive amount → addition on top of a VAT-exclusive amount). **Every** place a "total" is computed had to change in step, not just `compute_vat()` itself - dashboard totals, category breakdowns, monthly trend, and the Excel/PDF grand totals all sum `amount + vat_amount`, not `amount` alone. The JS live-total in `expense_form.html` does its own client-side copy of this math (`v * (1 + VAT_RATE/100)`) - it will silently drift out of sync with the server if only the Python side is changed. |

### Suppliers (VAT knowledge base)

| | |
|---|---|
| **Files** | `app/database.py` (`suppliers` table - soft-delete via `status`, `vat_number UNIQUE`; `expense_lines.supplier_id`; `_migrate_add_expense_line_supplier()` creates the table and adds the column for a database from before this feature - **a schema change**, see **Schema changes** below) · `app/programs/expenses/services/suppliers.py` (`resolve_supplier(conn, ...)` - takes the caller's own connection since it's called mid-transaction from `expenses._validate_lines()`, not a standalone service call like `list_suppliers()`/`delete_supplier()`; reuses an active supplier by VAT, creates one if the VAT is unseen, raises if the VAT belongs to a *deleted* supplier rather than silently reviving it) · `app/programs/expenses/services/expenses.py` (`_validate_lines()` - a VAT-able line resolves/requires a supplier, `_write_lines()` stores `supplier_id`, `_fetch_lines()` joins supplier name/VAT for display) · `app/programs/expenses/routes/expense_routes.py` (`_extract_lines_from_form()` now has five field lists, not four; all four places that render `expense_form.html` pass `suppliers` + `suppliers_json` - the latter because this app's plain Jinja2 `Environment` has no `tojson` filter registered, see `_active_suppliers_json()`) · `app/programs/expenses/routes/settings_routes.py` (`/settings/suppliers/{id}/delete`, gated by the new `delete_supplier` permission, not the general `manage_settings` one the rest of the Settings page uses) · `app/programs/expenses/templates/expenses/expense_form.html` (supplier fields shown/hidden per line by JS keyed off that row's VAT-able select, VAT `<datalist>` autofill - has the same **two-copies-of-every-line-field** pitfall as the rest of this template, server-rendered row *and* `<template id="lineTemplate">`), `expense_detail.html`, `settings.html` (Suppliers card) · `app/programs/expenses/pdf_voucher.py` and `app/programs/expenses/services/reports.py` (both append/expose "(Supplier, VAT: ...)" or dedicated Supplier columns - see the Reports/exports row for why exports pull from their own query rather than reusing `_fetch_lines()`) · `app/pdf_common.py` (`footer()` gained an optional `program_name` param while this was being built - unrelated to suppliers, but touched in the same session; see the Weekly Productions "Watch out for" row above for why the default changed). |
| **Tests** | `tests/test_services_suppliers.py` · `tests/test_services_expenses.py` (the VAT-able-line-requires-supplier block, including the "draft can be blank but never half-typed" rule and drafts needing a supplier before `submit_draft()` will let them through) · `tests/test_routes_suppliers.py` · `tests/test_database_migration.py` (the supplier migration block near the bottom). |
| **Watch out for** | **VAT number is the identity, not the typed name** - `resolve_supplier()` looks up by `vat_number` only; a second line reusing a known VAT with a *different* typed name silently reuses the existing supplier's original name rather than erroring or updating it. A VAT-able line's supplier is **required only when submitting for real** (`strict=True`) - a draft can leave both fields blank, but a half-typed pair (name with no VAT, or vice versa) is always rejected regardless of draft/strict, since that's clearly a mistake rather than "not ready yet." Deleting a supplier is **soft-delete only** and specifically manager-gated via the `delete_supplier` permission, not `manage_settings` - it never touches `expense_lines.supplier_id` or the supplier row's own `name`/`vat_number`, so every voucher line that already referenced it keeps showing exactly what it always did; only *new* lines lose access to it (`list_suppliers(status='active')`, the datalist source, excludes it). Trying to reuse a deleted supplier's VAT on a new line is a validation error, not a silent revival - a manager would need a dedicated "restore" action to bring it back, which doesn't exist yet. |

### Currency

| | |
|---|---|
| **Files** | `.env` / `.env.example` (`CURRENCY_SYMBOL`) - that's it for the running app. |
| **Tests** | None specifically check the symbol; run the full suite. |
| **Watch out for** | Avoid setting this to a Unicode currency glyph (e.g. the Rial sign ﷼) - reportlab's base PDF fonts can't render it reliably and it may come out blank or as a missing-glyph box in generated PDFs. Stick to a plain ASCII code (`SAR`, `USD`, `INR`, ...). |

### Category breakdown chart (donut)

| | |
|---|---|
| **Files** | `app/chart_helpers.py` (`build_donut_chart()` - the gradient/legend math, and `CHART_COLORS`, the muted accent palette) · `app/programs/expenses/routes/expense_routes.py` and `report_routes.py` (both call `build_donut_chart()` and pass the result as `donut` to their templates) · `app/programs/expenses/templates/expenses/_donut_chart.html` (the actual chart markup - a shared partial included by both `dashboard.html` and `reports.html`, each of which sets `empty_message` before including it; this used to be duplicated directly in both templates until a duplication check found it) · `app/static/style.css` (`.donut`, `.donut-hole`, `.donut-legend`, `.legend-row` etc.). |
| **Tests** | `tests/test_chart_helpers.py` |
| **Watch out for** | `by_category` arrives as **raw `sqlite3.Row` objects from `dashboard_stats()`** but as **plain dicts from `spend_breakdown()`** (one caller converts, the other doesn't) - `build_donut_chart()` was originally written using `.get()`, which `sqlite3.Row` doesn't support, and passed every dict-based test here while still crashing the real dashboard route the moment it ran. Fixed by switching to bracket indexing throughout, which both shapes support, and locked in with a test that builds a real `sqlite3.Row` rather than only ever testing against dicts. Any future test of this function should do the same for at least one case. The chart is pure CSS (`conic-gradient`), not JS or SVG, by design - see the module docstring for why; don't reach for a charting library here without a real reason to abandon that.

### Colours / branding / fonts / company details

| | |
|---|---|
| **Files** | `app/static/style.css` (`:root` CSS variables - now includes depth tokens like `--shadow-sm/md/lg` and `--line-strong` alongside the base colours) for the web UI · `app/programs/expenses/pdf_voucher.py` and `app/pdf_common.py` (`colors.black` / `colors.whitesmoke` / `colors.grey` - **grayscale only, deliberately**; `pdf_common.py`'s `document_header()` is also where the shared letterhead - name + address - is built, used by every report/audit PDF) · `app/programs/expenses/services/reports.py`, `app/services/audit.py` (`PatternFill` hex codes for Excel header colours, and their own copy of the name+address metadata block since Excel export doesn't go through `pdf_common.py`) · `COMPANY_NAME` and `COMPANY_ADDRESS` in `.env` (`app/config.py`, exposed to templates via `app/templates_env.py`) - address is optional and every letterhead location omits the line entirely when it's blank. |
| **Tests** | None check colour values or letterhead text directly; a full suite run plus a visual check (screenshot, or open a generated PDF/Excel file and look at it) is the only real verification - this is what caught the two bugs below. |
| **Watch out for** | **PDFs and the web UI are intentionally on separate colour rules** - the web app uses a black/white/red/blue palette, but every PDF is grayscale-only by explicit request. Don't "fix" a PDF to match the web palette without checking this is still wanted. If changing a CSS override for a specific element (e.g. a two-column row width), check selector *specificity* - a change was once silently ignored because the new rule had lower specificity than the rule it was meant to override (`.line-row .cat` lost to `.line-row .grid > div`). Match or exceed the specificity of what you're overriding. The Excel metadata block (`reports.py` and `audit.py`, not `pdf_common.py`) hardcodes which spreadsheet row is bold (the company name row, and separately the title row) - inserting a new line above the title (like the address) shifts every row number below it, so that hardcoded bolding and the `header_row = len(meta) + 1` calculation both have to move in step, not just the new line itself. |

### Roles / permissions

This has two genuinely different shapes of change - keep them separate:

**(A) Changing what an *existing* role can do** - this is now a **runtime,
UI-driven change**, not a code change, and a manager does it themselves at
**Users → Roles & Permissions** (`/users/roles`). Nothing below needs
editing for this case; it exists only for the other three situations.

**(B) Changing the permission *system* itself** - adding a brand-new
permission, changing a permission's fresh-install default, or (much
bigger, see (C)) adding/renaming/removing a *role*.

| | |
|---|---|
| **Files** | `app/config.py` (`ROLES`, and the `PERMISSIONS` / `PERMISSION_KEYS` / `DEFAULT_ROLE_PERMISSIONS` matrix - the single source of truth both `app/auth.py` and `app/seed.py` import from) · `app/database.py` (`role_permissions` table - purely additive, not the `CHECK`-constrained `users.role` column, so adding a *permission* is not a schema change; see (C) below for adding a *role*) · `app/auth.py` (`role_has_permission()` plus the thin `can_*()` wrappers - add a new wrapper here for a brand-new permission) · `app/services/roles.py` (`list_permission_matrix()`, `set_permission_for_roles()` - the actual business logic, incl. the self-lockout guard) · `app/routes/user_routes.py` (`/users/roles`, `/users/roles/update`) · `app/templates/roles_permissions.html`, `app/templates/_users_subnav.html` · `app/seed.py` (seeds `role_permissions` from `DEFAULT_ROLE_PERMISSIONS` on first run only - **never** re-seeds an existing install, so a new permission added here needs `INSERT OR IGNORE`-style handling, which the existing loop already does per-key) · `app/routes/guards.py` (uses `can_*()`, shouldn't need edits - this was true before this feature and remains true). |
| **Tests** | `tests/test_auth.py` (still asserts the *default* matrix - passes unmodified because `reset_db` reseeds `role_permissions` from `DEFAULT_ROLE_PERMISSIONS` before every test) · `tests/test_services_roles.py` (the matrix service, incl. the self-lockout guard and its atomicity) · `tests/test_routes_permissions.py` (`/users/roles` is manager-only, plus an end-to-end "manager flips a checkbox, a different logged-in session's permissions change immediately" test) · `tests/test_routes_csrf.py` (`/users/roles/update`). |
| **Watch out for** | `role_has_permission()` falls back to `DEFAULT_ROLE_PERMISSIONS` for any `(role, permission)` pair with no row yet - this is what makes a fresh install behave exactly like the old hardcoded matrix, and what makes adding a brand-new permission safe on an *existing* database (no row anywhere yet → every role gets the coded default until a manager changes it, rather than every role silently being denied). A new permission needs an entry in **all three** of `PERMISSIONS` (config.py), `DEFAULT_ROLE_PERMISSIONS` (config.py), and a `can_<x>()` wrapper (auth.py) - missing the wrapper means `guards.py` has nothing to call; missing the default means it silently denies every role instead of falling back sensibly. The self-lockout guard in `roles.py` only protects `manage_users` specifically (zero roles left able to manage users/permissions) - it does **not** stop a manager from, say, removing `manage_settings` from every role, which is recoverable (a manager can always re-grant it to themselves via `manage_users`) unlike locking out `manage_users` itself, which isn't. |

**(C) Adding/renaming/removing a role itself** (a fourth role, or renaming
`accountant`) is still the bigger change described below - it's a schema
change because of the `role` column's `CHECK` constraint, `app/config.py`'s
`ROLES` list has to grow, and `app/templates/base.html`'s nav is
role-conditional in a couple of places (`user.role in [...]` checks) that
don't come from `ROLES` automatically the way `users.html`'s role `<select>`
does. This case is **not** covered by the Roles & Permissions page - that
page only ever edits what an existing row in `ROLES` can do.

| | |
|---|---|
| **Files** | `app/config.py` (`ROLES` list) · `app/database.py` (the `role` column's `CHECK` constraint on the `users` table - **a schema change**, see below; `role_permissions` itself needs no schema change - a new role just gets new rows, same as seeding always did) · `app/seed.py` (the seeding loop already iterates `ROLES`, so a new role is picked up automatically *once the schema allows it*) · `app/templates/base.html` (role-conditional nav - not auto-derived from `ROLES`) · `app/templates/users.html` (role `<select>` - *is* auto-derived from `ROLES`, no edit needed there). |
| **Tests** | `tests/test_auth.py` · `tests/test_services_roles.py` · `tests/test_routes_permissions.py` · `tests/test_database_migration.py` (a new migration test for the `CHECK` constraint change, following `_build_old_shape_database()`'s pattern). |
| **Watch out for** | The `role` column has a SQLite `CHECK` constraint, so adding a role is a schema change even though it looks like "just add a string to a list" - `CREATE TABLE IF NOT EXISTS` will **not** update an existing database's constraint. See **Schema changes** below. |

### Adding a new program (Machinery Reports, Key Updates, etc.)

This has now been done for real once - Weekly Productions (see
`app/programs/weekly_productions/`) - so the pattern below is proven, not
theoretical, the same way "Schema changes" is proven by the vouchers ->
`expense_lines` migration.

| | |
|---|---|
| **Files** | New `app/programs/<name>/` package shaped like `app/programs/expenses/` (`routes/`, `services/`, `templates/<name>/`) · `app/main.py` (import the new routers, `include_router(..., prefix="/<name>")`) · `app/routes/programs_routes.py` (`PROGRAMS` list - flip `"available": True` and set its `"url"`) · `app/templates_env.py` (`PROGRAM_TEMPLATES_DIRS` - add the new templates directory) · `app/templates/base.html` (the shared nav is **not** auto-derived from `PROGRAMS` - a new program's own links have to be added to the nav by hand, gated by role the same way Expense Program's "+ New Voucher" link is; Weekly Productions' "Production" / "Production Entries" / "+ New Entry" links are the template to copy). |
| **Tests** | New `tests/test_routes_<name>.py` and `tests/test_services_<name>.py`, following the shape of `tests/test_routes_weekly_productions.py` and `tests/test_services_production.py`; also **check `tests/test_routes_programs.py`** - `test_program_hub_lists_future_programs_as_coming_soon` hardcodes which programs are still placeholders and will fail (correctly - update its expectations, don't just skip it) once another one goes live. |
| **Watch out for** | Give the new program its **own URL prefix** in `main.py`, even if it feels unnecessary at first - this is what actually prevents route collisions with other programs, not just the separate files. Namespace its templates under `templates/<name>/<name>/*.html` (see how Expense Program's are under `templates/expenses/expenses/`, Weekly Productions' under `templates/weekly_productions/weekly_productions/`) so an identical filename in two programs' template folders can never shadow each other. Reuse shared infrastructure (`app/auth.py`, `app/routes/guards.py`, `app/services/errors.py`, `app/pdf_common.py`, `app/excel_common.py`, `app/services/audit.py`, `app/database.next_document_number()` for pre-numbered docs) rather than copying it - user accounts, the audit log, backups, and document numbering are explicitly shared across all programs, not per-program. A new program that needs its own permissions (e.g. Weekly Productions' `record_production`/`delete_production`) follows the same three-places rule as any other new permission - see the "Roles / permissions" row above (`PERMISSIONS`, `DEFAULT_ROLE_PERMISSIONS` in `config.py`, plus a `can_<x>()` wrapper in `auth.py`) - `seed.py` picks up a new key automatically via its existing `PERMISSION_KEYS` loop, no seed.py edit needed. |

### Weekly Production reports (the program's own fields/rules)

| | |
|---|---|
| **Files** | `app/database.py` (`production_reports` + `production_report_lines` tables, a header + lines shape same as `expenses`/`expense_lines`; `production_settings` table, a generic key/value store holding the per-material weight factor (`weight_factor_gi`/`weight_factor_hdg`) and both weight targets (`weight_target_weekly_kg`/`weight_target_monthly_kg`) - a new setting is just a new key, no schema change; `production_locked_months` table (month `'YYYY-MM'` -> who/when locked it) - a locked month blocks *new* `create_report()` calls dated in it but not deletions of existing reports, purely additive, no migration needed; `_migrate_flat_production_entries()` migrates a database from the earliest flat one-row-per-entry shape, `_migrate_add_line_description_and_weight()` adds `description`/`weight_kg` via simple `ALTER TABLE ADD COLUMN` for a database from the header+lines shape that predates those two columns - **both migrations are schema changes**, see **Schema changes** below) · `app/programs/weekly_productions/services/production.py` (`parse_line_input()` - the single entry point that turns one "description + dims" box into description/width/height/length/thickness/weight_kg, routing to one of three formulas based on the dims string's length slot and, if empty, which keyword the description contains: `_parse_straight_dims()` (length given), `_parse_elbow_dims()` (angle named, or the word "reducer" for a fixed 90° - see `_REDUCER_RE`/`_REDUCER_DEFAULT_ANGLE`), or `_parse_tee_dims()` (the word "tee" *or* "cross" - both share `_TEE_RE` and `_tee_weight_per_piece()`, there's no formula difference between them); `get_weight_factors()`/`set_weight_factor()` and `get_weight_targets()`/`set_weight_target(period, ...)` (period is `'weekly'` or `'monthly'`), both falling back to a hardcoded default when their `production_settings` row doesn't exist yet, the same pattern `role_has_permission()` uses for `DEFAULT_ROLE_PERMISSIONS`; `is_month_locked()`/`list_locked_months()`/`lock_month()`/`unlock_month()`; `daily_summary()` - fills in zero-production days rather than only returning days with data, capped at `_MAX_DAILY_SUMMARY_DAYS` against a pathologically large date-range query) · `app/programs/weekly_productions/routes/production_routes.py` (`_extract_lines_from_form()` - three same-named field lists, `line_material_type`/`line_desc_dims`/`line_quantity`; `/settings`, `/settings/weight-factor`, `/settings/weight-target` (now takes a `period` field), `/settings/lock-month`, `/settings/unlock-month`, `/reports/export.xlsx`, `/reports/export.pdf` routes - `create_report()` raising a locked-month `ValidationError` flows through the existing `_rerender_with_error()` path unchanged, no new error-handling needed there) · `app/programs/weekly_productions/pdf_reports.py` (`build_production_listing_pdf()` - same A4 landscape, line-granularity shape as Expense Program's `build_transaction_listing_pdf()`, built on `app/pdf_common.py`'s shared header/footer/table style) · `app/programs/weekly_productions/templates/weekly_productions/dashboard.html` (two independent progress bars, weekly and monthly - each only rendered when its own target is `> 0`), `reports.html` (Download Excel *and* Download PDF links, plus the "By day" daily-totals table), `report_form.html` (one text box per line - has **two** copies of the row markup, the server-rendered initial row and the `<template id="lineTemplate">` used by "+ Add line", same pitfall as `expense_form.html`), `report_detail.html`, `settings.html` (weight-factor, weight-target, and lock-a-month editing, manager only - a locked month's row links straight to its daily report). |
| **Tests** | `tests/test_services_production.py` (parsing rules for straight/elbow/reducer/tee/cross lines, all weight formulas verified against hand calculations - weight-factor/weight-target settings, month lock/unlock incl. that a locked month still allows deleting an existing report, `daily_summary()`'s zero-fill and its leap-year-safe end-of-month math, and the PDF builder) · `tests/test_routes_weekly_productions.py` (now covers `/settings/weight-target` with a `period`, `/settings/lock-month`, `/settings/unlock-month`, `/reports/export.pdf`, and reducer/cross over real HTTP) · `tests/test_database_migration.py` (all production migration blocks near the bottom). |
| **Watch out for** | A report is a **header + lines** structure, same as an expense voucher. `material_type` is `CHECK`-constrained to `'GI'`/`'HDG'` at the schema level - adding a third material is a schema change. **Length is identified by its bare `m` suffix, not position** - whichever of the last two x-separated tokens ends in `m` (not `mm`) is length; the other is thickness. **An empty length slot means elbow/reducer or tee/cross, told apart by the description text** - an angle phrase (`_ANGLE_RE`) or the word "reducer" (`_REDUCER_RE`, fixed 90° unless an explicit angle is also present - explicit always wins) routes to the elbow formula; "tee" or "cross" (`_TEE_RE`) routes to the tee formula; none of those present is a validation error rather than a silent guess. R_in is never supplied for either shape and always defaults to 100mm (`_ELBOW_DEFAULT_R_IN_MM`, reused by both). **A locked month is checked in `create_report()`, not in the route** - so anything that ever calls the service layer directly (a future import tool, a script) gets the same protection automatically, unlike a check living only in `production_routes.py` would. `list_locked_months()` computes each month's real last day (`month_end`) rather than assuming 31 - a hardcoded `-31` breaks `datetime.strptime()` outright for February/April/June/September/November, it doesn't just render oddly. `app/pdf_common.py`'s `footer()` takes an optional `program_name` now (used to say "Weekly Productions" instead of a hardcoded "Expense Program" that used to leak into every PDF this app generates, including the audit log's - if adding a PDF export to a future program, pass its name explicitly rather than relying on the default, which is deliberately blank for shared/system-wide documents). A tee's bottom/side panel areas are computed and summed internally but **not stored or displayed separately** - only the combined per-line `weight_kg`, same as every other line type; don't reintroduce separate columns for that breakdown without checking this is still wanted. **Weight is computed once at creation time and stored on the line**, not recalculated when displayed - changing a material's factor in Settings only affects reports recorded afterward, exactly like changing `VAT_RATE` never retroactively changes past vouchers. Deleting a report is soft-delete only and does **not** touch its lines. Exports are at **line granularity** (one row per material line, header fields repeated), same convention as `reports.py`'s `export_rows()`. |

### Duplicate transaction line detection/cleanup

| | |
|---|---|
| **Files** | `app/programs/expenses/services/data_integrity.py` (`find_duplicate_lines()`, `remove_duplicate_line_group()`) · `app/programs/expenses/routes/settings_routes.py` (folded into the existing `/settings` page + a new `/settings/duplicates/clean` action) · `app/programs/expenses/templates/expenses/settings.html` (the "Data integrity" card). |
| **Tests** | `tests/test_services_data_integrity.py` · the "duplicate transaction line detection/cleanup" block in `tests/test_routes_permissions.py` |
| **Watch out for** | A "duplicate" (same voucher, same category/particulars/amount/VAT-able) is **never auto-deleted in bulk** - two genuinely separate purchases can legitimately look identical (two fuel top-ups of the same amount on the same day), so every group requires its own explicit click, reviewed by a human. Cleanup is **restricted to drafts only** and reuses `expenses.py`'s `_require_draft_owned_by()` - a pending/approved/rejected voucher is a submitted financial record, and this tool will report duplicates found in one but will never delete from it; the correction path for those is the normal reject/delete-with-reason flow, not silent auto-correction. Removing a line always calls `_renumber_lines()` afterward so remaining `line_no` values stay contiguous (1, 2, 3...) - forgetting this after any future change here would leave gaps that are harmless functionally but look wrong in the UI/PDF line numbering.

### Voucher fields (add/change a field on the voucher header)

| | |
|---|---|
| **Files** | `app/database.py` (`expenses` table in `SCHEMA` - **a schema change**, see below) · `app/programs/expenses/services/expenses.py` (`create_expense()` signature, `_DETAIL_QUERY`) · `app/programs/expenses/routes/expense_routes.py` (form parsing in `create_expense()`) · `app/programs/expenses/templates/expenses/expense_form.html`, `expense_detail.html`, `dashboard.html`, `expenses_list.html` · `app/programs/expenses/pdf_voucher.py` · `app/programs/expenses/services/reports.py` (`export_rows()`, if the field should be exportable). |
| **Tests** | `tests/test_services_expenses.py` · `tests/test_routes_permissions.py` (the end-to-end multi-line creation test) · `tests/test_pdf_voucher.py` |
| **Watch out for** | See **Schema changes** below - this is very likely one. |

### Transaction line fields (add/change a field on a line item)

| | |
|---|---|
| **Files** | `app/database.py` (`expense_lines` table - **a schema change**) · `app/programs/expenses/services/expenses.py` (`_validate_lines()`, `create_expense()`, `_fetch_lines()`) · `app/programs/expenses/routes/expense_routes.py` (`_extract_lines_from_form()` - remember the positional-alignment convention: every `line_*` field must stay the same length and order across all rows) · `expense_form.html` (both the server-rendered row **and** the JS `<template>` clone must get the new field) · `expense_detail.html`, `pdf_voucher.py`, `pdf_reports.py`, `reports.py` `export_rows()`. |
| **Tests** | `tests/test_services_expenses.py` (multi-line tests) · `tests/test_routes_permissions.py` |
| **Watch out for** | `expense_form.html` has **two** copies of each line field - the server-rendered initial row(s) and the `<template id="lineTemplate">` used by "+ Add line" - both must be updated or newly-added rows will be missing the field. |

### Drafts (save/edit/submit/discard unfinished vouchers)

| | |
|---|---|
| **Files** | `app/database.py` (`_migrate_add_draft_status` - its own schema migration, see below) · `app/programs/expenses/services/expenses.py` (`create_expense(..., is_draft=)`, `update_draft`, `submit_draft`, `discard_draft`, `_validate_lines(..., strict=)`, `_require_draft_owned_by`) · `app/programs/expenses/routes/expense_routes.py` (`/expenses/new` reads a hidden `action` field; `/expenses/{id}/edit`, `/submit`, `/discard`) · `app/programs/expenses/templates/expenses/expense_form.html` (edit-mode support, the two submit buttons), `expense_detail.html` (draft-only action buttons), `expenses_list.html` (draft status filter), `dashboard.html` (draft-count nudge) · `app/services/errors.py` (`ForbiddenError`) · `app/backup.py` (see "Watch out for" - this bit twice already). |
| **Tests** | `tests/test_services_expenses.py` (the draft-specific block near the bottom) · `tests/test_routes_permissions.py` (the "drafts, over real HTTP" block) · `tests/test_database_migration.py` · `tests/test_backup.py` |
| **Watch out for** | A draft has **no voucher number** (`voucher_no` is nullable) and uses **relaxed validation** (`_validate_lines(strict=False)`) - zero lines and a blank `paid_to` are both fine for a draft, but a line that *is* present still needs a valid category (the schema's `expense_lines.category_id` has never allowed NULL, and it wasn't worth a bigger migration to change that just for drafts). A draft is **hard-deleted** on discard, not soft-deleted like every other voucher status - it was never a committed financial record, so there's nothing to preserve. Ownership is enforced in the *service* layer via `ForbiddenError`, not just role guards - a plain `user` role can create drafts, but only that draft's own creator or a manager can edit/submit/discard a specific one; `app/routes/guards.py`'s role checks alone can't express "this specific record belongs to someone else." **`app/backup.py` bit twice already** on this feature: `_snapshot_counts()`'s status loop initially omitted `'draft'` entirely (silently undercounting `total_vouchers`), and separately its approved-amount total (and `inspect_backup()`'s per-record amount) initially summed only the VAT-exclusive `amount` column instead of `amount + vat_amount` - both are raw SQL against `expenses`/`expense_lines` that don't go through the service layer, so they don't automatically pick up either a new status or a VAT semantics change. Re-check both functions after any future status or money-shape change.

### Approval workflow (approve/reject/delete rules)

| | |
|---|---|
| **Files** | `app/programs/expenses/services/expenses.py` (`approve_expense()`, `reject_expense()`, `delete_expense()`) · `app/programs/expenses/routes/expense_routes.py` · `app/auth.py` (`can_review_expense`, `can_delete_expense`) · `expense_detail.html` (action buttons, shown/hidden by role and status). |
| **Tests** | `tests/test_services_expenses.py` · `tests/test_routes_permissions.py` |
| **Watch out for** | Deletion is **soft-delete only** (`status='deleted'`, never a real `DELETE`) - this is a deliberate financial-records decision, not an oversight. Don't add a hard-delete path. |

### Quick-clone (duplicate a voucher)

| | |
|---|---|
| **Files** | `app/programs/expenses/services/expenses.py` (`clone_expense()`) · `app/programs/expenses/routes/expense_routes.py` (`POST /expenses/{id}/clone`, plus `can_clone` in the detail route's context) · `app/programs/expenses/templates/expenses/expense_detail.html` (the Duplicate button). |
| **Tests** | `tests/test_services_expenses.py` (the quick-clone block) · `tests/test_routes_permissions.py` · `tests/test_routes_csrf.py` |
| **Watch out for** | `clone_expense()` reads the line columns it copies **by name**, so any new column on `expense_lines` has to be added to its `SELECT` and to the dict it hands `_write_lines()` - miss it and the field silently vanishes from every copy while every existing test still passes. It deliberately goes through `_write_lines()` rather than an `INSERT ... SELECT` so line numbering and VAT computation can never drift from the create/edit paths; that also means **VAT is recomputed at the current rate, not inherited** - the copy is a new transaction today, so a rate change since the original does apply to it. `receipt_path` is **not** copied (a receipt is evidence for one specific payment). The copy is always a draft, so `voucher_no` stays NULL and the period lock isn't checked at clone time - it's checked on submit, same as any other draft. Ownership is a record-level rule enforced in the service (`ForbiddenError` on someone else's draft), not a role guard - `guards.py` can't express "this specific record belongs to someone else". The Duplicate button is on the detail page only, **not** the expenses list: list rows are whole-row click targets and a nested form button fights the row click - if you add one there, restructure the row first. |

### Voucher sharing / hand-off (the "email it to the accountant" replacement)

| | |
|---|---|
| **Files** | `app/database.py` (`expense_shares` table - purely additive, `CREATE TABLE IF NOT EXISTS`, **not** a schema change needing a migration function) · `app/programs/expenses/services/sharing.py` (`share_voucher()`, `list_shares()`, `list_shareable_users()`, `SHAREABLE_STATUSES`) · `app/services/notifications.py` (the actual notifying) · `app/programs/expenses/routes/expense_routes.py` (`POST /expenses/{id}/share`, plus `can_share`/`shares`/`shareable_users` in the detail route's context) · `app/programs/expenses/templates/expenses/expense_detail.html` (the "Hand this voucher to someone" card) · `app/static/style.css` (`.share-people`, `.share-person`, `.share-log`). |
| **Tests** | `tests/test_services_sharing.py` · `tests/test_routes_permissions.py` (the sharing block) · `tests/test_routes_csrf.py` |
| **Watch out for** | **There is no SMTP anywhere in this app and adding it isn't a small change** - it means new `.env` secrets, a mail service, delivery-failure handling, and a second copy of a record the app already holds that goes stale the moment anything changes. Sharing deliberately points the recipient at the live voucher instead. If real email is ever genuinely required, treat it as a new feature with its own row here, not as a tweak to this one. The share row and the notification are written in **one transaction** - a notification pointing at a hand-off that didn't record would be worse than neither. Recipients are validated as a set before anything is written (an unknown or deactivated id fails the whole request, not just its own row). Only `SHAREABLE_STATUSES` (pending/approved/rejected) can be shared: a draft has no voucher number and isn't in anyone else's lists, so the link would lead nowhere actionable. Sharing grants the recipient **nothing** they couldn't already see - it only points at it - which is why the route uses `require_login_action` rather than a role check. |

### Notifications (the shared inbox + the Alerts bell)

| | |
|---|---|
| **Files** | `app/services/notifications.py` (`notify()`, `notify_role()`, `list_for_user()`, `count_unread()`, `mark_all_read()` - **the one implementation**; `notifications` table is shared infrastructure, so this is shared too) · `app/routes/notification_routes.py` (`GET /notifications`) · `app/templates/notifications.html` · `app/templates/_notification_bell.html` (the Alerts link + badge, included by **both** top bars) · `app/templates/base.html` and `app/templates/_standalone_header.html` (the two includes) · `app/templates_env.py` (`unread_notifications` template global) · `app/main.py` (router registration) · `app/programs/expenses/services/budgets.py` (`notify_managers()`/`get_notifications()`/`mark_notifications_read()` are now thin wrappers over this - names and signatures unchanged so existing callers and tests are unaffected). |
| **Tests** | `tests/test_services_sharing.py` (exercises notify/count/list end to end) · `tests/test_routes_permissions.py` (the bell badge and the read-clears-it behaviour) · `tests/test_services_budgets.py` (unchanged - proof the wrappers kept their contract) |
| **Watch out for** | **There are two top bars in this app, not one.** `base.html` covers every in-program page, but `programs.html` and `coming_soon.html` use `_standalone_header.html` instead - and the program hub is where people land right after login. A route test caught the bell being absent there; anything else added to the nav needs the same check. The bell is a template global (`unread_notifications(request)`) for the same reason `csrf_input` is - base.html is on every page and no route should have to remember to pass it; it returns 0 for a logged-out visitor rather than raising. It runs a `COUNT(*)` on every page render, which is only cheap because of `idx_notifications_user (user_id, read)` - don't drop that index. Opening `/notifications` marks **everything** read, by design: there's no per-item dismiss, and the permanent record is the audit log, never this table. |

### Toasts / one-shot user feedback

| | |
|---|---|
| **Files** | `app/flash.py` (`set_flash()`, `pop_flash()`, `LEVELS`, `MAX_LENGTH`) · `app/templates_env.py` (`pop_flash` template global) · `app/templates/base.html` (the toast container, popped once per page, plus the fade-out script) · `app/static/style.css` (`.toast-stack`, `.toast`, `.toast-success/info/warn/danger`) · every route that ends in a redirect and wants to say something (currently the seven voucher actions in `expense_routes.py`). |
| **Tests** | `tests/test_flash.py` · `tests/test_routes_permissions.py` (the "shown once, not twice" test over real HTTP) |
| **Watch out for** | The message is popped **inside base.html**, which means exactly one pop per rendered page - if you add a second `pop_flash()` call anywhere, the first caller wins and the toast silently disappears. Pages that don't extend `base.html` (`login.html`, and the two `_standalone_header.html` pages) show nothing; they're entry points, not redirect targets, so that's fine - but a new redirect landing on one of them would swallow its own confirmation. Session-backed on purpose, not a query string: a message in the URL survives a refresh, is editable by whoever holds the link, and leaks into browser history. This replaced an ad-hoc `flash_warning` session key that existed for exactly one route - if you find yourself adding another one-off session key for a message, use this instead. |

### Receipt drag-and-drop

| | |
|---|---|
| **Files** | `app/programs/expenses/templates/expenses/expense_form.html` (the `.dropzone` wrapper and its JS, at the bottom of the scripts block) · `app/static/style.css` (`.dropzone`, `.dropzone.is-over`, `.dropzone-hint`, `.dropzone-file`). |
| **Tests** | **None, and none are possible** - this is browser-only behaviour. Needs a real live check per step 6 above: start the app, drag an image onto the form, submit, and confirm the receipt actually attached. |
| **Watch out for** | The original `<input type="file" name="receipt">` is still in the DOM and is still what submits - the dropzone only writes into it via `DataTransfer`. Don't replace it with a hidden input or a JS-driven upload: with JS off, the plain field has to keep working. The extension test in the JS is a courtesy only; `sandbox.validate_file_signature()` reading actual bytes is the real gate, and a new allowed file type needs adding **there** (see the File uploads row), not just to the regex here. The window-level `dragover`/`drop` `preventDefault` matters more than it looks - without it, a missed drop makes the browser navigate to the file and silently abandon a half-filled form. |

### Recent category memory (the new-voucher form default)

| | |
|---|---|
| **Files** | `app/programs/expenses/services/expenses.py` (`recent_category_id()`, `RECENT_CATEGORY_WINDOW`) · `app/programs/expenses/routes/expense_routes.py` (`default_category_id` in the `/expenses/new` context) · `app/programs/expenses/templates/expenses/expense_form.html` (**both** copies of the category `<select>`). |
| **Tests** | `tests/test_services_expenses.py` (the recent-category block) · `tests/test_routes_permissions.py` (asserts the default reaches **both** copies by counting occurrences) |
| **Watch out for** | Same trap as the Transaction line fields row: `expense_form.html` has the field twice, server-rendered and inside `<template id="lineTemplate">`. Miss the template and every "+ Add line" row ignores the default while the first row honours it - which looks like it works. The server row only falls back to the default when the row has **no value of its own**, so a validation re-render never overwrites what the user actually typed; keep that condition if you touch it. `Salary advance` is excluded deliberately - selecting it opens the employee fields, so defaulting to it makes the common case noisier. Deactivated categories are excluded too (a category that isn't in the dropdown can't be its default). This is convenience only: nothing downstream trusts the value, and `None` (a brand-new account) is a normal result, not an error. |

### File uploads (receipts)

| | |
|---|---|
| **Files** | `app/config.py` (`ALLOWED_UPLOAD_EXTENSIONS`, `MAX_UPLOAD_MB`) · `app/sandbox.py` (`validate_file_signature()`, `safe_join()`, `set_nonexecutable()`) · `app/programs/expenses/services/expenses.py` (`save_receipt()`) · `app/programs/expenses/routes/expense_routes.py` (`get_upload()` for serving files back). |
| **Tests** | `tests/test_sandbox.py` · `tests/test_services_expenses.py` (`save_receipt` tests) · `tests/test_routes_permissions.py` (the end-to-end spoofed-upload test) |
| **Watch out for** | Two tests skip on native Windows (`test_set_nonexecutable_strips_execute_bit` in `test_sandbox.py`, `test_save_receipt_writes_a_nonexecutable_file` in `test_services_expenses.py`) - Windows/NTFS has no POSIX execute-bit concept, so `os.chmod()` can't meaningfully strip `S_IXUSR`/`S_IXGRP`/`S_IXOTH` there and there's nothing valid left to assert. This is a test-environment limitation only - the real property (uploaded files land non-executable) still holds and is still verified wherever this app actually runs (Docker, Linux, Mac). A third test, `test_safe_join_rejects_symlink_that_escapes_base`, needs to create a symlink to run at all; an unprivileged Windows account without Developer Mode enabled can't do that (`WinError 1314`), so it catches that specific failure and skips rather than failing on an environment gap unrelated to what it's actually checking. None of this affects Docker - Docker Desktop on Windows runs the container on Linux underneath, so a Docker-based install never hits any of this. |
| **Watch out for** | Files are validated by **actual byte signature**, not just the claimed extension/MIME type - if adding a new allowed file type, `sandbox.validate_file_signature()` needs a new magic-byte check for it, or a disguised file of the wrong type would be silently accepted. |

### Network / trusted hostnames

| | |
|---|---|
| **Files** | `app/database.py` (`network_configs` table - purely additive, no migration needed) · `app/services/network.py` (validation, CRUD, `get_active_hostnames()`) · `app/network_middleware.py` (`TrustedHostFromDBMiddleware` - the actual enforcement) · `app/main.py` (middleware registration - order matters, see "Watch out for") · `app/routes/network_routes.py` · `app/templates/network_settings.html` · `app/templates/base.html` (nav link) · `docker-compose.yml` (optional `cloudflared` service, gated behind the `cloudflare` Compose profile) · `.env.example` (`CLOUDFLARE_TUNNEL_TOKEN`). |
| **Tests** | `tests/test_services_network.py` · `tests/test_network_middleware.py` (the security-critical one - actually sends requests with custom `Host` headers to prove enforcement, not just that the service layer stores data correctly) |
| **Watch out for** | This is the **one** piece of "network configuration" that's genuinely live-editable from the web UI with no restart - it works specifically because `TrustedHostFromDBMiddleware` queries `get_active_hostnames()` fresh on every request rather than being fixed at app-construction time. That pattern does **not** generalize to most other settings: `SESSION_HTTPS_ONLY`, for instance, is baked into `SessionMiddleware` when the FastAPI `app` object is built in `main.py` and genuinely cannot be changed without a process restart - don't build a "live toggle" for it or anything else configured via `app.add_middleware()`. Self-lockout is the central risk of this whole feature: an empty allowlist means unrestricted access (today's default, unchanged for anyone who never touches this), and `localhost`/`127.0.0.1`/`testserver` are *always* accepted no matter what's configured - both of these must be preserved in any future change here, or a manager could lock themselves out of their own app with one form submission. Middleware registration order in `main.py` matters: `TrustedHostFromDBMiddleware` is added *after* `SessionMiddleware` so it ends up outermost (Starlette applies middleware in reverse registration order) and rejects a bad `Host` header before anything else touches the request. |

### Backup / restore

| | |
|---|---|
| **Files** | `app/backup.py` (nearly everything lives here) · `app/routes/backup_routes.py` · `app/sandbox.py` (used for restore-time permission stripping) · `app/pdf_common.py` is unrelated - don't confuse the two. |
| **Tests** | `tests/test_backup.py` (correctness **and** the security battery: zip-slip, zip-bomb, corrupt-file, path-traversal) |
| **Watch out for** | `_validate_sqlite_db()`'s `required` table set is the **minimum** proof a file is a petty cash database, not a list of every table - adding each new table to it (e.g. `expense_shares`) would make every backup taken before that table existed un-restorable. Leave it alone when adding a table; check it only when removing or renaming one of the six already there. If the `expenses` or `expense_lines` schema changes, **`app/backup.py`'s own raw SQL must be checked** - it queries these tables directly (backup manifest stats, the backup inspector) rather than going through the service layer, so schema changes don't automatically propagate here. This has caused real breakage once already. |

### Reports / exports (Excel, PDF)

| | |
|---|---|
| **Files** | `app/programs/expenses/services/reports.py` (Expense Program's exports) · `app/services/audit.py` (audit log's own export, intentionally separate since the audit log is shared/system-wide) · `app/pdf_common.py` (shared document header/footer/escaping/`standard_table_style()` - **use these rather than re-implementing** for any new PDF export, in any program) · `app/excel_common.py` (shared `write_metadata_block()`/`style_header_row()` - same idea, for Excel) · `app/programs/expenses/pdf_reports.py`. |
| **Tests** | `tests/test_services_reports.py` · `tests/test_services_audit.py` · `tests/test_pdf_common.py` · `tests/test_excel_common.py` |
| **Watch out for** | Exports are at **line-item granularity**, not one row per voucher - a voucher with 3 lines produces 3 export rows, with voucher-level fields (voucher_no, date, paid_to) repeated. This is deliberate (category and VAT are per-line), don't "simplify" it back to one row per voucher without re-checking category/VAT totals still attribute correctly. A subtle SQL bug (`LEFT JOIN` filter in the wrong `ON` clause, letting unrelated lines leak into a category's sum) was caught here once by an existing test's expected total not matching - if a total looks suspiciously large after a query change, check every `LEFT JOIN`'s filter is in a subquery or the correct `ON` clause, not just "somewhere in the join chain." `pdf_common.standard_table_style()` was extracted after a duplication check found the same black-header/zebra-stripe `TableStyle` block copy-pasted 5 times across 3 files with 3 slightly different variants (whether the zebra stripe covers a trailing totals row, what `VALIGN` to use, extra `LINEBELOW` rules) - its `zebra_end`/`valign`/`extra` parameters exist specifically to cover those real differences; a new PDF table should use it rather than writing a 4th variant inline. |

### Shared helpers extracted from duplication (tests, Excel, templates, PDF tables)

| | |
|---|---|
| **Files** | `tests/conftest.py` (`first_category_id()`, `second_category_id()`, `first_cost_center_id()` - shared test lookups; import these rather than adding another local copy) · `app/excel_common.py` (`write_metadata_block()`, `style_header_row()` - the Excel equivalent of `pdf_common.py`, for the same reason: two Excel exports had it duplicated byte-for-byte) · `app/pdf_common.py`'s `standard_table_style()` (see the Reports/exports row above) · `app/templates/_standalone_header.html` (shared top-of-document header for the two pages - `programs.html`, `coming_soon.html` - that don't extend `base.html`) · `app/programs/expenses/templates/expenses/_donut_chart.html` (see the donut chart row above) · `app/templates/_users_subnav.html` (the Accounts / Roles & Permissions tab bar, included by both `users.html` and `roles_permissions.html` - same reasoning as the two partials above: two pages, one bit of markup, edited in one place) · `app/templates/_notification_bell.html` (the Alerts link + badge - the app has two top bars, see the Notifications row) · `tests/conftest.py`'s `create_voucher_over_http()` (both the permission tests and the CSRF tests need a genuinely submitted voucher; it goes through the form on purpose, since a voucher made by calling the service directly wouldn't prove the form fields still line up). |
| **Tests** | `tests/test_excel_common.py` · existing tests for each consumer (`test_services_reports.py`, `test_services_audit.py`, `test_pdf_voucher.py`, `test_chart_helpers.py`, `test_routes_programs.py`) all continued to pass unchanged, which is the point - these were pure extractions, not behavior changes. |
| **Watch out for** | This codebase was audited for duplication with `jscpd` (`jscpd app tests --min-lines 5 --min-tokens 40`) - 57 clones found, 8 clone-pairs fixed via the extractions listed here, the rest left alone deliberately (see below). Re-run that command after a large change if you want the same objective measurement rather than guessing. Two real, unrelated bugs were caught *while verifying* these extractions, not by them: a test helper's hardcoded `expense_date` silently aged out of "this month" as real time passed (fixed to use `date.today()`); and `tests/conftest.py` pinned `COMPANY_NAME` for test isolation but never pinned `COMPANY_ADDRESS`, so a stray `.env` file left over from manual testing leaked the real address into test runs non-deterministically (fixed by pinning it to `""`). Neither would have been caught by a diff review alone - both only surfaced by actually running the tests and asking why a number that should have been deterministic wasn't. **Deliberately not touched**: the ~25-30 `guards.require_role_action()` + `guards.require_csrf()` clones across every route file (explicit security-critical boilerplate, not accidental copy-paste - a `Depends()`-based refactor is possible but wasn't done unprompted, since it touches every route file at once) and `database.py`'s two migration functions (each pins a historical schema snapshot on purpose - sharing that code would risk corrupting old migrations if the current schema changes later). The current `jscpd` baseline on this codebase is **187 clones**; the sharing/notifications/toasts/clone work above was checked against it and came back to exactly 187, having introduced two new clones (the bell markup across both top bars, and a duplicated voucher-creation setup across two test files) and then extracted both. A remaining 6-line overlap between `base.html` and `_standalone_header.html` (the bell include, the `who` span, the logout link and their closing tags) is left alone on purpose - the two files legitimately differ above that point, and extracting three trivial lines plus closing tags would cost more clarity than it buys.

### Cash float / imprest ledger

| | |
|---|---|
| **Files** | `app/programs/expenses/services/cash_ledger.py` (`get_balance()`, `top_up()`, `manual_adjust()`, `record_approval_movement()`, `record_rejection_movement()`, `is_float_low()`) · `app/programs/expenses/routes/cash_routes.py` (`/cash`, `/cash/topup`, `/cash/adjust`) · `app/programs/expenses/templates/expenses/cash_ledger.html` · `app/database.py` (`cash_float`, `cash_movements` tables) · `app/programs/expenses/services/expenses.py` (`approve_expense()` now returns `float_warning: bool` and deducts float; `reject_expense()` restores float) · `app/programs/expenses/routes/expense_routes.py` (`approve_expense` route reads return value, stashes warning in session) · `app/programs/expenses/templates/expenses/expense_detail.html` (flash banner) · `app/programs/expenses/templates/expenses/dashboard.html` (cash balance stat card) · `app/main.py` (router registered) · `app/templates/base.html` (nav link). |
| **Tests** | `tests/test_services_cash_ledger.py` |
| **Watch out for** | Float is **warn-only** — approval never blocked even when balance goes negative. `approve_expense()` now returns a bool (breaking change from prior void return); any caller that checked `if approve_expense(...)` is now checking the float warning, not an error. `record_approval_movement()` and `record_rejection_movement()` are called inside the same DB connection as the status change — they accept `conn` directly and never commit themselves. Budget over-run check runs **after** the approval commits (separate `get_db()` call) so it reads committed state — keep this order if touching either function. |

### Period close / month lock (expenses)

| | |
|---|---|
| **Files** | `app/programs/expenses/services/period_lock.py` (`lock_month()`, `unlock_month()`, `is_locked()`, `list_locked_months()`, `check_locked()`) · `app/database.py` (`expense_locked_months` table) · `app/programs/expenses/services/expenses.py` (`create_expense()` and `submit_draft()` both check lock before voucher number allocation) · `app/programs/expenses/routes/settings_routes.py` (`/settings/period-lock/lock`, `/settings/period-lock/unlock`) · `app/programs/expenses/templates/expenses/settings.html` (period close card). |
| **Tests** | `tests/test_services_period_lock.py` |
| **Watch out for** | Lock blocks **new** vouchers dated in that month only — identical rule to `production_locked_months`. Editing/approving/rejecting/deleting existing vouchers in a locked month is still allowed. Drafts bypass the lock (lock fires at `submit_draft()`, not at `create_expense(..., is_draft=True)`). `check_locked()` is the connection-level variant for use inside an existing transaction; `is_locked()` opens its own connection. |

### Budget vs Actual

| | |
|---|---|
| **Files** | `app/programs/expenses/services/budgets.py` (`set_budget()`, `delete_budget()`, `list_budgets()`, `get_budget_vs_actual()`, `check_over_budget()`, `notify_managers()`, `get_notifications()`, `mark_notifications_read()`) · `app/programs/expenses/routes/budget_routes.py` (`/budgets`, `/budgets/set`, `/budgets/{id}/delete`) · `app/programs/expenses/templates/expenses/budgets.html` · `app/database.py` (`budgets`, `notifications` tables) · `app/programs/expenses/services/expenses.py` (`approve_expense()` calls `check_over_budget()` + `notify_managers()` after commit) · `app/programs/expenses/routes/expense_routes.py` (dashboard route fetches unread notifications for manager) · `app/programs/expenses/templates/expenses/dashboard.html` (over-budget notification banner) · `app/main.py` (router registered) · `app/templates/base.html` (nav link). |
| **Tests** | `tests/test_services_budgets.py` |
| **Watch out for** | Over-budget is **warn-only** — never blocks voucher creation or submission. `check_over_budget()` opens a fresh connection and reads committed data — it must be called **after** the approval transaction commits, not inside it (SQLite's default isolation would otherwise miss the uncommitted approval). `NULL cost_center_id` = company-wide budget; `NULL category_id` = all categories. The UNIQUE constraint is `(cost_center_id, category_id, period)` — both NULLs are treated as equal by SQLite's `IS` comparison in the upsert query (standard `= NULL` would never match). Notifications are per manager-user; `mark_notifications_read()` clears all for that user when they visit the Budgets page. |

### VAT Input Tax Summary

| | |
|---|---|
| **Files** | `app/programs/expenses/services/vat_report.py` (`vat_summary()`, `build_vat_xlsx()`, `build_vat_pdf()`, `allocate_document_number()`) · `app/programs/expenses/routes/vat_routes.py` (`/vat-report`, `/vat-report/export.xlsx`, `/vat-report/export.pdf`) · `app/programs/expenses/templates/expenses/vat_report.html` · `app/main.py` (router registered) · `app/templates/base.html` (nav link — visible to all roles, not manager-only). |
| **Tests** | `tests/test_services_vat_report.py` |
| **Watch out for** | **Approved only** — pending/rejected/deleted lines never appear. Groups by `supplier_id`; a vatable line with `supplier_id=NULL` appears as "*(No supplier recorded)*" — this is intentional to surface data-quality gaps. `trx_count` counts `expense_lines` rows; `voucher_count` counts `DISTINCT expense_id` — these differ when one voucher has multiple vatable lines to the same supplier. Document numbers use the `VAT` doc_type prefix (`VAT-2026-00001`), separate sequence from `RPT` exports. PDF uses `standard_table_style()` with `zebra_end` set to exclude the totals row from zebra-striping. |

### GL account codes on categories

| | |
|---|---|
| **Files** | `app/database.py` (`gl_code TEXT` column added to `categories` table; `_migrate_add_category_gl_code()` migration for existing databases) · `app/programs/expenses/services/settings.py` (`update_gl_code()`) · `app/programs/expenses/routes/settings_routes.py` (`/settings/categories/{id}/gl-code`) · `app/programs/expenses/templates/expenses/settings.html` (GL Code column with inline edit form). |
| **Tests** | Covered by `tests/test_services_settings.py` (existing) — `update_gl_code` follows same pattern as `toggle_category`. |
| **Watch out for** | Free-text field, no format enforced. `update_gl_code()` stores `None` (SQL NULL) when the submitted value is blank, so cleared GL codes don't appear as empty strings. Migration guards against fresh installs where `categories` table doesn't exist yet when `_migrate_add_category_gl_code()` runs (same guard pattern as `_migrate_add_expense_line_supplier()`). |



| | |
|---|---|
| **Files** | `install.sh` (Linux/macOS) · `install.ps1` (Windows) · `install.bat` (thin double-click wrapper around `install.ps1` - bypasses PowerShell's execution policy for that one run only, changes nothing system-wide). |
| **Tests** | No automated tests - see "Watch out for". |
| **Watch out for** | **This has no automated test coverage** - it was checked by actually running it (`shellcheck` plus a real end-to-end run for `install.sh`; a real PowerShell AST parse-check plus a real partial run for `install.ps1`, since this sandbox can't fully exercise Windows-only paths like Docker Desktop or `venv\Scripts\`). A subtle bug was caught exactly this way: PowerShell's `$array[1..($array.Length-1)]` does **not** return an empty array when `$array` has one element - it silently returns a descending range that re-includes element 0, doubling an executable up as its own first argument. This broke Python detection completely until fixed with `Select-Object -Skip 1` (see `Split-Command` in `install.ps1`). A second, more consequential bug shipped anyway and was only caught by an actual user on Windows: `Setup-EnvFile`/`setup_env_file` (both scripts) generated `SECRET_KEY` by shelling out to Python, but is called from **both** the native path (which resolves a Python binary first) and the **Docker path (which never does - the whole point of choosing Docker is not needing Python on the host at all)**. On Docker, this crashed outright (`You cannot call a method on a null-valued expression` in PowerShell; a silent empty-command failure in bash). Fixed by generating the secret with tools guaranteed present regardless of path - `/dev/urandom` + `od` in bash, .NET's `RandomNumberGenerator` in PowerShell (`New-SecretKey`) - removing the Python dependency from this function entirely rather than just fixing the Docker path's call site. The lesson: this sandbox has no real Docker available, so **the Docker code path in both scripts has only ever been read, never actually run** - any future change here should be tested by a real person choosing that path before being trusted, the same way the native path already was. Any change to either script should be run for real, not just read - this exact class of bug reads as correct and only shows up at runtime. |

### Employee salary advances (receivable ledger + settlement)

| | |
|---|---|
| **Files** | `app/programs/expenses/services/advances.py` · `app/programs/expenses/routes/advances_routes.py` · `app/programs/expenses/templates/expenses/advances.html` · `app/programs/expenses/templates/expenses/advance_detail.html` · `app/database.py` (`employees`, `advance_ledger` tables; `employee_id/employee_name/employee_phone` on `expense_lines`; `_migrate_add_advance_line_fields()`) · `app/config.py` (`ADVANCE_CATEGORY_NAME`) · `app/programs/expenses/services/expenses.py` (`_validate_lines()`, `_write_lines()`, `approve_expense()`) · `app/programs/expenses/routes/expense_routes.py` (`_extract_lines_from_form()`, all form renders) · `app/programs/expenses/templates/expenses/expense_form.html` · `app/programs/expenses/templates/expenses/expense_detail.html` · `app/programs/expenses/pdf_voucher.py` · `app/main.py` · `app/templates/base.html`. |
| **Tests** | `tests/test_services_advances.py` (24 tests). |
| **Watch out for** | Employee name + phone required **only in strict mode** — drafts bypass. `record_advance()` called post-commit (same pattern as budget notifications — never inside the approval transaction). Settlement approval gates: manager/accountant → immediate; `user` role → `pending_approval=1`, needs approval. Settlement amount validated against outstanding balance. `expense_form.html` has **two** copies of each line field (server-rendered + `<template>` clone) — both must be updated together. `ADVANCE_CATEGORY_NAME` in `config.py` is the identity key — if manager renames the "Salary advance" category in Settings, advance tracking silently stops for that category. |

### Security (CSRF, sessions, sandboxing)

| | |
|---|---|
| **Files** | `app/auth.py` (password hashing, session helpers, lockout) · `app/routes/guards.py` (the **one** shared implementation of "not logged in" / "wrong role" / "bad CSRF" - every route file uses this rather than its own copy) · `app/sandbox.py` · `app/pdf_common.py` (`safe_text()` - escapes user text before it reaches reportlab's markup-interpreting `Paragraph`, since an unclosed tag typed into any free-text field used to crash PDF generation). |
| **Tests** | `tests/test_auth.py` · `tests/test_routes_csrf.py` · `tests/test_sandbox.py` · `tests/test_pdf_common.py` |
| **Watch out for** | Any new free-text field that ends up in a PDF **must** be passed through `pdf_common.safe_text()` before `Paragraph()` - this isn't optional, it's the only thing standing between a stray `<b>` a user types and a crashed export. `guards.py`'s wrong-role redirect target is `/programs` (the hub), not `/` - keep it that way. Bare `/` is the **login page** (`app/routes/auth_routes.py`'s `home()`, which just calls `login_form()`), so redirecting a logged-in-but-wrong-role user there would bounce them straight back to `/programs` anyway - an extra hop for nothing. `/` used to 404; a test in `tests/test_routes_programs.py` pins the current behaviour (anonymous -> 200 login form, logged in -> 302 `/programs`). |

---

## Schema changes (its own procedure - read this before touching `SCHEMA` in `app/database.py`)

This app has **already migrated its schema once** (single-transaction
vouchers → header + `expense_lines`), so the pattern below is proven, not
theoretical.

1. **Change `SCHEMA` in `app/database.py`** for what a *fresh* install should
   look like.
2. **Decide if existing databases need migrating.** If you're adding a new
   table or a new nullable column, `CREATE TABLE IF NOT EXISTS` /
   `ALTER TABLE ... ADD COLUMN` is usually enough and no migration function
   is needed. If you're removing/renaming a column, changing a `NOT NULL`
   constraint, or changing a `CHECK` constraint (including adding a role to
   the `users.role` check), SQLite can't alter that in place - you need a
   migration function following the pattern of
   `_migrate_single_line_vouchers()`: rename the old table, create the new
   one, copy data across row by row, drop the old table.
3. **Call the migration from `init_schema()`**, before the `SCHEMA` script
   runs, exactly like the existing call.
4. **Write a migration test** in `tests/test_database_migration.py`: hand-
   build a database in the *old* shape (see `_build_old_shape_database()`
   for the existing example), run `init_schema()`, and assert the data
   survived correctly. This is the only way to actually prove the migration
   works, since every test's own database is always freshly created in the
   *current* shape and never exercises the migration path otherwise.
5. **Check `app/backup.py` for raw SQL** referencing the changed table(s) -
   see the Backup/restore row above.
6. **Check every service that queries the changed table directly** - schema
   changes don't propagate automatically to hand-written SQL elsewhere.


### Asset & Maintenance module (the one deliberate seam)

This is the only deliberate coupling point between the Expense Program and the Asset Module. Every other change below is fully self-contained.

| | |
|---|---|
| **Files** | `app/database.py` (4 new tables: `assets`, `asset_assignments`, `asset_service_log`, `asset_tag_rules`; 3 additive `ALTER TABLE` columns: `expense_lines.asset_id`, `employees.user_id`, `categories.requires_asset`/`asset_category`; migration function `_migrate_add_asset_module_fields()`) · `app/config.py` (`ASSET_CATEGORIES`, `DEFAULT_ASSET_TAG_RULES`, `DEFAULT_ASSET_REQUIRED_CATEGORIES`, 5 new permission keys) · `app/auth.py` (5 new `can_*()` wrappers) · `app/seed.py` (tag rules + category ticks, first-run only) · `app/programs/expenses/routes/expense_routes.py` (`_extract_lines_from_form()` asset_ids, `_asset_picker_data()`, all four `TemplateResponse` contexts) · `app/programs/expenses/templates/expenses/expense_form.html` (**two** copies: server-rendered row **and** `<template id="lineTemplate">`, plus JS `CATEGORY_ASSET_RULES`, `updateAssetVisibility()`) · `app/programs/expenses/services/expenses.py` (`_pickable_asset_count()`, `_validate_lines()` asset branch, `_write_lines()`, `_fetch_lines()`, `submit_draft()`) · `app/programs/assets/` (full new program package) · `app/main.py` · `app/routes/programs_routes.py` · `app/templates_env.py` · `app/templates/base.html` |
| **Tests** | `tests/test_database_migration.py` (4 new asset migration tests) · `tests/test_services_assets.py` · `tests/test_services_service_log.py` · `tests/test_services_timeline.py` · `tests/test_routes_assets.py` |
| **Watch out for** | `expense_form.html` has **two** copies of every line field (server-rendered initial rows and `<template id="lineTemplate">` for "+ Add line") — both must include `line_asset_id` and the `asset-fields` div, or newly-added lines will be missing the picker. This is the same rule as the employee-advance fields. The "requires asset?" and "which type?" rules live in `categories.requires_asset`/`categories.asset_category` (data, not code) — changing which categories demand an asset is done at **Settings → Assets → Category rules** from the UI with no code change. The asset requirement is scoped to what the person **actually has to pick**: a driver holding no truck can still record maintenance costs (the line saves unlinked, voucher goes pending for manager review). The `_pickable_asset_count()` call in `expenses.py` and `pickable_assets()` in `assets.py` use the same rule — if you change one, change both. `asset_service_log` has NO amount column — by design. Adding one would create a second cost total that drifts from `expense_lines`. `app/backup.py`'s `_snapshot_counts()` and `inspect_backup()` raw SQL read only `amount + vat_amount` from `expense_lines` and are unaffected by the new `asset_id` column (confirmed and commented 2026-09). Divestment proceeds live on `assets.divest_proceeds`, not on an assignment row — an asset sold while unassigned has no custody row, and the CHECK constraint on `asset_assignments` (must have employee OR cost_centre) would reject a placeholder row. |

### Asset service log (non-cost maintenance notes)

| | |
|---|---|
| **Files** | `app/programs/assets/services/service_log.py` · `app/programs/assets/routes/service_routes.py` · `app/database.py` (`asset_service_log` table) · `app/sandbox.py` (reused unchanged for service photos) · `app/config.py` (`log_asset_service` permission) · `app/auth.py` (`can_log_asset_service()` wrapper) |
| **Tests** | `tests/test_services_service_log.py` |
| **Watch out for** | The service log has a **two-tier permission gate**: role-level (`log_asset_service`) checked by `guards.require_role_action`, then record-level (is this asset currently assigned to this person, or do they have `manage_assets`?) checked by `may_log_service()` in the service layer. A `ForbiddenError` from the service means the role was fine but the asset isn't theirs. This is the same pattern as the draft-voucher ownership check. Photos reuse `sandbox.validate_file_signature()`, `safe_join()`, and `set_nonexecutable()` from `app/sandbox.py` — the same checks as voucher receipts. |

### Asset custody (assign / transfer / divest)

| | |
|---|---|
| **Files** | `app/programs/assets/services/custody.py` · `app/programs/assets/routes/custody_routes.py` · `app/database.py` (`asset_assignments` table) · `app/config.py` (`assign_asset`, `divest_asset` keys) · `app/auth.py` (`can_assign_asset()`, `can_divest_asset()`) |
| **Tests** | `tests/test_services_assets.py` (custody tests) · `tests/test_routes_assets.py` |
| **Watch out for** | `asset_assignments` deliberately has **no unique index on `asset_id`** — one asset can have several simultaneous active holders (day shift / night shift on one truck). A "fix" adding that index would break the multi-holder design. Current holders = `SELECT ... WHERE status='active'`. Transfer and divest are **single transactions** — a failed mid-transfer would otherwise leave an asset held by nobody with no record of where it went. Divestment proceeds go on `assets.divest_proceeds`, not on a custody row. |

### Asset tag rules (format validation)

| | |
|---|---|
| **Files** | `app/programs/assets/services/assets.py` (`get_tag_rules()`, `set_tag_rule()`, `_check_tag()`) · `app/programs/assets/routes/custody_routes.py` (`/assets/settings/tag-rule` POST) · `app/database.py` (`asset_tag_rules` table) · `app/config.py` (`DEFAULT_ASSET_TAG_RULES`) · `app/seed.py` |
| **Tests** | `tests/test_services_assets.py` (tag pattern tests) · `tests/test_routes_assets.py` |
| **Watch out for** | The pattern is compiled at `set_tag_rule()` time and rejected if invalid — a bad regex at save time is a Settings error the manager sees immediately; a bad regex stored in the DB would silently lock the registry for every future `create_asset()`. Missing row falls back to `DEFAULT_ASSET_TAG_RULES` (same fallback shape as `role_has_permission()` → `DEFAULT_ROLE_PERMISSIONS`). |

### Asset reports (ASR / AMR documents)

| | |
|---|---|
| **Files** | `app/programs/assets/services/asset_reports.py` · `app/programs/assets/services/timeline.py` · `app/programs/assets/routes/asset_routes.py` (single-asset PDF/Excel) · `app/programs/assets/routes/custody_routes.py` (multi-asset PDF/Excel) · `app/database.py` (`next_document_number` already handles `ASR`/`AMR` via `document_counters`) |
| **Tests** | `tests/test_services_timeline.py` (report render tests) · `tests/test_routes_assets.py` (download tests) |
| **Watch out for** | Service-row amounts for cost events are `amount + vat_amount` (same rule as all other totals in this app). Service-log rows have `amount=None` — in Excel they render as "—", not "0.00". A zero would mean "cost nothing" which is a different and wrong claim about a non-cost event. The fuel banner ("Fuel tracked as general operating cost, not against this asset") is shown on any `Fleet/Truck` asset — this is deliberate, not a missing feature: fuel is filled via the chip system and never reaches `expense_lines`. All PDFs use `pdf_common.safe_text()` on every free-text field (same requirement as every other PDF in the app). |

### UI shell (breadcrumbs, density, skeletons, sticky form footers)

| | |
|---|---|
| **Files** | `app/breadcrumbs.py` (`trail_for()`, `siblings_for()`, and the `_SECTIONS` / `_CHILDREN` / `_ADMIN` maps - the single source of truth for what a path is called) · `app/templates/_breadcrumbs.html` (trail + the two drop-downs, incl. the sessionStorage recent-history script) · `app/templates_env.py` (`breadcrumbs()` / `breadcrumb_siblings()` globals) · `app/templates/base.html` (breadcrumb include, the `.page-body` wrapper the skeleton swaps, the density pre-paint script in `<head>`, the density toggle button, the nav-intent skeleton script) · `app/templates/_standalone_header.html` (its own copy of the density pre-paint script - these two pages deliberately don't extend `base.html`) · `app/static/style.css` (`.crumbs*`, `.form-actions`, `[data-density="compact"]`, `.skel*`). |
| **Tests** | `tests/test_breadcrumbs.py` (pure-function, no DB or server) · `tests/test_routes_ui_shell.py`'s `TestBreadcrumbsRendered` |
| **Watch out for** | **A new program needs a row in `_SECTIONS` *and* `_CHILDREN`** or its pages get no trail at all - `trail_for()` returns `[]` for anything unrecognised on purpose (a guessed trail sends people somewhere wrong, which is worse than no trail). The density preference is applied to `<html>` by an inline script in `<head>`, **not** a DOMContentLoaded handler: doing it later paints the comfortable layout first and then snaps to compact, which reads as a bug. That script exists in **two** files (`base.html` and `_standalone_header.html`) and both must change together. `.form-actions` is `position: sticky`, not `fixed` - sticky keeps the row in flow so it sits normally at the end of a short form and only pins on a long one; switching to `fixed` would float it over every form. The skeleton script replaces `[data-page-body]`'s innerHTML, so anything that must survive a navigation cannot live inside it. Export links (`.xlsx`/`.pdf`/`.doc`) are excluded from the skeleton by extension - they stream a file and leave the page where it is, so a skeleton there would never be cleared; a new export extension needs adding to that regex. |

### Notification centre (urgency bands, slide-over panel)

| | |
|---|---|
| **Files** | `app/database.py` (`notifications.level` column + `_migrate_add_notification_level()` - additive `ADD COLUMN`, **not** a rebuild, see Schema changes) · `app/services/notifications.py` (`LEVELS`, `GROUPS`, `_clean_level()`, `notify(..., level=)`, `notify_role(..., level=)`, `list_grouped()`, `mark_read()`) · `app/routes/notification_routes.py` (`POST /notifications/read-all`, `POST /notifications/{id}/read`, `_back()`) · `app/templates/_notification_drawer.html` · `app/templates/_notification_bell.html` (now a `<label>` for the panel's checkbox, not a link) · `app/templates/base.html` and `_standalone_header.html` (both include the panel) · `app/templates_env.py` (`notification_groups()` global) · `app/static/style.css` (`.notif-drawer*`, `.notif-group*`, `.linkish`) · callers that set a level: `app/programs/expenses/services/budgets.py` (`warn`), `sharing.py` (`action`). |
| **Tests** | `tests/test_services_notifications.py` · `tests/test_routes_ui_shell.py`'s `TestNotificationPanel` · `tests/test_database_migration.py` (the `level` column migration) |
| **Watch out for** | **Opening the panel deliberately does not mark anything read** - it can be flicked open from any page, and clearing the badge on every glance would make the badge meaningless. The `/notifications` *page* still does mark-all-read on open; don't "fix" the inconsistency without understanding it. `level` has **no `CHECK` constraint**, on purpose: a `CHECK` would make every future urgency band a table-rebuild migration, and `_clean_level()` already folds an unrecognised value into `info` rather than dropping the notification. A new band needs an entry in **both** `LEVELS` and `GROUPS` plus a `.notif-group-<key>` rule in the stylesheet. `mark_read()` scopes ownership **in the `WHERE` clause**, not lookup-then-update - and returns the same `False` for "someone else's" and "doesn't exist", which stops it being an existence oracle. The panel and the module drawer are two independent CSS-only checkboxes (`#notif-panel`, `#module-menu`) using the `:checked ~` sibling selector, so **neither trigger can be moved out of its checkbox's sibling scope** in the markup or it silently stops working. |

### Bulk actions on the Expenses list

| | |
|---|---|
| **Files** | `app/programs/expenses/services/expenses.py` (`BulkResult`, `_clean_bulk_ids()`, `bulk_approve()`, `bulk_reject()`, `bulk_delete()`, `MAX_BULK_IDS`) · `app/programs/expenses/routes/expense_routes.py` (`POST /expenses/bulk`) · `app/programs/expenses/templates/expenses/expenses_list.html` (checkbox column, `.bulk-bar`, the selection script) · `app/static/style.css` (`.bulk-bar`, `.pick-col`). |
| **Tests** | `tests/test_services_expenses_bulk.py` · `tests/test_routes_ui_shell.py`'s `TestBulkActions` / `TestBulkPermissions` |
| **Watch out for** | Bulk functions **return a `BulkResult`, they don't raise** for per-record problems - a batch is partially-successful by nature (one voucher a colleague actioned two seconds ago), and an exception can only say "the whole thing failed", which would be a lie and would discard the nineteen that worked. Only `_clean_bulk_ids()`'s two caller-mistake cases (nothing selected, too many selected) raise `ValidationError`. Each id is delegated to the **single-record** function rather than done in one bulk `UPDATE`: approval also moves the cash float, writes advance ledger rows and fires budget notifications, none of which a bulk statement would do. **The bulk endpoint must stay exactly as narrow as the single-record routes** - `delete` picks `can_delete_expense`, everything else `can_review_expense`, and there is a test asserting an accountant is refused bulk delete specifically because this is the obvious place for a permission hole to open. |

### Undo (5-second toast button)

| | |
|---|---|
| **Files** | `app/undo.py` (`offer()`, `claim()`, `peek()`, `TTL_SECONDS`, `BUTTON_SECONDS`, `MAX_RECORDS`) · `app/routes/undo_routes.py` (the `_KINDS` map from kind → permission + reversal function, and `_back()`'s open-redirect guard) · `app/main.py` (router registration) · `app/flash.py` (`set_flash(..., undo_token=, undo_label=)`) · `app/templates/base.html` (the toast's `<form action="/undo">` and its countdown) · `app/templates_env.py` (`UNDO_BUTTON_SECONDS` global, read from `app/undo.py`) · `app/programs/expenses/services/expenses.py` (`revert_approval()`, `revert_rejection()`, `restore_deleted()`, `statuses_for()`) · `app/programs/expenses/services/cash_ledger.py` (`record_undo_movement()`) · `app/programs/expenses/services/advances.py` (`advance_rows_for_expense()`, `remove_advance_rows_for_expense()`) · `app/programs/expenses/routes/expense_routes.py` (approve/reject/delete and the bulk route all call `undo.offer()`) · `app/static/style.css` (`.toast-undo*`). |
| **Tests** | `tests/test_undo.py` (token expiry, single use, wrong token, size cap) · `tests/test_services_expenses_bulk.py` (the three revert functions, incl. ledger and audit effects) · `tests/test_routes_ui_shell.py`'s `TestUndoRoute` (CSRF, bogus token, open-redirect) |
| **Watch out for** | **`BUTTON_SECONDS` (5) is deliberately shorter than `TTL_SECONDS` (10)** - a click landing at 4.9s still has to reach the server, and equal windows would lose a race the user can't see or avoid. The countdown is presentation only; a hidden button is still clickable by anyone crafting a POST, so **the real expiry is checked server-side in `app/undo.py`, always**. `undo_routes.py` **re-checks the permission** on every undo: a token proves who acted ten seconds ago, not that they may still act - Users → Roles & Permissions applies immediately, and an undo token must not be a ten-second hole in it. Undo **never erases**: each reversal writes its own audit entry (`undo_approve_expense` etc.) and the cash float gets a *reversing* `manual_adjustment` row rather than the original movement being deleted. That `manual_adjustment` type is not stylistic - `cash_movements.movement_type` carries a SQLite `CHECK`, so inventing a fourth value would turn this into a table-rebuild migration. **Advance ledger rows are the one thing actually deleted** on an undone approval (they're derived, not typed), and only behind `remove_advance_rows_for_expense()`'s guard: if any settlement for that employee is newer than the advance row, the undo refuses outright rather than half-reversing. `restore_deleted()` needs the **pre-delete status**, which the row doesn't keep - the route captures it via `statuses_for()` *before* deleting and puts it in the token's `extra`. Adding a reversible action anywhere in the app means one new row in `_KINDS` and nothing else in that file. `undo.offer()` returns `""` past `MAX_RECORDS`, so a huge bulk action simply gets no Undo button rather than a truncated id list that reverses only part of what it claims. |

### Word Editor document export formats

| | |
|---|---|
| **Files** | `app/programs/word_editor/services/export.py` (`export_allowed()`, `EDITABLE_FORMATS`, `OFFICIAL_EXPORT_MESSAGE`) · `app/programs/word_editor/routes/document_routes.py` (`doc_export()` asks `export_allowed()` and redirects to `/print` when refused) · `app/programs/word_editor/templates/word_editor/document_detail.html` (the `.doc` button is hidden once official). |
| **Tests** | `tests/test_services_documents.py`'s `TestExportAllowed` · `tests/test_routes_word_editor.py`'s official-export block |
| **Watch out for** | **Once a document is `official` it is PDF-only.** An editable `.doc` of an issued document opens in Word carrying the official number and letterhead while no longer matching `wd_documents.official_sha`, which is what the public verify page checks - so the export would be a forgery kit with a verification badge attached. The rule lives in the **service**, not the route, so a second caller can't bypass it; the route asks, it doesn't re-implement. `html` is blocked alongside `doc` because it's the same editable body without Word's wrapper - **a new editable format must be added to `EDITABLE_FORMATS`**, which is both the allowlist for the route and the block list for official documents. `export_allowed()` fails closed on a missing document. The register export (`/documents-export.xlsx`) is a list *of* documents, not a document body, and is unaffected. Hiding the template button is not the enforcement - a typed URL hits the same wall, and there's a test that types it. |

---

## Testing commands

```bash
pip install -r requirements-dev.txt

pytest                                      # everything - always run this last
pytest tests/test_services_expenses.py      # voucher/VAT business rules
pytest tests/test_services_reports.py       # exports
pytest tests/test_database_migration.py     # schema migration correctness
pytest tests/test_backup.py                 # backup/restore incl. security battery
pytest tests/test_sandbox.py                # file upload/path sandboxing
pytest tests/test_routes_permissions.py     # every role against every route
pytest tests/test_network_middleware.py     # trusted-host enforcement (real Host headers)
pytest tests/test_routes_csrf.py            # CSRF enforcement
pytest tests/test_services_roles.py         # role permission matrix + self-lockout guard
pytest tests/test_undo.py                   # undo token: expiry, single use, size cap
pytest tests/test_breadcrumbs.py            # breadcrumb trails (pure functions, instant)
pytest tests/test_services_notifications.py # urgency bands + read state
pytest tests/test_services_expenses_bulk.py # bulk actions + the three undo reversals
pytest tests/test_routes_ui_shell.py        # bulk/undo/notification endpoints over HTTP
pytest tests/test_services_sharing.py       # voucher hand-off + notifications
pytest tests/test_flash.py                  # one-shot toast messages
pytest tests/test_services_production.py    # Weekly Productions business rules
pytest tests/test_routes_weekly_productions.py  # Weekly Productions, every role over real HTTP
pytest tests/test_services_advances.py          # employee advance ledger + settlement approval
pytest tests/test_services_cash_ledger.py      # cash float / imprest ledger
pytest tests/test_services_period_lock.py      # expense period close / month lock
pytest tests/test_services_budgets.py          # budget vs actual + notifications
pytest tests/test_services_vat_report.py       # VAT input tax summary
pytest tests/test_services_suppliers.py     # VAT supplier knowledge base
pytest tests/test_routes_suppliers.py       # suppliers, every role over real HTTP
```

```bash
npm install -g jscpd
jscpd app tests --min-lines 5 --min-tokens 40   # objective duplication check - see
                                                 # "Shared helpers extracted from duplication"
                                                 # above for the last baseline (57 -> 49 clones)
```

A green `pytest` run is necessary but **not sufficient** for anything
touching money math, PDFs, or the multi-line form - those need an actual
live check (start the app, perform the action, open the resulting file) per
step 6 above. Several real bugs in this app rendered correctly as far as
the tests could tell but were only caught by looking at the actual output.
