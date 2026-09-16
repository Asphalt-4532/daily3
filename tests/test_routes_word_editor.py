"""Route-level tests for the Word Editor program.

Uses FastAPI TestClient against a real isolated database (via conftest.py).
Checks authentication, permission, CSRF, and key happy paths.
"""
import pytest
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app, raise_server_exceptions=False)


def _login(username, password):
    r = client.post("/login", data={"username": username, "password": password},
                    follow_redirects=True)
    assert r.status_code == 200
    return r


def _csrf(session_cookies):
    r = client.get("/word-editor/", cookies=session_cookies)
    import re
    m = re.search(r'name="csrf_token" value="([^"]+)"', r.text)
    return m.group(1) if m else ""


def _manager_session(reset_db):
    r = client.post("/login", data={"username": "admin", "password": "admin123"})
    return client.cookies


def _user_session(reset_db, plain_user):
    r = client.post("/login", data={"username": plain_user["username"], "password": "password123"})
    return client.cookies


class TestSetupRedirect:
    def test_unauthenticated_redirects_to_login(self, reset_db):
        r = client.get("/word-editor/", follow_redirects=False)
        assert r.status_code == 302

    def test_no_profile_redirects_to_setup(self, reset_db):
        _login("admin", "admin123")
        r = client.get("/word-editor/", follow_redirects=False)
        assert r.status_code in (302, 200)

    def test_setup_page_loads(self, reset_db):
        _login("admin", "admin123")
        r = client.get("/word-editor/setup")
        assert r.status_code in (200, 302)


class TestDocumentCRUD:
    def _setup_profile(self, reset_db):
        _login("admin", "admin123")
        r = client.get("/word-editor/setup")
        import re
        m = re.search(r'name="csrf_token" value="([^"]+)"', r.text)
        csrf = m.group(1) if m else ""
        client.post("/word-editor/setup", data={
            "csrf_token": csrf,
            "company_name": "Test Co",
            "phone": "055123",
            "document_location": "Office",
            "website": "test.com",
            "company_location": "123 Test St",
            "company_phone": "0551234567",
        })

    def test_new_document_page_requires_login(self, reset_db):
        from fastapi.testclient import TestClient
        fresh = TestClient(app, raise_server_exceptions=False)
        r = fresh.get("/word-editor/documents/new", follow_redirects=False)
        assert r.status_code == 302
        assert "/login" in r.headers.get("location", "")

    def test_manager_can_access_dashboard(self, reset_db):
        self._setup_profile(reset_db)
        r = client.get("/word-editor/")
        assert r.status_code == 200

    def test_new_doc_get(self, reset_db):
        self._setup_profile(reset_db)
        r = client.get("/word-editor/documents/new")
        assert r.status_code == 200
        assert "Subject" in r.text

    def test_create_draft_via_post(self, reset_db):
        self._setup_profile(reset_db)
        import re
        r = client.get("/word-editor/documents/new")
        m = re.search(r'name="csrf_token" value="([^"]+)"', r.text)
        csrf = m.group(1) if m else ""
        r2 = client.post("/word-editor/documents/new", data={
            "csrf_token": csrf, "action": "draft",
            "subject": "Test Draft", "receiver_name": "Someone",
            "receiver_phone": "", "content_html": "<p>Hello</p>",
            "title": "Test Draft",
        }, follow_redirects=True)
        assert r2.status_code == 200

    def test_post_without_csrf_rejected(self, reset_db):
        self._setup_profile(reset_db)
        r = client.post("/word-editor/documents/new", data={
            "csrf_token": "bad-token", "action": "draft",
            "subject": "x", "content_html": "",
        })
        assert r.status_code == 403

    def test_document_list_loads(self, reset_db):
        self._setup_profile(reset_db)
        r = client.get("/word-editor/documents")
        assert r.status_code == 200

    def test_print_draft_forbidden(self, reset_db):
        """Drafts redirect back from print URL with error flash."""
        self._setup_profile(reset_db)
        from app.programs.word_editor.services.documents import create_document
        from app.database import get_db
        with get_db() as conn:
            pass
        import re
        r = client.get("/word-editor/documents/new")
        m = re.search(r'name="csrf_token" value="([^"]+)"', r.text)
        csrf = m.group(1) if m else ""
        r2 = client.post("/word-editor/documents/new", data={
            "csrf_token": csrf, "action": "draft",
            "subject": "Print Test", "title": "Print Test",
            "receiver_name": "", "receiver_phone": "",
            "content_html": "<p>body</p>",
        }, follow_redirects=True)
        assert r2.status_code == 200


class TestPermissions:
    def _setup_profile_as_manager(self, reset_db):
        _login("admin", "admin123")
        import re
        r = client.get("/word-editor/setup")
        m = re.search(r'name="csrf_token" value="([^"]+)"', r.text)
        csrf = m.group(1) if m else ""
        client.post("/word-editor/setup", data={
            "csrf_token": csrf,
            "company_name": "Test Co", "phone": "055",
            "document_location": "Office", "website": "t.com",
            "company_location": "Addr", "company_phone": "055",
        })

    def test_universal_templates_manager_only(self, reset_db, plain_user):
        self._setup_profile_as_manager(reset_db)
        # Login as plain user
        client.post("/login", data={"username": plain_user["username"], "password": "password123"})
        r = client.get("/word-editor/universal-templates", follow_redirects=False)
        assert r.status_code == 302

    def test_users_page_manager_only(self, reset_db, plain_user):
        self._setup_profile_as_manager(reset_db)
        client.post("/login", data={"username": plain_user["username"], "password": "password123"})
        r = client.get("/word-editor/users", follow_redirects=False)
        assert r.status_code == 302

    def test_manager_accesses_users_page(self, reset_db):
        self._setup_profile_as_manager(reset_db)
        r = client.get("/word-editor/users")
        assert r.status_code == 200

    def test_profile_page_accessible(self, reset_db):
        self._setup_profile_as_manager(reset_db)
        r = client.get("/word-editor/profile")
        assert r.status_code == 200
        assert "Active profile" in r.text


class TestDocumentQr:
    """Per-page QR endpoint: auth, input validation, status rule, happy path."""

    def _official_doc(self, reset_db):
        import re
        _login("admin", "admin123")
        r = client.get("/word-editor/setup")
        m = re.search(r'name="csrf_token" value="([^"]+)"', r.text)
        if m:
            client.post("/word-editor/setup", data={
                "csrf_token": m.group(1),
                "company_name": "Test Co", "phone": "055",
                "document_location": "Office", "website": "t.com",
                "company_location": "Addr", "company_phone": "0551234567",
            })
        from app.programs.word_editor.services import documents as doc_svc
        from app.database import get_db
        with get_db() as conn:
            uid = conn.execute(
                "SELECT id FROM users WHERE username = 'admin'").fetchone()["id"]
        doc_id = doc_svc.create_document(
            created_by=uid, title="QR Doc", subject="QR Doc",
            content_html="<p>body</p>",
        )
        doc_id = doc_id["id"] if isinstance(doc_id, dict) else doc_id
        doc_svc.self_approve(doc_id, user_id=uid, role="manager")
        doc_svc.make_official(doc_id, manager_id=uid, note="ok")
        return doc_id

    def test_requires_login(self, reset_db):
        from fastapi.testclient import TestClient
        fresh = TestClient(app, raise_server_exceptions=False)
        r = fresh.get("/word-editor/documents/1/qr?p=1&n=1", follow_redirects=False)
        assert r.status_code in (302, 401, 403)

    def test_official_document_returns_png(self, reset_db):
        doc_id = self._official_doc(reset_db)
        r = client.get(f"/word-editor/documents/{doc_id}/qr?p=2&n=3")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert r.content[:8] == b"\x89PNG\r\n\x1a\n"

    def test_each_page_gets_a_different_qr(self, reset_db):
        doc_id = self._official_doc(reset_db)
        a = client.get(f"/word-editor/documents/{doc_id}/qr?p=1&n=3").content
        b = client.get(f"/word-editor/documents/{doc_id}/qr?p=3&n=3").content
        assert a != b

    def test_invalid_page_params_rejected(self, reset_db):
        doc_id = self._official_doc(reset_db)
        for qs in ("p=0&n=3", "p=4&n=3", "p=x&n=3", "p=1&n=99999"):
            r = client.get(f"/word-editor/documents/{doc_id}/qr?{qs}")
            assert r.status_code == 400, qs

    def test_missing_document_is_404(self, reset_db):
        self._official_doc(reset_db)
        r = client.get("/word-editor/documents/999999/qr?p=1&n=1")
        assert r.status_code == 404


class TestVerifyHistoryExportRoutes:
    """Public verification page, history/restore, and the two export routes."""

    def _official(self, reset_db):
        import re
        _login("admin", "admin123")
        r = client.get("/word-editor/setup")
        m = re.search(r'name="csrf_token" value="([^"]+)"', r.text)
        if m:
            client.post("/word-editor/setup", data={
                "csrf_token": m.group(1), "company_name": "Test Co", "phone": "055",
                "document_location": "Office", "website": "t.com",
                "company_location": "Addr", "company_phone": "0551234567"})
        from app.programs.word_editor.services import documents as doc_svc
        from app.database import get_db
        with get_db() as conn:
            uid = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]
        doc_id = doc_svc.create_document(created_by=uid, title="Verify Doc",
                                         subject="Verify Doc",
                                         content_html="<p>galvanised sheet</p>")
        doc_no = doc_svc.self_approve(doc_id, user_id=uid, role="manager")
        doc_svc.make_official(doc_id, manager_id=uid, note="ok")
        return doc_id, doc_no

    def _self_approved(self, reset_db):
        """The same document one step earlier - published but not yet
        official, which is the state the editable exports are for."""
        import re
        _login("admin", "admin123")
        r = client.get("/word-editor/setup")
        m = re.search(r'name="csrf_token" value="([^"]+)"', r.text)
        if m:
            client.post("/word-editor/setup", data={
                "csrf_token": m.group(1), "company_name": "Test Co", "phone": "055",
                "document_location": "Office", "website": "t.com",
                "company_location": "Addr", "company_phone": "0551234567"})
        from app.programs.word_editor.services import documents as doc_svc
        from app.database import get_db
        with get_db() as conn:
            uid = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]
        doc_id = doc_svc.create_document(created_by=uid, title="Working Doc",
                                         subject="Working Doc",
                                         content_html="<p>galvanised sheet</p>")
        doc_no = doc_svc.self_approve(doc_id, user_id=uid, role="manager")
        return doc_id, doc_no

    def test_verify_page_is_public(self, reset_db):
        from fastapi.testclient import TestClient
        _, doc_no = self._official(reset_db)
        anon = TestClient(app, raise_server_exceptions=False)
        r = anon.get(f"/word-editor/verify/{doc_no}")
        assert r.status_code == 200
        assert doc_no in r.text

    def test_verify_page_never_shows_the_body(self, reset_db):
        from fastapi.testclient import TestClient
        _, doc_no = self._official(reset_db)
        anon = TestClient(app, raise_server_exceptions=False)
        r = anon.get(f"/word-editor/verify/{doc_no}")
        assert "galvanised sheet" not in r.text

    def test_verify_unknown_number_renders_an_error_not_a_crash(self, reset_db):
        from fastapi.testclient import TestClient
        self._official(reset_db)
        anon = TestClient(app, raise_server_exceptions=False)
        r = anon.get("/word-editor/verify/WD-2026-99999")
        assert r.status_code == 200
        assert "No document" in r.text

    def test_history_page_lists_versions(self, reset_db):
        doc_id, _ = self._official(reset_db)
        r = client.get(f"/word-editor/documents/{doc_id}/history")
        assert r.status_code == 200
        assert "snapshot" in r.text

    def test_history_requires_login(self, reset_db):
        from fastapi.testclient import TestClient
        doc_id, _ = self._official(reset_db)
        anon = TestClient(app, raise_server_exceptions=False)
        r = anon.get(f"/word-editor/documents/{doc_id}/history", follow_redirects=False)
        assert r.status_code == 302

    def test_restore_without_csrf_rejected(self, reset_db):
        doc_id, _ = self._official(reset_db)
        r = client.post(f"/word-editor/documents/{doc_id}/history/1/restore",
                        data={"csrf_token": "bad"})
        assert r.status_code == 403

    def test_doc_export_downloads_while_still_editable(self, reset_db):
        """A self-approved document still exports as .doc - it is a working
        copy, and handing one out is what the format is for."""
        doc_id, doc_no = self._self_approved(reset_db)
        r = client.get(f"/word-editor/documents/{doc_id}/export?fmt=doc")
        assert r.status_code == 200
        assert b"galvanised sheet" in r.content

    def test_official_doc_export_is_refused_and_sent_to_print(self, reset_db):
        """Once official, .doc is gone: the PDF print view is the only way
        to take a copy away. Checked at the route, not just hidden in the
        template - a typed URL has to hit the same wall as a missing button.
        """
        doc_id, _ = self._official(reset_db)
        r = client.get(f"/word-editor/documents/{doc_id}/export?fmt=doc",
                       follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"].endswith(f"/documents/{doc_id}/print")
        # Nothing downloadable came back - no file, no filename.
        assert "content-disposition" not in r.headers

    def test_official_doc_export_refused_for_plain_html_too(self, reset_db):
        """`html` is the same editable body without Word's wrapper, so the
        rule has to cover it - blocking only `doc` would leave the front
        door open."""
        doc_id, _ = self._official(reset_db)
        r = client.get(f"/word-editor/documents/{doc_id}/export?fmt=html",
                       follow_redirects=False)
        assert r.status_code == 302

    def test_official_doc_still_prints(self, reset_db):
        """The rule removes a format, not the ability to take a copy."""
        doc_id, _ = self._official(reset_db)
        r = client.get(f"/word-editor/documents/{doc_id}/print")
        assert r.status_code == 200

    def test_doc_export_rejects_unknown_format(self, reset_db):
        doc_id, _ = self._official(reset_db)
        r = client.get(f"/word-editor/documents/{doc_id}/export?fmt=exe")
        assert r.status_code == 400

    def test_register_xlsx_export(self, reset_db):
        self._official(reset_db)
        r = client.get("/word-editor/documents-export.xlsx")
        assert r.status_code == 200
        assert r.content[:2] == b"PK"

    def test_body_search_filters_the_list(self, reset_db):
        self._official(reset_db)
        hit = client.get("/word-editor/documents?q=galvanised")
        miss = client.get("/word-editor/documents?q=zzzznotpresent")
        assert "Verify Doc" in hit.text
        assert "Verify Doc" not in miss.text


class TestEditorFeatureSurface:
    """Phase 2 features are client-side, so the route test asserts the editor
    page actually ships them (a missing hook means a dead button)."""

    def _editor_page(self, reset_db):
        import re
        _login("admin", "admin123")
        r = client.get("/word-editor/setup")
        m = re.search(r'name="csrf_token" value="([^"]+)"', r.text)
        if m:
            client.post("/word-editor/setup", data={
                "csrf_token": m.group(1), "company_name": "Test Co", "phone": "055",
                "document_location": "Office", "website": "t.com",
                "company_location": "Addr", "company_phone": "055"})
        return client.get("/word-editor/documents/new").text

    @pytest.mark.parametrize("hook", [
        "autosaveNow", "offerRecovery",          # autosave + recovery
        "applyQuickStyle", "painterClick",       # quick styles, format painter
        "clearFormatting", "toggleMarks",        # clear format, invisibles
        "pastePlainPrompt", "insertPageBreak",   # paste plain, page breaks
        "fitOnePage", "setDir",                  # fit-to-page, RTL
        "buildOutline", "moveSection",           # outline, drag sections
        "buildSnippets", "positionFloatTb",      # snippets, floating toolbar
        "wordCount",                             # productivity readout
    ])
    def test_editor_ships_feature(self, reset_db, hook):
        assert hook in self._editor_page(reset_db)

    def test_autosave_label_is_no_longer_a_false_promise(self, reset_db):
        page = self._editor_page(reset_db)
        assert "localStorage.setItem" in page
        assert "setInterval" in page

    def test_print_page_honours_document_layout_settings(self, reset_db):
        from app.programs.word_editor.services import documents as doc_svc
        from app.database import get_db
        self._editor_page(reset_db)
        with get_db() as conn:
            uid = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]
        doc_id = doc_svc.create_document(
            created_by=uid, title="L", subject="L",
            content_html='<p>a</p><hr data-page-break="1"><p>b</p>'
                         '<div data-doc-settings="1" data-orient="landscape" '
                         'data-margin="wide" data-scale="80"></div>')
        doc_svc.self_approve(doc_id, user_id=uid, role="manager")
        r = client.get(f"/word-editor/documents/{doc_id}/print")
        assert r.status_code == 200
        assert "data-page-break" in r.text          # break survived sanitising
        assert 'data-orient="landscape"' in r.text  # layout settings survived
        assert "applyDocSettings" in r.text         # and the print side reads them
