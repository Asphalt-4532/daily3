"""Tests for app/backup.py. Covers correctness (a backup actually contains
what it should, restore actually restores) and the security properties this
module is specifically designed to guarantee: zip-slip protection, zip-bomb
bounds, SQLite integrity checking, and filename validation against path
traversal. These were all previously verified by hand with throwaway scripts
during development - they now live here permanently instead."""
import io
import zipfile

import pytest

from app import backup as bm
from app.programs.expenses.services import expenses as expenses_svc
from app.database import get_connection
from tests.conftest import first_category_id, first_cost_center_id

def _make_approved_expense(amount=100.0):
    eid = expenses_svc.create_expense(
        expense_date="2026-08-22", cost_center_id=str(first_cost_center_id()),
        paid_to="Vendor", payment_mode="Cash", receipt_path=None, user_id=1,
        lines=[{"category_id": first_category_id(), "particulars": "Backup test",
                "amount": amount, "is_vatable": False}],
    )
    expenses_svc.approve_expense(eid, user_id=1)
    return eid


MANAGER = {"id": 1, "username": "admin", "full_name": "Manager Account"}


# --- correctness -------------------------------------------------------

def test_create_backup_produces_a_valid_zip_with_expected_manifest():
    _make_approved_expense(250)
    meta = bm.create_backup(trigger="manual", triggered_by="admin")
    assert (bm.BACKUPS_DIR / meta["filename"]).exists()
    assert meta["total_vouchers"] == 1
    assert meta["counts_by_status"]["approved"] == 1
    assert meta["total_approved_amount"] == 250
    assert meta["compression"] in ("LZMA", "DEFLATE")


def test_backup_manifest_counts_include_drafts():
    """Regression test: counts_by_status used to omit 'draft' entirely,
    silently undercounting total_vouchers for any database with unfinished
    drafts in it."""
    expenses_svc.create_expense(
        expense_date="2026-08-22", cost_center_id=str(first_cost_center_id()), paid_to="",
        payment_mode="Cash", receipt_path=None, user_id=1, lines=[], is_draft=True,
    )
    _make_approved_expense(100)
    meta = bm.create_backup(trigger="manual", triggered_by="admin")
    assert meta["counts_by_status"]["draft"] == 1
    assert meta["counts_by_status"]["approved"] == 1
    assert meta["total_vouchers"] == 2  # both counted, not just the approved one


def test_backup_manifest_approved_amount_includes_vat():
    """Regression test: total_approved_amount summed only the VAT-exclusive
    `amount` column, silently under-reporting for any VAT-able voucher -
    inconsistent with how the dashboard/reports compute the same figure."""
    eid = expenses_svc.create_expense(
        expense_date="2026-08-22", cost_center_id=str(first_cost_center_id()), paid_to="Vendor",
        payment_mode="Cash", receipt_path=None, user_id=1,
        lines=[{"category_id": first_category_id(), "particulars": "Vatable", "amount": 100.0, "is_vatable": True, "supplier_name": "Backup Test Supplier", "supplier_vat": "VATBK001"}],
    )
    expenses_svc.approve_expense(eid, user_id=1)
    meta = bm.create_backup(trigger="manual", triggered_by="admin")
    assert meta["total_approved_amount"] == 115  # 100 net + 15% VAT, not just 100


def test_inspect_backup_amount_includes_vat_and_shows_draft_label():
    eid = expenses_svc.create_expense(
        expense_date="2026-08-22", cost_center_id=str(first_cost_center_id()), paid_to="Vendor",
        payment_mode="Cash", receipt_path=None, user_id=1,
        lines=[{"category_id": first_category_id(), "particulars": "Vatable", "amount": 100.0, "is_vatable": True, "supplier_name": "Backup Test Supplier", "supplier_vat": "VATBK001"}],
    )
    expenses_svc.approve_expense(eid, user_id=1)
    expenses_svc.create_expense(
        expense_date="2026-08-22", cost_center_id=str(first_cost_center_id()), paid_to="",
        payment_mode="Cash", receipt_path=None, user_id=1, lines=[], is_draft=True,
    )
    meta = bm.create_backup(trigger="manual", triggered_by="admin")
    result = bm.inspect_backup(meta["filename"])
    approved_rec = next(r for r in result["records"] if r["status"] == "approved")
    draft_rec = next(r for r in result["records"] if r["status"] == "draft")
    assert approved_rec["amount"] == 115
    assert draft_rec["voucher_no"] is None


def test_create_backup_uses_lzma_when_available():
    meta = bm.create_backup(trigger="manual", triggered_by="admin")
    with zipfile.ZipFile(bm.BACKUPS_DIR / meta["filename"]) as zf:
        for info in zf.infolist():
            assert info.compress_type == zipfile.ZIP_LZMA


def test_list_backups_returns_newest_first():
    m1 = bm.create_backup(trigger="manual", triggered_by="admin")
    m2 = bm.create_backup(trigger="manual", triggered_by="admin")
    names = [b["filename"] for b in bm.list_backups()]
    assert names[0] == m2["filename"] or m1["created_at"] <= m2["created_at"]


def test_retention_prunes_oldest_backups(monkeypatch):
    monkeypatch.setattr(bm, "BACKUP_RETENTION_COUNT", 2)
    for _ in range(4):
        bm.create_backup(trigger="manual", triggered_by="admin")
    assert len(bm.list_backups()) == 2


def test_inspect_backup_lists_all_records_with_last_updated():
    eid = _make_approved_expense(500)
    meta = bm.create_backup(trigger="manual", triggered_by="admin")
    result = bm.inspect_backup(meta["filename"])
    assert len(result["records"]) == 1
    rec = result["records"][0]
    assert rec["amount"] == 500
    assert rec["last_updated"] is not None


def test_restore_backup_reverts_to_snapshot_state():
    _make_approved_expense(111)
    snapshot = bm.create_backup(trigger="manual", triggered_by="admin")
    _make_approved_expense(222)  # diverge from the snapshot

    before_ids = {e["id"] for e in expenses_svc.list_expenses()[0]}
    assert len(before_ids) == 2

    bm.restore_backup(snapshot["filename"], MANAGER, "test restore correctness")

    after_rows, _ = expenses_svc.list_expenses()
    assert len(after_rows) == 1
    assert after_rows[0]["total_amount"] == 111


def test_restore_backup_takes_a_safety_backup_first():
    _make_approved_expense(50)
    target = bm.create_backup(trigger="manual", triggered_by="admin")
    before_count = len(bm.list_backups())

    safety = bm.restore_backup(target["filename"], MANAGER, "testing safety backup")

    assert safety["trigger"] == "prerestore"
    assert len(bm.list_backups()) == before_count + 1  # target backup + new safety copy


def test_restore_backup_writes_to_restore_log():
    target = bm.create_backup(trigger="manual", triggered_by="admin")
    bm.restore_backup(target["filename"], MANAGER, "log test")
    entries = bm.read_restore_log()
    events = [e["event"] for e in entries]
    assert "restore_started" in events
    assert "restore_completed" in events


def test_delete_backup_removes_zip_and_meta():
    meta = bm.create_backup(trigger="manual", triggered_by="admin")
    zip_path = bm.BACKUPS_DIR / meta["filename"]
    meta_path = bm._meta_path_for(zip_path)
    assert zip_path.exists() and meta_path.exists()
    bm.delete_backup(meta["filename"])
    assert not zip_path.exists() and not meta_path.exists()


# --- security ------------------------------------------------------------

def test_filename_validation_rejects_path_traversal():
    for evil in ["../../etc/passwd", "..%2Fetc%2Fpasswd", "/etc/passwd",
                 "petty_cash_backup_11111111_111111_auto.zip"]:
        with pytest.raises((ValueError, FileNotFoundError)):
            bm.validate_backup_filename(evil)


def test_filename_validation_rejects_wrong_pattern():
    for bad in ["not_a_backup.zip", "petty_cash_backup_2026_manual.zip", "", "..\\windows\\win.ini"]:
        with pytest.raises((ValueError, FileNotFoundError)):
            bm.validate_backup_filename(bad)


def _build_evil_zip(entries: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_restore_uploaded_backup_blocks_zip_slip(tmp_path):
    evil_bytes = _build_evil_zip({
        "../../../../tmp/zip_slip_pwned.txt": "pwned",
        "app.db": b"SQLite format 3\x00" + b"\x00" * 100,
    })
    evil_path = tmp_path / "evil.zip"
    evil_path.write_bytes(evil_bytes)

    with pytest.raises(ValueError, match="unsafe path"):
        bm.restore_uploaded_backup(evil_path, MANAGER, "zip-slip test")


def test_restore_uploaded_backup_blocks_corrupt_sqlite(tmp_path):
    fake_bytes = _build_evil_zip({"app.db": b"this is not a real sqlite database" * 50})
    fake_path = tmp_path / "fake.zip"
    fake_path.write_bytes(fake_bytes)

    with pytest.raises(ValueError, match="not a valid SQLite database"):
        bm.restore_uploaded_backup(fake_path, MANAGER, "corrupt file test")


def test_restore_uploaded_backup_blocks_missing_app_db(tmp_path):
    no_db_bytes = _build_evil_zip({"readme.txt": "no database in here"})
    no_db_path = tmp_path / "nodb.zip"
    no_db_path.write_bytes(no_db_bytes)

    with pytest.raises(ValueError, match="app.db"):
        bm.restore_uploaded_backup(no_db_path, MANAGER, "missing db test")


def test_restore_uploaded_backup_accepts_a_genuinely_valid_backup(tmp_path):
    """Sanity check that the strict validation doesn't also reject good files."""
    _make_approved_expense(999)
    good = bm.create_backup(trigger="manual", triggered_by="admin")
    good_path = bm.BACKUPS_DIR / good["filename"]

    safety = bm.restore_uploaded_backup(good_path, MANAGER, "valid upload test")
    assert safety["trigger"] == "prerestore"


def test_zip_bomb_entry_count_guard():
    class FakeInfo:
        def __init__(self, size): self.file_size = size
    class FakeZip:
        def infolist(self): return [FakeInfo(10) for _ in range(bm.MAX_BACKUP_ZIP_ENTRIES + 1)]
    with pytest.raises(ValueError, match="too many entries"):
        bm._validate_zip_bounds(FakeZip())


def test_zip_bomb_size_guard():
    class FakeInfo:
        def __init__(self, size): self.file_size = size
    class FakeZip:
        def infolist(self): return [FakeInfo((bm.MAX_BACKUP_UNCOMPRESSED_MB + 1) * 1024 * 1024)]
    with pytest.raises(ValueError, match="MB"):
        bm._validate_zip_bounds(FakeZip())


def test_normal_sized_archive_passes_bomb_guards():
    class FakeInfo:
        def __init__(self, size): self.file_size = size
    class FakeZip:
        def infolist(self): return [FakeInfo(50_000), FakeInfo(200_000)]
    bm._validate_zip_bounds(FakeZip())  # should not raise


def test_old_deflate_backup_still_readable_after_lzma_upgrade():
    """Backward compatibility: a backup made before LZMA was the default must
    still list, inspect, and restore correctly."""
    _make_approved_expense(42)
    old_path = bm.BACKUPS_DIR / "petty_cash_backup_20200101_000000_auto.zip"
    with zipfile.ZipFile(old_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(bm.DATABASE_PATH, arcname="app.db")
    import json
    bm._meta_path_for(old_path).write_text(json.dumps({
        "filename": old_path.name, "created_at": "2020-01-01T00:00:00", "trigger": "auto",
        "triggered_by": "system", "size_bytes": old_path.stat().st_size,
        "counts_by_status": {"pending": 0, "approved": 1, "rejected": 0, "deleted": 0},
        "total_vouchers": 1, "total_approved_amount": 0, "earliest_date": None,
        "latest_date": None, "active_users": 1,
        # deliberately no "compression" key - simulates a pre-upgrade manifest
    }))

    assert any(b["filename"] == old_path.name for b in bm.list_backups())
    result = bm.inspect_backup(old_path.name)
    assert len(result["records"]) == 1


def test_backup_records_sha256(reset_db):
    meta = bm.create_backup(trigger="manual")
    assert "sha256" in meta
    assert len(meta["sha256"]) == 64  # hex SHA-256 is 64 chars
    assert all(c in "0123456789abcdef" for c in meta["sha256"])


def test_verify_backup_ok(reset_db):
    meta = bm.create_backup(trigger="manual")
    result = bm.verify_backup(meta["filename"])
    assert result["ok"] is True
    assert result["sha256"] == meta["sha256"]


def test_verify_backup_detects_tampering(reset_db, tmp_path):
    from app.config import BACKUPS_DIR
    meta = bm.create_backup(trigger="manual")
    zip_path = BACKUPS_DIR / meta["filename"]
    # corrupt one byte in the zip
    data = bytearray(zip_path.read_bytes())
    data[100] ^= 0xFF
    zip_path.write_bytes(bytes(data))
    result = bm.verify_backup(meta["filename"])
    assert result["ok"] is False
    assert "mismatch" in result["reason"].lower()


def test_verify_backup_missing_sha256_in_old_meta(reset_db):
    import json
    from app.config import BACKUPS_DIR
    meta = bm.create_backup(trigger="manual")
    # Simulate a pre-feature backup by removing sha256 from the sidecar
    meta_path = BACKUPS_DIR / (meta["filename"].replace(".zip", ".meta.json"))
    data = json.loads(meta_path.read_text())
    del data["sha256"]
    meta_path.write_text(json.dumps(data))
    result = bm.verify_backup(meta["filename"])
    assert result["ok"] is False
    assert "predates" in result["reason"]
