"""ft_office_preview.py — Office document preview helper for FTViewDocs.

Converts .docx files to temporary PDF previews so FTViewDocs can reuse its
existing PDF thumbnail/viewer pipeline.

Conversion engines tried, in order:
  1. Microsoft Word COM automation (requires Word + pywin32)
  2. LibreOffice/soffice headless conversion, if available on PATH
  3. Built-in DOCX text fallback PDF made with Pillow (no extra dependency)

The original .docx is never modified. Preview PDFs are cached under the
user's temp folder and refreshed when the source file timestamp or size
changes.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OFFICE_EXTS = {".docx"}


def _cache_dir() -> Path:
    d = Path(tempfile.gettempdir()) / "FTOfficePreviewCache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_pdf_path(path: str) -> Path:
    src = Path(path)
    try:
        st = src.stat()
        stamp = f"{src.resolve()}|{st.st_mtime_ns}|{st.st_size}|v2-text-fallback"
    except Exception:
        stamp = str(src)
    digest = hashlib.sha1(stamp.encode("utf-8", "replace")).hexdigest()
    return _cache_dir() / f"{digest}.pdf"


def get_office_preview_pdf(path: str) -> str:
    """Return a PDF preview path for a .docx file, creating it if needed."""
    ext = Path(path).suffix.lower()
    if ext not in OFFICE_EXTS:
        raise RuntimeError(f"Unsupported Office preview type: {ext}")

    src = Path(path)
    if not src.exists():
        raise FileNotFoundError(str(path))

    out_pdf = _cache_pdf_path(path)
    if out_pdf.exists() and out_pdf.stat().st_size > 0:
        return str(out_pdf)

    errors = []

    try:
        _convert_with_word(src, out_pdf)
        if out_pdf.exists() and out_pdf.stat().st_size > 0:
            return str(out_pdf)
    except Exception as e:
        errors.append(f"Word COM: {e}")

    try:
        _convert_with_libreoffice(src, out_pdf)
        if out_pdf.exists() and out_pdf.stat().st_size > 0:
            return str(out_pdf)
    except Exception as e:
        errors.append(f"LibreOffice: {e}")

    # Last-resort built-in fallback.  This is not a Word-layout renderer; it
    # extracts readable text from document.xml and writes it into a simple
    # multi-page PDF so FTViewDocs thumbnails and page navigation still work.
    try:
        _convert_docx_text_to_pdf(src, out_pdf)
        if out_pdf.exists() and out_pdf.stat().st_size > 0:
            return str(out_pdf)
    except Exception as e:
        errors.append(f"Built-in text PDF: {e}")

    # Always return a valid PDF fallback rather than forcing FTViewDocs to
    # display a black unreadable thumbnail.  This page includes the conversion
    # errors and still lets the viewer/page bar operate normally.
    _convert_error_to_pdf(src, out_pdf, errors)
    return str(out_pdf)


def _convert_with_word(src: Path, out_pdf: Path) -> None:
    # Windows-only, requires pywin32 and Microsoft Word installed.
    import win32com.client  # type: ignore

    word = None
    doc = None
    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        doc = word.Documents.Open(str(src), ReadOnly=True, AddToRecentFiles=False)
        doc.ExportAsFixedFormat(str(out_pdf), 17)
    finally:
        try:
            if doc is not None:
                doc.Close(False)
        except Exception:
            pass
        try:
            if word is not None:
                word.Quit()
        except Exception:
            pass


def _convert_with_libreoffice(src: Path, out_pdf: Path) -> None:
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    if not exe:
        raise RuntimeError("LibreOffice/soffice not found on PATH")
    out_dir = out_pdf.parent
    cmd = [exe, "--headless", "--convert-to", "pdf", "--outdir", str(out_dir), str(src)]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=90)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "conversion failed").strip())
    produced = out_dir / (src.stem + ".pdf")
    if produced.exists():
        if out_pdf.exists():
            try:
                out_pdf.unlink()
            except Exception:
                pass
        produced.replace(out_pdf)


def _docx_text(path: Path) -> list[str]:
    """Extract basic paragraph text from a DOCX package."""
    with zipfile.ZipFile(path) as zf:
        try:
            xml_bytes = zf.read("word/document.xml")
        except KeyError:
            raise RuntimeError("word/document.xml not found in DOCX package")

    root = ET.fromstring(xml_bytes)
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs = []

    for para in root.findall(".//w:p", ns):
        parts = []
        for node in para.iter():
            tag = node.tag.rsplit("}", 1)[-1]
            if tag == "t" and node.text:
                parts.append(node.text)
            elif tag == "tab":
                parts.append("    ")
            elif tag == "br":
                parts.append("\n")
        text = "".join(parts).strip()
        if text:
            paragraphs.append(text)

    return paragraphs or ["(No extractable text found in this DOCX file.)"]


def _load_font(size: int, bold: bool = False):
    candidates = []
    if os.name == "nt":
        fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        candidates.extend([
            fonts / ("arialbd.ttf" if bold else "arial.ttf"),
            fonts / ("segoeuib.ttf" if bold else "segoeui.ttf"),
            fonts / ("calibrib.ttf" if bold else "calibri.ttf"),
        ])
    for p in candidates:
        try:
            if p.exists():
                return ImageFont.truetype(str(p), size)
        except Exception:
            pass
    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf", size)
    except Exception:
        return ImageFont.load_default()


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    # Keep explicit line breaks, then word-wrap each line.
    out = []
    for source_line in str(text).replace("\r", "").split("\n"):
        words = source_line.split()
        if not words:
            out.append("")
            continue
        line = words[0]
        for word in words[1:]:
            test = line + " " + word
            try:
                w = draw.textbbox((0, 0), test, font=font)[2]
            except Exception:
                w = draw.textlength(test, font=font)
            if w <= max_width:
                line = test
            else:
                out.append(line)
                line = word
        out.append(line)
    return out


def _convert_docx_text_to_pdf(src: Path, out_pdf: Path) -> None:
    paragraphs = _docx_text(src)

    # A4-ish page at moderate resolution: good enough for preview, not huge.
    page_w, page_h = 1240, 1754
    margin = 90
    header_h = 90
    line_gap = 8

    title_font = _load_font(34, bold=True)
    body_font = _load_font(25, bold=False)
    small_font = _load_font(18, bold=False)

    pages = []
    img = Image.new("RGB", (page_w, page_h), "white")
    draw = ImageDraw.Draw(img)
    y = margin

    def new_page():
        nonlocal img, draw, y
        pages.append(img)
        img = Image.new("RGB", (page_w, page_h), "white")
        draw = ImageDraw.Draw(img)
        y = margin

    # Header on first page.
    draw.text((margin, y), src.name, fill=(20, 20, 20), font=title_font)
    y += header_h
    draw.text((margin, y), "DOCX text preview fallback", fill=(100, 100, 100), font=small_font)
    y += 55

    for para in paragraphs:
        lines = _wrap_text(draw, para, body_font, page_w - margin * 2)
        needed = len(lines) * (body_font.size + line_gap) + 25
        if y + needed > page_h - margin:
            new_page()
        for line in lines:
            if y + body_font.size + line_gap > page_h - margin:
                new_page()
            draw.text((margin, y), line, fill=(0, 0, 0), font=body_font)
            y += body_font.size + line_gap
        y += 20

    pages.append(img)

    if not pages:
        pages = [Image.new("RGB", (page_w, page_h), "white")]

    first, rest = pages[0], pages[1:]
    first.save(str(out_pdf), "PDF", resolution=150, save_all=True, append_images=rest)


def _convert_error_to_pdf(src: Path, out_pdf: Path, errors: list[str]) -> None:
    """Create a one-page PDF explaining why the DOCX could not be previewed."""
    page_w, page_h = 1240, 1754
    margin = 90
    title_font = _load_font(34, bold=True)
    body_font = _load_font(24, bold=False)
    small_font = _load_font(18, bold=False)

    img = Image.new("RGB", (page_w, page_h), "white")
    draw = ImageDraw.Draw(img)
    y = margin

    draw.text((margin, y), "DOCX preview unavailable", fill=(120, 0, 0), font=title_font)
    y += 70
    draw.text((margin, y), src.name, fill=(20, 20, 20), font=body_font)
    y += 65

    msg = (
        "FTViewDocs could not create a layout preview for this DOCX file. "
        "The original file can still be opened externally. "
        "Install Microsoft Word + pywin32, or LibreOffice, for better DOCX previews."
    )
    for line in _wrap_text(draw, msg, body_font, page_w - margin * 2):
        draw.text((margin, y), line, fill=(0, 0, 0), font=body_font)
        y += body_font.size + 8

    y += 40
    draw.text((margin, y), "Conversion attempts:", fill=(60, 60, 60), font=small_font)
    y += 35

    for err in errors or ["Unknown error"]:
        for line in _wrap_text(draw, "- " + str(err), small_font, page_w - margin * 2):
            if y > page_h - margin - 30:
                break
            draw.text((margin, y), line, fill=(80, 80, 80), font=small_font)
            y += small_font.size + 6

    img.save(str(out_pdf), "PDF", resolution=150)
