"""Tests for Word Editor document and profile services."""
import pytest
from app.programs.word_editor.services import documents as doc_svc
from app.programs.word_editor.services import profile as profile_svc
from app.services.errors import ValidationError, ForbiddenError, NotFoundError


# ── PROFILE ────────────────────────────────────────────────────────────────

def test_profile_initially_missing(reset_db):
    assert not profile_svc.profile_exists()


def test_create_initial_profile(reset_db, manager_user):
    profile_svc.create_initial_profile(
        company_name="Test Co", phone="055123",
        document_location="Office", website="test.com",
        company_location="123 Test St", company_phone="0551234567",
        created_by=manager_user["id"],
    )
    assert profile_svc.profile_exists()
    p = profile_svc.get_active_profile()
    assert p["company_name"] == "Test Co"
    assert p["company_location"] == "123 Test St"
    assert p["company_phone"] == "0551234567"
    assert p["website"] == "test.com"


def test_create_profile_requires_all_fields(reset_db, manager_user):
    with pytest.raises(ValidationError):
        profile_svc.create_initial_profile(
            company_name="", phone="055123",
            document_location="Office", website="test.com",
            company_location="123 St", company_phone="055",
            created_by=manager_user["id"],
        )


def test_submit_change_request(reset_db, manager_user, plain_user):
    profile_svc.create_initial_profile(
        company_name="Test Co", phone="055123",
        document_location="Office", website="test.com",
        company_location="Old Address", company_phone="0551111",
        created_by=manager_user["id"],
    )
    profile_svc.submit_change_request(
        requested_by=plain_user["id"],
        company_name="New Co", phone="055999",
        document_location="Floor 2", website="new.com",
        company_location="New Address", company_phone="0559999",
    )
    pending = profile_svc.list_pending_requests()
    assert len(pending) == 1
    assert pending[0]["company_location"] == "New Address"


def test_approve_change_request_updates_active(reset_db, manager_user, plain_user):
    profile_svc.create_initial_profile(
        company_name="Old Co", phone="055111",
        document_location="Old Loc", website="old.com",
        company_location="Old Addr", company_phone="055000",
        created_by=manager_user["id"],
    )
    profile_svc.submit_change_request(
        requested_by=plain_user["id"],
        company_name="New Co", phone="055999",
        document_location="New Loc", website="new.com",
        company_location="New Addr", company_phone="055999",
    )
    reqs = profile_svc.list_pending_requests()
    profile_svc.approve_change_request(reqs[0]["id"], manager_user["id"])
    p = profile_svc.get_active_profile()
    assert p["company_name"] == "New Co"
    assert p["company_location"] == "New Addr"
    assert len(profile_svc.list_pending_requests()) == 0


# ── DOCUMENTS ──────────────────────────────────────────────────────────────

def test_create_document_is_draft(reset_db, plain_user):
    doc_id = doc_svc.create_document(created_by=plain_user["id"], subject="Test")
    docs = doc_svc.list_documents(user_id=plain_user["id"], role="user")
    assert any(d["id"] == doc_id and d["status"] == "draft" for d in docs)


def test_draft_has_no_doc_no(reset_db, plain_user):
    doc_id = doc_svc.create_document(created_by=plain_user["id"], subject="Test")
    docs = doc_svc.list_documents(user_id=plain_user["id"], role="user")
    doc = next(d for d in docs if d["id"] == doc_id)
    assert doc["doc_no"] is None


def test_self_approve_assigns_doc_no(reset_db, plain_user):
    doc_id = doc_svc.create_document(created_by=plain_user["id"], subject="My Report")
    doc_no = doc_svc.self_approve(doc_id, user_id=plain_user["id"], role="user")
    assert doc_no.startswith("WD-")
    doc = doc_svc.get_document(doc_id, user_id=plain_user["id"], role="user")
    assert doc["status"] == "self_approved"
    assert doc["doc_no"] == doc_no


def test_self_approve_requires_subject(reset_db, plain_user):
    doc_id = doc_svc.create_document(created_by=plain_user["id"], subject="")
    with pytest.raises(ValidationError, match="Subject"):
        doc_svc.self_approve(doc_id, user_id=plain_user["id"], role="user")


def test_draft_not_printable_via_status(reset_db, plain_user):
    doc_id = doc_svc.create_document(created_by=plain_user["id"], subject="Draft")
    doc = doc_svc.get_document(doc_id, user_id=plain_user["id"], role="user")
    assert doc["status"] == "draft"


def test_make_official_requires_self_approved(reset_db, plain_user, manager_user):
    doc_id = doc_svc.create_document(created_by=plain_user["id"], subject="Test")
    with pytest.raises(ForbiddenError):
        doc_svc.make_official(doc_id, manager_id=manager_user["id"])


def test_make_official_flow(reset_db, plain_user, manager_user):
    doc_id = doc_svc.create_document(created_by=plain_user["id"], subject="Test")
    doc_svc.self_approve(doc_id, user_id=plain_user["id"], role="user")
    doc_svc.make_official(doc_id, manager_id=manager_user["id"], note="Approved.")
    doc = doc_svc.get_document(doc_id, user_id=manager_user["id"], role="manager")
    assert doc["status"] == "official"
    assert doc["manager_note"] == "Approved."


def test_doc_no_sequence(reset_db, plain_user):
    ids = []
    for i in range(3):
        did = doc_svc.create_document(created_by=plain_user["id"], subject=f"Doc {i}")
        doc_svc.self_approve(did, user_id=plain_user["id"], role="user")
        ids.append(did)
    docs = doc_svc.list_documents(user_id=plain_user["id"], role="user",
                                   status_filter="self_approved")
    nos = sorted(d["doc_no"] for d in docs if d["doc_no"])
    seqs = [int(n.split("-")[2]) for n in nos]
    assert seqs == sorted(seqs)
    assert seqs[-1] - seqs[0] == 2


def test_visibility_own_doc(reset_db, plain_user, second_user):
    doc_id = doc_svc.create_document(created_by=plain_user["id"], subject="Private")
    docs_owner = doc_svc.list_documents(user_id=plain_user["id"], role="user")
    docs_other = doc_svc.list_documents(user_id=second_user["id"], role="user")
    assert any(d["id"] == doc_id for d in docs_owner)
    assert not any(d["id"] == doc_id for d in docs_other)


def test_manager_sees_all_docs(reset_db, plain_user, manager_user):
    doc_id = doc_svc.create_document(created_by=plain_user["id"], subject="Private")
    docs = doc_svc.list_documents(user_id=manager_user["id"], role="manager")
    assert any(d["id"] == doc_id for d in docs)


def test_discard_draft_hard_deletes(reset_db, plain_user):
    doc_id = doc_svc.create_document(created_by=plain_user["id"], subject="Temp")
    doc_svc.discard_draft(doc_id, user_id=plain_user["id"], role="user")
    docs = doc_svc.list_documents(user_id=plain_user["id"], role="user")
    assert not any(d["id"] == doc_id for d in docs)


def test_cannot_discard_approved_doc(reset_db, plain_user):
    doc_id = doc_svc.create_document(created_by=plain_user["id"], subject="Test")
    doc_svc.self_approve(doc_id, user_id=plain_user["id"], role="user")
    with pytest.raises(ForbiddenError):
        doc_svc.discard_draft(doc_id, user_id=plain_user["id"], role="user")


def test_cannot_edit_approved_doc(reset_db, plain_user):
    doc_id = doc_svc.create_document(created_by=plain_user["id"], subject="Test")
    doc_svc.self_approve(doc_id, user_id=plain_user["id"], role="user")
    with pytest.raises(ForbiddenError):
        doc_svc.update_document(
            doc_id, user_id=plain_user["id"], role="user",
            title="New", subject="New", receiver_name="",
            receiver_phone="", content_html="",
        )


def test_delete_doc_requires_reason(reset_db, plain_user):
    doc_id = doc_svc.create_document(created_by=plain_user["id"], subject="Test")
    doc_svc.self_approve(doc_id, user_id=plain_user["id"], role="user")
    with pytest.raises(ValidationError):
        doc_svc.delete_document(doc_id, user_id=plain_user["id"], role="user", reason="")


# ── TEMPLATE SLOTS ─────────────────────────────────────────────────────────

def test_personal_template_slot_limit(reset_db, plain_user):
    for i in range(1, 6):
        did = doc_svc.create_document(created_by=plain_user["id"], subject=f"T{i}")
        doc_svc.save_to_template_slot(did, i, f"T{i}", user_id=plain_user["id"], role="user")
    assert len(doc_svc.list_templates(plain_user["id"])) == 5
    with pytest.raises(ValidationError):
        did = doc_svc.create_document(created_by=plain_user["id"], subject="Extra")
        doc_svc.save_to_template_slot(did, 6, "Extra", user_id=plain_user["id"], role="user")


def test_universal_template_limit(reset_db, manager_user):
    for i in range(2):
        doc_svc.create_universal_template(
            title=f"UT{i}", content_html="", subject="",
            created_by=manager_user["id"],
        )
    with pytest.raises(ValidationError):
        doc_svc.create_universal_template(
            title="Third", content_html="", subject="",
            created_by=manager_user["id"],
        )


# ── SUBJECTS ───────────────────────────────────────────────────────────────

def test_save_and_delete_subject(reset_db, plain_user):
    doc_svc.save_subject(plain_user["id"], "Weekly Report")
    subjects = doc_svc.list_subjects(plain_user["id"])
    assert any(s["subject"] == "Weekly Report" for s in subjects)
    sid = next(s["id"] for s in subjects if s["subject"] == "Weekly Report")
    doc_svc.delete_subject(sid, user_id=plain_user["id"])
    assert not any(s["subject"] == "Weekly Report" for s in doc_svc.list_subjects(plain_user["id"]))


def test_cannot_delete_other_users_subject(reset_db, plain_user, second_user):
    doc_svc.save_subject(plain_user["id"], "My Subject")
    subjects = doc_svc.list_subjects(plain_user["id"])
    sid = subjects[0]["id"]
    with pytest.raises(NotFoundError):
        doc_svc.delete_subject(sid, user_id=second_user["id"])


# ---------------------------------------------------------------------------
# Export format rules - an official document is PDF-only
# ---------------------------------------------------------------------------
#
# The rule lives in the service (export_allowed) rather than only in the
# route, so it is testable without a server and cannot be bypassed by a
# second caller later. These are plain dicts because the function only ever
# reads doc["status"].

class TestExportAllowed:

    def test_draft_can_be_exported_editable(self):
        from app.programs.word_editor.services import export as export_svc
        assert export_svc.export_allowed({"status": "draft"}, "doc") is True

    def test_self_approved_can_be_exported_editable(self):
        """A working copy. Exporting one to keep editing is what the format
        is for."""
        from app.programs.word_editor.services import export as export_svc
        assert export_svc.export_allowed({"status": "self_approved"}, "doc") is True

    def test_official_cannot_be_exported_as_doc(self):
        """An editable .doc of an issued document would open in Word
        carrying the official number and letterhead while no longer matching
        the hash the verify page checks."""
        from app.programs.word_editor.services import export as export_svc
        assert export_svc.export_allowed({"status": "official"}, "doc") is False

    def test_official_cannot_be_exported_as_html_either(self):
        """`html` is the same editable body without Word's wrapper -
        blocking only `doc` would leave the front door open."""
        from app.programs.word_editor.services import export as export_svc
        assert export_svc.export_allowed({"status": "official"}, "html") is False

    def test_every_editable_format_is_blocked_when_official(self):
        from app.programs.word_editor.services import export as export_svc
        assert all(not export_svc.export_allowed({"status": "official"}, fmt)
                   for fmt in export_svc.EDITABLE_FORMATS)

    def test_unknown_format_is_never_allowed(self):
        from app.programs.word_editor.services import export as export_svc
        assert export_svc.export_allowed({"status": "draft"}, "exe") is False

    def test_missing_document_is_not_allowed(self):
        from app.programs.word_editor.services import export as export_svc
        assert export_svc.export_allowed(None, "doc") is False
