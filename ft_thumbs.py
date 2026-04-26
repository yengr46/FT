"""
ft_thumbs.py — shared thumbnail utilities for FileTagger apps.

Phase T2:
- centralises thumbnail image preparation utilities
- keeps behaviour compatible with existing FT.py
- avoids owning the SQLite cache yet; FT.py still owns DB persistence
"""

from __future__ import annotations

from io import BytesIO
from typing import Optional, Tuple

from PIL import Image, ImageOps, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True


def scale_to_fit(img: Image.Image, sz: int) -> Image.Image:
    """Scale image to fit inside sz x sz, preserving aspect ratio."""
    w, h = img.size
    if w == 0 or h == 0:
        return img
    scale = min(sz / w, sz / h)
    new_w = max(1, int(w * scale))
    new_h = max(1, int(h * scale))
    if new_w == w and new_h == h:
        return img
    return img.resize((new_w, new_h), Image.BILINEAR)


def make_placeholder(sz: int, ghost: bool = False) -> Image.Image:
    """Create a neutral placeholder thumbnail."""
    col = (30, 0, 0) if ghost else (50, 50, 50)
    return Image.new("RGB", (sz, sz), col)


def open_image_rgb(path: str, longpath_func=None) -> Image.Image:
    """Open an image path, apply EXIF orientation and return RGB image.

    longpath_func may be FT.py's _longpath on Windows.
    """
    p = longpath_func(path) if longpath_func else path
    img = Image.open(p)
    img = ImageOps.exif_transpose(img)
    return img.convert("RGB")


def make_thumb_from_image(img: Image.Image, sz: int) -> Image.Image:
    """Return a fitted RGB thumbnail image from an already-open PIL image."""
    if img.mode != "RGB":
        img = img.convert("RGB")
    return scale_to_fit(img, sz)


def make_thumb_from_path(path: str, sz: int, longpath_func=None) -> Image.Image:
    """Open path and return a fitted RGB thumbnail image."""
    img = open_image_rgb(path, longpath_func=longpath_func)
    return make_thumb_from_image(img, sz)


def image_to_jpeg_bytes(img: Image.Image, quality: int = 85) -> bytes:
    """Encode a PIL image as JPEG bytes."""
    if img.mode != "RGB":
        img = img.convert("RGB")
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def jpeg_bytes_to_image(data: bytes) -> Image.Image:
    """Decode JPEG bytes to a RGB PIL image."""
    return Image.open(BytesIO(data)).convert("RGB")


def make_thumb_jpeg_from_path(path: str, sz: int, longpath_func=None, quality: int = 85) -> bytes:
    """Open path, make thumbnail, return JPEG bytes."""
    return image_to_jpeg_bytes(make_thumb_from_path(path, sz, longpath_func=longpath_func), quality=quality)


def fit_text(text: str, max_px: int, font_spec=("Segoe UI", 9)) -> str:
    """Return text truncated with ellipsis so it fits within max_px pixels.

    Kept here so other apps can use the same thumbnail label fitting behaviour.
    """
    try:
        import tkinter.font as tkfont
        f = tkfont.Font(family=font_spec[0], size=font_spec[1])
        if f.measure(text) <= max_px:
            return text
        lo, hi = 1, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if f.measure(text[:mid] + "…") <= max_px:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo] + "…"
    except Exception:
        max_chars = max(1, max_px // 7)
        return text if len(text) <= max_chars else text[:max_chars - 1] + "…"


def build_decorations(*, selected=False, culled=False, edited=False, unreadable=False,
                      gps=False, group_badge=None, count_badge=None) -> dict:
    """Return a simple decoration dictionary for thumbnail renderers.

    This does not draw anything yet; it gives FT and future helper apps a common
    vocabulary for thumbnail overlays/badges.
    """
    watermark = None
    if culled:
        watermark = "DELETING"
    elif selected:
        watermark = "SELECTED"

    return {
        "watermark": watermark,
        "edited": bool(edited),
        "unreadable": bool(unreadable),
        "gps_badge": bool(gps),
        "group_badge": group_badge,
        "count_badge": count_badge,
    }
