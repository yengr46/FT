"""ft_contactsheet.py — contact sheet dialog and PDF generation for FileTagger.

Extracted from FT.py in FT43.
This module owns only contact-sheet UI/generation code.
"""

from __future__ import annotations

import os
import ntpath
import threading
from datetime import datetime, timezone, timedelta
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None
import tkinter as tk
from tkinter import messagebox

from PIL import Image

try:
    from ft_gps import _get_gps_coords
except Exception:
    def _get_gps_coords(path):
        return None



def _parent(app):
    """Return the Tk parent used by FTmod and FTView."""
    return getattr(app, "win", None) or app


def _norm_path(path):
    if not path:
        return ""
    path = str(path)
    if path.startswith("\\\\?\\"):
        path = path[4:]
    return os.path.normpath(path)


def _path_contains(root, path):
    """True if path is at or below root, using Windows-safe comparisons."""
    if not root or not path:
        return False
    try:
        root_n = _norm_path(root)
        path_n = _norm_path(path)
        if os.name == "nt":
            root_k = os.path.normcase(root_n)
            path_k = os.path.normcase(path_n)
        else:
            root_k = os.path.abspath(root_n)
            path_k = os.path.abspath(path_n)
        try:
            common = os.path.commonpath([root_k, path_k])
            return common == root_k
        except Exception:
            return path_k == root_k or path_k.startswith(root_k.rstrip("\\/") + os.sep)
    except Exception:
        return False


def _candidate_roots_from_app(app):
    """Return every plausible configured root exposed by FTView/FTmod/FT."""
    out = []

    def add(value):
        try:
            if value:
                value = _norm_path(value)
                if value and value not in out:
                    out.append(value)
        except Exception:
            pass

    # Explicit hook first, but it is not blindly trusted: later we choose the
    # broadest configured root containing the selected/displayed files.
    fn = getattr(app, "_ft_contact_sheet_root_func", None)
    if callable(fn):
        try:
            add(fn())
        except Exception:
            pass

    # FTmod / FT style.
    try:
        cfg = getattr(app, "mode_cfg", None)
        if isinstance(cfg, dict):
            add(cfg.get("root", ""))
    except Exception:
        pass

    # FTView style.
    try:
        rv = getattr(app, "root_var", None)
        if rv is not None:
            add(rv.get().strip())
    except Exception:
        pass
    try:
        roots_by_mode = getattr(app, "roots_by_mode", None) or {}
        mode = getattr(app, "mode", "photos")
        add(roots_by_mode.get(mode, ""))
        for v in roots_by_mode.values():
            add(v)
    except Exception:
        pass

    # FTmod module globals (PHOTOS_ROOTS/PDFS_ROOTS/PHOTOS_ROOT/PDFS_ROOT) are
    # not attributes on the instance, so inspect the app's defining module.
    try:
        import sys as _sys
        mod = _sys.modules.get(app.__class__.__module__)
        if mod is not None:
            mode = getattr(app, "mode", "photos")
            if mode == "pdfs":
                add(getattr(mod, "PDFS_ROOT", ""))
                for item in getattr(mod, "PDFS_ROOTS", []) or []:
                    add(item[0] if isinstance(item, (tuple, list)) else item)
            else:
                add(getattr(mod, "PHOTOS_ROOT", ""))
                for item in getattr(mod, "PHOTOS_ROOTS", []) or []:
                    add(item[0] if isinstance(item, (tuple, list)) else item)
            # Add both sets as fallbacks, because collections/selected files can
            # be passed in while mode state is still settling.
            add(getattr(mod, "PHOTOS_ROOT", ""))
            add(getattr(mod, "PDFS_ROOT", ""))
            for name in ("PHOTOS_ROOTS", "PDFS_ROOTS"):
                for item in getattr(mod, name, []) or []:
                    add(item[0] if isinstance(item, (tuple, list)) else item)
    except Exception:
        pass

    return out


def _safe_contact_base(path):
    """Return path only if it is a safe contact-sheet base.

    Rejects broad Windows user container folders and drive roots.
    Contact sheets should be written under a configured root or the currently
    displayed source folder.
    """
    try:
        if not path:
            return ""
        path = _norm_path(path)
        if not os.path.isdir(path):
            return ""
        drive, tail = ntpath.splitdrive(path)
        tail_parts = [part for part in tail.replace("/", "\\").split("\\") if part]
        if drive and len(tail_parts) == 0:
            return ""
        if drive and len(tail_parts) == 1 and tail_parts[0].lower() == "users":
            return ""
        return path
    except Exception:
        return ""


def _current_folder_from_app(app, files=None):
    """Best-effort source folder currently being displayed in FTView/FTMain."""
    try:
        files = [f for f in (files or []) if f]
        if files:
            dirs = []
            for f in files:
                f = _norm_path(f)
                d = f if os.path.isdir(f) else os.path.dirname(f)
                if d and os.path.isdir(d):
                    dirs.append(d)
            if dirs:
                try:
                    common = os.path.commonpath(dirs)
                    common = _safe_contact_base(common)
                    if common:
                        return common
                except Exception:
                    pass
                first = _safe_contact_base(dirs[0])
                if first:
                    return first
    except Exception:
        pass

    for name in (
        "current_folder", "current_dir", "folder", "selected_folder",
        "active_folder", "current_path", "folder_path"
    ):
        try:
            v = getattr(app, name, None)
            if callable(v):
                v = v()
            v = _safe_contact_base(v)
            if v:
                return v
        except Exception:
            pass

    for name in ("folder_var", "current_folder_var", "path_var"):
        try:
            v = getattr(app, name, None)
            if v is not None and hasattr(v, "get"):
                v = _safe_contact_base(v.get())
                if v:
                    return v
        except Exception:
            pass

    return ""


def _infer_root_from_files(files):
    """Infer the FT library root from source files.

    For Ian's FT layout, source files such as:
        S:\\Photos\\School Photos\\Ian\\x.jpg
    must save contact sheets under:
        S:\\Photos\\_ContactSheets

    That means the library root is the drive plus the first path component,
    not the current folder and not the folder containing the selected files.
    """
    try:
        first = _norm_path(next((f for f in files or [] if f), ""))
        if not first:
            return ""

        # Use ntpath for Windows-style paths even if this helper is inspected
        # on a non-Windows machine.  On the user's Windows system this produces
        # the same result, and it avoids S:\\ paths being misread as relative.
        drive, tail = ntpath.splitdrive(first)
        if drive:
            parts = [part for part in tail.replace("/", "\\").split("\\") if part]
            if parts:
                return ntpath.normpath(drive + "\\" + parts[0])
            return ntpath.normpath(drive + "\\")

        # Non-Windows / UNC fallback.
        drive, tail = os.path.splitdrive(first)
        parts = [part for part in tail.replace("/", os.sep).split(os.sep) if part]
        if drive and parts:
            return os.path.normpath(drive + os.sep + parts[0])
        if parts:
            return os.path.normpath(os.path.sep + parts[0])
    except Exception:
        pass
    return ""


def _active_mode_root(app, files=None):
    """Return the safest contact-sheet output base.

    Preferred order:
      1. configured root that contains the displayed/selected files
      2. configured active root
      3. current displayed source folder
      4. safe inferred folder from source files

    It must never return broad user folders such as the Windows Users folder.
    """
    files = [_norm_path(f) for f in (files or []) if f]
    candidates = [_safe_contact_base(r) for r in _candidate_roots_from_app(app)]
    candidates = [r for r in candidates if r]

    if files and candidates:
        containing = []
        for root in candidates:
            try:
                if any(_path_contains(root, f) for f in files):
                    containing.append(root)
            except Exception:
                pass
        if containing:
            return sorted(containing, key=lambda r: len(_norm_path(r)), reverse=True)[0]

    if candidates:
        return candidates[0]

    current = _current_folder_from_app(app, files=files)
    if current:
        return current

    inferred = _safe_contact_base(_infer_root_from_files(files))
    if inferred:
        return inferred

    return ""

def _call_output_dir(app, files=None):
    """Return a safe _ContactSheets output folder.

    The folder is based on the configured root or current displayed folder.
    It never falls back to the Windows Users folder, a drive root, or the program install folder.
    """
    root = _active_mode_root(app, files=files)
    root = _safe_contact_base(root)

    if not root:
        raise RuntimeError(
            "Cannot create contact sheet: no valid Photos/Documents root or current folder is available."
        )

    d = os.path.join(root, "_ContactSheets")
    os.makedirs(d, exist_ok=True)
    return d

def _force_contact_sheet_path(app, files, requested_path):
    """Force generated sheets into <mode root>\\_ContactSheets.

    The dialog's Save As field may have stale text from older builds or from a
    current-folder default.  Generation always rewrites the directory to the
    active mode root while preserving the filename.
    """
    out_dir = _call_output_dir(app, files=files)
    raw_path = str(requested_path or "").strip()
    # Use ntpath as well because Windows-style paths may be handled while the
    # code is being tested on a non-Windows machine.
    name = ntpath.basename(raw_path) or os.path.basename(raw_path)
    if not name:
        name = "ContactSheet.pdf"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return os.path.join(out_dir, name)


def _aest_now():
    """Current Australia/Brisbane time for contact-sheet filenames.

    Windows Python often lacks the IANA timezone database unless tzdata is
    installed, so use ZoneInfo when available and otherwise fall back to a fixed
    UTC+10 AEST offset.  Brisbane does not use daylight saving.
    """
    try:
        if ZoneInfo is not None:
            return datetime.now(ZoneInfo("Australia/Brisbane"))
    except Exception:
        pass
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=10), "AEST"))


def _safe_filename_part(text, default="ContactSheet"):
    text = str(text or "").strip() or default
    text = text.replace(" ", "_")
    bad = r'\/:*?"<>|'
    for ch in bad:
        text = text.replace(ch, "_")
    # Keep readable but avoid absurd path lengths.
    while "__" in text:
        text = text.replace("__", "_")
    return text.strip("._ ")[:80] or default


def _layout_name(cols, panel_shape):
    return f"{int(cols)}col_{_safe_filename_part(panel_shape, 'Portrait')}Panels"


def _app_window(app):
    """Return a concrete Tk window for dialogs/progress windows."""
    return getattr(app, "win", None) or app


def _longpath_for(app, path):
    fn = getattr(app, "_ft_longpath_func", None)
    if callable(fn):
        try:
            return fn(path)
        except Exception:
            pass
    return path


def _thumb_bytes_for(app, path):
    fn = getattr(app, "_ft_thumb_get_func", None)
    if callable(fn):
        try:
            return fn(path)
        except Exception:
            return None
    return None



def _photo_date_taken(path):
    """Return Date Taken for photo if available, otherwise modified date."""
    try:
        from PIL import Image as _PILImage
        img = _PILImage.open(path)
        exif = img.getexif()
        # 36867 = DateTimeOriginal, 306 = DateTime
        raw = exif.get(36867) or exif.get(306)
        if raw:
            # EXIF format: YYYY:MM:DD HH:MM:SS
            raw = str(raw).strip()
            if len(raw) >= 10:
                return raw[:10].replace(":", "-")
    except Exception:
        pass
    try:
        from datetime import datetime as _DT2
        return _DT2.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _doc_modified_date(path):
    """Return file modified date for PDF/DOCX."""
    try:
        from datetime import datetime as _DT2
        return _DT2.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _contact_caption_parts(app, path):
    """Return (filename, yyyy-mm-dd) for a contact sheet caption.

    The date is deliberately returned separately so it can be drawn at the
    right-hand end of the caption line and never lost when filenames are long.
    Photos use EXIF Date Taken when available, while PDF/DOCX documents use
    the file modified date as the scanned/document date.
    """
    fname = os.path.basename(path)
    ext = os.path.splitext(path)[1].lower()
    date_txt = ""

    try:
        if ext in (".pdf", ".docx"):
            date_txt = _doc_modified_date(path)
        elif ext in (".jpg", ".jpeg"):
            date_txt = _photo_date_taken(_longpath_for(app, path))
    except Exception:
        date_txt = ""

    return fname, date_txt


def _pdf_fit_text(pdf, text, max_w, font="Helvetica", style="", size=7):
    """Trim text with ellipsis to fit max_w."""
    text = str(text or "").encode("latin-1", errors="replace").decode("latin-1")
    pdf.set_font(font, style, size=size)
    if pdf.get_string_width(text) <= max_w:
        return text
    ell = "..."
    while len(text) > 1 and pdf.get_string_width(text + ell) > max_w:
        text = text[:-1]
    return (text + ell) if text else ell


def _pdf_fit_filename(pdf, filename, max_w, font="Helvetica", style="", size=7):
    """Fit a filename into max_w, truncating the name before the extension.

    Example: '2026-04-10 MGEN Documentation with data.docx' becomes
    '2026-04-10 MGEN Documentation with da..docx' rather than hiding
    the extension or pushing the date off the right edge.
    """
    filename = str(filename or "").encode("latin-1", errors="replace").decode("latin-1")
    pdf.set_font(font, style, size=size)
    if max_w <= 0:
        return ""
    if pdf.get_string_width(filename) <= max_w:
        return filename

    stem, ext = os.path.splitext(filename)
    suffix = ".." + ext if ext else ".."
    if pdf.get_string_width(suffix) > max_w:
        return _pdf_fit_text(pdf, suffix, max_w, font, style, size)

    lo, hi = 0, len(stem)
    best = suffix
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = stem[:mid].rstrip() + suffix
        if pdf.get_string_width(candidate) <= max_w:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _displayed_files(app):
    """Files currently visible/loaded in FTmod or FTView, in display order."""
    for name in ("_all_files", "files", "all_files"):
        try:
            files = list(getattr(app, name, []) or [])
            if files:
                return files
        except Exception:
            pass
    return []


def _selected_files(app):
    """Selected files in FTmod or FTView."""
    for name in ("_selected", "selected_files"):
        try:
            selected = list(getattr(app, name, []) or [])
            if selected:
                order = {p: i for i, p in enumerate(_displayed_files(app))}
                selected.sort(key=lambda p: order.get(p, 10**9))
                return selected
        except Exception:
            pass
    return []


def _contact_source_files(app):
    """Return contact-sheet source files. Prefer selected, then FT tagged, then displayed."""
    selected = _selected_files(app)
    if selected:
        return selected
    try:
        tagged = list(getattr(app, "tagged", []) or [])
        if tagged:
            return sorted(tagged, key=contact_sheet_sort_key(app))
    except Exception:
        pass
    return _displayed_files(app)


def _default_collection_name(app):
    name = getattr(app, "collection", "") or ""
    if name:
        return name
    folder = getattr(app, "current_folder", "") or ""
    if folder:
        return os.path.basename(folder.rstrip("/\\")) or "Contact Sheet"
    return "Contact Sheet"

# Light-theme defaults used by the current FileTagger UI.
BG2 = "#dddddd"
BG3 = "#dddddd"
TEXT_BRIGHT = "#111111"
HOVER_BD = "#888888"
GREEN = "#27ae60"
ACCENT = "#4f8ef7"
TEXT_DIM = "#444444"


def _contactsheet_fit_grid(page_w_mm, page_h_mm, margin_mm, header_mm, gap_mm,
                           cols, rows, panel_ratio, line_h_mm=3.5, shrink_tol=0.05):
    """Return (rows_per_page, cell_w_mm, cell_h_mm) for contact-sheet layout.

    The same helper is used by the preview and PDF generator.  If rows is 0,
    rows are chosen automatically.  Auto mode allows cells/panels to shrink by
    up to shrink_tol (5% by default) when that lets one more row fit on the page.
    """
    try:
        cols = max(1, int(cols or 1))
    except Exception:
        cols = 1
    try:
        rows = max(0, int(rows or 0))
    except Exception:
        rows = 0
    try:
        panel_ratio = float(panel_ratio)
    except Exception:
        panel_ratio = 1.33
    panel_ratio = max(0.05, panel_ratio)
    gap_mm = max(0.0, float(gap_mm or 0.0))
    margin_mm = max(0.0, float(margin_mm or 0.0))
    header_mm = max(0.0, float(header_mm or 0.0))
    line_h_mm = max(0.0, float(line_h_mm or 0.0))
    shrink_tol = max(0.0, float(shrink_tol or 0.0))

    usable_w = max(1.0, float(page_w_mm) - 2.0 * margin_mm)
    usable_h = max(1.0, float(page_h_mm) - 2.0 * margin_mm - header_mm - gap_mm)

    cell_w = max(1.0, (usable_w - gap_mm * (cols - 1)) / cols)

    def height_for_width(width):
        # Cell contains image/panel area plus one caption/date line.
        return max(1.0, width * panel_ratio + line_h_mm)

    natural_cell_h = height_for_width(cell_w)

    if rows > 0:
        # Manual rows: reduce height to what the page can actually provide,
        # matching the visible behaviour where forced 2x2 shrinks slightly.
        forced_h = max(1.0, (usable_h - gap_mm * (rows - 1)) / rows)
        return rows, cell_w, min(natural_cell_h, forced_h)

    # Auto rows: start with natural size, then try to gain one extra row if it
    # can be done by shrinking height/width within the permitted tolerance.
    natural_rows = max(1, int((usable_h + gap_mm) // (natural_cell_h + gap_mm)))

    candidate = natural_rows + 1
    candidate_h = max(1.0, (usable_h - gap_mm * (candidate - 1)) / candidate)
    if candidate_h >= natural_cell_h * (1.0 - shrink_tol):
        return candidate, cell_w, candidate_h

    return natural_rows, cell_w, natural_cell_h

def ops_contact_sheet(app):
    """Contact Sheet from current FTmod/FTView selection or visible files.

    If the application supplies its own thin wrapper, use it so app-specific
    hooks such as output folder, long-path handling, and thumbnail DB lookup
    are installed before the shared dialog runs.
    """
    wrapper = getattr(app, "_contact_sheet_dialog", None)
    if callable(wrapper):
        return wrapper()
    return contact_sheet_dialog(app)

def contact_sheet_sort_key(app):
    """Return the correct sort key function for contact sheet file ordering."""
    if app.mode == "photos":
        return lambda path: os.path.basename(path).lower()
    else:
        import re as _re
        _date_pat = _re.compile(r'^\d{4}-\d{2}-\d{2}')
        def _key(path):
            fname = os.path.basename(path)
            if fname.lower().startswith('scan'):
                return (0, fname.lower())
            m = _date_pat.match(fname)
            if m:
                return (1, tuple(~ord(c) for c in m.group(0)))
            try:    mtime = os.path.getmtime(path)
            except: mtime = 0.0
            return (2, -mtime)
        return _key


def contact_sheet_dialog(app):
    """Contact sheet settings dialog with preview of first page layout.

    Shared by FTmod and FTView.  It uses selected files when present,
    otherwise FT tagged files, otherwise the currently displayed file list.
    """
    parent = _parent(app)
    try:
        app._contact_sheet_output_manually_changed = False
    except Exception:
        pass
    files = [f for f in _contact_source_files(app) if f and os.path.isfile(f)]
    if not files:
        messagebox.showinfo("Contact Sheet",
            "No selected, tagged, or displayed files are available.", parent=parent)
        return

    n_files  = len(files)
    cols_var = tk.IntVar(value=6)
    rows_var = tk.IntVar(value=0)   # 0 = auto
    orient_var = tk.StringVar(value="Portrait")
    # Panel shape is independent of page orientation.  Default to the old
    # portrait-style panels: on A4 portrait, 2 columns auto-fits 2 rows
    # (4 images per page) with tall image frames.
    panel_var = tk.StringVar(value="Portrait")
    rotate_to_panel_var = tk.BooleanVar(value=False)  # rotate-to-fit selected panel shape

    sheet_name = _default_collection_name(app)
    contact_dir  = _call_output_dir(app, files=files)
    title_var = tk.StringVar(value=sheet_name)

    def _safe_int(var, default):
        try:
            value = str(var.get()).strip()
            if value == "":
                return default
            return int(value)
        except Exception:
            return default

    def _make_default_filename():
        stamp = _aest_now().strftime("%Y-%m-%d_%H-%M")
        source = _safe_filename_part(title_var.get() or sheet_name, "ContactSheet")
        layout = _layout_name(max(1, _safe_int(cols_var, 6)), panel_var.get())
        return f"{stamp}_{source}_{layout}.pdf"

    out_var   = tk.StringVar(value=os.path.join(contact_dir, _make_default_filename()))

    # ── Inner functions — defined first so widgets can reference them ──────

    def _panel_ratio():
        # height / width ratio of the image frame within each contact-sheet cell.
        # Portrait restores the old style: two columns on A4 portrait gives
        # two rows / four large portrait panels per page.
        return 1.33 if panel_var.get() == "Portrait" else 0.68

    def _calc(landscape, cols, rows):
        page_w = 297.0 if landscape else 210.0
        page_h = 210.0 if landscape else 297.0
        rows_pp, cell_w_mm, cell_h_mm = _contactsheet_fit_grid(
            page_w, page_h, margin_mm=10.0, header_mm=8.0, gap_mm=1.5,
            cols=cols, rows=rows, panel_ratio=_panel_ratio(),
            line_h_mm=3.5, shrink_tol=0.05)
        cells_pp  = cols * rows_pp
        n_pages   = max(1, -(-n_files // cells_pp))
        return n_pages, rows_pp, cell_w_mm, cell_h_mm


    def _update(*_):
        landscape = orient_var.get() == "Landscape"
        cols = max(1, _safe_int(cols_var, 6))
        rows = max(0, _safe_int(rows_var, 0))
        n_pages, rpp, cw, ch = _calc(landscape, cols, rows)
        # Keep the default filename aligned with Title/Columns/Panel shape until
        # the user manually browses/types a different path.
        try:
            if not getattr(app, "_contact_sheet_output_manually_changed", False):
                out_var.set(os.path.join(contact_dir, _make_default_filename()))
        except Exception:
            pass
        info_lbl.config(text=
            f"{n_files} images  —  {cols} cols × {rpp} rows  —  {n_pages} page{'s' if n_pages!=1 else ''}")
        _draw_preview(landscape, cols, rpp, cw, ch, n_pages)

    def _draw_preview(landscape, cols, rows_pp, cell_w_mm, cell_h_mm, n_pages):
        """Draw first-page grid layout on the preview canvas."""
        prev_canvas.update_idletasks()
        cw = prev_canvas.winfo_width()
        ch = prev_canvas.winfo_height()
        if cw < 10 or ch < 10: return
        prev_canvas.delete("all")

        # Page proportions
        if landscape:
            ratio = 297.0 / 210.0
        else:
            ratio = 210.0 / 297.0

        # Fit page into canvas with margin
        pad = 8
        if cw / ch > ratio:
            ph = ch - pad * 2
            pw = ph * ratio
        else:
            pw = cw - pad * 2
            ph = pw / ratio
        px = (cw - pw) / 2
        py = (ch - ph) / 2

        # Page bg
        prev_canvas.create_rectangle(px, py, px+pw, py+ph,
                                     fill="white", outline="#aaaaaa", width=1)

        # Header band
        hdr_h = ph * 0.04
        prev_canvas.create_rectangle(px, py, px+pw, py+hdr_h,
                                     fill="#eeeeee", outline="")
        prev_canvas.create_text(px + pw/2, py + hdr_h/2,
                                text=f"{title_var.get()}  —  Page 1 of {n_pages}",
                                fill="#333", font=("Segoe UI", 7), anchor="center")

        # Grid cells
        margin_frac = 0.03
        mx = pw * margin_frac
        my = hdr_h + ph * 0.01
        grid_w = pw - mx * 2
        grid_h = ph - my - ph * margin_frac
        cell_w = grid_w / cols
        cell_h = grid_h / rows_pp if rows_pp else grid_h

        for r in range(rows_pp):
            for c in range(cols):
                x0 = px + mx + c * cell_w
                y0 = py + my + r * cell_h
                x1 = x0 + cell_w - 1
                y1 = y0 + cell_h - 1
                # Cell border
                prev_canvas.create_rectangle(x0, y0, x1, y1,
                                             fill="#f8f8f8", outline="#cccccc")
                # Seq line at top
                seq_h = cell_h * 0.12
                prev_canvas.create_rectangle(x0, y0, x1, y0+seq_h,
                                             fill="#e0e0e0", outline="")
                # Image panel area, shaped independently from page orientation
                slot_y = y0 + seq_h
                slot_h = max(1, cell_h - seq_h * 2)
                slot_w = max(1, cell_w - 2)
                ratio_h_w = _panel_ratio()
                if slot_h / slot_w >= ratio_h_w:
                    frame_w = slot_w
                    frame_h = slot_w * ratio_h_w
                else:
                    frame_h = slot_h
                    frame_w = slot_h / ratio_h_w
                fx0 = x0 + 1 + (slot_w - frame_w) / 2
                fy0 = slot_y + (slot_h - frame_h) / 2
                prev_canvas.create_rectangle(fx0, fy0, fx0+frame_w, fy0+frame_h,
                                             fill="#cccccc", outline="")
                # Filename line at bottom
                prev_canvas.create_rectangle(x0, y0+seq_h+slot_h, x1, y1,
                                             fill="#eeeeee", outline="")

    # ── Dialog ────────────────────────────────────────────────────────────
    dlg = tk.Toplevel(parent)
    dlg.title("Contact Sheet")
    dlg.configure(bg=BG3)
    dlg.resizable(True, True)
    dlg.transient(parent)
    parent.update_idletasks()
    dlg_w, dlg_h = 700, 700
    x = parent.winfo_rootx() + (parent.winfo_width()  - dlg_w) // 2
    y = parent.winfo_rooty() + (parent.winfo_height() - dlg_h) // 2
    dlg.geometry(f"{dlg_w}x{dlg_h}+{x}+{y}")

    tk.Label(dlg, text="Generate Contact Sheet", bg=BG3, fg=TEXT_BRIGHT,
             font=("Segoe UI",12,"bold")).pack(pady=(12,4))

    # ── Controls ─────────────────────────────────────────────────────────
    ctrl = tk.Frame(dlg, bg=BG3); ctrl.pack(fill="x", padx=16, pady=4)

    tk.Label(ctrl, text="Title:", bg=BG3, fg=TEXT_BRIGHT,
             font=("Segoe UI",9)).grid(row=0, column=0, sticky="e", padx=(0,4), pady=3)
    tk.Entry(ctrl, textvariable=title_var, bg=BG2, fg=TEXT_BRIGHT,
             font=("Segoe UI",9), relief="flat",
             highlightthickness=1, highlightbackground=HOVER_BD,
             width=32).grid(row=0, column=1, columnspan=5, sticky="ew", pady=3)

    tk.Label(ctrl, text="Columns:", bg=BG3, fg=TEXT_BRIGHT,
             font=("Segoe UI",9)).grid(row=1, column=0, sticky="e", padx=(0,4), pady=3)
    tk.Spinbox(ctrl, from_=1, to=20, textvariable=cols_var, width=4,
               bg=BG2, fg=TEXT_BRIGHT, buttonbackground=BG2,
               font=("Segoe UI",9), command=_update).grid(
                   row=1, column=1, sticky="w", pady=3)

    tk.Label(ctrl, text="Rows:", bg=BG3, fg=TEXT_BRIGHT,
             font=("Segoe UI",9)).grid(row=1, column=2, sticky="e", padx=(12,4), pady=3)
    tk.Spinbox(ctrl, from_=0, to=30, textvariable=rows_var, width=4,
               bg=BG2, fg=TEXT_BRIGHT, buttonbackground=BG2,
               font=("Segoe UI",9), command=_update).grid(
                   row=1, column=3, sticky="w", pady=3)
    tk.Label(ctrl, text="(0=auto)", bg=BG3, fg=TEXT_DIM,
             font=("Segoe UI",8)).grid(row=1, column=4, sticky="w", padx=2)

    tk.Label(ctrl, text="Orientation:", bg=BG3, fg=TEXT_BRIGHT,
             font=("Segoe UI",9)).grid(row=2, column=0, sticky="e", padx=(0,4), pady=3)
    or_fr = tk.Frame(ctrl, bg=BG3)
    or_fr.grid(row=2, column=1, columnspan=5, sticky="w")
    for txt in ("Portrait", "Landscape"):
        tk.Radiobutton(or_fr, text=txt, variable=orient_var, value=txt,
                       bg=BG3, fg=TEXT_BRIGHT, selectcolor=BG2,
                       activebackground=BG3, font=("Segoe UI",9),
                       command=_update).pack(side="left", padx=(0,12))

    tk.Label(ctrl, text="Panel shape:", bg=BG3, fg=TEXT_BRIGHT,
             font=("Segoe UI",9)).grid(row=3, column=0, sticky="e", padx=(0,4), pady=3)
    panel_fr = tk.Frame(ctrl, bg=BG3)
    panel_fr.grid(row=3, column=1, columnspan=5, sticky="w")
    for txt in ("Portrait", "Landscape"):
        tk.Radiobutton(panel_fr, text=txt, variable=panel_var, value=txt,
                       bg=BG3, fg=TEXT_BRIGHT, selectcolor=BG2,
                       activebackground=BG3, font=("Segoe UI",9),
                       command=_update).pack(side="left", padx=(0,12))

    tk.Checkbutton(ctrl, text="Rotate images to better fit selected panel shape",
                   variable=rotate_to_panel_var, bg=BG3, fg=TEXT_BRIGHT,
                   selectcolor=BG2, activebackground=BG3,
                   font=("Segoe UI",9), command=_update).grid(
                       row=4, column=1, columnspan=5, sticky="w", pady=(2, 3))
    ctrl.columnconfigure(1, weight=1)

    # Info line
    info_lbl = tk.Label(dlg, text="", bg=BG3, fg=TEXT_BRIGHT,
                        font=("Segoe UI",9))
    info_lbl.pack()

    # ── Preview canvas ────────────────────────────────────────────────────
    prev_frame = tk.Frame(dlg, bg="#333", highlightthickness=1,
                          highlightbackground="#555")
    prev_frame.pack(fill="both", expand=True, padx=16, pady=4)
    prev_canvas = tk.Canvas(prev_frame, bg="#444", highlightthickness=0)
    prev_canvas.pack(fill="both", expand=True)
    prev_canvas.bind("<Configure>", lambda e: _update())

    # ── Save As row ───────────────────────────────────────────────────────
    save_fr = tk.Frame(dlg, bg=BG3); save_fr.pack(fill="x", padx=16, pady=(4,2))
    tk.Label(save_fr, text="Save to:", bg=BG3, fg=TEXT_BRIGHT,
             font=("Segoe UI",9)).pack(side="left", padx=(0,4))
    tk.Entry(save_fr, textvariable=out_var, bg=BG2, fg=TEXT_BRIGHT,
             font=("Segoe UI",8), relief="flat",
             highlightthickness=1, highlightbackground=HOVER_BD).pack(
                 side="left", fill="x", expand=True, padx=(0,4))
    def _browse_save():
        import tkinter.filedialog as _fd
        p = _fd.asksaveasfilename(parent=dlg,
            defaultextension=".pdf", filetypes=[("PDF","*.pdf")],
            initialdir=os.path.dirname(out_var.get()),
            initialfile=os.path.basename(out_var.get()))
        if p:
            try: app._contact_sheet_output_manually_changed = True
            except Exception: pass
            out_var.set(p)
    tk.Button(save_fr, text="...", bg=ACCENT, fg="white",
              font=("Segoe UI",9,"bold"), relief="flat", padx=6,
              cursor="hand2", command=_browse_save).pack(side="left")

    # ── Buttons ───────────────────────────────────────────────────────────
    btn_fr = tk.Frame(dlg, bg=BG3); btn_fr.pack(pady=(6,10))
    tk.Button(btn_fr, text="Cancel", bg="#442222", fg="white",
              font=("Segoe UI",10,"bold"), relief="flat", padx=14, pady=6,
              cursor="hand2", command=dlg.destroy).pack(side="left", padx=8)
    tk.Button(btn_fr, text="Generate", bg="#226633", fg="white",
              font=("Segoe UI",10,"bold"), relief="flat", padx=14, pady=6,
              cursor="hand2",
              command=lambda: run_contact_sheet(app,
                  files, max(1, _safe_int(cols_var, 6)),
                  max(0, _safe_int(rows_var, 0)),
                  orient_var.get() == "Landscape",
                  title_var.get().strip(),
                  out_var.get().strip(), dlg,
                  panel_shape=panel_var.get(),
                  rotate_to_panel=bool(rotate_to_panel_var.get()))
              ).pack(side="left", padx=8)

    # Bind variable changes to update
    cols_var.trace_add("write", _update)
    rows_var.trace_add("write", _update)
    orient_var.trace_add("write", _update)
    title_var.trace_add("write", _update)
    panel_var.trace_add("write", _update)
    rotate_to_panel_var.trace_add("write", _update)

    dlg.after(200, _update)


def run_contact_sheet(app, files, cols, rows, landscape, title, out_path, dlg,
                      panel_shape="Portrait", rotate_to_panel=True):
    """Generate the contact sheet PDF."""
    # Always build the final output path at Generate time so the timestamp is
    # current AEST and the directory is the FT library root, not the current
    # folder.  The dialog field is only a display/default; it is not trusted for
    # the folder or timestamp.
    out_dir = _call_output_dir(app, files=files)
    stamp = _aest_now().strftime("%Y-%m-%d_%H-%M")
    source = _safe_filename_part(title or _default_collection_name(app), "ContactSheet")
    layout = _layout_name(max(1, int(cols)), panel_shape)
    out_path = os.path.join(out_dir, f"{stamp}_{source}_{layout}.pdf")
    if os.path.exists(out_path):
        if not messagebox.askyesno("File exists",
                f"Overwrite:\n{os.path.basename(out_path)}?", parent=dlg):
            return
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    dlg.destroy()

    win = _app_window(app)
    prog = tk.Toplevel(win)
    prog.title("Generating..."); prog.configure(bg=BG3)
    prog.resizable(False, False); prog.transient(win)
    win.update_idletasks()
    x = win.winfo_rootx() + (win.winfo_width()  - 320) // 2
    y = win.winfo_rooty() + (win.winfo_height() - 80)  // 2
    prog.geometry(f"320x80+{x}+{y}")
    prog_lbl = tk.Label(prog, text="Starting...", bg=BG3, fg=TEXT_BRIGHT,
                        font=("Segoe UI",10)); prog_lbl.pack(pady=16)
    prog.update()

    def do_generate():
        try:
            from fpdf import FPDF
            from PIL import ImageOps as _IOS
            import tempfile, io
            from datetime import datetime as _DT

            margin = 10.0
            hdr_h  = 8.0
            gap    = 1.5

            if landscape:
                page_w, page_h = 297.0, 210.0
                pdf = FPDF(orientation="L", unit="mm", format=[210.0, 297.0])
            else:
                page_w, page_h = 210.0, 297.0
                pdf = FPDF(orientation="P", unit="mm", format="A4")
            pdf.set_auto_page_break(False)
            pdf.set_margins(margin, margin, margin)

            pw = page_w - 2 * margin
            ph = page_h - 2 * margin

            # Cell layout: seq/GPS line + image panel + filename.
            # Panel shape is independent of A4 page orientation.
            # Auto rows may shrink cell/image height by up to 5% to fit one
            # additional row, matching the manual forced-rows behaviour.
            line_h    = 3.5
            panel_ratio = 1.33 if str(panel_shape).lower().startswith("p") else 0.68
            rows_pp, cell_w, cell_h = _contactsheet_fit_grid(
                page_w, page_h, margin, hdr_h, gap, cols, rows,
                panel_ratio, line_h_mm=line_h, shrink_tol=0.05)

            cells_pp  = cols * rows_pp
            total     = len(files)
            n_pages   = max(1, -(-total // cells_pp))
            grid_top  = margin + hdr_h + gap

            title_enc = title.encode("latin-1", errors="replace").decode("latin-1")
            stamp     = _DT.now().strftime("%d %b %Y  %H:%M")
            fname_pt  = max(5, min(7, int(cell_w * 1.8)))
            date_pt   = max(4, min(6, fname_pt - 1))
            seq_pt    = fname_pt
            caption_h = max(line_h * 1.10, fname_pt * 0.95 + 1.2)

            def _render_img(fpath):
                """Return PIL Image for contact-sheet cell.

                Use the shared preview path first so JPG, PDF, and DOCX all
                render through the same logic used by FTView/FTmod thumbnails.
                This prevents DOCX/PDF cells being accepted but drawn empty.
                """
                ext = os.path.splitext(fpath)[1].lower()

                # Shared preview pipeline: handles .jpg/.jpeg, .pdf, .docx.
                try:
                    from ft_viewer import make_preview_thumbnail
                    img, ok, err = make_preview_thumbnail(
                        fpath,
                        1200,
                        longpath_func=(lambda p: _longpath_for(app, p)),
                    )
                    if ok and img is not None:
                        return img.convert("RGB")
                except Exception:
                    pass

                # Direct PDF fallback at higher resolution.
                if ext == ".pdf":
                    try:
                        import fitz
                        doc  = fitz.open(_longpath_for(app, fpath))
                        page = doc[0]
                        mat  = fitz.Matrix(3, 3)
                        pix  = page.get_pixmap(matrix=mat, alpha=False)
                        doc.close()
                        return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                    except Exception:
                        pass

                # Direct image fallback.
                if ext in (".jpg", ".jpeg"):
                    try:
                        img = Image.open(_longpath_for(app, fpath))
                        return _IOS.exif_transpose(img).convert("RGB")
                    except Exception:
                        pass

                # Last resort: cached thumbnail blob.
                jpeg = _thumb_bytes_for(app, fpath)
                if jpeg:
                    try:
                        return Image.open(io.BytesIO(jpeg)).convert("RGB")
                    except Exception:
                        pass
                return None

            for page_idx in range(n_pages):
                pdf.add_page()
                page_files = files[page_idx * cells_pp : (page_idx+1) * cells_pp]

                # ── Header ───────────────────────────────────────────────
                pdf.set_fill_color(240, 240, 240)
                pdf.rect(margin, margin, pw, hdr_h, "F")
                pdf.set_draw_color(160, 160, 160)
                pdf.set_line_width(0.3)
                pdf.line(margin, margin + hdr_h, margin + pw, margin + hdr_h)

                pdf.set_font("Helvetica", "B", size=10)
                pdf.set_text_color(0, 0, 0)
                pdf.text(margin + 1, margin + hdr_h * 0.72, title_enc)

                pg_str = f"Page {page_idx+1} of {n_pages}"
                pdf.set_font("Helvetica", "", size=8)
                pdf.set_text_color(80, 80, 80)
                pg_w = pdf.get_string_width(pg_str)
                pdf.text(margin + pw - pg_w - 1, margin + hdr_h * 0.72, pg_str)

                pdf.set_font("Helvetica", "", size=7)
                pdf.text(margin + pw * 0.55, margin + hdr_h * 0.72, stamp)

                # ── Cells ────────────────────────────────────────────────
                for i, fpath in enumerate(page_files):
                    row = i // cols
                    col = i %  cols
                    cx  = margin + col * cell_w
                    cy  = grid_top + row * cell_h
                    seq = page_idx * cells_pp + i + 1

                    win.after(0, prog_lbl.config,
                        {"text": f"Page {page_idx+1}/{n_pages}  —  {seq}/{total}"})
                    win.after(0, prog.update)

                    bd = 0.4
                    # Cell border
                    pdf.set_fill_color(200, 200, 200)
                    pdf.rect(cx, cy, cell_w, cell_h, "F")
                    ix = cx + bd; iy = cy + bd
                    iw = cell_w - 2*bd; ih = cell_h - 2*bd

                    # ── Seq/GPS line ──────────────────────────────────
                    pdf.set_fill_color(230, 230, 230)
                    pdf.rect(ix, iy, iw, line_h, "F")
                    pdf.set_font("Helvetica", "B", size=seq_pt)
                    pdf.set_text_color(0, 0, 0)
                    pdf.text(ix + 0.5, iy + line_h * 0.78, str(seq))

                    if app.mode == "photos" and _get_gps_coords(fpath):
                        pdf.set_fill_color(180, 0, 0)
                        gw = pdf.get_string_width("GPS") + 1.5
                        pdf.rect(ix + iw - gw, iy, gw, line_h, "F")
                        pdf.set_text_color(255, 255, 255)
                        pdf.text(ix + iw - gw + 0.5, iy + line_h * 0.78, "GPS")

                    # ── Image panel ──────────────────────────────────
                    img_slot_y = iy + line_h
                    # Reserve a real caption block at the bottom.  Filenames
                    # must never be cut off, especially 2-column landscape.
                    img_slot_h = max(1.0, cell_h - line_h - caption_h)
                    # Keep the visible image panel at the chosen portrait/landscape
                    # shape inside the available slot.
                    if img_slot_h / iw >= panel_ratio:
                        frame_w = iw
                        frame_h = iw * panel_ratio
                    else:
                        frame_h = img_slot_h
                        frame_w = img_slot_h / panel_ratio
                    frame_x = ix + (iw - frame_w) / 2
                    frame_y = img_slot_y + (img_slot_h - frame_h) / 2

                    pdf.set_fill_color(180, 180, 180)
                    pdf.rect(frame_x, frame_y, frame_w, frame_h, "F")
                    try:
                        pil = _render_img(fpath)
                        if pil:
                            if rotate_to_panel:
                                frame_landscape = frame_w > frame_h
                                image_landscape = pil.width > pil.height
                                if frame_landscape != image_landscape:
                                    pil = pil.rotate(90, expand=True)
                            pil.thumbnail((int(frame_w*12), int(frame_h*12)), Image.BILINEAR)
                            pil = pil.convert("RGB")
                            buf = io.BytesIO()
                            pil.save(buf, "JPEG", quality=82)
                            buf.seek(0)
                            with tempfile.NamedTemporaryFile(
                                    suffix=".jpg", delete=False) as tf:
                                tf.write(buf.read())
                                tmp = tf.name
                            iw_px, ih_px = pil.size
                            r = iw_px / ih_px if ih_px else 1
                            if r > frame_w / frame_h:
                                dw = frame_w;      dh = frame_w / r
                            else:
                                dh = frame_h;      dw = frame_h * r
                            ox = frame_x + (frame_w - dw) / 2
                            oy = frame_y + (frame_h - dh) / 2
                            pdf.image(tmp, x=ox, y=oy, w=dw, h=dh)
                            os.unlink(tmp)
                    except Exception:
                        pass

                    # ── Filename/date caption block ───────────────────
                    fn_y = iy + line_h + img_slot_h
                    pdf.set_fill_color(245, 245, 245)
                    pdf.rect(ix, fn_y, iw, caption_h, "F")

                    filename, cap_date = _contact_caption_parts(app, fpath)
                    date_label = f"Date:{cap_date}" if cap_date else ""

                    pdf.set_font("Helvetica", "", size=fname_pt)
                    pdf.set_text_color(0, 0, 0)
                    baseline_y = fn_y + max(3.2, fname_pt * 0.68)
                    left_x = ix + 0.5
                    right_x = ix + iw - 0.5
                    gap_w = 1.8

                    if date_label:
                        date_w = pdf.get_string_width(date_label)
                        name_w = max(1.0, (right_x - left_x) - date_w - gap_w)
                        display_name = _pdf_fit_filename(
                            pdf, filename, name_w, "Helvetica", "", fname_pt
                        )
                        pdf.text(left_x, baseline_y, display_name)
                        pdf.text(right_x - date_w, baseline_y, date_label)
                    else:
                        display_name = _pdf_fit_filename(
                            pdf, filename, iw - 1.0, "Helvetica", "", fname_pt
                        )
                        pdf.text(left_x, baseline_y, display_name)

            try:
                pdf.output(out_path)
            except PermissionError:
                win.after(0, lambda p=prog: (
                    p.withdraw(),
                    messagebox.showerror("Contact Sheet Failed",
                        f"File is open in another program:\n{os.path.basename(out_path)}",
                        parent=win),
                    p.destroy()))
                return
            win.after(0, lambda: contact_sheet_done(app, 
                out_path, total, n_pages, prog))

        except Exception as e:
            import traceback
            err = traceback.format_exc()
            win.after(0, lambda msg=f"{e}\n\n{err}", p=prog: (
                p.withdraw(),
                messagebox.showerror("Contact Sheet Failed", msg, parent=win),
                p.destroy()))

    threading.Thread(target=do_generate, daemon=True).start()


def _refresh_contact_sheet_tree(app, out_path):
    """Make _ContactSheets appear immediately and refresh its counts."""
    try:
        contact_dir = os.path.normpath(os.path.dirname(out_path))
        root = os.path.normpath(os.path.dirname(contact_dir))
    except Exception:
        return

    # FTView uses folder_tree.
    tree_obj = getattr(app, "folder_tree", None)
    if tree_obj is not None:
        try:
            if hasattr(tree_obj, "refresh_after_folder_created"):
                tree_obj.refresh_after_folder_created(root)
            elif hasattr(tree_obj, "refresh_node"):
                tree_obj.refresh_node(root)
            if hasattr(tree_obj, "refresh_after_file_ops"):
                tree_obj.refresh_after_file_ops([root, contact_dir])
            return
        except Exception:
            pass

    # FTmod uses _tree_widget.
    tree_obj = getattr(app, "_tree_widget", None)
    if tree_obj is not None:
        try:
            if hasattr(tree_obj, "refresh_after_folder_created"):
                tree_obj.refresh_after_folder_created(root)
            elif hasattr(tree_obj, "refresh_node"):
                tree_obj.refresh_node(root)
            if hasattr(tree_obj, "refresh_after_file_ops"):
                tree_obj.refresh_after_file_ops([root, contact_dir])
            elif hasattr(tree_obj, "refresh_stats"):
                tree_obj.refresh_stats()
            return
        except Exception:
            pass

    # Strong app-level fallbacks.  These make a newly-created _ContactSheets
    # folder appear immediately instead of only after restart.
    for name in ("_refresh_tree", "refresh_current"):
        fn = getattr(app, name, None)
        if callable(fn):
            try:
                fn()
                return
            except Exception:
                pass

    try:
        ft = getattr(app, "folder_tree", None)
        root = _active_mode_root(app)
        if ft is not None and root and hasattr(ft, "set_root"):
            ft.set_root(root)
            return
    except Exception:
        pass


def contact_sheet_done(app, out_path, n_images, n_pages, prog=None):
    """Show completion message with Open in PDF Viewer and Close buttons."""
    try:
        if prog: prog.withdraw()
    except: pass
    if not os.path.exists(out_path):
        try:
            if prog: prog.destroy()
        except: pass
        messagebox.showerror("Contact Sheet",
            f"File not found:\n{out_path}", parent=_app_window(app))
        return
    try:
        _refresh_contact_sheet_tree(app, out_path)
    except Exception:
        pass
    size_kb = os.path.getsize(out_path) // 1024
    msg = (f"{n_images} images  —  {n_pages} page{'s' if n_pages!=1 else ''}\n"
           f"{size_kb:,} KB\n\n{out_path}")
    parent = _app_window(app)
    dlg = tk.Toplevel(parent)
    dlg.title("Contact Sheet Ready")
    dlg.configure(bg=BG3)
    dlg.resizable(False, False)
    dlg.transient(parent)
    parent.update_idletasks()
    x = parent.winfo_rootx() + (parent.winfo_width()  - 380) // 2
    y = parent.winfo_rooty() + (parent.winfo_height() - 180) // 2
    dlg.geometry(f"380x180+{x}+{y}")
    try:
        if prog: prog.destroy()
    except: pass
    dlg.lift(); dlg.focus_force()
    tk.Label(dlg, text="Contact Sheet Ready", bg=BG3, fg="#66cc66",
             font=("Segoe UI",12,"bold")).pack(pady=(12,4))
    tk.Label(dlg, text=msg, bg=BG3, fg=TEXT_BRIGHT,
             font=("Segoe UI",9), justify="left").pack(padx=16, pady=4)
    btn_fr = tk.Frame(dlg, bg=BG3); btn_fr.pack(pady=(6,10))

    def _open_pdf():
        try:
            import ctypes
            ctypes.windll.shell32.ShellExecuteW(None, "open", out_path, None, None, 1)
        except Exception as e:
            messagebox.showerror("Cannot open", str(e), parent=_app_window(app))
        dlg.destroy()

    tk.Button(btn_fr, text="Open in PDF Viewer", bg="#226633", fg="white",
              font=("Segoe UI",10,"bold"), relief="flat", padx=14, pady=6,
              cursor="hand2", command=_open_pdf).pack(side="left", padx=6)
    tk.Button(btn_fr, text="Close", bg="#444", fg="white",
              font=("Segoe UI",9), relief="flat", padx=10, pady=5,
              cursor="hand2", command=dlg.destroy).pack(side="left", padx=6)


