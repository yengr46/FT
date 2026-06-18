"""ft_pdf_ops.py — PDF operations for the FileTagger suite.

Shared low-level PDF helpers.  These functions do not import tkinter and do not
know about FT/FTmod/FTView UI state.  They take an input PDF and write a new PDF
beside it by default.

Currently provided:
    convert_pdf_to_grayscale(input_pdf, output_pdf=None, zoom=2.0)
    convert_pdf_to_bw(input_pdf, output_pdf=None, zoom=2.0, threshold=128)

Both functions rasterise each page at a higher resolution, convert the page image,
and reinsert it into a same-sized PDF page.  Output is a new file; originals are
never modified.
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass
from typing import Optional, List

try:
    import fitz  # PyMuPDF
except Exception as exc:  # pragma: no cover - runtime dependency
    fitz = None
    _FITZ_IMPORT_ERROR = exc
else:
    _FITZ_IMPORT_ERROR = None

try:
    from PIL import Image
except Exception as exc:  # pragma: no cover - runtime dependency
    Image = None
    _PIL_IMPORT_ERROR = exc
else:
    _PIL_IMPORT_ERROR = None


@dataclass
class PdfConvertResult:
    input_path: str
    output_path: str
    pages: int
    mode: str


@dataclass
class PdfSplitResult:
    input_path: str
    output_paths: List[str]
    pages: int
    mode: str = "split"


def _require_dependencies() -> None:
    if fitz is None:
        raise RuntimeError(f"PyMuPDF is required for PDF conversion: {_FITZ_IMPORT_ERROR}")
    if Image is None:
        raise RuntimeError(f"Pillow is required for PDF conversion: {_PIL_IMPORT_ERROR}")


def _unique_output_path(input_pdf: str, suffix: str) -> str:
    folder = os.path.dirname(os.path.abspath(input_pdf))
    stem, _ = os.path.splitext(os.path.basename(input_pdf))
    candidate = os.path.join(folder, f"{stem}{suffix}.pdf")
    if not os.path.exists(candidate):
        return candidate
    n = 2
    while True:
        candidate = os.path.join(folder, f"{stem}{suffix}_{n}.pdf")
        if not os.path.exists(candidate):
            return candidate
        n += 1



def _unique_page_output_path(folder: str, stem: str, page_no: int) -> str:
    """Return a non-overwriting path like Stem-001.pdf, Stem-001_2.pdf."""
    candidate = os.path.join(folder, f"{stem}-{page_no:03d}.pdf")
    if not os.path.exists(candidate):
        return candidate
    n = 2
    while True:
        candidate = os.path.join(folder, f"{stem}-{page_no:03d}_{n}.pdf")
        if not os.path.exists(candidate):
            return candidate
        n += 1


def _pixmap_to_pil(pix):
    """Convert a PyMuPDF pixmap to a PIL RGB image."""
    if pix.alpha:
        return Image.frombytes("RGBA", [pix.width, pix.height], pix.samples).convert("RGB")
    if pix.n >= 4:
        # CMYK or other formats can appear in some PDFs. Convert through PNG bytes.
        png = pix.tobytes("png")
        return Image.open(io.BytesIO(png)).convert("RGB")
    return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)


def _image_to_png_bytes(img: "Image.Image") -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _convert_pdf(input_pdf: str, output_pdf: Optional[str], *, mode: str,
                 zoom: float = 2.0, threshold: int = 128) -> PdfConvertResult:
    _require_dependencies()

    input_pdf = os.path.abspath(input_pdf)
    if not os.path.isfile(input_pdf):
        raise FileNotFoundError(input_pdf)
    if os.path.splitext(input_pdf)[1].lower() != ".pdf":
        raise ValueError("Input file is not a PDF")

    output_pdf = os.path.abspath(output_pdf) if output_pdf else _unique_output_path(
        input_pdf, "_gray" if mode == "grayscale" else "_bw"
    )

    src = fitz.open(input_pdf)
    out = fitz.open()
    mat = fitz.Matrix(float(zoom), float(zoom))

    try:
        for page in src:
            rect = page.rect
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img = _pixmap_to_pil(pix)

            if mode == "grayscale":
                img = img.convert("L")
            elif mode == "bw":
                # True 1-bit black/white. Dithering gives a better result for scans/photos.
                gray = img.convert("L")
                img = gray.point(lambda x: 255 if x >= threshold else 0, mode="1")
            else:
                raise ValueError(f"Unsupported conversion mode: {mode}")

            png_bytes = _image_to_png_bytes(img)
            new_page = out.new_page(width=rect.width, height=rect.height)
            new_page.insert_image(rect, stream=png_bytes)

        if out.page_count == 0:
            raise RuntimeError("PDF contains no pages")

        os.makedirs(os.path.dirname(output_pdf), exist_ok=True)
        out.save(output_pdf, garbage=4, deflate=True, clean=True)
        return PdfConvertResult(
            input_path=input_pdf,
            output_path=output_pdf,
            pages=out.page_count,
            mode=mode,
        )
    finally:
        out.close()
        src.close()


def convert_pdf_to_grayscale(input_pdf: str, output_pdf: Optional[str] = None,
                             *, zoom: float = 2.0) -> PdfConvertResult:
    """Create a grayscale copy of input_pdf and return conversion details."""
    return _convert_pdf(input_pdf, output_pdf, mode="grayscale", zoom=zoom)


def convert_pdf_to_bw(input_pdf: str, output_pdf: Optional[str] = None,
                      *, zoom: float = 2.0, threshold: int = 128) -> PdfConvertResult:
    """Create a true black/white copy of input_pdf and return conversion details."""
    return _convert_pdf(input_pdf, output_pdf, mode="bw", zoom=zoom, threshold=threshold)


def split_pdf_to_single_pages(input_pdf: str, output_folder: Optional[str] = None) -> PdfSplitResult:
    """Split input_pdf into one PDF per page.

    Output files are written beside the source PDF by default using:
        Document-001.pdf
        Document-002.pdf

    Existing files are never overwritten.
    """
    if fitz is None:
        raise RuntimeError(f"PyMuPDF is required for PDF splitting: {_FITZ_IMPORT_ERROR}")

    input_pdf = os.path.abspath(input_pdf)
    if not os.path.isfile(input_pdf):
        raise FileNotFoundError(input_pdf)
    if os.path.splitext(input_pdf)[1].lower() != ".pdf":
        raise ValueError("Input file is not a PDF")

    folder = os.path.abspath(output_folder) if output_folder else os.path.dirname(input_pdf)
    os.makedirs(folder, exist_ok=True)
    stem, _ = os.path.splitext(os.path.basename(input_pdf))

    output_paths = []
    src = fitz.open(input_pdf)
    try:
        if src.page_count <= 0:
            raise RuntimeError("PDF contains no pages")

        for page_idx in range(src.page_count):
            out_path = _unique_page_output_path(folder, stem, page_idx + 1)
            out = fitz.open()
            try:
                out.insert_pdf(src, from_page=page_idx, to_page=page_idx)
                out.save(out_path, garbage=4, deflate=True, clean=True)
            finally:
                out.close()
            output_paths.append(out_path)

        return PdfSplitResult(
            input_path=input_pdf,
            output_paths=output_paths,
            pages=len(output_paths),
            mode="split",
        )
    finally:
        src.close()

