# Expense Program

A small web app for recording factory petty cash expenses (snacks, fuel, vehicle
maintenance, water, salary advances, and general petty cash), generating a proper
voucher for every entry, and tracking spend by category and cost centre over time.

Runs on your homelab; 5–10 people can use it at once from any browser on your
network.

> **Requesting a change?** Upload `CHANGE_IMPACT_GUIDE.md` (in this same
> folder) first. It maps common change requests to the exact files and
> tests they affect, so it doesn't need to be rediscovered each time.

---

## What it does

- A voucher can hold **more than one transaction**: add as many lines as
  needed, each with its own category, description, and amount. **Not every
  transaction is VAT-able** — mark each line individually. Amounts are
  always entered excluding VAT; if a line is VAT-able, VAT is added on top
  automatically at the rate configured in `.env`. A voucher's total and
  VAT total are always the sum of its lines.
- **Save a voucher as a draft** before it's ready — a draft doesn't need
  every field filled in (even zero transaction lines is fine) and doesn't
  get a voucher number until you submit it. Come back and edit it, submit
  it for approval once it's complete, or discard it outright if you don't
  need it — a draft was never a committed record, so discarding it deletes
  it completely rather than going through the usual soft-delete trail.
- Every expense gets a **sequential voucher number** (`PCV-2026-00001`, ...) and a
  printable **A4 PDF voucher** listing every transaction line, a VAT/subtotal
  breakdown, prepared-by / approved-by signature lines, and who printed it
  and when.
- **Approval workflow**: users record expenses as *pending*; a manager or
  accountant approves or rejects them. Only approved expenses count toward
  totals and reports.
- **Roles**:
  - `manager` — full access: create vouchers, approve/reject, **delete** vouchers,
    manage users, manage categories/cost centres, view the audit log. This is
    the out-of-the-box default — see **Roles & Permissions** below for how to
    change it.
  - `accountant` — can view everything and approve/reject vouchers. Cannot
    create, delete, or manage users/settings, by default.
  - `user` — records (creates) petty cash vouchers. Cannot approve, reject,
    or delete, by default.
  - **What each role can actually do is editable** — see **Roles &
    Permissions** below. There are always exactly these three roles; adding
    or renaming a role itself isn't available from the UI (see
    `CHANGE_IMPACT_GUIDE.md` if you need that).
- **Deleting a voucher is soft-delete only.** A manager can delete a voucher, but
  it's never actually removed from the database — it's marked `deleted`, excluded
  from lists/totals/reports, and the deletion (who, when, why) is permanently
  recorded in the audit log alongside a full snapshot of what was deleted. Nothing
  financial ever silently disappears.
- **Cost centres** (e.g. Factory Floor, Transport, Admin Office) and **categories**
  are both editable from Settings, seeded with the six categories you described.
- **Everything is filterable by date and downloadable as both Excel and PDF**:
  the Expenses list (with its full filter set — status, category, cost centre,
  date range), the Reports page (a raw listing export plus a separate category/
  cost-centre summary export), and the Audit log (which also gained its own
  date-range filter for this). Every generated PDF is **A4**, **pre-numbered**
  (`RPT-2026-00001`, `AUD-2026-00001`, ...), and shows who generated it and
  when — see **Exports & document numbering** below.
- **Audit log**: every login, voucher action, and user/settings change is
  recorded and cannot be edited from the app.
- Optional receipt photo/PDF attachment per expense (validated file type and
  size on upload).
- **Automatic backups** on a schedule, with a manager-facing page to inspect,
  download, or restore any of them — see **Backups** below.
- **Network settings** — control which hostname(s) the app accepts requests
  for, live-editable with no restart. Relevant if you expose this beyond your
  LAN (e.g. a Cloudflare Tunnel) — see **Network (trusted hostnames)** below.
- **Duplicate transaction line detection** on the Settings page — flags lines
  that look like an accidental double-entry (same voucher, category,
  description, amount, and VAT status) for review. Never deletes in bulk, and
  only ever cleans up drafts — a submitted voucher is reported, not touched.
- **Category breakdown donut chart** on the Dashboard and Reports pages —
  each category gets a small, muted accent color (used only in this chart,
  never elsewhere in the UI) so the spend mix is scannable at a glance
  alongside the usual black/white/red/blue interface.
- **Share a voucher with your accountant** — hand any submitted voucher to
  one or more people from its detail page, with an optional note. They get
  a notification linking straight back to the voucher, where the PDF and
  the receipt already live. Nothing is emailed or copied out — see
  **Sharing a voucher** below for why.
- **Notifications** — **Alerts** in the top bar opens a slide-over panel
  grouping what's waiting for you by urgency (**Action needed**,
  **Warnings**, **Information**) with an unread count on the badge. A full
  `/notifications` page is still there for the whole list.
- **Undo** — approving, rejecting or deleting a voucher shows a
  confirmation with an **Undo** button for five seconds. Click it and the
  voucher goes back where it was — including the cash float. Nothing is
  erased: both the original action and the undo stay in the audit log.
- **Bulk actions** — tick several rows on the Expenses list and a toolbar
  appears to approve, reject or delete them in one go, with the same
  permissions and the same reason requirement as doing them one at a time.
- **Breadcrumbs** at the top of every page, with a **Recent** drop-down of
  where you've just been and a **Go to** drop-down for the rest of the
  program you're in.
- **Compact / Comfortable** toggle in the top bar — switch to a tighter
  table for long financial grids. Remembered per browser.
- **Save/Submit buttons stay put** at the bottom of long forms instead of
  scrolling away, and pages show placeholder blocks while the next one
  loads rather than sitting frozen on the old one.
- **Duplicate a voucher** — the **Duplicate** button on any voucher copies
  it into a fresh draft for you to adjust, for spend that repeats week
  after week. See **Duplicating a voucher** below for what does and
  doesn't carry over.
- **Drag a receipt onto the form** — the receipt field on the New Voucher
  page accepts a dropped photo or PDF as well as a click-to-browse. The
  file still goes through exactly the same validation either way.
- **The category you use most is pre-selected** on a new voucher, per
  person, based on your recent entries. Change it like any other dropdown —
  it's a starting point, not a rule.
- **Confirmations appear as a small banner** in the corner that fades by
  itself, rather than a dialog you have to dismiss.

### Exports & document numbering

Every downloadable document in this app — voucher PDFs, the Expenses/Reports/
Audit Excel and PDF exports — follows the same convention, matching standard
cost-accounting practice:

- **A4** for every PDF (vouchers are portrait; multi-column listings are
  landscape so the columns stay readable).
- **Pre-numbered**: vouchers keep their own `PCV-YYYY-NNNNN` sequence exactly
  as before. Every other generated document (report/audit exports, in both
  Excel and PDF) gets its own sequential reference — `RPT-2026-00001` for
  report-style exports, `AUD-2026-00001` for audit log exports — allocated at
  the moment it's generated, so no two exported documents ever share a number.
- **Generated by / Printed on**: every export shows who requested it and the
  exact timestamp it was produced, right at the top of the document.
- **Date-wise filtering**: every listing page (Expenses, Reports, Audit log)
  filters by date range, and whatever filter is currently applied carries
  through into the export — download exactly the slice of data you're
  looking at, not the whole table.



- Passwords hashed with **bcrypt**; never stored or logged in plain text.
- **CSRF protection** — every form that changes data carries a per-session token
  that's verified on submit.
- **Account lockout** — 5 failed logins locks an account for 15 minutes
  (configurable via `.env`), preventing password-guessing attacks.
- **Soft-delete only** for vouchers — see above. There is no way to permanently
  erase a financial record from the UI.
- All database queries are parameterised (no SQL injection surface).
- File uploads are restricted to images/PDF and a configurable size limit;
  filenames are sanitised against path traversal.
- Session cookies are `httponly` and `SameSite=Lax` by default; set
  `SESSION_HTTPS_ONLY=true` in `.env` if you put this behind HTTPS.
- **Backup/restore hardening**: restore is manager-only, requires typing
  `RESTORE` plus a reason, and always takes an automatic safety backup first.
  Every backup filename is checked against a strict allowlist pattern before
  any file operation (blocks path traversal). Restoring from an *uploaded*
  file — the disaster-recovery path — treats it as untrusted: the archive is
  checked for zip-slip paths, capped on entry count and total uncompressed
  size (zip-bomb protection), and the database inside it must pass a SQLite
  integrity check and contain the expected tables before anything is touched.
  A separate restore log outside the database records every attempt,
  including rejected ones, so the record survives even a restore that
  replaces the database itself.

---

## Static showcase (`site/`) — for testers, not a deployment

`site/index.html` is a single self-contained page showing the interface and
the rules this app enforces. It has no backend, no database and no login —
it exists so a tester can see what the app looks like without installing
anything.

Deploy it on Netlify by dragging the `site/` folder onto Netlify's drop
zone, or by pointing Netlify at this repository and leaving the build
command empty (`netlify.toml` at the root already sets
`publish = "site"`). The real app is **not** deployable this way — it's a
FastAPI + SQLite server and still has to be self-hosted per the sections
below.

---

## Easiest setup: the installer

From the project folder:

```bash
./install.sh          # Linux / macOS
```

```powershell
.\install.ps1          # Windows (PowerShell) - or just double-click install.bat
```

It checks for everything this app needs (Docker, or Python 3.10+ and the
`venv` module) and installs whatever's missing, asks whether you want the
Docker or plain-Python setup, creates `.env` with a freshly generated
`SECRET_KEY` (never the insecure placeholder), initializes the database,
and - for the plain-Python path - runs the full test suite automatically as
a final self-check before declaring success.

Useful flags: `--docker` / `--native` to skip the prompt and pick a path
directly, `--yes` to not prompt before installing missing prerequisites
(handy for a fresh unattended VM), `--skip-tests` to skip the automatic
self-check. Safe to re-run - it won't recreate an existing `venv` or `.env`.

If you'd rather do it by hand, or the installer can't auto-install
something on your system, the two sections below are exactly what it
automates.

---

## Quick start (Docker — recommended for your homelab)

1. Copy `.env.example` to `.env` and edit it:
   - Set `SECRET_KEY` to a long random string (this signs login sessions).
   - Set `COMPANY_NAME` and `COMPANY_ADDRESS` (the address is optional -
     leave it blank to omit it) - shown on every voucher, report, and the
     login page.
   - Set `CURRENCY_SYMBOL` if you don't want the SAR default.
   - You can leave `DEFAULT_ADMIN_USERNAME` / `DEFAULT_ADMIN_PASSWORD` as-is —
     you'll change the password after first login.

2. From the project folder:

   ```bash
   docker compose up -d --build
   ```

3. Open `http://<your-server-ip>:8000` from any device on your network - the
   bare address is the login page itself, so there's nothing to navigate to
   first.

4. Log in with the admin credentials from your `.env` file. **Go to Users and set
   a real password immediately** — the example password is not secure.

Your data (the SQLite database and any uploaded receipts) lives in the `data/`
folder next to `docker-compose.yml`, which is mounted into the container. Back
this folder up regularly — see **Backups** below.

To stop it: `docker compose down` (your data in `data/` is untouched).
To update after changing code: `docker compose up -d --build`.

---

## Quick start (without Docker)

Needs Python 3.10+.

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # then edit .env - see step 1 above
python init_db.py               # creates the database + prints admin login
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Then open `http://<your-server-ip>:8000` from another device on the network, or
`http://localhost:8000` on the same machine - either lands straight on the
login page.

To keep it running after you close the terminal, run it as a systemd service or
inside `screen`/`tmux`, or just use the Docker option above.

---

## Day-to-day use

1. A **user** logs in, clicks **+ New Voucher**, fills in the date, who was
   paid, and cost centre, then adds one or more transaction lines (category,
   description, amount excluding VAT, and whether that line is VAT-able) and
   optionally attaches a receipt photo. **Submit for approval** validates
   everything and immediately creates a numbered voucher (status: *pending*)
   with a printable PDF - or **Save as draft** first if it's not ready yet
   (see **Multiple transactions per voucher, and VAT** and **Drafts** below).
2. A **manager or accountant** opens **Expenses**, reviews pending vouchers, and
   clicks **Approve** (or **Reject** with a reason).
3. If a voucher was recorded in error, only a **manager** can delete it — this
   requires a reason, which is kept permanently in the audit log along with the
   voucher's details, even though the voucher itself disappears from lists and
   totals.
4. **Reports** and the dashboard total only count approved expenses — so figures
   always reflect signed-off spending.
5. Print or save any voucher PDF from its detail page (browser's print dialog
   works fine on the embedded preview).
6. Use **Reports → Listing → Excel** for a spreadsheet of any date range, e.g.
   for your accountant or monthly books.

## Setting up your categories & cost centres

Go to **Settings** (admin only). The six categories you described are pre-loaded
and editable. Add a cost centre for each department/location you want to track
separately (e.g. `FACTORY` / Factory Floor, `TRANSPORT` / Vehicles, `ADMIN` /
Office) — this is what powers the "which department is costing us most" view in
Reports.

## Multiple transactions per voucher, and VAT

One voucher can cover several purchases in a single payment — click **+ Add
line** on the New Voucher page for each one. Every line has its own category,
description, amount, and a **VAT-able?** yes/no — a single voucher can freely
mix VAT-able and non-VAT-able lines.

The amount you enter for every line is always **excluding VAT**. If a line
is marked VAT-able, the app automatically adds VAT on top at the configured
rate to get that line's total; if not, nothing is added. A voucher's overall
total is always the sum of every line's total (amount, plus VAT where it
applies). The rate is set once for the whole app in `.env`:

```
VAT_RATE=15.0
```

Change this if your VAT rate changes — vouchers already recorded keep the
rate that was in effect when they were entered, so past vouchers are never
retroactively altered.

### Suppliers (VAT knowledge base)

A VAT-able line requires a **supplier name** and **supplier VAT number** —
they show up right on that line ("Fuel top-up (Al Noor Fuel Station, VAT:
100234567800003)") on the voucher detail page, the printable PDF, and the
Reports exports. A draft can be saved with these left blank (fill them in
later); submitting for real requires them.

Typing a VAT number you've used before auto-fills the supplier name — you
never have to retype a supplier you've already recorded. The first time a
name/VAT pair is used, it's saved automatically to a small supplier list;
there's no separate "add a supplier" step. Duplicate purchases from the
same supplier always attach to the same supplier record, keyed by VAT
number.

**Settings → Suppliers** (manager only, since it's part of the Settings
page) lists every supplier, searchable by VAT number, and shows how many
lines reference each one. **Deleting a supplier is soft-delete only** and
manager-only — it drops out of the picker offered for *new* lines, but
every voucher that already used it keeps showing exactly the same name and
VAT it always did, since deleting never touches the supplier's own name/VAT
or any voucher line that points at it.

## Drafts

Not ready to submit a voucher yet? Click **Save as draft** instead of
**Submit for approval** — a draft doesn't need every field filled in (even
zero transaction lines is fine) and doesn't get a voucher number yet, since
it isn't a real transaction until it's actually submitted.

- A draft shows up on the **Dashboard** with a reminder, and under
  **Expenses** when you filter by status = Draft.
- Open it any time to **keep editing**, click **Submit for approval** once
  it's complete (this is when the usual full validation applies and a real
  voucher number is issued), or **Discard** it if you don't need it after
  all.
- Discarding a draft **deletes it completely** rather than soft-deleting it
  like a real voucher — nothing about it was ever committed to the audit
  trail, so there's nothing that needs preserving.
- Only the draft's own creator, or a manager, can edit, submit, or discard
  it — anyone else gets a permission error, even if they can otherwise see
  it in the Expenses list.

## Sharing a voucher

Open any submitted voucher and use **Hand this voucher to someone** — tick
who should see it, add a note if it helps ("please book this against
April"), and click **Share**. Each person gets a notification linking back
to that voucher, and the hand-off is listed on the voucher itself along
with who shared it and when. It's also written to the audit log.

**Why this isn't an email.** An emailed voucher becomes a second copy of a
record the app already holds — it goes stale the moment anything changes,
and it needs a mail server this app has no reason to depend on. Pointing
the accountant at the live record instead means they always see the current
version, with the PDF and the receipt attached where they already are.

A voucher can only be shared once it's been **submitted**. A draft has no
voucher number and doesn't appear in anyone else's lists, so there'd be
nothing for the recipient to act on. A deleted voucher can't be shared
either.

## Notifications

**Alerts** in the top bar shows how many things are waiting for you and
opens a panel over the right-hand side of whatever page you're on, grouped
by how urgent they are:

- **Action needed** — someone is waiting on you (a voucher handed to you).
- **Warnings** — something to look at (a budget over-run, for managers).
- **Information** — everything else.

Opening the panel does **not** mark anything read, on purpose: you can flick
it open from anywhere, and a badge that cleared itself every time you
glanced at it would stop meaning anything. Use **Mark read** on an item or
**Mark all read** at the bottom. The full `/notifications` page still marks
everything read when you open it, since going there is a deliberate act.

The permanent record of what actually happened is always the audit log — a
notification is a nudge, not a record.

## Undo

Approving, rejecting or deleting a voucher shows a confirmation with an
**Undo** button and a five-second countdown. Click it and the voucher goes
straight back to where it was: a reverted approval returns to *pending*, a
deleted voucher comes back at whatever status it held before, and the cash
float is put back to match.

A few things worth knowing:

- **Nothing is erased.** The original action and the undo are both written
  to the audit log, and the cash float shows the movement *and* its
  reversal. This is a correction, not a cover-up.
- **Five seconds means five seconds.** After that the button is gone and the
  app won't accept a late one. Past that point, use the normal route — a
  voucher approved by mistake gets rejected, not un-approved.
- **It can refuse.** If someone else has already actioned the voucher in the
  meantime, or the voucher created a salary advance that's already been
  settled, undo says so rather than quietly overwriting what they did.
- Discarding a **draft** has no undo — a draft is deleted outright and was
  never a committed record, so there's nothing to put back.

## Bulk actions on the Expenses list

Tick the checkbox on any rows you want and a toolbar appears at the bottom
of the list: **Approve**, **Reject** or **Delete** them together. Reject and
delete need a reason, exactly as they do one at a time, and it applies to
the whole selection.

Only rows the action can actually apply to get a checkbox at all, and each
voucher is re-checked on the server — so if a colleague approved one of your
selection two seconds ago, you get "17 approved, 1 skipped" rather than an
error for the lot. The whole batch gets one **Undo** button, same five
seconds.

Permissions are identical to the one-at-a-time buttons: `accountant` can
bulk approve and reject, only `manager` can bulk delete.

## Duplicating a voucher

Recurring spend — the same supplier, the same lines, a different week —
doesn't need retyping. Open the original and click **Duplicate**: you land
straight in an editable draft with the lines already filled in, usually
needing only the date and an amount changed.

What carries over: who was paid, the cost centre, the payment mode, and
every transaction line with its category, description, amount and VAT flag.

What deliberately doesn't:

- **The voucher number.** The copy is a draft and gets its own number when
  you submit it. Two vouchers sharing a reference would break the books.
- **The receipt.** A receipt is the evidence for one specific payment —
  reusing last month's photo on this month's claim isn't a shortcut, it's
  a wrong record.
- **The approval history.** The copy starts as an unsubmitted draft no
  matter what the original's status was.
- **The old VAT amounts.** VAT is recalculated at the rate in effect now,
  because the copy is a new transaction today. If your rate changed since
  the original, the copy uses the new one.

The date resets to today. You can duplicate anyone's submitted voucher, but
not someone else's unfinished draft — that's their work in progress.

## Users

**Settings → Users** (manager only) to add accounts and set roles: `manager`,
`accountant`, or `user`. Give `user` access to whoever records expenses
day-to-day, `accountant` to whoever should review and approve/reject but not
delete or manage the system, and keep `manager` for whoever should have full
oversight. A manager can also reset anyone's password from this page (useful if
an account gets locked out — see Troubleshooting).

### Roles & Permissions

**Users → Roles & Permissions** (manager only) is a sub-tab of the Users page
— a grid of every permission in the app (create/approve/delete expenses,
manage users, manage settings, view the audit log, manage backups,
record/delete production entries) against the three roles. Tick or untick a box and click **Save** on that row to
change what a role can do — for example, letting `accountant` also delete
vouchers, or letting `user` view the audit log. The change applies
**immediately, with no restart**, to every account with that role.

This only changes what an *existing* role is allowed to do — it doesn't add,
rename, or remove a role. There are always exactly `manager`, `accountant`,
and `user`; that list is fixed in the database schema, and changing it is a
bigger job (see `CHANGE_IMPACT_GUIDE.md`'s "Roles / permissions" and "Schema
changes" sections if you actually need a fourth role).

**Safety net:** the app refuses to save a change that would leave **zero**
roles able to manage users & permissions — the same self-lockout protection
the Network page's trusted-hostname list already has. You can move that
permission to a different role, just not remove it from every role at once.

## Network (trusted hostnames)

**Settings → Network** (manager only) controls which hostname(s) this app
will accept requests for. It's deliberately narrow — it doesn't touch DNS,
firewalls, or port forwarding, and it's unrelated to what a router or
Cloudflare does; it only decides whether the app itself answers a given
request, based on the `Host` header.

By default the list is empty, meaning **any hostname is accepted** — the
same unrestricted behavior this app has always had, unchanged unless you
add an entry. Once you add one, only matching hostname(s) (plus
`localhost`/`127.0.0.1`, always) are accepted — everything else gets a
plain `400 Invalid host header`. The page shows exactly what hostname you're
currently using to reach it, and warns you before you restrict access, so
you don't lock yourself out.

Unlike most of this app's settings, this one takes effect **immediately**,
with no restart — it's read from the database on every request rather than
fixed at startup, specifically so it can be managed from the web UI at all.

### Exposing this beyond your LAN with a Cloudflare Tunnel

A Cloudflare Tunnel gives this app a real public URL (e.g.
`expense.yourdomain.com`) without opening any inbound port on your router —
`cloudflared` makes an outbound-only connection out to Cloudflare, which
handles TLS and proxies traffic back to the app. This repo ships an optional
`cloudflared` service in `docker-compose.yml` for exactly this.

1. In the [Cloudflare Zero Trust dashboard](https://one.dash.cloudflare.com/),
   go to **Networks → Tunnels → Create a tunnel**, choose **Cloudflared** as
   the connector, and give it a name.
2. On the install step, Cloudflare shows a command containing a long token —
   copy just the token value into `.env`:
   ```
   CLOUDFLARE_TUNNEL_TOKEN=eyJhIjoi...
   ```
3. Still in the dashboard, add a **Public Hostname** for the tunnel (e.g.
   `expense.yourdomain.com`) pointing at service `http://petty-cash:8000`
   (the app's own container name and port — cloudflared reaches it over the
   Docker network, not the internet).
4. Start the tunnel alongside the app:
   ```bash
   docker compose --profile cloudflare up -d
   ```
5. In the app, go to **Settings → Network** and add that same hostname
   (e.g. `expense.yourdomain.com`) — this is the step that actually matters
   for the app itself, and matches what this whole feature is for.
6. Since all traffic through the tunnel arrives as HTTPS, consider setting
   `SESSION_HTTPS_ONLY=true` in `.env` — but only if the tunnel is the *only*
   way you'll access the app; leave it `false` if you also still use plain
   HTTP on your LAN, since a `Secure` session cookie won't be sent back over
   a plain HTTP connection and you'd be unable to log in that way. This
   setting is fixed at startup (a restart is needed after changing it) — see
   `CHANGE_IMPACT_GUIDE.md` if you want to understand why it can't be made
   live-editable the way the Network page's hostname list is.

## Backups

The app backs itself up automatically — no cron job or external tooling needed.

**How it works:**
- Every `BACKUP_INTERVAL_HOURS` (default **24**), a backup runs in the background:
  a live, consistent snapshot of the database (via SQLite's own hot-backup API,
  so it's never a half-written copy) plus every uploaded receipt, zipped
  together and saved to `data/backups/`.
- Backups are **compressed with LZMA** — the same method 7-Zip uses when it
  writes a `.zip` with LZMA selected, and a standard-library feature (no extra
  dependency). On a real test with 500 voucher records this cut backup size by
  roughly 40% versus plain zip compression. Receipt photos/PDFs are already
  compressed formats, so don't shrink much further — the savings are mainly on
  the database itself. If a Python build somehow lacks LZMA support, it falls
  back automatically to plain zip compression rather than failing the backup.
- Only the most recent `BACKUP_RETENTION_COUNT` backups are kept (default
  **30**); older ones are pruned automatically.
- You can also click **Backups → Back up now** any time for an on-demand copy.

**Using a backup — go to the Backups page (manager only):**
- Every backup in the list shows *when* it was taken, *how many vouchers* it
  contains (broken down by status), the *total approved amount*, the *date
  range* it covers, and its file size.
- Click **View records** to open a read-only preview of every voucher inside
  that specific backup — voucher number, category, amount, status, and a
  **last-updated timestamp** for each one — before you decide whether to
  restore it.
- **Download** lets you save a copy off the server. **Do this periodically and
  store it somewhere else** (another machine, a USB drive, cloud storage) —
  backups sitting only on `data/backups/` don't protect you if the whole
  server or disk is lost.

**Restoring** (manager only, and deliberately hard to do by accident):
- Requires typing `RESTORE` and giving a reason — both are checked before
  anything happens.
- Before touching anything, the app automatically takes a fresh safety backup
  of whatever is live right now, so a restore is always itself undoable.
- If you're rebuilding on a new server and need to bring back a backup you'd
  downloaded earlier, use **Restore from an offsite / downloaded backup** on
  the same page — the uploaded file goes through the same validation as
  everything else (see Security below) before it's trusted.
- A **restore log** (`Backups → View restore log`) records every restore
  attempt — including ones that were rejected — in a plain file *outside* the
  database, so the record survives even though the restore itself replaces
  the database file that the normal in-app audit log lives in.

**Turning it off or changing the schedule** — edit `.env`:
```
BACKUP_ENABLED=true          # set false to disable entirely
BACKUP_INTERVAL_HOURS=24
BACKUP_RETENTION_COUNT=30
```

**One deployment constraint:** run this app as a single process (the default
- `docker compose up` and the plain `uvicorn` command in this README both do
that). Backup/restore operations are serialized with an in-process lock that
only works within one process; running multiple workers or replicas would let
a scheduled backup and a restore collide.


## Asset & Maintenance Program

The Asset & Maintenance Program (`/assets/`) tracks every physical thing the
company owns — trucks, machines, office equipment, furniture — and connects
each asset's cost history directly to the vouchers that recorded it.

### What it does

- **Asset Registry**: a central directory of every asset, searchable by tag,
  name, category, or serial number. Each asset page shows its full life
  history: registration (purchase cost and date), every approved cost voucher
  line linked to it, every non-cost service note, and every custody change.
- **Total Cost of Ownership (TCO)**: automatically computed from
  `purchase cost + sum of all approved voucher lines` linked to that asset.
  No second place to enter costs — the voucher is the only source.
- **Chain of Custody**: assign an asset to one or more people (day shift and
  night shift can both hold the same truck), transfer it, or close the
  assignment when it's returned. Every custody event is logged, with who
  did it and when, and the full history never changes.
- **Non-cost maintenance notes**: anyone with `log_asset_service` permission
  (and an active assignment on that asset) can record a service note — a
  nut fitted, a greasing, a breakdown observation. These have no cost field,
  because cost goes on a voucher. You can attach a photo.
- **Meter readings**: log km or machine-hours alongside service notes. A
  backwards reading (probably a typo) is warned, not blocked — refusing the
  note would mean the maintenance never gets recorded.
- **Asset divesting**: mark a sold or scrapped asset as Divested, record the
  proceeds for your accountant, and close all its open assignments in one
  step. Proceeds are never posted to the cash float — that's the accountant's
  job in Qoyod.
- **Maintenance reports**: filter by date range, asset, category, cost centre,
  holder, or kind (cost / service), and download as a pre-numbered PDF or
  Excel file. A single-asset history is available from the asset detail page.

### The seam with the Expense Program

The only deliberate connection between the two programs is one nullable column
on `expense_lines.asset_id`. When a user records a voucher line in a category
marked "requires asset?" (e.g. Vehicle maintenance), they are asked to pick the
asset the cost is for. The asset module reads that column — it never writes cost
anywhere separately.

**Fuel is not captured here.** Truck fuel is filled automatically via the chip
system and doesn't come through the voucher form. Every truck asset page shows
a clear banner noting this, so a manager reading the TCO knows the figure is
maintenance costs only, not total operating cost.

**A driver with no truck assigned** can still record a maintenance voucher. The
asset picker is only shown and required when the person actually holds at least
one asset of the right type. The unlinked line is flagged at approval time for
a manager to review.

### Roles & Permissions for Assets

These defaults are editable at **Users → Roles & Permissions** the same as
every other permission:

| Permission | manager | accountant | user |
|---|---|---|---|
| View assets | ✅ | ✅ | ✅ |
| Log service notes | ✅ | ✅ | ✅ |
| Manage assets (register/edit) | ✅ | ❌ | ❌ |
| Assign / transfer assets | ✅ | ✅ | ❌ |
| Divest assets | ✅ | ❌ | ❌ |

### Tag formats

Asset tags can be given enforced formats per category (e.g. `TR-04` for trucks,
`PRESS-01` for heavy machines) at **Assets → Settings → Tag format rules**.
The pattern is a regular expression. Leaving it blank allows any text. Bad
patterns are rejected immediately at save time so a typo can't lock the
registry.

### Which expense categories require an asset?

Configured at **Assets → Settings → Category rules**. Three states:
- Tick **required** + set a type → user must pick an asset of that type
- Set a type but **not required** → picker shown, optional
- Neither → picker hidden (fuel, advances, snacks, etc.)

No code change is needed to add a new maintenance category or change which
categories demand an asset.

## Architecture

This app is a **program hub**: after login, you land on `/programs` and pick
a program. Today there are two real programs — Expense Program and Weekly
Productions; Machinery Reports and Key Updates are still placeholders that
show "coming in the future" until they're built. The codebase is laid out so
that building one of those later is additive — new files in a new folder —
never an edit to an existing program's files, which is exactly how Weekly
Productions was added alongside Expense Program.

### Weekly Productions

Records production output as a **report** (header: date, production line,
shift, notes) holding one or more **lines**. Each line is entered as a
single free-text box — a description followed by dimensions
(`widthxheightxlengthxthickness`, e.g. `cable tray 100x50x2.44mx0.7`) — plus
a material (**GI** or **HDG**) and a quantity. This is the same header +
lines shape Expense Program's vouchers use. Every report gets its own
pre-numbered reference (`PRD-2026-00001`, ...) the moment it's recorded.

- **Dimensions box**: the last word is dimensions, everything before it is
  the description. Length always ends in a bare `m` (not `mm`) — whichever
  of the two trailing values has that suffix is length; the other is
  thickness (`mm` suffix optional). Thickness must be over 0 and no more
  than 4mm. Width may be a dash-separated series (`100-150`, `200-100-200`)
  for reducers/transitions — only the first number is used.
- **Elbows/fittings**: leave the length part empty (`200x50xx0.7mm`) and
  name the bend angle in the description (`fitting 90 degree elbow ...`).
  The inner radius defaults to 100mm (never given in the input); length and
  the calculated blank width come from the standard envelope-box formula
  (`R_out = R_in + width`; `length = R_out × sin(θ)`). Writing "reducer"
  instead uses the same formula at a fixed 90° with no angle text needed
  (an explicit angle still overrides it, e.g. `reducer 45 degree ...`).
- **Tees / crosses**: also leave the length part empty, but include the
  word "tee" or "cross" in the description instead of an angle (`cable
  tray tee 200-200-200x100xx0.7mm`) — both use the same formula. The blank
  envelope (`X = width + 2×R_in`, `Y = 2×width + 2×R_in`) feeds a
  bottom-panel-plus-side-panel area calculation internally; only the
  combined weight is shown, matching the single "Weight (kg)" figure every
  other line type shows.
- **Weight** is calculated automatically and stored per line: straight
  pieces use `((width + 2×height + 30) / 1000) × length × thickness ×
  factor`; elbows/reducers/tees/crosses use surface-area formulas instead
  of a linear one. The per-material factor (GI/HDG) is editable at
  **Settings → Weekly Productions → Weight calculation** (manager only) —
  changing it only affects reports recorded afterward, never retroactively.
- **Weekly + monthly weight targets**: set either (or both) at **Settings
  → Weekly Productions → Weight targets** (manager only, 0 hides that
  one). The dashboard shows a progress bar for each against this
  week's/month's produced weight.
- **Locking a month**: at **Settings → Weekly Productions → Lock a month**
  (manager only), a manager can lock a calendar month so no **new**
  production report can be dated in it — a period-close, matching
  Expense Program's own soft-delete philosophy: correcting an existing
  record (with a reason, as always) is still allowed even in a locked
  month, since fixing a mistake isn't the same as backdating new
  production. Each locked month links straight to its **daily
  production report**.
- **Daily production report**: the Reports page's "By day" table — total
  reports/quantity/weight for every date in the current filter range,
  including days with zero production, so a locked month reads as a
  complete log.
- **Record output**: any `user` or `manager` (governed by the
  `record_production` permission, editable at **Users → Roles &
  Permissions**) clicks **+ New Entry**.
- **Dashboard**: this week's total quantity and weight (plus the weight
  target progress bar, if one is set), and by-line/by-material breakdowns
  for the last 6 weeks.
- **Reports**: filterable by status, line, material, and date range —
  shows matching reports plus by-line/by-material summaries (including
  weight), downloadable as **Excel or PDF** (one row per material line,
  description and weight included, same "line granularity" export
  convention as Expense Program's own exports).
- **Deleting a report is soft-delete only**, same as expenses — requires a
  reason (governed by `delete_production`), and is recorded in the audit
  log. Nothing is ever hard-deleted; a deleted report's lines are kept, not
  removed.

```
app/
  main.py                 Composition root. Registers shared routers
                          unprefixed, and each program's router(s) under
                          that program's own URL prefix. This is the ONLY
                          file that needs a new line when a program is added.
  config.py, database.py,  Shared infrastructure. No program-specific code
  auth.py, sandbox.py,      belongs here - these don't know what a
  scheduler.py, backup.py,  "voucher" is any more than app/routes/guards.py
  seed.py, templates_env.py does.
  network_middleware.py    Enforces the trusted-hostname allowlist managed
                          at Settings -> Network - reads the database on
                          every request rather than being fixed at
                          startup, which is what makes it live-editable
                          from the web UI at all.
  pdf_common.py             Shared PDF building blocks: text escaping (see
                          Security below), the pre-numbered document
                          header/footer every report PDF uses, and the
                          audit log PDF - any future program generating
                          PDF reports imports from here rather than
                          reinventing it.
  flash.py                 One-shot "that worked" messages. A route that
                          ends in a redirect has nowhere to put a
                          confirmation, so it parks one in the session and
                          base.html renders it once as a toast.
  routes/                  Shared routes: login, the program hub itself,
                          user management (incl. the Roles & Permissions
                          sub-page), backups, the audit log, network
                          configuration, the notifications inbox.
  services/                Shared services: errors.py (typed exceptions
                          every service raises), auth.py (login), users.py,
                          roles.py (the role permission matrix), audit.py,
                          network.py, notifications.py (the one
                          implementation of the shared notifications
                          table - both budget alerts and voucher
                          hand-offs go through it).
  templates/                Shared page shells (base.html, login.html,
                          users.html, roles_permissions.html,
                          _users_subnav.html, audit.html, programs.html,
                          coming_soon.html, network_settings.html) and
                          static/style.css.
  programs/
    expenses/                Everything Expense-Program-specific, and
                          ONLY Expense-Program-specific code, lives here.
      routes/                 expense_routes.py, report_routes.py,
                          settings_routes.py (categories/cost centres) -
                          registered in main.py under the
                          /expense-program prefix.
      services/                expenses.py, reports.py, settings.py,
                          sharing.py (handing a voucher to someone) -
                          the actual business rules.
      pdf_voucher.py, pdf_reports.py   Program-specific PDF rendering,
                          built on top of app/pdf_common.py.
      templates/expenses/*.html         Namespaced under "expenses/" so a
                          future program's same-named template
                          (e.g. another "dashboard.html") can never
                          silently shadow this one.
```

### How program isolation actually holds up

"Won't collide with this program" is enforced two ways, not just promised:

1. **URL prefix.** Every Expense Program route is registered in `main.py`
   via `app.include_router(router, prefix="/expense-program")`. A future
   program registered under `/weekly-productions` could define its own
   `/reports` or `/settings` route with zero conflict — FastAPI sees
   `/expense-program/reports` and `/weekly-productions/reports` as
   completely different routes. Shared concerns (`/users`, `/backups`,
   `/audit`, `/programs`) stay unprefixed and are registered exactly once.
2. **Namespaced templates.** `app/templates_env.py` builds the Jinja2
   loader from a *list* of directories — the shared one plus one per
   program. Expense Program templates are referenced as
   `"expenses/dashboard.html"`, not bare `"dashboard.html"`, so even an
   identical filename in a future program's own templates folder can't
   shadow or be shadowed.

Adding a real second program means: a new `app/programs/<name>/` package
shaped like `expenses/` above, a new prefix and a few new `include_router`
lines in `main.py`, and a new entry in `app/routes/programs_routes.py`'s
`PROGRAMS` list pointing `"available": True` at its own URL. Nothing in
`app/programs/expenses/` needs to change.

### Separation of concerns, loose coupling, encapsulation

These three principles still shape every layer, same as before the
multi-program split — they just now apply per-program as well as
app-wide:

- **Separation of concerns**: `routes/*.py` is HTTP-only (parse the
  request, call a service, turn the result into a response);
  `services/*.py` holds every business rule with zero FastAPI imports;
  presentation lives only in `templates/`.
- **Loose coupling**: every service function opens what it needs through
  `app.database.get_db()` and raises a typed exception
  (`app/services/errors.py`) rather than an HTTP status code — this is
  also what lets `tests/test_services_*.py` call them directly, no server
  involved. Permission/CSRF checks live once, in `app/routes/guards.py`.
- **Encapsulation**: service modules declare `__all__`; `app/backup.py`
  and `app/sandbox.py` in particular hide a lot of internal machinery
  (zip-slip checks, file-signature validation) behind a small public
  surface.

### Security: sandboxing untrusted input

- **File uploads** (`app/sandbox.py`) are validated by actual content, not
  just the claimed extension — a script renamed to `.jpg` is rejected
  because its bytes don't match a real JPEG signature. Paths are joined
  via `sandbox.safe_join()`, which resolves symlinks before checking
  containment, and written files have their execute bit stripped.
- **PDF generation** (`app/pdf_common.py`'s `safe_text()`) escapes every
  piece of user-supplied text before it reaches reportlab's `Paragraph`,
  which otherwise interprets embedded markup — an unclosed tag typed into
  a voucher's "particulars" field used to crash PDF generation for every
  export containing that voucher; it's now rendered as harmless literal
  text.
- **Backup/restore** (`app/backup.py`) treats any restore source as
  untrusted: strict filename validation, zip-slip and zip-bomb checks, and
  a full SQLite integrity check before anything is written to the live
  database — with an automatic safety backup taken first regardless.

### Running the tests

```bash
pip install -r requirements-dev.txt
pytest                                     # everything
pytest tests/test_services_expenses.py     # just the voucher rules
pytest tests/test_backup.py                # backup/restore incl. the
                                            # zip-slip / zip-bomb / corrupt-
                                            # file security checks
pytest tests/test_sandbox.py               # file-upload/path sandboxing
pytest tests/test_routes_programs.py       # the program hub + isolation
pytest tests/test_routes_permissions.py    # every role against every route
pytest tests/test_routes_csrf.py           # CSRF enforcement
pytest tests/test_services_roles.py        # the role permission matrix,
                                            # incl. the self-lockout guard
pytest tests/test_services_sharing.py      # voucher hand-off + notifications
pytest tests/test_flash.py                 # one-shot toast messages
```

Each test file maps onto one module in the layout above, so "evaluate this
one feature" is literally `pytest tests/test_<feature>.py`. The route-level
tests (`test_routes_*.py`) use FastAPI's `TestClient` against a temporary,
fully isolated copy of the app - they never touch your real `data/` folder,
even if you run them on a machine where the app is already deployed
(`tests/conftest.py` redirects all data paths via the `PETTY_CASH_DATA_DIR`
environment variable before anything else is imported).

### Upgrading a piece later

- **Swap the database** (e.g. SQLite → Postgres): only `app/database.py`
  changes.
- **Swap the spreadsheet library**: only `app/programs/expenses/services/reports.py`.
- **Swap the PDF library or voucher layout**: only `app/programs/expenses/pdf_voucher.py`.
- **Change a business rule** (e.g. who can approve what): a manager can
  usually do this from **Users → Roles & Permissions** with no code change
  at all - see that section above. Changing a permission's *default* (what
  a fresh install starts with) is `app/config.py`'s `DEFAULT_ROLE_PERMISSIONS`
  and `app/auth.py`'s `can_*()` functions - `pytest tests/test_auth.py
  tests/test_services_roles.py tests/test_routes_permissions.py` will tell
  you immediately if a route stopped enforcing it correctly.
- **Add a real second program**: see "How program isolation actually holds
  up" above.

## Troubleshooting

- **Account locked out**: after 5 wrong password attempts, an account locks for
  15 minutes automatically (this is a security feature, not a bug). Wait it out,
  or have a manager reset the password from **Users**, which also clears the
  lock immediately.
- **Forgot the only manager's password**: stop the app, delete `data/app.db`,
  and restart — this creates a fresh database with the default manager login
  from `.env` (you will lose existing data, so only do this before you have real
  vouchers recorded, or restore from a backup afterwards).
- **Can't reach it from other computers**: make sure the homelab machine's
  firewall allows port 8000, and that you're using its LAN IP (not `localhost`)
  from other devices.
- **Uploaded receipt won't open**: only image and PDF files are supported;
  very large files may need `client_max_body_size` raised if you put this behind
  a reverse proxy like nginx.
