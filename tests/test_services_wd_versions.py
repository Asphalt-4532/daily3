"""Word Editor versioning, official snapshots, audit trail, search, export.

Service-layer tests - no HTTP involved, per the project's testing split.
"""
import pytest

from app.database import get_db
from app.programs.word_editor.services import documents as doc_svc
from app.programs.word_editor.services import export as export_svc
from app.services.errors import NotFoundError, ValidationError


def _admin_id():
    with get_db() as conn:
        return conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]


def _new_doc(uid, body="<p>first</p>", subject="Steel order"):
    return doc_svc.create_document(created_by=uid, title=subject, subject=subject,
                                   content_html=body)


class TestVersioning:
    def test_creation_records_version_one(self, reset_db):
        uid = _admin_id()
        doc_id = _new_doc(uid)
        versions = doc_svc.list_versions(doc_id, user_id=uid, role="manager")
        assert len(versions) == 1
        assert versions[0]["version_no"] == 1

    def test_each_edit_appends_a_version(self, reset_db):
        uid = _admin_id()
        doc_id = _new_doc(uid)
        for body in ("<p>second</p>", "<p>third</p>"):
            doc_svc.update_document(doc_id, user_id=uid, role="manager",
                                    title="t", subject="s", receiver_name="",
                                    receiver_phone="", content_html=body)
        versions = doc_svc.list_versions(doc_id, user_id=uid, role="manager")
        assert [v["version_no"] for v in versions] == [3, 2, 1]

    def test_unchanged_body_does_not_create_a_version(self, reset_db):
        uid = _admin_id()
        doc_id = _new_doc(uid, "<p>same</p>")
        doc_svc.update_document(doc_id, user_id=uid, role="manager", title="t",
                                subject="s", receiver_name="", receiver_phone="",
                                content_html="<p>same</p>")
        assert len(doc_svc.list_versions(doc_id, user_id=uid, role="manager")) == 1

    def test_restore_appends_rather_than_rewrites(self, reset_db):
        uid = _admin_id()
        doc_id = _new_doc(uid, "<p>original</p>")
        doc_svc.update_document(doc_id, user_id=uid, role="manager", title="t",
                                subject="s", receiver_name="", receiver_phone="",
                                content_html="<p>changed</p>")
        doc_svc.restore_version(doc_id, 1, user_id=uid, role="manager")
        doc = doc_svc.get_document(doc_id, user_id=uid, role="manager")
        assert "original" in doc["content_json"]
        assert len(doc_svc.list_versions(doc_id, user_id=uid, role="manager")) == 3

    def test_missing_version_raises_not_found(self, reset_db):
        uid = _admin_id()
        doc_id = _new_doc(uid)
        with pytest.raises(NotFoundError):
            doc_svc.get_version(doc_id, 99, user_id=uid, role="manager")


class TestSnapshots:
    def test_making_official_freezes_a_snapshot(self, reset_db):
        uid = _admin_id()
        doc_id = _new_doc(uid)
        doc_svc.self_approve(doc_id, user_id=uid, role="manager")
        doc_svc.make_official(doc_id, manager_id=uid, note="ok")
        doc = doc_svc.get_document(doc_id, user_id=uid, role="manager")
        assert doc["official_sha"] == doc_svc.content_sha(doc["content_json"])
        snaps = [v for v in doc_svc.list_versions(doc_id, user_id=uid, role="manager")
                 if v["is_snapshot"]]
        assert len(snaps) == 1


class TestAuditTrail:
    def _actions(self):
        with get_db() as conn:
            return [r["action"] for r in conn.execute(
                "SELECT action FROM audit_log ORDER BY id")]

    def test_document_lifecycle_is_logged(self, reset_db):
        uid = _admin_id()
        doc_id = _new_doc(uid)
        doc_svc.update_document(doc_id, user_id=uid, role="manager", title="t",
                                subject="s", receiver_name="", receiver_phone="",
                                content_html="<p>v2</p>")
        doc_svc.self_approve(doc_id, user_id=uid, role="manager")
        doc_svc.make_official(doc_id, manager_id=uid, note="")
        doc_svc.delete_document(doc_id, user_id=uid, role="manager", reason="error")
        actions = self._actions()
        for expected in ("wd_document_created", "wd_document_edited",
                         "wd_document_self_approved", "wd_document_official",
                         "wd_document_deleted"):
            assert expected in actions


class TestSanitizedOnSave:
    def test_script_never_reaches_the_database(self, reset_db):
        uid = _admin_id()
        doc_id = _new_doc(uid, "<p>ok</p><script>alert(1)</script>")
        doc = doc_svc.get_document(doc_id, user_id=uid, role="manager")
        assert "script" not in doc["content_json"].lower()


class TestFullTextSearch:
    def test_finds_a_word_in_the_body(self, reset_db):
        uid = _admin_id()
        _new_doc(uid, "<p>galvanised sheet delivery</p>", subject="Order A")
        _new_doc(uid, "<p>timber pallets</p>", subject="Order B")
        hits = doc_svc.list_documents(user_id=uid, role="manager", text_query="galvanised")
        assert len(hits) == 1 and hits[0]["subject"] == "Order A"

    def test_no_query_returns_everything(self, reset_db):
        uid = _admin_id()
        _new_doc(uid, subject="Order A")
        _new_doc(uid, subject="Order B")
        assert len(doc_svc.list_documents(user_id=uid, role="manager")) == 2


class TestPublicVerification:
    def test_official_document_verifies(self, reset_db):
        uid = _admin_id()
        doc_id = _new_doc(uid)
        doc_no = doc_svc.self_approve(doc_id, user_id=uid, role="manager")
        doc_svc.make_official(doc_id, manager_id=uid, note="")
        result = doc_svc.verify_by_doc_no(doc_no)
        assert result["doc_no"] == doc_no
        assert result["status"] == "official"
        assert result["official_sha"]

    def test_result_never_includes_the_body(self, reset_db):
        uid = _admin_id()
        doc_id = _new_doc(uid, "<p>confidential price</p>")
        doc_no = doc_svc.self_approve(doc_id, user_id=uid, role="manager")
        assert "content_json" not in doc_svc.verify_by_doc_no(doc_no)

    def test_unknown_number_raises_not_found(self, reset_db):
        with pytest.raises(NotFoundError):
            doc_svc.verify_by_doc_no("WD-2026-99999")

    @pytest.mark.parametrize("bad", ["", "PCV-2026-00001", "WD-26-1", "'; DROP TABLE--"])
    def test_malformed_number_rejected(self, reset_db, bad):
        with pytest.raises(ValidationError):
            doc_svc.verify_by_doc_no(bad)


class TestExport:
    def test_doc_export_is_word_openable_html(self, reset_db):
        uid = _admin_id()
        doc_id = _new_doc(uid, "<p>body text</p>")
        doc = doc_svc.get_document(doc_id, user_id=uid, role="manager")
        html = export_svc.build_export_html(doc, {"company_name": "Test Co"})
        assert "<html" in html and "body text" in html and "Test Co" in html

    def test_register_workbook_is_xlsx_bytes(self, reset_db):
        uid = _admin_id()
        _new_doc(uid)
        docs = doc_svc.list_documents(user_id=uid, role="manager")
        data = export_svc.build_register_workbook(docs, {"company_name": "Test Co"},
                                                  generated_by="admin")
        assert data[:2] == b"PK"
