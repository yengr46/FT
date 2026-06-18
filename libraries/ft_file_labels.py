"""ft_file_labels.py — shared FT file-list display labels.

Keeps file-list prefix behaviour consistent across FT apps.

Current conventions:
- Photos may display [GPS] when the caller supplies has_gps=True.
- Documents display [PDF] or [DOCX].
- Unknown/other file types display no prefix.
"""

from __future__ import annotations

import os

PHOTO_EXTS = {".jpg", ".jpeg"}
PDF_EXTS = {".pdf"}
DOCX_EXTS = {".docx"}
DOCUMENT_EXTS = PDF_EXTS | DOCX_EXTS


def type_prefix(path: str, *, has_gps: bool | None = None) -> str:
    """Return the display prefix for a file."""
    ext = os.path.splitext(str(path))[1].lower()

    if ext in PDF_EXTS:
        return "[PDF]"
    if ext in DOCX_EXTS:
        return "[DOCX]"
    if ext in PHOTO_EXTS and has_gps:
        return "[GPS]"
    return ""


def display_name(path: str, *, has_gps: bool | None = None) -> str:
    """Return the file-list display label for path."""
    name = os.path.basename(str(path))
    prefix = type_prefix(path, has_gps=has_gps)
    return f"{prefix} {name}" if prefix else name
