"""Sandboxing utilities for handling untrusted, user-supplied file content.

Concern: three guarantees applied consistently wherever this app touches a
file whose name or bytes came from outside (receipt uploads, backup
restores): (1) a resolved path can never escape its intended directory,
(2) a file's actual bytes are checked against what its extension claims,
not just trusted at face value, and (3) anything written to disk from
untrusted input is stripped of execute permission.
Depends on: nothing app-specific - pure filesystem/bytes operations, so
it's trivial to unit test and safe to import from anywhere.
Used by: app.services.expenses (receipt uploads), app.backup (restored
receipt files), app.routes.expense_routes (serving uploads back out).
"""
import os
from pathlib import Path

from app.services.errors import ValidationError

__all__ = ["validate_file_signature", "safe_join", "set_nonexecutable"]

# Real file signatures ("magic bytes") for the upload types this app
# accepts. An extension is just a claim; these are what the bytes actually
# have to start with to be believed.
_JPEG = (b"\xff\xd8\xff",)
_PNG = (b"\x89PNG\r\n\x1a\n",)
_PDF = (b"%PDF",)
_SIGNATURES = {".jpg": _JPEG, ".jpeg": _JPEG, ".png": _PNG, ".pdf": _PDF}


def validate_file_signature(ext: str, contents: bytes) -> None:
    """Raises ValidationError if `contents` doesn't actually start with the
    real file signature for `ext`. Stops a script, executable, or HTML file
    renamed to .jpg/.pdf from ever reaching disk as a 'receipt'. WEBP is
    handled separately since its signature spans two offsets."""
    ext = ext.lower()
    if ext == ".webp":
        if not (contents[:4] == b"RIFF" and contents[8:12] == b"WEBP"):
            raise ValidationError("This file doesn't look like a real WEBP image.")
        return
    sigs = _SIGNATURES.get(ext)
    if sigs is None:
        return  # not one of the types we sanity-check by content; the caller's
        # extension allowlist is what actually gates which extensions reach here
    if not any(contents.startswith(sig) for sig in sigs):
        raise ValidationError(
            f"This file doesn't look like a real {ext.lstrip('.').upper()} file.")


def safe_join(base: Path, filename: str) -> Path:
    """Resolves `filename` under `base` and guarantees the result can't
    escape it via path traversal, an absolute path, or a symlink. Raises
    ValueError if it would. Use this instead of `base / filename` for any
    filename that came from a request."""
    candidate = (base / Path(filename).name).resolve()
    base_resolved = base.resolve()
    if candidate != base_resolved and base_resolved not in candidate.parents:
        raise ValueError(f"Path escapes the allowed directory: {filename!r}")
    return candidate


def set_nonexecutable(path: Path) -> None:
    """Explicitly strips execute permission from a file written from
    untrusted input. Defense in depth: nothing in this app would ever try
    to execute an uploaded file, but a file that structurally cannot be
    executed removes an entire class of "what if" from consideration -
    including from a zip entry that tried to smuggle in an execute bit via
    its stored Unix permissions (shutil.copy2 preserves those by default)."""
    try:
        os.chmod(path, 0o640)
    except OSError:
        pass
