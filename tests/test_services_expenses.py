"""Tests for app/programs/expenses/services/expenses.py, called directly -
no HTTP involved. This is the point of pulling business logic out of the
routes: these tests run in milliseconds and don't care what web framework
(or none) sits in front of them."""
import pytest
import sys
from datetime import date

from app.programs.expenses.services import expenses as svc
from app.services.errors import ValidationError, NotFoundError, ConflictError, ForbiddenError
from app.database import get_connection
from app.config import VAT_RATE
from tests.conftest import first_category_id, second_category_id, first_cost_center_id


def _make_expense(**overrides):
    """Convenience wrapper: pass `lines=[...]` for a real multi-line test, or
    just amount=/category_id=/particulars=/is_vatable= for a quick
    single-line voucher (most tests only need one line). A VAT-able single
    line gets a default test supplier automatically unless the caller
    already passed supplier_name/supplier_vat."""
    lines = overrides.pop("lines", None)
    if lines is None:
        is_vatable = overrides.pop("is_vatable", False)
        line = {
            "category_id": overrides.pop("category_id", first_category_id()),
            "particulars": overrides.pop("particulars", "Test particulars"),
            "amount": overrides.pop("amount", 100.0),
            "is_vatable": is_vatable,
            "supplier_name": overrides.pop("supplier_name", "Test Supplier" if is_vatable else ""),
            "supplier_vat": overrides.pop("supplier_vat", "TESTVAT0001" if is_vatable else ""),
        }
        lines = [line]
    kwargs = dict(
        expense_date=date.today().isoformat(), cost_center_id=str(first_cost_center_id()),
        paid_to="Test Vendor", payment_mode="Cash", receipt_path=None, user_id=1, lines=lines,
    )
    kwargs.update(overrides)
    return svc.create_expense(**kwargs)


def test_create_expense_assigns_sequential_voucher_numbers():
    id1 = _make_expense()
    id2 = _make_expense()
    e1, e2 = svc.get_expense(id1), svc.get_expense(id2)
    assert e1["voucher_no"] == "PCV-2026-00001"
    assert e2["voucher_no"] == "PCV-2026-00002"


def test_create_expense_starts_pending():
    eid = _make_expense()
    assert svc.get_expense(eid)["status"] == "pending"


@pytest.mark.parametrize("amount", [0, -5, -0.01])
def test_create_expense_rejects_non_positive_amount(amount):
    with pytest.raises(ValidationError):
        _make_expense(amount=amount)


def test_create_expense_rejects_unknown_category():
    with pytest.raises(ValidationError):
        _make_expense(category_id=999999)


def test_create_expense_rejects_unknown_cost_center():
    with pytest.raises(ValidationError):
        _make_expense(cost_center_id="999999")


def test_create_expense_allows_no_cost_center():
    eid = _make_expense(cost_center_id="")
    assert svc.get_expense(eid)["cost_center_id"] is None


def test_create_expense_rejects_empty_paid_to():
    with pytest.raises(ValidationError):
        _make_expense(paid_to="   ")


def test_create_expense_rejects_no_lines_at_all():
    with pytest.raises(ValidationError, match="at least one"):
        _make_expense(lines=[])


def test_create_expense_rejects_a_line_with_no_particulars():
    with pytest.raises(ValidationError):
        _make_expense(lines=[{"category_id": first_category_id(), "particulars": "  ",
                               "amount": 50, "is_vatable": False}])


# --- multiple transactions per voucher, the core of this feature --------

def test_voucher_can_hold_multiple_transaction_lines():
    eid = _make_expense(lines=[
        {"category_id": first_category_id(), "particulars": "Snacks",
         "amount": 50.0, "is_vatable": False},
        {"category_id": second_category_id(), "particulars": "Fuel",
         "amount": 200.0, "is_vatable": True,
         "supplier_name": "Fuel Station Co", "supplier_vat": "VATFUEL001"},
    ])
    e = svc.get_expense(eid)
    assert len(e["lines"]) == 2
    assert e["lines"][0]["particulars"] == "Snacks"
    assert e["lines"][1]["particulars"] == "Fuel"
    # 50 (no VAT) + 200 * 1.15 (VAT added on top) = 50 + 230 = 280
    assert e["total_amount"] == pytest.approx(280.0)


def test_voucher_total_is_sum_of_all_lines():
    eid = _make_expense(lines=[
        {"category_id": first_category_id(), "particulars": "A", "amount": 30, "is_vatable": False},
        {"category_id": first_category_id(), "particulars": "B", "amount": 45.50, "is_vatable": False},
        {"category_id": first_category_id(), "particulars": "C", "amount": 10.25, "is_vatable": False},
    ])
    e = svc.get_expense(eid)
    assert e["total_amount"] == pytest.approx(85.75)


def test_voucher_can_mix_vatable_and_non_vatable_lines():
    """The exact scenario this feature exists for: one voucher, some lines
    VAT-able and some not."""
    eid = _make_expense(lines=[
        {"category_id": first_category_id(), "particulars": "Vatable item",
         "amount": 115.0, "is_vatable": True,
         "supplier_name": "VAT Item Supplier", "supplier_vat": "VATMIX001"},
        {"category_id": first_category_id(), "particulars": "Exempt item",
         "amount": 40.0, "is_vatable": False},
    ])
    e = svc.get_expense(eid)
    vatable_line = next(l for l in e["lines"] if l["particulars"] == "Vatable item")
    exempt_line = next(l for l in e["lines"] if l["particulars"] == "Exempt item")
    assert vatable_line["is_vatable"] == 1
    assert vatable_line["vat_amount"] > 0
    assert exempt_line["is_vatable"] == 0
    assert exempt_line["vat_amount"] == 0
    # 115 * 1.15 (VAT added on top) + 40 (no VAT) = 132.25 + 40 = 172.25
    assert e["total_amount"] == pytest.approx(172.25)


# --- VAT computation -----------------------------------------------------

def test_compute_vat_for_non_vatable_line_is_zero():
    rate, vat = svc.compute_vat(100.0, False)
    assert rate == 0.0
    assert vat == 0.0


def test_compute_vat_adds_vat_on_top_of_exclusive_amount():
    """amount is always VAT-exclusive. At 15% VAT, a 100 exclusive amount
    gets 15 VAT added on top - not extracted from an inclusive figure."""
    rate, vat = svc.compute_vat(100.0, True)
    assert rate == VAT_RATE
    assert vat == pytest.approx(100.0 * VAT_RATE / 100, abs=0.01)
    assert vat == 15.0  # at the default 15% rate


def test_compute_vat_of_zero_amount_is_zero():
    rate, vat = svc.compute_vat(0, True)
    assert vat == 0.0


def test_created_line_stores_the_vat_rate_in_effect_at_entry_time():
    eid = _make_expense(amount=115.0, is_vatable=True)
    e = svc.get_expense(eid)
    assert e["lines"][0]["vat_rate"] == VAT_RATE
    assert e["total_vat"] > 0


# --- approve / reject / delete (voucher-level, unaffected by line count) -

def test_approve_expense_happy_path():
    eid = _make_expense()
    svc.approve_expense(eid, user_id=1)
    e = svc.get_expense(eid)
    assert e["status"] == "approved"
    assert e["approved_by"] == 1
    assert e["approved_at"] is not None


def test_approve_expense_twice_raises_conflict():
    eid = _make_expense()
    svc.approve_expense(eid, user_id=1)
    with pytest.raises(ConflictError):
        svc.approve_expense(eid, user_id=1)


def test_approve_nonexistent_expense_raises_not_found():
    with pytest.raises(NotFoundError):
        svc.approve_expense(999999, user_id=1)


def test_reject_expense_requires_pending_status():
    eid = _make_expense()
    svc.approve_expense(eid, user_id=1)
    with pytest.raises(ConflictError):
        svc.reject_expense(eid, user_id=1, reason="too late")


def test_reject_expense_records_reason():
    eid = _make_expense()
    svc.reject_expense(eid, user_id=1, reason="duplicate submission")
    e = svc.get_expense(eid)
    assert e["status"] == "rejected"
    assert e["rejection_reason"] == "duplicate submission"


def test_delete_expense_requires_reason():
    eid = _make_expense()
    with pytest.raises(ValidationError):
        svc.delete_expense(eid, user_id=1, reason="")


def test_delete_expense_is_soft_delete():
    eid = _make_expense()
    svc.approve_expense(eid, user_id=1)
    svc.delete_expense(eid, user_id=1, reason="entered twice by mistake")
    e = svc.get_expense(eid)
    # the record still exists and is inspectable - it's just marked deleted
    assert e is not None
    assert e["status"] == "deleted"
    assert e["delete_reason"] == "entered twice by mistake"
    assert e["deleted_by"] == 1


def test_delete_expense_twice_raises_not_found():
    eid = _make_expense()
    svc.delete_expense(eid, user_id=1, reason="first delete")
    with pytest.raises(NotFoundError):
        svc.delete_expense(eid, user_id=1, reason="second delete")


def test_delete_expense_audit_snapshot_includes_every_line():
    import json
    from app.database import get_db
    eid = _make_expense(lines=[
        {"category_id": first_category_id(), "particulars": "Line one", "amount": 20, "is_vatable": False},
        {"category_id": first_category_id(), "particulars": "Line two", "amount": 30, "is_vatable": True,
         "supplier_name": "Snapshot Supplier", "supplier_vat": "VATSNAP001"},
    ])
    svc.delete_expense(eid, user_id=1, reason="test snapshot")
    with get_db() as conn:
        row = conn.execute(
            "SELECT details FROM audit_log WHERE action='delete_expense' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    snapshot = json.loads(row["details"])
    assert len(snapshot["lines"]) == 2
    particulars = {l["particulars"] for l in snapshot["lines"]}
    assert particulars == {"Line one", "Line two"}


# --- listing / dashboard ---------------------------------------------------

def test_list_expenses_excludes_deleted_by_default():
    eid1 = _make_expense()
    eid2 = _make_expense()
    svc.delete_expense(eid2, user_id=1, reason="oops")
    rows, total = svc.list_expenses()
    ids = [r["id"] for r in rows]
    assert eid1 in ids
    assert eid2 not in ids


def test_list_expenses_can_show_deleted_explicitly():
    eid = _make_expense()
    svc.delete_expense(eid, user_id=1, reason="oops")
    rows, total = svc.list_expenses(status="deleted")
    assert any(r["id"] == eid for r in rows)


def test_list_expenses_total_excludes_deleted_even_if_status_filter_unset():
    _make_expense(amount=100)
    eid2 = _make_expense(amount=200)
    svc.delete_expense(eid2, user_id=1, reason="oops")
    rows, total = svc.list_expenses()
    assert total == 100


def test_list_expenses_category_filter_matches_voucher_with_any_matching_line():
    cat_a = first_category_id()
    cat_b = second_category_id()
    eid = _make_expense(lines=[
        {"category_id": cat_a, "particulars": "A", "amount": 10, "is_vatable": False},
        {"category_id": cat_b, "particulars": "B", "amount": 20, "is_vatable": False},
    ])
    rows, _total = svc.list_expenses(category_id=str(cat_b))
    assert any(r["id"] == eid for r in rows)


def test_dashboard_stats_only_counts_approved_in_month_total():
    eid1 = _make_expense(amount=500)
    _make_expense(amount=300)  # left pending
    svc.approve_expense(eid1, user_id=1)
    stats = svc.dashboard_stats()
    assert stats["month_total"] == 500
    assert stats["pending_count"] == 1
    assert stats["pending_total"] == 300


# --- save_receipt: streaming, size cap, and signature validation --------

def test_save_receipt_accepts_a_real_jpeg():
    import io
    jpeg_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 200
    stored_name = svc.save_receipt("photo.jpg", io.BytesIO(jpeg_bytes))
    assert stored_name.endswith(".jpg")
    from app.config import UPLOADS_DIR
    assert (UPLOADS_DIR / stored_name).exists()
    assert (UPLOADS_DIR / stored_name).read_bytes() == jpeg_bytes


def test_save_receipt_rejects_disallowed_extension():
    import io
    with pytest.raises(ValidationError):
        svc.save_receipt("malware.exe", io.BytesIO(b"MZ\x90\x00"))


def test_save_receipt_rejects_content_that_does_not_match_extension():
    """A script renamed to .jpg must be rejected even though the extension
    passes the allowlist - the actual bytes are checked too."""
    import io
    fake = io.BytesIO(b"#!/bin/sh\necho pwned\n")
    with pytest.raises(ValidationError, match="doesn't look like a real"):
        svc.save_receipt("innocent.jpg", fake)


def test_save_receipt_enforces_size_cap_during_streaming_not_after():
    """The size check must trigger while reading, not only after the whole
    file is buffered - this is what prevents an oversized upload from being
    fully read into memory first."""
    import io
    from app.config import MAX_UPLOAD_MB

    class GrowingStream:
        """Simulates a file-like object that would keep yielding data
        forever if not stopped early by the size cap mid-read."""
        def __init__(self):
            self.calls = 0
        def read(self, n):
            self.calls += 1
            if self.calls > (MAX_UPLOAD_MB + 5):  # would be way over the cap
                return b""
            return b"\xff\xd8\xff" + (b"a" * (n - 3))  # valid jpeg-looking chunks

    stream = GrowingStream()
    with pytest.raises(ValidationError, match="too large"):
        svc.save_receipt("huge.jpg", stream)
    # confirms the read loop stopped at/near the cap rather than draining
    # the entire (effectively unbounded) stream first
    assert stream.calls <= MAX_UPLOAD_MB + 2


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows/NTFS has no POSIX execute-bit concept for chmod to meaningfully "
           "strip - see the sibling test in test_sandbox.py for the full reasoning.",
)
def test_save_receipt_writes_a_nonexecutable_file():
    import io, os, stat
    jpeg_bytes = b"\xff\xd8\xff" + b"\x00" * 100
    stored_name = svc.save_receipt("photo.jpg", io.BytesIO(jpeg_bytes))
    from app.config import UPLOADS_DIR
    mode = os.stat(UPLOADS_DIR / stored_name).st_mode
    assert not (mode & stat.S_IXUSR)
    assert not (mode & stat.S_IXGRP)
    assert not (mode & stat.S_IXOTH)


# --- drafts ----------------------------------------------------------------

MANAGER = {"id": 1, "role": "manager"}


def _other_user_id():
    """A second real user, distinct from id=1 (the manager fixture seeds),
    for ownership tests. Created as a plain 'user' so it can own drafts."""
    from app.services import users as users_svc
    return users_svc.create_user(
        full_name="Other User", username="other_user_draft_test",
        password="pass1234", role="user", acting_user_id=1,
    )


def test_create_expense_as_draft_has_no_voucher_number():
    eid = _make_expense(is_draft=True)
    e = svc.get_expense(eid)
    assert e["status"] == "draft"
    assert e["voucher_no"] is None


def test_create_expense_as_draft_allows_blank_paid_to():
    eid = svc.create_expense(
        expense_date="2026-08-22", cost_center_id="", paid_to="", payment_mode="Cash",
        receipt_path=None, user_id=1, lines=[], is_draft=True,
    )
    e = svc.get_expense(eid)
    assert e["status"] == "draft"
    assert e["paid_to"] == ""


def test_create_expense_as_draft_allows_zero_lines():
    eid = svc.create_expense(
        expense_date="2026-08-22", cost_center_id="", paid_to="Vendor", payment_mode="Cash",
        receipt_path=None, user_id=1, lines=[], is_draft=True,
    )
    e = svc.get_expense(eid)
    assert e["lines"] == []
    assert e["total_amount"] == 0


def test_create_expense_as_draft_allows_zero_amount_placeholder_line():
    eid = svc.create_expense(
        expense_date="2026-08-22", cost_center_id="", paid_to="Vendor", payment_mode="Cash",
        receipt_path=None, user_id=1,
        lines=[{"category_id": first_category_id(), "particulars": "not sure yet",
                "amount": "", "is_vatable": False}],
        is_draft=True,
    )
    e = svc.get_expense(eid)
    assert len(e["lines"]) == 1
    assert e["lines"][0]["amount"] == 0


def test_create_expense_as_draft_still_requires_valid_category_if_line_present():
    with pytest.raises(ValidationError):
        svc.create_expense(
            expense_date="2026-08-22", cost_center_id="", paid_to="Vendor", payment_mode="Cash",
            receipt_path=None, user_id=1,
            lines=[{"category_id": 999999, "particulars": "x", "amount": "10", "is_vatable": False}],
            is_draft=True,
        )


def test_submitting_normally_still_requires_full_validation():
    """is_draft defaults to False - the original strict behavior is
    unchanged for a direct (non-draft) submission."""
    with pytest.raises(ValidationError, match="at least one"):
        svc.create_expense(
            expense_date="2026-08-22", cost_center_id="", paid_to="Vendor",
            payment_mode="Cash", receipt_path=None, user_id=1, lines=[],
        )


def test_update_draft_replaces_header_and_lines():
    eid = _make_expense(is_draft=True, paid_to="First Vendor")
    svc.update_draft(
        eid, MANAGER, expense_date="2026-08-23", cost_center_id="", paid_to="Updated Vendor",
        payment_mode="Bank Transfer",  receipt_path=None,
        lines=[{"category_id": first_category_id(), "particulars": "Updated line",
                "amount": "77", "is_vatable": False}],
    )
    e = svc.get_expense(eid)
    assert e["status"] == "draft"
    assert e["paid_to"] == "Updated Vendor"
    assert e["payment_mode"] == "Bank Transfer"
    assert len(e["lines"]) == 1
    assert e["lines"][0]["particulars"] == "Updated line"


def test_update_draft_rejects_non_owner_non_manager():
    other_id = _other_user_id()
    eid = _make_expense(is_draft=True, user_id=other_id)
    with pytest.raises(ForbiddenError):
        svc.update_draft(
            eid, {"id": 999, "role": "user"}, expense_date="2026-08-22", cost_center_id="",
            paid_to="Hijacked", payment_mode="Cash", receipt_path=None, lines=[],
        )


def test_update_draft_allows_manager_to_edit_someone_elses_draft():
    other_id = _other_user_id()
    eid = _make_expense(is_draft=True, user_id=other_id, paid_to="Owned by other user")
    svc.update_draft(
        eid, MANAGER, expense_date="2026-08-22", cost_center_id="", paid_to="Manager edited this",
        payment_mode="Cash", receipt_path=None, lines=[],
    )
    assert svc.get_expense(eid)["paid_to"] == "Manager edited this"


def test_update_draft_rejects_once_no_longer_a_draft():
    eid = _make_expense()  # submitted immediately, not a draft
    with pytest.raises(ConflictError):
        svc.update_draft(
            eid, MANAGER, expense_date="2026-08-22", cost_center_id="", paid_to="x",
            payment_mode="Cash", receipt_path=None, lines=[],
        )


def test_submit_draft_allocates_voucher_number_and_moves_to_pending():
    eid = _make_expense(is_draft=True, paid_to="Ready Vendor")
    voucher_no = svc.submit_draft(eid, MANAGER)
    e = svc.get_expense(eid)
    assert e["status"] == "pending"
    assert e["voucher_no"] == voucher_no
    assert voucher_no.startswith("PCV-")


def test_submit_draft_fails_if_still_incomplete():
    eid = svc.create_expense(
        expense_date="2026-08-22", cost_center_id="", paid_to="", payment_mode="Cash",
        receipt_path=None, user_id=1, lines=[], is_draft=True,
    )
    with pytest.raises(ValidationError):
        svc.submit_draft(eid, MANAGER)
    # nothing changed - still a draft, still no voucher number
    e = svc.get_expense(eid)
    assert e["status"] == "draft"
    assert e["voucher_no"] is None


def test_submit_draft_rejects_non_owner_non_manager():
    other_id = _other_user_id()
    eid = _make_expense(is_draft=True, user_id=other_id, paid_to="Vendor")
    with pytest.raises(ForbiddenError):
        svc.submit_draft(eid, {"id": 999, "role": "user"})


def test_discard_draft_hard_deletes_it_completely():
    from app.database import get_connection
    eid = _make_expense(is_draft=True)
    svc.discard_draft(eid, MANAGER)
    assert svc.get_expense(eid) is None
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM expenses WHERE id=?", (eid,)).fetchone()
        assert row is None  # really gone, not soft-deleted
        lines = conn.execute("SELECT * FROM expense_lines WHERE expense_id=?", (eid,)).fetchall()
        assert lines == []
    finally:
        conn.close()


def test_discard_draft_rejects_if_not_a_draft():
    eid = _make_expense()  # submitted, not a draft
    with pytest.raises(ConflictError):
        svc.discard_draft(eid, MANAGER)


def test_discard_draft_allowed_for_manager_even_if_not_owner():
    other_id = _other_user_id()
    eid = _make_expense(is_draft=True, user_id=other_id)
    svc.discard_draft(eid, MANAGER)  # should not raise
    assert svc.get_expense(eid) is None


def test_discard_draft_rejects_non_owner_non_manager():
    other_id = _other_user_id()
    eid = _make_expense(is_draft=True, user_id=other_id)
    with pytest.raises(ForbiddenError):
        svc.discard_draft(eid, {"id": 999, "role": "user"})
    assert svc.get_expense(eid) is not None  # untouched


def test_list_expenses_excludes_drafts_by_default():
    draft_id = _make_expense(is_draft=True)
    real_id = _make_expense()
    rows, _total = svc.list_expenses()
    ids = [r["id"] for r in rows]
    assert real_id in ids
    assert draft_id not in ids


def test_list_expenses_shows_drafts_when_explicitly_filtered():
    draft_id = _make_expense(is_draft=True)
    rows, _total = svc.list_expenses(status="draft")
    assert any(r["id"] == draft_id for r in rows)


def test_dashboard_stats_reports_draft_count():
    _make_expense(is_draft=True)
    _make_expense(is_draft=True)
    stats = svc.dashboard_stats()
    assert stats["draft_count"] == 2


def test_dashboard_recent_excludes_drafts():
    draft_id = _make_expense(is_draft=True)
    real_id = _make_expense()
    stats = svc.dashboard_stats()
    recent_ids = [r["id"] for r in stats["recent"]]
    assert real_id in recent_ids
    assert draft_id not in recent_ids


# --- VAT-able lines require a supplier ---------------------------------------

def test_vatable_line_requires_supplier_when_submitting():
    with pytest.raises(ValidationError, match="supplier"):
        _make_expense(lines=[
            {"category_id": first_category_id(), "particulars": "No supplier given",
             "amount": 100.0, "is_vatable": True},
        ])


def test_vatable_line_with_supplier_succeeds():
    eid = _make_expense(lines=[
        {"category_id": first_category_id(), "particulars": "Fuel", "amount": 100.0,
         "is_vatable": True, "supplier_name": "Fuel Co", "supplier_vat": "VATFUELCO001"},
    ])
    line = svc.get_expense(eid)["lines"][0]
    assert line["supplier_name"] == "Fuel Co"
    assert line["supplier_vat"] == "VATFUELCO001"


def test_non_vatable_line_does_not_require_supplier():
    eid = _make_expense(lines=[
        {"category_id": first_category_id(), "particulars": "Snacks", "amount": 20.0,
         "is_vatable": False},
    ])
    line = svc.get_expense(eid)["lines"][0]
    assert line["supplier_name"] is None


def test_draft_allows_vatable_line_with_blank_supplier():
    eid = _make_expense(is_draft=True, lines=[
        {"category_id": first_category_id(), "particulars": "Fuel later", "amount": 100.0,
         "is_vatable": True},
    ])
    line = svc.get_expense(eid)["lines"][0]
    assert line["supplier_name"] is None


def test_draft_rejects_half_typed_supplier_even_though_relaxed():
    """Blank is fine for a draft, but a name with no VAT (or vice versa) is
    always an error, draft or not - it's clearly a mistake, not 'not ready
    yet'."""
    with pytest.raises(ValidationError):
        _make_expense(is_draft=True, lines=[
            {"category_id": first_category_id(), "particulars": "Fuel", "amount": 100.0,
             "is_vatable": True, "supplier_name": "Half Typed Co", "supplier_vat": ""},
        ])


def test_submitting_a_draft_requires_supplier_to_have_been_added():
    eid = _make_expense(is_draft=True, lines=[
        {"category_id": first_category_id(), "particulars": "Fuel", "amount": 100.0,
         "is_vatable": True},
    ])
    with pytest.raises(ValidationError, match="supplier"):
        svc.submit_draft(eid, {"id": 1, "role": "manager"})


def test_draft_with_supplier_can_be_submitted():
    eid = _make_expense(is_draft=True, lines=[
        {"category_id": first_category_id(), "particulars": "Fuel", "amount": 100.0,
         "is_vatable": True, "supplier_name": "Draft Fuel Co", "supplier_vat": "VATDRAFTFUEL"},
    ])
    voucher_no = svc.submit_draft(eid, {"id": 1, "role": "manager"})
    assert voucher_no.startswith("PCV-")
    line = svc.get_expense(eid)["lines"][0]
    assert line["supplier_name"] == "Draft Fuel Co"


def test_repeat_vat_number_reuses_the_same_supplier_across_vouchers():
    eid1 = _make_expense(lines=[
        {"category_id": first_category_id(), "particulars": "Fuel 1", "amount": 50.0,
         "is_vatable": True, "supplier_name": "Repeat Supplier", "supplier_vat": "VATREPEAT001"},
    ])
    eid2 = _make_expense(lines=[
        {"category_id": first_category_id(), "particulars": "Fuel 2", "amount": 60.0,
         "is_vatable": True, "supplier_name": "Repeat Supplier", "supplier_vat": "VATREPEAT001"},
    ])
    line1 = svc.get_expense(eid1)["lines"][0]
    line2 = svc.get_expense(eid2)["lines"][0]
    assert line1["supplier_id"] == line2["supplier_id"]


# ---------------------------------------------------------------------------
# Quick-clone (duplicate a voucher into a fresh draft)
# ---------------------------------------------------------------------------

def test_clone_copies_lines_into_a_new_draft():
    src = _make_expense(lines=[
        {"category_id": first_category_id(), "particulars": "Water", "amount": 30.0, "is_vatable": False},
        {"category_id": second_category_id(), "particulars": "Snacks", "amount": 20.0, "is_vatable": False},
    ])
    new_id = svc.clone_expense(src, {"id": 1, "role": "manager"})

    assert new_id != src
    copy = svc.get_expense(new_id)
    assert copy["status"] == "draft"
    assert copy["voucher_no"] is None
    assert [l["particulars"] for l in copy["lines"]] == ["Water", "Snacks"]
    assert [l["line_no"] for l in copy["lines"]] == [1, 2]
    assert copy["paid_to"] == svc.get_expense(src)["paid_to"]


def test_clone_leaves_the_original_untouched():
    src = _make_expense()
    before = svc.get_expense(src)
    svc.clone_expense(src, {"id": 1, "role": "manager"})
    after = svc.get_expense(src)
    assert after["status"] == before["status"]
    assert after["voucher_no"] == before["voucher_no"]
    assert len(after["lines"]) == len(before["lines"])


def test_clone_does_not_copy_the_receipt():
    """A receipt is evidence for one specific payment - carrying it over
    would attach last month's proof to this month's claim."""
    src = _make_expense(receipt_path="some-receipt.jpg")
    new_id = svc.clone_expense(src, {"id": 1, "role": "manager"})
    assert svc.get_expense(new_id)["receipt_path"] is None


def test_clone_dates_the_copy_today():
    src = _make_expense(expense_date="2020-01-15")
    new_id = svc.clone_expense(src, {"id": 1, "role": "manager"})
    assert svc.get_expense(new_id)["expense_date"] == date.today().isoformat()


def test_clone_recomputes_vat_at_the_current_rate():
    src = _make_expense(amount=200.0, is_vatable=True)
    new_id = svc.clone_expense(src, {"id": 1, "role": "manager"})
    line = svc.get_expense(new_id)["lines"][0]
    assert line["vat_rate"] == VAT_RATE
    assert line["vat_amount"] == round(200.0 * VAT_RATE / 100, 2)


def test_clone_keeps_the_supplier_on_a_vatable_line():
    src = _make_expense(lines=[
        {"category_id": first_category_id(), "particulars": "Diesel", "amount": 90.0,
         "is_vatable": True, "supplier_name": "Clone Fuel Co", "supplier_vat": "VATCLONE0001"},
    ])
    new_id = svc.clone_expense(src, {"id": 1, "role": "manager"})
    line = svc.get_expense(new_id)["lines"][0]
    assert line["supplier_name"] == "Clone Fuel Co"
    assert line["supplier_vat"] == "VATCLONE0001"


def test_cloned_draft_can_be_submitted_and_gets_its_own_number():
    src = _make_expense()
    new_id = svc.clone_expense(src, {"id": 1, "role": "manager"})
    voucher_no = svc.submit_draft(new_id, {"id": 1, "role": "manager"})
    assert voucher_no.startswith("PCV-")
    assert voucher_no != svc.get_expense(src)["voucher_no"]


def test_clone_of_a_deleted_voucher_is_refused():
    src = _make_expense()
    svc.approve_expense(src, 1)
    svc.delete_expense(src, 1, "duplicate entry")
    with pytest.raises(ConflictError):
        svc.clone_expense(src, {"id": 1, "role": "manager"})


def test_clone_of_someone_elses_draft_is_refused():
    """Ownership is a record-level rule, so it lives here rather than in a
    role guard - see _require_draft_owned_by for the same reasoning."""
    did = _make_expense(is_draft=True, user_id=1)
    with pytest.raises(ForbiddenError):
        svc.clone_expense(did, {"id": 999, "role": "user"})


def test_manager_may_clone_someone_elses_draft():
    from app.services import users as users_service
    other_manager_id = users_service.create_user(
        username="clone_mgr", full_name="Clone Manager", password="pass1234",
        role="manager", acting_user_id=1,
    )
    did = _make_expense(is_draft=True, user_id=1)
    new_id = svc.clone_expense(did, {"id": other_manager_id, "role": "manager"})
    assert svc.get_expense(new_id)["prepared_by"] == other_manager_id


def test_clone_of_a_missing_voucher_is_not_found():
    with pytest.raises(NotFoundError):
        svc.clone_expense(99999, {"id": 1, "role": "manager"})


# ---------------------------------------------------------------------------
# Recent category memory (form default)
# ---------------------------------------------------------------------------

def test_recent_category_is_none_for_a_brand_new_user():
    assert svc.recent_category_id(4242) is None


def test_recent_category_is_the_one_used_most():
    common = second_category_id()
    _make_expense(category_id=common, particulars="A")
    _make_expense(category_id=common, particulars="B")
    _make_expense(category_id=first_category_id(), particulars="C")
    assert svc.recent_category_id(1) == common


def test_recent_category_is_per_user():
    mine = second_category_id()
    _make_expense(category_id=mine, user_id=1)
    assert svc.recent_category_id(1) == mine
    assert svc.recent_category_id(777) is None


def test_recent_category_ignores_deleted_vouchers():
    """A voucher that was deleted was a mistake, not a habit."""
    binned = second_category_id()
    eid = _make_expense(category_id=binned)
    svc.approve_expense(eid, 1)
    svc.delete_expense(eid, 1, "wrong category entirely")
    assert svc.recent_category_id(1) != binned


def test_recent_category_skips_a_deactivated_category():
    """A category that no longer appears in the dropdown can't be the
    default for it."""
    from app.programs.expenses.services import settings as settings_svc
    cat = second_category_id()
    _make_expense(category_id=cat)
    settings_svc.toggle_category(cat, acting_user_id=1)
    assert svc.recent_category_id(1) != cat


def test_recent_category_counts_drafts_too():
    cat = second_category_id()
    _make_expense(is_draft=True, lines=[
        {"category_id": cat, "particulars": "Unfinished", "amount": 10.0, "is_vatable": False},
    ])
    assert svc.recent_category_id(1) == cat
