"""
ft_print.py — shared print orchestration helpers for FT apps.

Current strategy:
- PDF/DOCX documents are converted/combined into one temporary PDF and opened.
- Images should be routed by the calling app to the contact-sheet workflow.
"""

from __future__ import annotations

import os
import tempfile
import traceback
from datetime import datetime
from pathlib import Path
from typing import Iterable, List


def _norm_existing_files(files: Iterable[str]) -> List[str]:
    out = []
    for f in files or []:
        try:
            p = os.path.normpath(str(f))
            if os.path.isfile(p):
                out.append(p)
        except Exception:
            pass
    return out


def _timestamp_name(prefix: str = "FTPrint") -> str:
    return datetime.now().strftime(prefix + "_%Y-%m-%d_%H-%M-%S.pdf")


def _docx_to_pdf(docx_path: str, out_dir: str) -> str:
    """Convert DOCX to PDF if possible and return generated PDF path."""
    try:
        from ft_office_preview import get_office_preview_pdf
        pdf = get_office_preview_pdf(docx_path)
        if pdf and os.path.isfile(pdf):
            return pdf
    except Exception:
        pass

    try:
        from docx import Document
        from fpdf import FPDF
    except Exception as e:
        raise RuntimeError(
            "Cannot convert DOCX to PDF. Install/support ft_office_preview, "
            "or install python-docx and fpdf2."
        ) from e

    out_pdf = os.path.join(out_dir, Path(docx_path).stem + "_preview.pdf")
    doc = Document(docx_path)
    pdf = FPDF(unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)

    title = os.path.basename(docx_path)
    pdf.set_font("Helvetica", style="B", size=12)
    pdf.multi_cell(0, 6, title)
    pdf.ln(2)
    pdf.set_font("Helvetica", size=10)

    for para in doc.paragraphs:
        txt = para.text.strip()
        if not txt:
            pdf.ln(3)
            continue
        safe = txt.encode("latin-1", errors="replace").decode("latin-1")
        pdf.multi_cell(0, 5, safe)

    pdf.output(out_pdf)
    return out_pdf


def combine_pdfs(pdf_paths: Iterable[str], output_pdf: str) -> str:
    """Combine PDF files into output_pdf using PyMuPDF."""
    try:
        import fitz
    except Exception as e:
        raise RuntimeError("PyMuPDF is required to combine PDFs. Install with: pip install pymupdf") from e

    pdf_paths = _norm_existing_files(pdf_paths)
    if not pdf_paths:
        raise RuntimeError("No PDF files to combine.")

    out_doc = fitz.open()
    try:
        for p in pdf_paths:
            src = fitz.open(p)
            try:
                out_doc.insert_pdf(src)
            finally:
                src.close()
        out_doc.save(output_pdf)
    finally:
        out_doc.close()

    return output_pdf


def open_file(path: str) -> None:
    """Open file with the system default viewer."""
    if os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        import subprocess, sys
        if sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])


def print_documents_as_combined_pdf(files: Iterable[str], parent=None, title: str = "FT selected documents") -> dict:
    """Convert/concatenate PDF and DOCX files into one temporary PDF, then open it."""
    files = _norm_existing_files(files)
    docs = [f for f in files if os.path.splitext(f)[1].lower() in {".pdf", ".docx"}]
    if not docs:
        return {"ok": False, "path": "", "message": "No PDF or DOCX files selected."}

    try:
        temp_dir = tempfile.mkdtemp(prefix="FTPrint_")
        pdfs = []
        errors = []

        for f in docs:
            ext = os.path.splitext(f)[1].lower()
            try:
                if ext == ".pdf":
                    pdfs.append(f)
                elif ext == ".docx":
                    pdfs.append(_docx_to_pdf(f, temp_dir))
            except Exception as e:
                errors.append(f"{os.path.basename(f)}: {e}")

        if not pdfs:
            return {
                "ok": False,
                "path": "",
                "message": "No files could be prepared for printing.\n\n" + "\n".join(errors)
            }

        out_pdf = os.path.join(temp_dir, _timestamp_name("FTPrint_Selected_Documents"))
        combine_pdfs(pdfs, out_pdf)
        open_file(out_pdf)

        msg = f"Opened combined PDF:\n{out_pdf}"
        if errors:
            msg += "\n\nSome files could not be included:\n" + "\n".join(errors)
        return {"ok": True, "path": out_pdf, "message": msg}

    except Exception as e:
        return {
            "ok": False,
            "path": "",
            "message": str(e) + "\n\n" + traceback.format_exc()
        }


__all__ = [
    "combine_pdfs",
    "open_file",
    "print_documents_as_combined_pdf",
]
