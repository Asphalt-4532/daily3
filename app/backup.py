"""Automatic + on-demand backups of the database and uploaded receipts.

Design notes (why it's built this way):
- Uses sqlite3's built-in online backup API (Connection.backup()) instead of a
  raw file copy, so a backup taken while the app is live can never capture a
  half-written page.
- Every backup is written to a temp file first and only moved into place with
  os.replace() once complete, so a crash mid-backup can never leave a corrupt
  or half-written file in the list a manager might restore from.
- Backups are compressed with LZMA (the same compression method 7-Zip uses
  when it writes a .zip with "LZMA" selected - notably better ratio than the
  plain DEFLATE zip default, at negligible cost for backups this size) via
  Python's standard library, with an automatic fallback to DEFLATE if a given
  Python build lacks lzma support. Because the compression method is stored
  per-entry inside the zip format itself, old DEFLATE-compressed backups and
  new LZMA-compressed ones read back identically - nothing else in this file
  needs to know or care which one was used for any given backup.
- A metadata .json sidecar is written alongside each .zip at backup time (not
  computed later) so listing backups is fast and always reflects the exact
  snapshot, even if the backup is later restored elsewhere.
- Restoring is the single most destructive action in this app, so: the backup
  filename is strictly validated (no path traversal), archive entries are
  checked for zip-slip before extraction, a fresh safety backup of the CURRENT
  state is taken automatically before anything is overwritten, and the fact
  that a restore happened is logged to a plain file (restore_log.jsonl)
  OUTSIDE the database - because the database itself is what's about to be
  replaced, so its own audit_log can't be trusted to survive the operation.
- A single process-wide lock serializes all backup/restore operations so a
  scheduled backup, a manual "Backup now" click, and a restore can never run
  concurrently and corrupt each other.
"""
import json
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
import zipfile
from datetime import datetime
from pathlib import Path

from app.config import (
    DATABASE_PATH, UPLOADS_DIR, BACKUPS_DIR, RESTORE_LOG_PATH, BACKUP_RETENTION_COUNT,
    MAX_BACKUP_ZIP_ENTRIES, MAX_BACKUP_UNCOMPRESSED_MB,
)
from app.database import get_connection, log_action
from app import sandbox

_lock = threading.RLock()  # reentrant: restore_backup() calls create_backup() while holding it

_FILENAME_RE = re.compile(
    r"^petty_cash_backup_\d{8}_\d{6}(_[0-9a-f]{6})?(_auto|_manual|_prerestore)?\.zip$"
)


def _pick_compression() -> int:
    """LZMA gives a meaningfully better ratio than DEFLATE for a SQLite file
    at negligible extra CPU cost for backups this size, and it's a standard
    library feature - no new dependency. Falls back safely if a particular
    Python build was compiled without lzma support (rare, but this should
    never be the thing that makes a scheduled backup fail)."""
    try:
        import lzma  # noqa: F401  (only probing availability)
        zipfile.ZipFile(tempfile.SpooledTemporaryFile(), "w", zipfile.ZIP_LZMA).close()
        return zipfile.ZIP_LZMA
    except Exception:
        print("[backup] lzma compression unavailable on this Python build - "
              "falling back to DEFLATE (backups will be a bit larger).")
        return zipfile.ZIP_DEFLATED


COMPRESSION = _pick_compression()


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------

def validate_backup_filename(filename: str) -> Path:
    """Raises ValueError for anything that isn't exactly one of our own backup
    filenames sitting directly inside BACKUPS_DIR. Blocks path traversal
    (../../etc/passwd), absolute paths, symlink tricks, and made-up names."""
    if not filename or not _FILENAME_RE.match(filename):
        raise ValueError("Invalid backup filename")
    candidate = (BACKUPS_DIR / filename).resolve()
    backups_resolved = BACKUPS_DIR.resolve()
    if backups_resolved not in candidate.parents:
        raise ValueError("Invalid backup path")
    if not candidate.exists() or not candidate.is_file():
        raise FileNotFoundError("Backup not found")
    return candidate


def _meta_path_for(zip_path: Path) -> Path:
    return zip_path.with_name(zip_path.stem + ".meta.json")


def _validate_sqlite_db(path: Path) -> None:
    """Raises ValueError if `path` isn't a healthy SQLite database with the
    tables this app expects. Called on every restore, right before the file
    would be swapped into place - a corrupt or unrelated file is rejected
    here, before it can ever touch the live system."""
    with open(path, "rb") as f:
        header = f.read(16)
    if header[:15] != b"SQLite format 3":
        raise ValueError("File is not a valid SQLite database (bad file header).")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise ValueError(f"Database integrity check failed: {result[0] if result else 'unknown error'}")
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        required = {"users", "expenses", "categories", "cost_centers", "audit_log", "voucher_counters"}
        missing = required - tables
        if missing:
            raise ValueError(f"This doesn't look like a petty cash backup - missing tables: "
                              f"{', '.join(sorted(missing))}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Creating a backup
# ---------------------------------------------------------------------------

def _snapshot_counts(conn) -> dict:
    # Asset module check (CHANGE_IMPACT_GUIDE.md): expense_lines gained
    # asset_id (nullable) in the Asset & Maintenance module. This function
    # reads only `amount + vat_amount` from expense_lines - it never touches
    # asset_id, so no change is needed here. Confirmed 2026-09.
    #
    # Voucher sharing check (same guide row): that feature added the
    # `expense_shares` table only - no new voucher status, no new column on
    # expenses/expense_lines, and no change to what a total means. The status
    # loop and the approved-amount sum below are therefore still complete.
    # `expense_shares` is deliberately absent from _validate_sqlite_db()'s
    # `required` set too: that set is the minimum proof a file is a petty
    # cash database, and adding every new table to it would make every backup
    # taken before that table existed un-restorable. Confirmed 2026-09.
    counts = {}
    for status in ("draft", "pending", "approved", "rejected", "deleted"):
        counts[status] = conn.execute(
            "SELECT COUNT(*) c FROM expenses WHERE status = ?", (status,)
        ).fetchone()["c"]
    total_approved = conn.execute(
        "SELECT COALESCE(SUM(el.amount + el.vat_amount),0) t FROM expense_lines el "
        "JOIN expenses e ON e.id = el.expense_id WHERE e.status='approved'"
    ).fetchone()["t"]
    date_range = conn.execute(
        "SELECT MIN(expense_date) mn, MAX(expense_date) mx FROM expenses "
        "WHERE status NOT IN ('deleted', 'draft')"
    ).fetchone()
    active_users = conn.execute("SELECT COUNT(*) c FROM users WHERE active=1").fetchone()["c"]
    return {
        "counts_by_status": counts,
        "total_vouchers": sum(counts.values()),
        "total_approved_amount": total_approved,
        "earliest_date": date_range["mn"],
        "latest_date": date_range["mx"],
        "active_users": active_users,
    }


def create_backup(trigger: str = "auto", triggered_by=None) -> dict:
    """Creates one backup (db + uploaded receipts) and returns its metadata dict.
    trigger: 'auto' | 'manual' | 'prerestore'."""
    with _lock:
        BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now()
        stamp = timestamp.strftime("%Y%m%d_%H%M%S")
        # A random disambiguator, not just the timestamp: two backups triggered
        # within the same second (a scheduled one landing right as someone
        # clicks "Back up now", or a safety copy taken during a restore) must
        # never silently overwrite each other.
        disambiguator = secrets.token_hex(3)
        suffix = {"auto": "_auto", "manual": "_manual", "prerestore": "_prerestore"}.get(trigger, "")
        base_name = f"petty_cash_backup_{stamp}_{disambiguator}{suffix}"
        zip_name = f"{base_name}.zip"

        # 1. Consistent hot-copy of the live DB via SQLite's own backup API,
        #    plus a metadata snapshot taken from the exact same connection.
        source_conn = get_connection()
        tmp_db_fd, tmp_db_path = tempfile.mkstemp(suffix=".db")
        os.close(tmp_db_fd)
        try:
            counts = _snapshot_counts(source_conn)
            dest_conn = sqlite3.connect(tmp_db_path)
            try:
                source_conn.backup(dest_conn)
            finally:
                dest_conn.close()
        finally:
            source_conn.close()

        # 2. Zip db + uploads to a temp file, then atomically move into place -
        #    so a crash mid-write can never leave a broken backup in the list.
        tmp_zip_fd, tmp_zip_path = tempfile.mkstemp(suffix=".zip.tmp", dir=BACKUPS_DIR)
        os.close(tmp_zip_fd)
        try:
            with zipfile.ZipFile(tmp_zip_path, "w", COMPRESSION) as zf:
                zf.write(tmp_db_path, arcname="app.db")
                if UPLOADS_DIR.exists():
                    for f in sorted(UPLOADS_DIR.rglob("*")):
                        if f.is_file():
                            rel = f.relative_to(UPLOADS_DIR)
                            zf.write(f, arcname=f"uploads/{rel}")
            final_zip_path = BACKUPS_DIR / zip_name
            os.replace(tmp_zip_path, final_zip_path)
        finally:
            if os.path.exists(tmp_db_path):
                os.unlink(tmp_db_path)
            if os.path.exists(tmp_zip_path):
                os.unlink(tmp_zip_path)

        size_bytes = final_zip_path.stat().st_size

        # SHA-256 of the completed zip, computed once at write time so future
        # verify_backup() calls can detect corruption or accidental modification
        # without re-reading metadata heuristics.  hashlib is stdlib; no dep.
        import hashlib
        h = hashlib.sha256()
        with open(final_zip_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        sha256 = h.hexdigest()

        meta = {
            "filename": zip_name,
            "created_at": timestamp.isoformat(timespec="seconds"),
            "trigger": trigger,
            "triggered_by": triggered_by,
            "size_bytes": size_bytes,
            "sha256": sha256,
            "compression": "LZMA" if COMPRESSION == zipfile.ZIP_LZMA else "DEFLATE",
            **counts,
        }
        _meta_path_for(final_zip_path).write_text(json.dumps(meta, indent=2))

        _apply_retention()
        return meta


def _apply_retention():
    metas = sorted(BACKUPS_DIR.glob("*.meta.json"), key=lambda p: p.name, reverse=True)
    for stale in metas[BACKUP_RETENTION_COUNT:]:
        try:
            data = json.loads(stale.read_text())
            zip_path = BACKUPS_DIR / data.get("filename", "")
            if zip_path.exists():
                zip_path.unlink()
        except (json.JSONDecodeError, OSError):
            pass
        stale.unlink(missing_ok=True)


def list_backups():
    results = []
    for meta_path in BACKUPS_DIR.glob("*.meta.json"):
        try:
            results.append(json.loads(meta_path.read_text()))
        except (json.JSONDecodeError, OSError):
            continue
    results.sort(key=lambda m: m.get("created_at", ""), reverse=True)
    return results


def delete_backup(filename: str) -> None:
    zip_path = validate_backup_filename(filename)
    with _lock:
        zip_path.unlink(missing_ok=True)
        _meta_path_for(zip_path).unlink(missing_ok=True)


def delete_orphaned_meta(filename: str) -> None:
    """Cleans up a .meta.json whose .zip is already gone (e.g. deleted outside
    the app). validate_backup_filename() requires the zip to exist, so orphans
    need this separate, narrower path - it still enforces the same filename
    allowlist, it just doesn't require the zip to be present."""
    if not filename or not _FILENAME_RE.match(filename):
        raise ValueError("Invalid backup filename")
    zip_path = (BACKUPS_DIR / filename).resolve()
    if BACKUPS_DIR.resolve() not in zip_path.parents:
        raise ValueError("Invalid backup path")
    if zip_path.exists():
        raise ValueError("Backup file still exists - use delete_backup() instead")
    with _lock:
        _meta_path_for(zip_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Restoring a backup
# ---------------------------------------------------------------------------

def _append_restore_log(entry: dict) -> None:
    """Append-only log OUTSIDE the database, so a record of every restore
    survives even though the restore itself replaces the database file."""
    with open(RESTORE_LOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


def read_restore_log(limit: int = 100):
    if not RESTORE_LOG_PATH.exists():
        return []
    lines = RESTORE_LOG_PATH.read_text().splitlines()[-limit:]
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    entries.reverse()
    return entries


def _validate_zip_entries(zf: zipfile.ZipFile) -> None:
    """Zip-slip guard: refuse to extract any archive containing an absolute
    path or a '..' path segment. Defense-in-depth - our own code is the only
    thing that creates these zips, but a restore is destructive enough that
    this check costs nothing and rules out a whole bug class."""
    for member in zf.namelist():
        p = Path(member)
        if p.is_absolute() or ".." in p.parts:
            raise ValueError(f"Refusing to restore: unsafe path in backup archive ({member!r})")


def _validate_zip_bounds(zf: zipfile.ZipFile) -> None:
    """Zip-bomb guard: caps total entries and total uncompressed size before
    anything is extracted. Only matters for externally-supplied files (our own
    backups are always small and well-formed), but an uploaded 'backup' is
    untrusted input and must be treated as such."""
    infos = zf.infolist()
    if len(infos) > MAX_BACKUP_ZIP_ENTRIES:
        raise ValueError(f"Archive has too many entries ({len(infos)} > {MAX_BACKUP_ZIP_ENTRIES}).")
    total_uncompressed = sum(i.file_size for i in infos)
    limit_bytes = MAX_BACKUP_UNCOMPRESSED_MB * 1024 * 1024
    if total_uncompressed > limit_bytes:
        raise ValueError(
            f"Archive would expand to {total_uncompressed / 1024 / 1024:.1f} MB, "
            f"over the {MAX_BACKUP_UNCOMPRESSED_MB} MB limit.")


def _extract_and_validate(zip_path: Path, tmp_dir: Path, *, untrusted: bool) -> Path:
    """Safely extracts a backup zip into tmp_dir and returns the path to its
    app.db - validated for zip-slip (always) and zip-bomb + SQLite integrity
    (when untrusted=True, i.e. a manager-uploaded file rather than one this
    app created itself). Raises ValueError on anything suspicious; nothing is
    written to the live system if this raises."""
    with zipfile.ZipFile(zip_path) as zf:
        _validate_zip_entries(zf)
        if untrusted:
            _validate_zip_bounds(zf)
        zf.extractall(tmp_dir)

    tmp_db = tmp_dir / "app.db"
    if not tmp_db.exists():
        raise ValueError("Archive is missing app.db - this doesn't look like a valid backup.")
    _validate_sqlite_db(tmp_db)
    return tmp_db


def _swap_in_from_tmp_dir(tmp_db: Path, tmp_dir: Path) -> None:
    """Atomically swaps the live database for tmp_db, then replaces the
    receipts folder with whatever uploads/ the archive contained (if any).
    Only called after _extract_and_validate() has already confirmed tmp_db is
    a healthy, correctly-shaped SQLite database."""
    os.replace(str(tmp_db), str(DATABASE_PATH))
    sandbox.set_nonexecutable(DATABASE_PATH)

    if UPLOADS_DIR.exists():
        for f in UPLOADS_DIR.rglob("*"):
            if f.is_file():
                f.unlink()
        # Remove empty subdirs left behind
        for d in sorted(UPLOADS_DIR.rglob("*"), reverse=True):
            if d.is_dir():
                try:
                    d.rmdir()
                except OSError:
                    pass
    tmp_uploads = tmp_dir / "uploads"
    if tmp_uploads.exists():
        UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        for f in tmp_uploads.rglob("*"):
            if f.is_file():
                rel = f.relative_to(tmp_uploads)
                dest = UPLOADS_DIR / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dest)
                # shutil.copy2 preserves the source file's permission bits,
                # which for a zip-extracted file reflect whatever Unix mode
                # the archive's entry claimed - never trust that, especially
                # coming from the untrusted-upload restore path.
                sandbox.set_nonexecutable(dest)


def _log_restore_to_new_db(acting_user: dict, action: str, details: dict) -> None:
    """Best-effort: note the restore in the now-restored DB's own audit log
    too, for convenience - the durable record is restore_log.jsonl."""
    try:
        conn = get_connection()
        try:
            log_action(conn, acting_user.get("id"), action, json.dumps(details))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass


def get_backup_meta(filename: str):
    zip_path = validate_backup_filename(filename)
    meta_path = _meta_path_for(zip_path)
    if not meta_path.exists():
        return None
    try:
        return json.loads(meta_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def verify_backup(filename: str) -> dict:
    """Re-compute SHA-256 of the zip on disk and compare to what was recorded
    in the .meta.json sidecar at write time.  Returns:
        {"ok": True,  "sha256": "<hex>"}                  — file intact
        {"ok": False, "reason": "<human-readable msg>"}   — mismatch or missing hash
    This is intentionally a read-only check — nothing is changed.
    """
    import hashlib
    zip_path = validate_backup_filename(filename)
    meta = get_backup_meta(filename)
    if not meta:
        return {"ok": False, "reason": "No metadata sidecar found for this backup."}
    stored = meta.get("sha256")
    if not stored:
        return {"ok": False, "reason": "Backup predates SHA-256 tracking (created before this feature was added). Re-run a manual backup to get a verifiable copy."}
    h = hashlib.sha256()
    with open(zip_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    actual = h.hexdigest()
    if actual == stored:
        return {"ok": True, "sha256": actual}
    return {"ok": False, "reason": f"SHA-256 mismatch — file may be corrupted or modified.\n  Expected: {stored}\n  Got:      {actual}"}


def inspect_backup(filename: str) -> dict:
    """Opens a backup read-only (without touching the live system) and
    returns every voucher record inside it, each with its own last-updated
    timestamp and key details - so a manager can see exactly what they'd be
    restoring before they commit to it."""
    zip_path = validate_backup_filename(filename)

    with zipfile.ZipFile(zip_path) as zf:
        _validate_zip_entries(zf)
        with tempfile.TemporaryDirectory() as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            zf.extract("app.db", tmp_dir)
            db_path = tmp_dir / "app.db"

            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT e.id, e.voucher_no, e.expense_date, e.paid_to, e.status, e.payment_mode, "
                    "(SELECT COALESCE(SUM(amount + vat_amount),0) FROM expense_lines WHERE expense_id = e.id) AS amount, "
                    "(SELECT GROUP_CONCAT(DISTINCT cat.name) FROM expense_lines el "
                    " JOIN categories cat ON cat.id = el.category_id "
                    " WHERE el.expense_id = e.id) AS category_name, "
                    "cc.name AS cost_center_name, "
                    "u1.full_name AS prepared_by_name, u2.full_name AS approved_by_name, "
                    "COALESCE(e.deleted_at, e.approved_at, e.created_at) AS last_updated, "
                    "e.created_at "
                    "FROM expenses e "
                    "LEFT JOIN cost_centers cc ON cc.id = e.cost_center_id "
                    "JOIN users u1 ON u1.id = e.prepared_by "
                    "LEFT JOIN users u2 ON u2.id = e.approved_by "
                    "ORDER BY e.expense_date DESC, e.id DESC"
                ).fetchall()
                records = [dict(r) for r in rows]
            finally:
                conn.close()

    meta = get_backup_meta(filename) or {}
    return {"meta": meta, "records": records}


def restore_backup(filename: str, acting_user: dict, reason: str) -> dict:
    """Restores the database + receipts from one of this app's own backups
    (selected from the Backups list). Always takes a fresh safety backup of
    the current state first. Returns that safety backup's metadata so the
    caller can tell the user how to undo this."""
    zip_path = validate_backup_filename(filename)

    with _lock:
        _append_restore_log({
            "event": "restore_started",
            "backup_file": filename,
            "by_username": acting_user.get("username"),
            "by_full_name": acting_user.get("full_name"),
            "reason": reason,
            "at": datetime.now().isoformat(timespec="seconds"),
        })

        safety_meta = create_backup(
            trigger="prerestore",
            triggered_by=f"system (auto safety copy before restore by {acting_user.get('username')})",
        )

        with tempfile.TemporaryDirectory() as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            # untrusted=False: this file was written by create_backup() above,
            # under our own BACKUPS_DIR, matched against the strict filename
            # allowlist - but we still run the SQLite integrity check, since a
            # damaged disk is as real a risk as a hostile file.
            tmp_db = _extract_and_validate(zip_path, tmp_dir, untrusted=False)
            _swap_in_from_tmp_dir(tmp_db, tmp_dir)

        _log_restore_to_new_db(acting_user, "database_restored", {
            "backup_file": filename, "reason": reason, "safety_backup": safety_meta["filename"],
        })
        _append_restore_log({
            "event": "restore_completed",
            "backup_file": filename,
            "safety_backup": safety_meta["filename"],
            "at": datetime.now().isoformat(timespec="seconds"),
        })

        return safety_meta


def restore_uploaded_backup(uploaded_zip_path: Path, acting_user: dict, reason: str) -> dict:
    """Disaster-recovery path: restores from a backup file a manager uploaded
    (e.g. a copy pulled back down from offsite storage onto a freshly
    rebuilt server), rather than one already sitting in BACKUPS_DIR. Treated
    as untrusted input: zip-slip guard, zip-bomb bounds, and a full SQLite
    integrity + schema check all run before anything about the live system
    is touched. Still takes a safety backup of the current state first."""
    if not zipfile.is_zipfile(uploaded_zip_path):
        raise ValueError("That file isn't a valid .zip archive.")

    with _lock:
        _append_restore_log({
            "event": "restore_from_upload_started",
            "by_username": acting_user.get("username"),
            "by_full_name": acting_user.get("full_name"),
            "reason": reason,
            "at": datetime.now().isoformat(timespec="seconds"),
        })

        safety_meta = create_backup(
            trigger="prerestore",
            triggered_by=f"system (auto safety copy before upload-restore by {acting_user.get('username')})",
        )

        with tempfile.TemporaryDirectory() as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            tmp_db = _extract_and_validate(uploaded_zip_path, tmp_dir, untrusted=True)
            _swap_in_from_tmp_dir(tmp_db, tmp_dir)

        _log_restore_to_new_db(acting_user, "database_restored_from_upload", {
            "reason": reason, "safety_backup": safety_meta["filename"],
        })
        _append_restore_log({
            "event": "restore_from_upload_completed",
            "safety_backup": safety_meta["filename"],
            "at": datetime.now().isoformat(timespec="seconds"),
        })

        return safety_meta
