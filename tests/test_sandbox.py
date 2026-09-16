import io
import os
import stat
import sys

import pytest

from app import sandbox
from app.services.errors import ValidationError


# --- validate_file_signature ------------------------------------------

def test_validate_file_signature_accepts_real_jpeg():
    jpeg_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 50
    sandbox.validate_file_signature(".jpg", jpeg_bytes)  # should not raise


def test_validate_file_signature_accepts_real_png():
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50
    sandbox.validate_file_signature(".png", png_bytes)  # should not raise


def test_validate_file_signature_accepts_real_pdf():
    sandbox.validate_file_signature(".pdf", b"%PDF-1.4\n" + b"\x00" * 50)  # should not raise


def test_validate_file_signature_accepts_real_webp():
    webp_bytes = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 50
    sandbox.validate_file_signature(".webp", webp_bytes)  # should not raise


def test_validate_file_signature_rejects_script_disguised_as_jpeg():
    fake = b"#!/bin/sh\necho pwned\n"
    with pytest.raises(ValidationError, match="doesn't look like a real JPG"):
        sandbox.validate_file_signature(".jpg", fake)


def test_validate_file_signature_rejects_html_disguised_as_pdf():
    fake = b"<html><script>alert(1)</script></html>"
    with pytest.raises(ValidationError):
        sandbox.validate_file_signature(".pdf", fake)


def test_validate_file_signature_rejects_exe_disguised_as_png():
    fake = b"MZ\x90\x00\x03\x00\x00\x00"  # PE/EXE header
    with pytest.raises(ValidationError):
        sandbox.validate_file_signature(".png", fake)


def test_validate_file_signature_rejects_fake_webp_riff_without_webp_tag():
    # a real RIFF file that's actually WAV audio, not WEBP - RIFF header
    # alone isn't sufficient, the WEBP tag at offset 8 must also match
    fake_wav = b"RIFF" + b"\x00\x00\x00\x00" + b"WAVE" + b"\x00" * 50
    with pytest.raises(ValidationError):
        sandbox.validate_file_signature(".webp", fake_wav)


def test_validate_file_signature_allows_unrecognized_extension_through():
    # extensions with no signature check defined are the caller's allowlist's
    # job to reject, not this function's - it shouldn't raise for them
    sandbox.validate_file_signature(".xyz", b"anything at all")


# --- safe_join -----------------------------------------------------------

def test_safe_join_normal_filename(tmp_path):
    result = sandbox.safe_join(tmp_path, "receipt.jpg")
    assert result == (tmp_path / "receipt.jpg").resolve()


def test_safe_join_neutralizes_path_traversal_by_stripping_to_bare_name(tmp_path):
    """Path(filename).name strips any directory component before the join,
    so '../../etc/passwd' collapses to just 'passwd' safely inside base -
    it never gets the chance to traverse in the first place."""
    result = sandbox.safe_join(tmp_path, "../../etc/passwd")
    assert result.parent == tmp_path.resolve()
    assert result.name == "passwd"


def test_safe_join_neutralizes_absolute_path_the_same_way(tmp_path):
    result = sandbox.safe_join(tmp_path, "/etc/passwd")
    assert result.parent == tmp_path.resolve()
    assert result.name == "passwd"


def test_safe_join_rejects_symlink_that_escapes_base(tmp_path):
    """The one way a filename *can* actually escape base after the .name
    stripping: it's the name of a symlink that already lives inside base
    and points somewhere else - .resolve() follows it, and this must be
    caught."""
    base = tmp_path / "uploads"
    base.mkdir()
    outside_target = tmp_path / "outside_secret.txt"
    outside_target.write_text("secret")
    evil_link = base / "evil_link.jpg"
    try:
        evil_link.symlink_to(outside_target)
    except OSError as e:
        # Unprivileged Windows accounts can't create symlinks at all
        # (WinError 1314) unless Developer Mode is on or running as
        # Administrator - that's an environment limitation, not something
        # this test is actually checking. Skip rather than fail so a
        # Windows contributor without that privilege isn't blocked by a
        # test that never got to exercise safe_join() at all.
        pytest.skip(f"can't create a symlink in this environment: {e}")

    with pytest.raises(ValueError):
        sandbox.safe_join(base, "evil_link.jpg")


# --- set_nonexecutable ----------------------------------------------------

@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows/NTFS has no POSIX execute-bit concept - os.chmod() can't "
           "meaningfully strip S_IXUSR/S_IXGRP/S_IXOTH there, so there's nothing "
           "valid for this test to check on that platform. The real property "
           "(uploaded files land non-executable) still applies and is still "
           "tested wherever this app actually runs - Docker, Linux, or Mac.",
)
def test_set_nonexecutable_strips_execute_bit(tmp_path):
    f = tmp_path / "test_file.jpg"
    f.write_bytes(b"content")
    os.chmod(f, 0o755)  # simulate a file that arrived with execute bits set
    assert os.stat(f).st_mode & stat.S_IXUSR

    sandbox.set_nonexecutable(f)

    mode = os.stat(f).st_mode
    assert not (mode & stat.S_IXUSR)
    assert not (mode & stat.S_IXGRP)
    assert not (mode & stat.S_IXOTH)


def test_set_nonexecutable_does_not_raise_for_missing_file(tmp_path):
    missing = tmp_path / "does_not_exist.jpg"
    sandbox.set_nonexecutable(missing)  # should not raise
