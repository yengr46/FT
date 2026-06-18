
"""
FTView.py — simple standalone thumbnail browser prototype.
Thumbnail refactor: image loading now uses shared ft_thumbs.py helpers.

Layout:
    Header
    Folder tree | File list | Thumbnail grid | Zoom

Rules:
    - default root S:\Photos
    - file list fixed at 200 px
    - thumbnail pane fixed at 1000 px
    - zoom takes the rest
    - no partial thumbnails on the right
    - partial bottom row allowed
    - clicking thumbnail syncs file list and zoom
    - clicking file syncs thumbnail and zoom
    - zoom: mouse wheel zoom, drag pan, double-click fit
"""

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

# ── Crash logger — catches errors before main() including import-time errors ──
def _ftview_excepthook(exc_type, exc_val, exc_tb):
    import traceback, datetime
    _log = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "FTView_error.log")
    with open(_log, "a") as _f:
        _f.write(f"\n--- {datetime.datetime.now()} ---\n")
        traceback.print_exception(exc_type, exc_val, exc_tb, file=_f)
    try:
        import tkinter as _tk; from tkinter import messagebox as _mb
        _r = _tk.Tk(); _r.withdraw()
        _mb.showerror("FTView error", f"{exc_type.__name__}: {exc_val}\n\nSee FTView_error.log in helpers/ for details.")
        _r.destroy()
    except Exception:
        pass
    _sys.__excepthook__(exc_type, exc_val, exc_tb)
_sys.excepthook = _ftview_excepthook
# ─────────────────────────────────────────────────────────────────────────────

import os
import sys
import threading
import queue
import ctypes
try:
    import ctypes.wintypes as wintypes
except Exception:
    wintypes = None
import shutil
import subprocess
from libraries import ft_file_ops
from libraries import ft_contactsheet
from libraries import ft_pdf_ops
from libraries import ft_print
import configparser

try:
    from libraries.ft_project_roots import read_project_roots
except Exception:
    def read_project_roots(base_file=None):
        return {"photos": "", "pdfs": "", "project": ""}

# ---- Windows DPI awareness -------------------------------------------------
# This MUST run before importing tkinter / creating Tk widgets.  On Windows
# text scaling such as 125% changes the coordinate system used by Tk unless
# the process is made DPI-aware first.
def _set_process_dpi_awareness():
    if os.name != "nt":
        return
    try:
        # Prefer per-monitor DPI awareness.  This fails harmlessly if another
        # library has already set awareness.
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

_set_process_dpi_awareness()
# ---------------------------------------------------------------------------

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".avi", ".mkv", ".wmv", ".mpg", ".mpeg", ".mts", ".m2ts"}

from libraries.ft_thumb_layout import calculate_thumb_layout
try:
    from libraries.ft_file_labels import display_name as _ft_file_labels_display_name
    def _file_display_name(path):
        import os as _os
        ext = _os.path.splitext(str(path))[1].lower()
        if ext in VIDEO_EXTS:
            tag = ext.lstrip(".").upper()
            return f"[{tag}]  {_os.path.basename(str(path))}"
        return _ft_file_labels_display_name(path)
except Exception:
    def _file_display_name(path):
        import os as _os
        ext = _os.path.splitext(str(path))[1].lower()
        name = _os.path.basename(str(path))
        if ext == ".pdf":
            return "[PDF]  " + name
        if ext == ".docx":
            return "[DOCX]  " + name
        if ext in VIDEO_EXTS:
            tag = ext.lstrip(".").upper()
            return f"[{tag}]  {name}"
        return name
from libraries.ft_viewer import ViewerPanel, make_preview_thumbnail

# ── Global per-PC thumbnail cache ─────────────────────────────────────────────
try:
    from libraries import ft_thumb_cache as _ft_thumb_cache
    from io import BytesIO as _BytesIO
    _HAVE_THUMB_CACHE = True
except ImportError:
    _ft_thumb_cache = None
    _HAVE_THUMB_CACHE = False

# Match FTMod's storage size so thumbs are shared across all FT apps
_THUMB_STORE_SIZE = 250

try:
    from PIL import Image, ImageTk, ImageFile
    Image.MAX_IMAGE_PIXELS = None
    ImageFile.LOAD_TRUNCATED_IMAGES = True
except ImportError:
    tk.Tk().withdraw()
    messagebox.showerror("Missing library", "Pillow is required.\n\nRun: pip install Pillow")
    sys.exit(1)

try:
    from libraries.ft_thumbs import get_thumbnail
except ImportError:
    tk.Tk().withdraw()
    messagebox.showerror(
        "Missing module",
        "ft_thumbs.py is required in the same folder as FTView.py."
    )
    sys.exit(1)

try:
    from libraries.ft_widgets import FileCountTree
except ImportError:
    tk.Tk().withdraw()
    messagebox.showerror(
        "Missing module",
        "ft_widgets.py is required in the same folder as FTView.py."
    )
    sys.exit(1)
try:
    from libraries.ft_widgets import show_file_sort_menu, _sort_btn_label
except ImportError:
    show_file_sort_menu = None  # type: ignore[assignment]
    def _sort_btn_label(column="file", reverse=False):  # type: ignore[misc]
        labels = {"name": "Name", "date_taken": "Date", "file": "Name", "size": "Size"}
        return f"{labels.get(column, column)} {'↓' if reverse else '↑'} ▾"

PHOTO_EXTS = {".jpg", ".jpeg"}
PDF_EXTS = {".pdf"}
DOCX_EXTS = {".docx"}
DOCUMENT_EXTS = PDF_EXTS | DOCX_EXTS
DEFAULT_ROOT = ""
INI_FILE = "FTView.ini"

try:
    from libraries.ft_movie import MoviePlayerPanel, make_movie_thumbnail, make_movie_thumbnail_fast, get_video_info, _fmt_duration as _fmt_video_duration
    HAVE_FT_MOVIE = True
except ImportError:
    HAVE_FT_MOVIE = False


def _script_dir():
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except Exception:
        return os.getcwd()


def _ini_path():
    return os.path.join(_script_dir(), INI_FILE)


def _load_ftview_ini():
    """Load FTView roots/mode.

    Standalone default rule:
    - If Projects.ini exists, use the active project's Photos/PDFs roots.
    - If FTView.ini explicitly contains roots, keep those.
    - If neither exists, leave root boxes empty for user entry.
    """
    cfg = configparser.ConfigParser()
    project_roots = read_project_roots(__file__)
    first_root = project_roots.get("photos", "") or DEFAULT_ROOT
    data = {
        "photos_root": first_root,
        # pdf_root defaults to photos_root — no longer a separate typed root in new schema
        "pdf_root": first_root,
        "movies_root": DEFAULT_ROOT,
        "mode": "photos",
        "roots": project_roots.get("roots", []),
    }
    path = _ini_path()
    if os.path.exists(path):
        try:
            cfg.read(path, encoding="utf-8")
            data["photos_root"] = cfg.get("Roots", "PhotosRoot", fallback=data["photos_root"]).strip() or data["photos_root"]
            data["pdf_root"] = cfg.get("Roots", "PDFRoot", fallback=data["pdf_root"]).strip() or data["pdf_root"]
            data["movies_root"] = cfg.get("Roots", "MoviesRoot", fallback="").strip()
            mode = cfg.get("LastUsed", "Mode", fallback="Photos").strip().lower()
            if mode.startswith("pdf"):
                data["mode"] = "pdfs"
            elif mode.startswith("movie"):
                data["mode"] = "movies"
            else:
                data["mode"] = "photos"
        except Exception:
            pass
    return data


def _save_ftview_ini(photos_root, pdf_root, mode, movies_root=""):
    cfg = configparser.ConfigParser()
    cfg["Roots"] = {
        "PhotosRoot": photos_root or DEFAULT_ROOT,
        "PDFRoot": pdf_root or DEFAULT_ROOT,
        "MoviesRoot": movies_root or DEFAULT_ROOT,
    }
    if mode == "pdfs":
        mode_str = "PDFs"
    elif mode == "movies":
        mode_str = "Movies"
    else:
        mode_str = "Photos"
    cfg["LastUsed"] = {"Mode": mode_str}
    try:
        with open(_ini_path(), "w", encoding="utf-8") as f:
            cfg.write(f)
    except Exception:
        pass


BG = "#dddddd"
BG2 = "#eeeeee"
ACCENT = "#1a5276"
SELECT_BG = "#d9ecff"
TEXT = "#111111"
DIM = "#555555"
CANVAS_BG = "#777777"


def _longpath(path):
    """Return a disk-open path for Windows.

    FTView deliberately prefers the normal mounted-drive path (for example
    V:\\folder\\file.jpg) when it exists.  Some VeraCrypt/logical-drive
    mounts work perfectly through Explorer/Pillow with the normal path but do
    not behave reliably when the same path is forced through the \\?\\ long-path
    namespace.  The long-path prefix is therefore only used as a fallback for
    genuinely long paths or when the normal path cannot be seen.
    """
    if not path:
        return path
    path = _ui_path(path) if "_ui_path" in globals() else str(path)
    if os.name == "nt":
        normal = os.path.abspath(path).replace("/", "\\")
        try:
            if os.path.exists(normal):
                return normal
        except Exception:
            pass
        if len(normal) >= 240 and not normal.startswith("\\\\?\\"):
            return "\\\\?\\" + normal
        return normal
    return path


def _ui_path(path):
    """Return the normal Windows/UI path used for tree IDs and file lists.

    This keeps the Windows long-path prefix (\\?\) out of Tk item IDs and
    out of the file list.  Mounted VeraCrypt drives can expose child entries
    through os.scandir(_longpath(...)) with \\?\ prefixes; mixing those with
    normal V:\... paths breaks tree selection and thumbnail loading.
    """
    if not path:
        return path
    path = str(path)
    if path.startswith("\\\\?\\"):
        path = path[4:]
    path = os.path.normpath(path)
    # On Windows, "V:" means current directory on drive V, not the drive root.
    # Treat bare drive letters as roots for mounted logical drives.
    if os.name == "nt" and len(path) == 2 and path[1] == ":":
        path += "\\"
    return path


def _scale_to_fit(img, box_w, box_h):
    w, h = img.size
    if w <= 0 or h <= 0:
        return img
    scale = min(box_w / w, box_h / h)
    nw = max(1, int(w * scale))
    nh = max(1, int(h * scale))
    return img.resize((nw, nh), Image.BILINEAR)


def _scan_files(folder, exts=None):
    exts = {e.lower() for e in (exts or PHOTO_EXTS)}
    folder = _ui_path(folder)
    try:
        files = []
        for e in os.scandir(_longpath(folder)):
            if e.is_file() and os.path.splitext(e.name)[1].lower() in exts:
                files.append(_ui_path(os.path.join(folder, e.name)))
        return sorted(files, key=lambda p: os.path.basename(p).lower())
    except Exception:
        return []


def _count_files(folder, exts=None):
    """Count matching files directly in folder."""
    exts = {e.lower() for e in (exts or PHOTO_EXTS)}
    folder = _ui_path(folder)
    try:
        return sum(
            1 for e in os.scandir(_longpath(folder))
            if e.is_file() and os.path.splitext(e.name)[1].lower() in exts
        )
    except Exception:
        return 0


def _display_count(n):
    """Display zero counts as a dash."""
    return "  -  " if n == 0 else str(n)


def _file_size_value(path):
    """Return raw file size in bytes for sorting."""
    try:
        return os.path.getsize(path)
    except Exception:
        return -1


def _file_size_text(path):
    """Return file size as xxx.x kb or xxx.x mb."""
    size = _file_size_value(path)
    if size < 0:
        return "? kb"
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} mb"
    return f"{size / 1024:.1f} kb"


def _has_subdirs(folder):
    folder = _ui_path(folder)
    try:
        return any(e.is_dir(follow_symlinks=False) for e in os.scandir(_longpath(folder)))
    except Exception:
        return False



class ZoomCanvas(tk.Canvas):
    def __init__(self, parent, zbar_height=26):
        self._zbar_height = zbar_height
        super().__init__(parent, bg=CANVAS_BG, highlightthickness=0)
        self.img = None
        self.photo = None
        self.scale = 1.0
        self.fit_scale = 1.0
        self.offset = [0, 0]
        self.drag_start = None
        self._fit_after_id = None
        self.bind("<Configure>", self.on_configure)
        self.bind("<MouseWheel>", self.on_wheel)
        self.bind("<ButtonPress-1>", self.on_press)
        self.bind("<B1-Motion>", self.on_drag)
        self.bind("<Double-Button-1>", lambda e: self.fit())


    def on_configure(self, event=None):
        # When the visible zoom area changes, re-centre the image using the
        # actual visible canvas dimensions.  This prevents a stale offset from
        # leaving the image mostly below the screen.
        if self._fit_after_id is not None:
            try:
                self.after_cancel(self._fit_after_id)
            except Exception:
                pass
        self._fit_after_id = self.after_idle(self._finish_configure)

    def _finish_configure(self):
        self._fit_after_id = None
        if self.img is None:
            self.render()
        else:
            self.fit()

    def set_image(self, img):
        self.img = img
        self.scale = 1.0
        self.fit_scale = 1.0
        self.offset = [0, 0]
        # Cancel any pending fit before scheduling a fresh one
        if self._fit_after_id is not None:
            try:
                self.after_cancel(self._fit_after_id)
            except Exception:
                pass
        # Use after() not after_idle() so layout is fully settled first
        self._fit_after_id = self.after(30, self.fit)

    def fit(self):
        self._fit_after_id = None
        self.update_idletasks()
        cw = int(self.winfo_width())
        # Use the canvas's own reported height, but never more than
        # (parent frame height - 26) to exclude the zbar above us.
        try:
            parent_h = self.master.winfo_height()
            ch = max(1, parent_h - self._zbar_height)
        except Exception:
            ch = int(self.winfo_height())

        if cw < 20 or ch < 20:
            self.after(50, self.fit)
            return

        if self.img is None:
            self.delete("all")
            self.create_text(
                cw // 2,
                ch // 2,
                text="No image selected",
                fill="white",
                font=("Segoe UI", 14),
            )
            return

        iw, ih = self.img.size
        if iw <= 0 or ih <= 0:
            return

        self.fit_scale = min(cw / iw, ch / ih)
        self.scale = self.fit_scale
        nw, nh = max(1, int(iw * self.scale)), max(1, int(ih * self.scale))

        # Absolute centre rule:
        # image centre must equal visible zoom canvas centre.
        self.offset = [int((cw - nw) / 2), int((ch - nh) / 2)]
        self.render()

    def render(self):
        self.delete("all")
        if self.img is None:
            return
        iw, ih = self.img.size
        nw = max(1, int(iw * self.scale))
        nh = max(1, int(ih * self.scale))

        cw = max(1, int(self.winfo_width()))
        try:
            parent_h = self.master.winfo_height()
            ch = max(1, parent_h - self._zbar_height)
        except Exception:
            ch = max(1, int(self.winfo_height()))

        # If the current offset would put the fitted image mostly off-screen,
        # recentre it. This still allows intentional panning while zoomed.
        if self.scale == self.fit_scale:
            self.offset = [int((cw - nw) / 2), int((ch - nh) / 2)]

        try:
            disp = self.img.resize((nw, nh), Image.BILINEAR)
            self.photo = ImageTk.PhotoImage(disp)
            self.create_image(self.offset[0], self.offset[1], anchor="nw", image=self.photo)
        except Exception:
            self.create_text(
                self.winfo_width() // 2,
                self.winfo_height() // 2,
                text="Could not render image",
                fill="white",
                font=("Segoe UI", 14),
            )

    def on_wheel(self, event):
        if self.img is None:
            return "break"
        old = self.scale
        factor = 1.12 if event.delta > 0 else 1 / 1.12
        self.scale = max(self.fit_scale * 0.5, min(self.scale * factor, self.fit_scale * 12))
        mx, my = event.x, event.y
        ox, oy = self.offset
        self.offset[0] = int(mx - (mx - ox) * (self.scale / old))
        self.offset[1] = int(my - (my - oy) * (self.scale / old))
        self.render()
        return "break"

    def on_press(self, event):
        self.drag_start = (event.x, event.y, self.offset[0], self.offset[1])

    def on_drag(self, event):
        if not self.drag_start:
            return
        sx, sy, ox, oy = self.drag_start
        self.offset = [ox + event.x - sx, oy + event.y - sy]
        self.render()


class FTView(tk.Tk):
    TREE_W = 420
    FILES_W = 260
    THUMBS_W = 810          # includes thumbnail content + scrollbar strip
    THUMB_CONTENT_W = 800   # usable thumbnail content width; scrollbar is outside this
    THUMB_SCROLL_W = 10
    DIVIDER_W = 6
    FILES_MIN_W = 140
    ZOOM_MIN_W = 300

    def __init__(self):
        super().__init__()
        self.title("FTView version 2.3")
        self.configure(bg=BG)
        # Match the original working startup: force the Win32 work-area size
        # immediately, before widgets are created.
        self._maximize()
        self.minsize(1000, 650)

        self._ini_data = _load_ftview_ini()
        self.mode = self._ini_data.get("mode", "photos")
        if self.mode == "pdfs":
            _mode_display = "PDFs"
        elif self.mode == "movies":
            _mode_display = "Movies"
        else:
            _mode_display = "Photos"
        self.mode_var = tk.StringVar(value=_mode_display)
        self.roots_by_mode = {
            "photos": self._ini_data.get("photos_root", DEFAULT_ROOT),
            "pdfs": self._ini_data.get("pdf_root", DEFAULT_ROOT),
            "movies": self._ini_data.get("movies_root", DEFAULT_ROOT),
        }
        self.root_var = tk.StringVar(value=self.roots_by_mode.get(self.mode, DEFAULT_ROOT))
        self.rows_var = tk.IntVar(value=6)
        self.cols_var = tk.IntVar(value=6)
        self.nav_var = tk.StringVar(value="0 of 0")
        self.goto_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="Ready")

        self.current_folder = ""
        self.all_files = []
        self.files = []
        self.selected_idx = None
        self.selected_files = set()
        self.last_selected_idx = None
        # Last normal click action: True = select/add, False = unselect/remove.
        # Shift-click repeats this action across the anchor..current range.
        self.last_selection_action = True
        self.view_filter = "all"
        self.thumb_refs = []
        self.thumb_cells = {}
        self.thumb_labels = {}
        self.thumb_watermarks = {}
        self.zoom_token = 0
        self.thumb_generation = 0
        self.thumb_queue = None
        self.thumb_requested = set()
        self.thumb_loaded = set()
        self.thumb_loading = set()
        self.thumb_request_queue = None
        self.thumb_loader_running = False
        self.thumb_loader_workers = 0
        self.MAX_THUMB_WORKERS = 4   # more workers help for slow movie thumbs
        self.current_thumb_pane_w = self.THUMBS_W
        self._sashes_ready = False
        self._sash_update_pending = False
        self.thumb_box = (80, 80)
        self.thumb_cols = 6
        self.thumb_cell_h = 120
        self.thumb_total_h = 1
        self.file_sort_column = "file"
        self.file_sort_reverse = False
        self._thumb_layout = None
        self._thumb_cell_w_for_drag = None
        self._syncing_spinners = False
        self._dragging_thumb_divider = False
        self._dragging_files_divider = False
        self._syncing_file_selection = False
        self._ignore_file_select_until_idle = False
        self._destination_dialog_active = False
        self._destination_folder_var = None

        self._build_ui()
        # Apply repeatedly during early Tk/Windows layout so the first usable
        # window is the full work area, not Tk's default partial size.
        self._maximize()
        self.after(1, self._maximize)
        self.after(50, self._maximize)
        self.after(200, self._maximize)
        self.after(260, lambda: self._print_window_metrics("startup"))
        self.after(120, self._layout_body_panels)
        self.after(240, self._set_all_sashes)
        self.after(300, self._initial_root)

    def _windows_work_area(self):
        """Return the usable Windows work area in physical pixels.

        The work area excludes the Windows taskbar.  Because DPI awareness is
        set before tkinter is imported, these coordinates match the coordinates
        used by SetWindowPos and the real outer window rectangle.
        """
        if os.name == "nt" and wintypes is not None:
            try:
                rect = wintypes.RECT()
                SPI_GETWORKAREA = 48
                ok = ctypes.windll.user32.SystemParametersInfoW(
                    SPI_GETWORKAREA, 0, ctypes.byref(rect), 0
                )
                if ok:
                    left = int(rect.left)
                    top = int(rect.top)
                    width = int(rect.right - rect.left)
                    height = int(rect.bottom - rect.top)
                    if width > 100 and height > 100:
                        return left, top, width, height
            except Exception:
                pass
        return 0, 0, int(self.winfo_screenwidth()), max(600, int(self.winfo_screenheight()) - 60)

    def _top_level_hwnd(self):
        """Return the real Win32 top-level window handle for the Tk root.

        On Windows, winfo_id() can refer to Tk's inner client window rather than
        the decorated top-level frame.  Moving/resizing that child produces the
        exact symptom we saw: the calculated work area is correct, but FTView
        remains visually offset/not full screen.
        """
        hwnd = int(self.winfo_id())
        if os.name != "nt":
            return hwnd
        try:
            user32 = ctypes.windll.user32
            parent = user32.GetParent(hwnd)
            if parent:
                hwnd = int(parent)
        except Exception:
            pass
        return hwnd

    def _window_rect(self):
        """Return the real outer window rectangle, if available."""
        if os.name != "nt" or wintypes is None:
            return None
        try:
            rect = wintypes.RECT()
            hwnd = self._top_level_hwnd()
            if ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
        except Exception:
            return None
        return None

    def _dpi_for_window(self):
        if os.name != "nt":
            return "n/a"
        try:
            return int(ctypes.windll.user32.GetDpiForWindow(self._top_level_hwnd()))
        except Exception:
            try:
                return int(ctypes.windll.user32.GetDpiForSystem())
            except Exception:
                return "unknown"

    def _print_window_metrics(self, label):
        """Diagnostic proof printed at startup for DPI/work-area problems."""
        try:
            wa = self._windows_work_area()
            wr = self._window_rect()
            print(
                f"FTView window metrics [{label}]: "
                f"workarea={wa} outer_rect={wr} "
                f"client={self.winfo_width()}x{self.winfo_height()} "
                f"screen={self.winfo_screenwidth()}x{self.winfo_screenheight()} "
                f"tk_scaling={self.tk.call('tk', 'scaling')} dpi={self._dpi_for_window()} hwnd={self._top_level_hwnd()}"
            )
        except Exception as e:
            print(f"FTView window metrics [{label}] failed: {e}")

    def _maximize(self):
        """Fit FTView inside the actual Windows work area.

        Do NOT call geometry(width=workarea_height) after this.  Tk geometry
        sizes the *client* area, while SetWindowPos sizes the real outer window
        including the title bar and borders.  Using Tk geometry with the full
        work-area height makes the outer window taller than the work area at
        125% text scaling, which is what pushes the bottom under the taskbar.
        """
        self._sash_update_pending = False
        try:
            if os.name == "nt":
                left, top, width, height = self._windows_work_area()
                try:
                    self.state("normal")
                except Exception:
                    pass
                try:
                    self.update_idletasks()
                except Exception:
                    pass
                hwnd = self._top_level_hwnd()
                SWP_NOZORDER = 0x0004
                SWP_NOACTIVATE = 0x0010
                SWP_SHOWWINDOW = 0x0040
                ctypes.windll.user32.SetWindowPos(
                    hwnd, None, int(left), int(top), int(width), int(height),
                    SWP_NOZORDER | SWP_NOACTIVATE | SWP_SHOWWINDOW,
                )
                try:
                    self.update_idletasks()
                except Exception:
                    pass

                # Safety clamp: if Windows/Tk decorations still leave any part
                # outside the work area, force the outer rect back inside it.
                rect = self._window_rect()
                if rect:
                    r_left, r_top, r_right, r_bottom = rect
                    wa_right = left + width
                    wa_bottom = top + height
                    dx = 0
                    dy = 0
                    if r_left < left:
                        dx = left - r_left
                    elif r_right > wa_right:
                        dx = wa_right - r_right
                    if r_top < top:
                        dy = top - r_top
                    elif r_bottom > wa_bottom:
                        dy = wa_bottom - r_bottom
                    if dx or dy:
                        ctypes.windll.user32.SetWindowPos(
                            hwnd, None, int(r_left + dx), int(r_top + dy),
                            int(r_right - r_left), int(r_bottom - r_top),
                            SWP_NOZORDER | SWP_NOACTIVATE | SWP_SHOWWINDOW,
                        )
            else:
                self.geometry(f"{self.winfo_screenwidth()}x{self.winfo_screenheight()}+0+0")
        except Exception as e:
            print(f"FTView work-area maximise failed: {e}")
            try:
                self.state("zoomed")
            except Exception:
                pass

    def _make_toolbar_button(self, parent, text, command, colour):
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=colour,
            fg="white",
            activebackground=colour,
            activeforeground="white",
            font=("Segoe UI", 9, "bold"),
            relief="raised",
            bd=1,
            padx=6,
            pady=1,
        )

    def _make_toolbar_group(self, parent, padx=(6, 2)):
        group = tk.Frame(parent, bg=BG2, highlightbackground="black", highlightcolor="black", highlightthickness=1, bd=0)
        group.pack(side="left", padx=padx, pady=4)
        return group

    def _update_view_filter_buttons(self):
        """Show All / Selected are mode buttons: active one stays coloured, inactive is dehighlighted."""
        try:
            if self.view_filter == "selected":
                self.btn_show_all.configure(bg="#777777", activebackground="#777777")
                self.btn_show_selected.configure(bg="#2f6f3e", activebackground="#2f6f3e")
            else:
                self.btn_show_all.configure(bg="#2f6f3e", activebackground="#2f6f3e")
                self.btn_show_selected.configure(bg="#777777", activebackground="#777777")
        except Exception:
            pass

    def _build_ui(self):
        header = tk.Frame(self, bg=BG2, height=38)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        tk.Label(header, text="Mode", bg=BG2, fg=DIM, font=("Segoe UI", 9, "bold")).pack(side="left", padx=(8, 4))
        self.mode_cb = ttk.Combobox(header, textvariable=self.mode_var, values=["Photos", "PDFs", "Movies"], width=9, state="readonly")
        self.mode_cb.pack(side="left", padx=(0, 10))
        self.mode_cb.bind("<<ComboboxSelected>>", lambda e: self._on_mode_changed())

        tk.Label(header, text="Root", bg=BG2, fg=DIM, font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 4))
        root_entry = tk.Entry(header, textvariable=self.root_var, width=40)
        root_entry.pack(side="left", padx=(0, 4))
        root_entry.bind("<Return>", lambda e: self.set_root(self.root_var.get().strip(), ask_save=True))
        self._make_toolbar_button(header, "...", self.browse_root, ACCENT).pack(side="left", padx=(0, 6))

        tk.Label(header, text="Cols", bg=BG2, fg=DIM).pack(side="left")
        self.cols_spin = tk.Spinbox(header, from_=1, to=20, width=3, textvariable=self.cols_var, command=self._on_cols_changed)
        self.cols_spin.pack(side="left", padx=(2, 5))
        self.cols_spin.bind("<Return>", lambda e: self._on_cols_changed())
        self.cols_spin.bind("<FocusOut>", lambda e: self._on_cols_changed())

        tk.Label(header, text="Rows", bg=BG2, fg=DIM).pack(side="left")
        self.rows_spin = tk.Spinbox(header, from_=1, to=200, width=3, textvariable=self.rows_var, command=self._on_rows_changed)
        self.rows_spin.pack(side="left", padx=(2, 5))
        self.rows_spin.bind("<Return>", lambda e: self._on_rows_changed())
        self.rows_spin.bind("<FocusOut>", lambda e: self._on_rows_changed())

        self._make_toolbar_button(header, "Refresh", self.refresh_current, "#555555").pack(side="left", padx=(2, 4))

        select_group = self._make_toolbar_group(header, padx=(4, 2))
        self._make_toolbar_button(select_group, "Select All", self.select_all_files, "#704a9e").pack(side="left", padx=2, pady=2)
        self._make_toolbar_button(select_group, "Clear", self.clear_selected_files, "#704a9e").pack(side="left", padx=2, pady=2)
        self._make_toolbar_button(select_group, "Filter", self.show_filename_filter_dialog, "#704a9e").pack(side="left", padx=2, pady=2)
        self.btn_contact_sheet = self._make_toolbar_button(select_group, "Contact Sheet", self.create_contact_sheet, "#704a9e")
        self.btn_contact_sheet.pack(side="left", padx=2, pady=2)

        view_group = self._make_toolbar_group(header, padx=(4, 2))
        self.btn_show_all = self._make_toolbar_button(view_group, "Show All", self.show_all_files, "#2f6f3e")
        self.btn_show_all.pack(side="left", padx=2, pady=2)
        self.btn_show_selected = self._make_toolbar_button(view_group, "Selected", self.show_selected_files, "#2f6f3e")
        self.btn_show_selected.pack(side="left", padx=2, pady=2)

        nav_group = self._make_toolbar_group(header, padx=(4, 2))
        self._make_toolbar_button(nav_group, "First", self.nav_first, "#405b77").pack(side="left", padx=2, pady=2)
        self._make_toolbar_button(nav_group, "Page Up", self.nav_page_up, "#405b77").pack(side="left", padx=2, pady=2)
        tk.Label(nav_group, textvariable=self.nav_var, bg=BG2, fg=DIM, width=11, anchor="center",
                 font=("Segoe UI", 9, "bold")).pack(side="left", padx=3, pady=2)
        self._make_toolbar_button(nav_group, "Page Down", self.nav_page_down, "#405b77").pack(side="left", padx=2, pady=2)
        self._make_toolbar_button(nav_group, "Last", self.nav_last, "#405b77").pack(side="left", padx=2, pady=2)
        tk.Label(nav_group, text="Goto", bg=BG2, fg=DIM).pack(side="left", padx=(5, 2), pady=2)
        self.goto_entry = tk.Entry(nav_group, textvariable=self.goto_var, width=5)
        self.goto_entry.pack(side="left", padx=(0, 2), pady=2)
        self.goto_entry.bind("<Return>", lambda e: self.nav_goto())
        # Goto is activated by Enter in the entry; no separate Go button.
        tk.Label(header, textvariable=self.status_var, bg=BG2, fg=DIM, anchor="e").pack(side="right", padx=8)
        self._update_view_filter_buttons()

        # Main body: absolute panel placement.
        #
        # This deliberately avoids PanedWindow and avoids pack/grid negotiation
        # for the four main panels.  The thumbnail panel is placed at an exact
        # x position with an exact width of 1000 px.  Tk cannot stretch it.
        self.body = tk.Frame(self, bg=BG)
        self.body.pack(fill="both", expand=True)
        self.body.bind("<Configure>", self._layout_body_panels)

        # Folder tree: fixed width. Use the shared FT FileCountTree logic.
        # FTView supplies labels/extensions only: Files and Tree.
        self.tree_frame = tk.Frame(self.body, bg=BG2)
        self.tree_frame.place(x=0, y=0, width=self.TREE_W, height=1)
        self.tree_frame.pack_propagate(False)

        self.folder_tree = FileCountTree(
            self.tree_frame,
            extensions=self._current_exts(self.root_var.get()),
            col_own="Files",
            col_child="Tree",
            show_root_entry=False,
            bg=BG2,
        )
        self.folder_tree.pack(fill="both", expand=True)
        self.tree = self.folder_tree.tree()
        self.tree.bind("<ButtonRelease-1>", self.on_tree_click, add="+")

        # Files list: fixed width.
        self.files_frame = tk.Frame(self.body, bg=BG2)
        self.files_frame.place(x=self.TREE_W, y=0, width=self.FILES_W, height=1)
        self.files_frame.pack_propagate(False)

        # FILES header row: label + sort button
        _fhdr = tk.Frame(self.files_frame, bg=BG2)
        _fhdr.pack(fill="x")
        tk.Label(_fhdr, text="FILES", bg=BG2, fg=DIM,
                 font=("Segoe UI", 8, "bold")).pack(side="left", padx=(4, 0), pady=2)
        self._sort_btn = tk.Button(
            _fhdr, text=_sort_btn_label(self.file_sort_column, self.file_sort_reverse),
            font=("Segoe UI", 8, "bold"), bg=BG2, fg=ACCENT,
            relief="flat", cursor="hand2",
            command=self._show_sort_menu,
        )
        self._sort_btn.pack(side="right", padx=(0, 20))

        try:
            style = ttk.Style(self)
            style.configure("FTView.FileList.Treeview", font=("Segoe UI", 9), rowheight=22)
            style.configure("FTView.FileList.Treeview.Heading", font=("Segoe UI", 8, "bold"))
        except Exception:
            pass
        self.file_list = ttk.Treeview(self.files_frame, show="headings", columns=("file", "size"), selectmode="browse", style="FTView.FileList.Treeview")
        self.file_list.heading("file", text="File", anchor="w")
        self.file_list.heading("size", text="Size", anchor="e")
        self.file_list.column("file", width=330, minwidth=220, stretch=True, anchor="w")
        self.file_list.column("size", width=105, minwidth=105, stretch=False, anchor="e")
        file_sb = ttk.Scrollbar(self.files_frame, orient="vertical", command=self.file_list.yview)
        self.file_list.configure(yscrollcommand=file_sb.set)
        file_sb.pack(side="right", fill="y")
        self.file_list.pack(side="left", fill="both", expand=True, pady=(0, 4))
        self.file_list.bind("<<TreeviewSelect>>", self.on_file_select)
        self.file_list.bind("<Button-1>", self._on_file_list_mouse_down)
        self.file_list.bind("<Button-3>", self.show_file_list_menu)

        # Thumbnails: fixed initial panel, adjustable against Zoom via divider.
        self.thumbs_frame = tk.Frame(self.body, bg=BG)
        self.thumbs_frame.place(x=self.TREE_W + self.FILES_W + self.DIVIDER_W, y=0, width=self.THUMBS_W, height=1)
        self.thumbs_frame.pack_propagate(False)
        self.thumbs_frame.grid_propagate(False)

        tk.Label(self.thumbs_frame, text="THUMBNAILS", bg=BG2, fg=DIM, font=("Segoe UI", 8, "bold")).pack(fill="x", ipady=2)

        # The thumbnail content width excludes the scrollbar.  This prevents the
        # rightmost column being hidden behind the scrollbar.
        self.thumb_canvas = tk.Canvas(self.thumbs_frame, bg=BG, highlightthickness=0, width=self.THUMB_CONTENT_W)
        self.thumb_canvas.place(x=0, y=22, width=self.THUMB_CONTENT_W, height=1)

        self.thumb_scrollbar = ttk.Scrollbar(self.thumbs_frame, orient="vertical", command=self._thumb_yview)
        self.thumb_canvas.configure(yscrollcommand=self._on_thumb_yscroll)
        self.thumb_scrollbar.place(x=self.THUMB_CONTENT_W, y=22, width=self.THUMB_SCROLL_W, height=1)

        self.thumb_inner = tk.Frame(self.thumb_canvas, bg=BG, width=self.THUMB_CONTENT_W)
        self.thumb_window = self.thumb_canvas.create_window((0, 0), window=self.thumb_inner, anchor="nw", width=self.THUMB_CONTENT_W)
        self.thumb_inner.grid_propagate(False)
        self.thumb_canvas.bind("<MouseWheel>", self.on_thumb_wheel)
        self.thumb_canvas.bind("<Configure>", self._on_thumb_canvas_configure)

        self.busy_overlay = tk.Frame(self.thumbs_frame, bg="#ffffff", bd=2, relief="solid")
        self.busy_label = tk.Label(
            self.busy_overlay,
            text="Sorting and Generating Thumbnails...",
            bg="#ffffff",
            fg="#333333",
            font=("Segoe UI", 20, "bold"),
            padx=28,
            pady=22,
        )
        self.busy_label.place(relx=0.5, rely=0.5, anchor="center")
        self.busy_overlay.bind("<Button-1>", lambda e: "break")
        self.busy_overlay.bind("<MouseWheel>", lambda e: "break")

        # Adjustable divider between file list and thumbnails.
        self.files_thumb_divider = tk.Frame(self.body, bg="#999999", cursor="sb_h_double_arrow")
        self.files_thumb_divider.place(x=self.TREE_W + self.FILES_W, y=0, width=self.DIVIDER_W, height=1)
        self.files_thumb_divider.bind("<ButtonPress-1>", self._start_files_divider_drag)
        self.files_thumb_divider.bind("<B1-Motion>", self._drag_files_divider)
        self.files_thumb_divider.bind("<ButtonRelease-1>", self._end_files_divider_drag)
        self.files_thumb_divider.lift()

        # Adjustable divider between thumbnails and zoom.
        self.thumb_zoom_divider = tk.Frame(self.body, bg="#999999", cursor="sb_h_double_arrow")
        self.thumb_zoom_divider.place(x=self.TREE_W + self.FILES_W + self.DIVIDER_W + self.THUMBS_W, y=0, width=self.DIVIDER_W, height=1)
        self.thumb_zoom_divider.bind("<ButtonPress-1>", self._start_thumb_divider_drag)
        self.thumb_zoom_divider.bind("<B1-Motion>", self._drag_thumb_divider)
        self.thumb_zoom_divider.bind("<ButtonRelease-1>", self._end_thumb_divider_drag)
        self.thumb_zoom_divider.lift()

        # Viewer: starts immediately after the thumbnail panel and divider.
        self.zoom_frame = tk.Frame(self.body, bg=CANVAS_BG)
        self.zoom_frame.place(x=self.TREE_W + self.FILES_W + self.THUMBS_W + self.DIVIDER_W, y=0)

        self.viewer = ViewerPanel(
            self.zoom_frame,
            bg=CANVAS_BG,
            longpath_func=_longpath,
            on_select_index=self._viewer_select_index,
            on_file_changed=self._on_viewer_file_changed,
        )
        self.viewer.pack(fill="both", expand=True)
        # Backward-compatible name for any remaining internal calls.
        self.zoom = self.viewer.canvas

        # Movie viewer — created only when ft_movie is available.
        # Hidden until Movies mode is selected.
        self.movie_viewer = None
        if HAVE_FT_MOVIE:
            self.movie_viewer = MoviePlayerPanel(
                self.zoom_frame,
                bg=CANVAS_BG,
                longpath_func=_longpath,
                on_select_index=self._viewer_select_index,
                output_folder=self._get_framegrab_folder,
                on_edit_done=self._on_movie_edit_done,
            )
        # Show the correct panel for the startup mode
        if self.mode == "movies":
            self._activate_viewer("movies")
        else:
            self._activate_viewer("photos")


    def _on_movie_edit_done(self, path: str):
        """Called after a movie edit commit.

        Reloads the folder containing the saved file, navigating there if it
        differs from the current view. Selects and scrolls to the new file.
        """
        try:
            path      = _ui_path(os.path.abspath(path))
            saved_dir = _ui_path(os.path.dirname(path))
            # Always reload the folder that holds the saved file
            if saved_dir and os.path.isdir(saved_dir):
                self.load_folder(saved_dir)
            # After reload, select and scroll to the saved file
            if path in self.files:
                idx = self.files.index(path)
                self.select_index(idx)
                self._scroll_thumb_to_index(idx)
        except Exception:
            pass

    def _get_framegrab_folder(self, video_path=None):
        """Return the FrameGrabs subfolder alongside the given video file.

        If video_path is None, falls back to the currently selected file.
        The returned folder is created if it does not already exist.
        Falls back to the movies root if no video path can be determined.
        """
        path = video_path
        if not path and self.selected_idx is not None and 0 <= self.selected_idx < len(self.files):
            path = self.files[self.selected_idx]
        if path:
            video_dir = os.path.dirname(os.path.abspath(_ui_path(path)))
            grab_dir = os.path.join(video_dir, "FrameGrabs")
            try:
                os.makedirs(grab_dir, exist_ok=True)
            except Exception:
                pass
            return grab_dir
        return self.roots_by_mode.get("movies", "")

    def _scroll_thumb_to_index(self, idx: int):
        """Scroll the thumbnail panel to make idx visible."""
        try:
            if not self.thumb_cols or not self.thumb_cell_h:
                return
            row = idx // self.thumb_cols
            y = row * self.thumb_cell_h
            total = max(1, self.thumb_total_h)
            self.thumb_canvas.yview_moveto(y / total)
        except Exception:
            pass

    def _invalidate_thumb(self, idx: int):
        """Remove a thumb from loaded set so it gets regenerated."""
        self.thumb_loaded.discard(idx)
        self.thumb_loading.discard(idx)
        self.thumb_requested.discard(idx)
        try:
            label = self.thumb_labels[idx][0]
            label.configure(image="")
            label.image = None
        except Exception:
            pass

    def _reload_single_thumb(self, idx: int):
        """Request thumbnail generation for a single index."""
        if idx < 0 or idx >= len(self.files):
            return
        if not self.thumb_labels.get(idx):
            return
        label = self.thumb_labels[idx][0]
        bw, bh = self.thumb_box if hasattr(self, "thumb_box") else (160, 160)
        path = self.files[idx]
        generation = self.thumb_generation
        if self.thumb_request_queue is None:
            import queue as _queue
            self.thumb_request_queue = _queue.Queue()
        self.thumb_requested.add(idx)
        self.thumb_request_queue.put((generation, idx, path, label, bw, bh))
        import threading as _threading
        while self.thumb_loader_workers < self.MAX_THUMB_WORKERS:
            self.thumb_loader_workers += 1
            _threading.Thread(target=self.thumb_loader_worker, daemon=True).start()

    def _on_thumb_canvas_configure(self, event=None):
        """Canvas height changed: keep scrolling/page math tied to visible pixels."""
        try:
            self._clamp_thumb_scroll()
        except Exception:
            pass
        self.after_idle(self._after_thumb_scroll)

    def _thumb_yview(self, *args):
        self.thumb_canvas.yview(*args)
        self.after_idle(self._after_thumb_scroll)

    def _on_thumb_yscroll(self, first, last):
        try:
            self.thumb_scrollbar.set(first, last)
        except Exception:
            pass
        self.after_idle(self._update_nav_status)

    def _after_thumb_scroll(self):
        self._update_nav_status()
        self.schedule_visible_thumbs()

    def _first_visible_thumb_index(self):
        """Return the first thumbnail actually visible in the canvas."""
        if not self.files:
            return 0
        try:
            self.thumb_inner.update_idletasks()
            y0 = max(0, int(self.thumb_canvas.canvasy(0)))
            for idx in sorted(self.thumb_cells):
                cell = self.thumb_cells[idx]
                try:
                    if cell.winfo_y() + cell.winfo_height() > y0:
                        return min(len(self.files) - 1, idx)
                except Exception:
                    continue
        except Exception:
            pass
        try:
            row = max(0, int(self.thumb_canvas.canvasy(0) // max(1, self.thumb_cell_h)))
            return min(len(self.files) - 1, row * max(1, self.thumb_cols))
        except Exception:
            return 0

    def _visible_thumb_count(self):
        """Return one screenful of thumbnails based on real visible canvas height.

        Page Up/Down must use FULL visible rows only.  A partial bottom row is
        deliberately visible so the user can see more rows exist, but it should
        not increase the page jump.
        """
        if not self.files:
            return 1
        try:
            self.thumb_canvas.update_idletasks()
            visible_h = max(1, int(self.thumb_canvas.winfo_height()))
            row_h = max(1, int(self.thumb_cell_h))
            cols = max(1, int(self.thumb_cols))
            full_rows = max(1, visible_h // row_h)
            return max(1, full_rows * cols)
        except Exception:
            return max(1, int(getattr(self, "thumb_cols", 1) or 1))

    def _update_nav_status(self):
        total = len(self.files)
        if total <= 0:
            self.nav_var.set("0 of 0")
            return
        first = self._first_visible_thumb_index() + 1
        self.nav_var.set(f"{first} of {total}")

    def _scroll_thumb_to_index(self, idx, *, select=False):
        """Scroll the thumbnail canvas so idx is visible, using actual widget positions."""
        if not self.files:
            self._update_nav_status()
            return
        idx = max(0, min(int(idx), len(self.files) - 1))
        try:
            self.thumb_inner.update_idletasks()
            cell = self.thumb_cells.get(idx)
            if cell is not None and cell.winfo_exists():
                y = int(cell.winfo_y())
            else:
                row = idx // max(1, self.thumb_cols)
                y = row * max(1, self.thumb_cell_h)
        except Exception:
            row = idx // max(1, self.thumb_cols)
            y = row * max(1, self.thumb_cell_h)

        total_h = max(1, int(self.thumb_total_h))
        try:
            visible_h = max(1, int(self.thumb_canvas.winfo_height()))
        except Exception:
            visible_h = 1
        max_y = max(0, total_h - visible_h)
        y = max(0, min(y, max_y))
        self.thumb_canvas.yview_moveto(0 if total_h <= visible_h else y / total_h)
        if select:
            self.select_index(idx, from_thumb=True)
        self.after_idle(self._after_thumb_scroll)

    def nav_first(self):
        self._scroll_thumb_to_index(0, select=False)

    def nav_last(self):
        """Scroll so the final row is fully visible, not just partly visible."""
        if not self.files:
            return
        try:
            cols = max(1, int(self.thumb_cols))
            row_h = max(1, int(self.thumb_cell_h))
            visible_h = max(1, int(self.thumb_canvas.winfo_height()))
            full_rows = max(1, visible_h // row_h)
            total_rows = max(1, (len(self.files) + cols - 1) // cols)
            first_row = max(0, total_rows - full_rows)
            last_first = min(len(self.files) - 1, first_row * cols)
        except Exception:
            last_first = max(0, len(self.files) - self._visible_thumb_count())
        self._scroll_thumb_to_index(last_first, select=False)

    def _visible_thumb_step(self):
        return self._visible_thumb_count()

    def nav_page_up(self):
        if not self.files:
            return
        first = self._first_visible_thumb_index()
        self._scroll_thumb_to_index(max(0, first - self._visible_thumb_step()), select=False)

    def nav_page_down(self):
        if not self.files:
            return
        first = self._first_visible_thumb_index()
        self._scroll_thumb_to_index(min(len(self.files) - 1, first + self._visible_thumb_step()), select=False)

    def nav_goto(self):
        text = self.goto_var.get().strip()
        if not text:
            return
        try:
            idx = int(text) - 1
        except Exception:
            messagebox.showwarning("Goto", "Enter a file number, starting at 1.", parent=self)
            return
        if not self.files:
            self._update_nav_status()
            return
        idx = max(0, min(idx, len(self.files) - 1))
        self.goto_var.set(str(idx + 1))
        self._scroll_thumb_to_index(idx, select=True)


    def _layout_body_panels(self, event=None):
        """Absolute layout: Tree | Files | Thumbnails | Divider | Zoom remainder."""
        try:
            total_w = max(1, self.body.winfo_width())
            total_h = max(1, self.body.winfo_height())

            left_fixed = self.TREE_W + self.FILES_W + self.DIVIDER_W
            max_thumbs_w = max(self.THUMB_CONTENT_W + self.THUMB_SCROLL_W, total_w - left_fixed - self.DIVIDER_W - self.ZOOM_MIN_W)
            self.THUMBS_W = max(self.THUMB_CONTENT_W + self.THUMB_SCROLL_W, min(self.THUMBS_W, max_thumbs_w))
            self.THUMB_CONTENT_W = max(80, self.THUMBS_W - self.THUMB_SCROLL_W)

            x_tree = 0
            x_files = self.TREE_W
            x_files_div = x_files + self.FILES_W
            x_thumbs = x_files_div + self.DIVIDER_W
            x_divider = x_thumbs + self.THUMBS_W
            x_zoom = x_divider + self.DIVIDER_W
            zoom_w = max(self.ZOOM_MIN_W, total_w - x_zoom)

            self.tree_frame.place(x=x_tree, y=0, width=self.TREE_W, height=total_h, relheight=0)
            self.files_frame.place(x=x_files, y=0, width=self.FILES_W, height=total_h, relheight=0)
            self.files_thumb_divider.place(x=x_files_div, y=0, width=self.DIVIDER_W, height=total_h, relheight=0)
            self.thumbs_frame.place(x=x_thumbs, y=0, width=self.THUMBS_W, height=total_h, relheight=0)
            self.thumb_zoom_divider.place(x=x_divider, y=0, width=self.DIVIDER_W, height=total_h, relheight=0)
            panel_h = max(1, total_h)
            self.zoom_frame.place(x=x_zoom, y=0, width=zoom_w, height=panel_h, relheight=0)
            self.zoom_frame.configure(width=zoom_w, height=panel_h)

            self.thumb_canvas.place(x=0, y=22, width=self.THUMB_CONTENT_W, height=max(1, panel_h - 22), relheight=0)
            self.thumb_canvas.configure(width=self.THUMB_CONTENT_W, height=max(1, panel_h - 22))
            self.thumb_canvas.itemconfigure(self.thumb_window, width=self.THUMB_CONTENT_W)
            try:
                # Scrollbar is outside the thumbnail content area, not over the rightmost column.
                self.thumb_scrollbar.place(x=self.THUMB_CONTENT_W, y=22, width=self.THUMB_SCROLL_W, height=max(1, panel_h - 22), relheight=0)
            except Exception:
                pass
            self.after_idle(self._update_nav_status)

            # Keep both dividers on top so they can always receive mouse events
            self.files_thumb_divider.lift()
            self.thumb_zoom_divider.lift()
            if event is not None and self.files and not self._dragging_thumb_divider and not self._dragging_files_divider:
                self.after_idle(self._sync_columns_to_current_thumb_size)
        except Exception:
            pass

    def _enforce_thumb_width(self, event=None):
        """Apply current thumbnail/zoom split and keep content clear of the scrollbar."""
        try:
            self.current_thumb_pane_w = self.THUMBS_W
            self.THUMB_CONTENT_W = max(80, self.THUMBS_W - self.THUMB_SCROLL_W)
            self.thumb_canvas.itemconfigure(self.thumb_window, width=self.THUMB_CONTENT_W)
            self._layout_body_panels()
        except Exception:
            pass

    def _thumb_width_limit(self):
        return self.THUMB_CONTENT_W

    def _set_main_sashes(self):
        self._layout_body_panels()

    def on_thumb_zoom_divider_released(self, event=None):
        self._set_thumb_sash(refresh_layout=True)

    def _set_thumb_sash(self, refresh_layout=False):
        try:
            self._enforce_thumb_width()
            if refresh_layout and self.files:
                self.refresh_thumbs()
        except Exception:
            pass

    def _set_all_sashes(self):
        self._set_main_sashes()
        self._set_thumb_sash()

    def show_busy(self, message="Sorting and Generating Thumbnails..."):
        try:
            self.busy_label.configure(text=message)
            self.busy_overlay.place(x=0, y=22, relwidth=1, relheight=1, height=-22)
            self.busy_overlay.lift()
            self.update_idletasks()
        except Exception:
            pass

    def hide_busy(self):
        try:
            self.busy_overlay.place_forget()
        except Exception:
            pass

    # Thumbnail layout controls

    def _safe_int_var(self, var, default=1):
        try:
            return max(1, int(var.get()))
        except Exception:
            try:
                var.set(default)
            except Exception:
                pass
            return default

    def _set_rows_without_refresh(self, rows):
        self._syncing_spinners = True
        try:
            self.rows_var.set(max(1, int(rows)))
        finally:
            self._syncing_spinners = False

    def _set_cols_without_refresh(self, cols):
        self._syncing_spinners = True
        try:
            self.cols_var.set(max(1, int(cols)))
        finally:
            self._syncing_spinners = False

    def _on_cols_changed(self):
        if self._syncing_spinners:
            return
        cols = self._safe_int_var(self.cols_var, 6)
        rows = max(1, (len(self.files) + cols - 1) // cols) if self.files else 1
        self._set_rows_without_refresh(rows)
        self.refresh_thumbs()

    def _on_rows_changed(self):
        if self._syncing_spinners:
            return
        rows = self._safe_int_var(self.rows_var, 1)
        cols = max(1, (len(self.files) + rows - 1) // rows) if self.files else self._safe_int_var(self.cols_var, 6)
        self._set_cols_without_refresh(cols)
        self.refresh_thumbs()

    def _start_files_divider_drag(self, event):
        self._dragging_files_divider = True
        self._drag_files_start_x = event.x_root
        self._drag_files_start_w = self.FILES_W
        return "break"

    def _drag_files_divider(self, event):
        try:
            dx = event.x_root - self._drag_files_start_x
            total_w = max(1, self.body.winfo_width())
            max_w = max(self.FILES_MIN_W, total_w - self.TREE_W - self.DIVIDER_W - 160 - self.DIVIDER_W - self.ZOOM_MIN_W)
            self.FILES_W = max(self.FILES_MIN_W, min(max_w, self._drag_files_start_w + dx))
            self._layout_body_panels()
        except Exception:
            pass
        return "break"

    def _end_files_divider_drag(self, event):
        self._dragging_files_divider = False
        return "break"

    def _start_thumb_divider_drag(self, event):
        self._dragging_thumb_divider = True
        self._drag_start_x = event.x_root
        self._drag_start_thumbs_w = self.THUMBS_W
        self._drag_keep_cell_w = self._thumb_cell_w_for_drag
        return "break"

    def _drag_thumb_divider(self, event):
        try:
            dx = event.x_root - self._drag_start_x
            total_w = max(1, self.body.winfo_width())
            left_fixed = self.TREE_W + self.FILES_W + self.DIVIDER_W
            min_w = 160
            max_w = max(min_w, total_w - left_fixed - self.DIVIDER_W - self.ZOOM_MIN_W)
            self.THUMBS_W = max(min_w, min(max_w, self._drag_start_thumbs_w + dx))
            self.THUMB_CONTENT_W = max(80, self.THUMBS_W - self.THUMB_SCROLL_W)
            self._layout_body_panels()
        except Exception:
            pass
        return "break"

    def _end_thumb_divider_drag(self, event):
        self._dragging_thumb_divider = False
        self._syncing_file_selection = False
        self._sync_columns_to_current_thumb_size()
        self.refresh_thumbs()
        return "break"

    def _sync_columns_to_current_thumb_size(self):
        """When the divider changes the thumbnail panel width, adjust columns to preserve thumb size."""
        if not self.files:
            return
        target_cell_w = self._drag_keep_cell_w or self._thumb_cell_w_for_drag
        if not target_cell_w:
            return
        try:
            gap = 6
            boundary_gap = 3
            available = max(1, self.THUMB_CONTENT_W - 2 * boundary_gap)
            cols = max(1, int(round((available + gap) / (target_cell_w + gap))))
            rows = max(1, (len(self.files) + cols - 1) // cols)
            self._set_cols_without_refresh(cols)
            self._set_rows_without_refresh(rows)
        except Exception:
            pass

    # Mode helpers

    def _is_contact_sheets_folder(self, folder=None):
        """True for the generated contact-sheet output folder.

        Contact sheets are PDF files even when FTView is currently in Photos
        mode, so this folder must be scanned with PDF_EXTS or it will appear
        empty and show "No Images".
        """
        folder = folder if folder is not None else getattr(self, "current_folder", "")
        try:
            return os.path.basename(_ui_path(folder).rstrip("/\\")).lower() == "_contactsheets"
        except Exception:
            return False

    @property
    def _active_viewer(self):
        """Return the viewer panel for the current mode."""
        if self.mode == "movies" and self.movie_viewer is not None:
            return self.movie_viewer
        return self.viewer

    def _activate_viewer(self, mode):
        """Show the correct viewer panel, hide the other."""
        try:
            if mode == "movies" and self.movie_viewer is not None:
                self.viewer.pack_forget()
                self.movie_viewer.pack(fill="both", expand=True)
            else:
                if self.movie_viewer is not None:
                    self.movie_viewer.pack_forget()
                self.viewer.pack(fill="both", expand=True)
            self.zoom = self.viewer.canvas
        except Exception:
            pass

    def _current_exts(self, folder=None):
        if self._is_contact_sheets_folder(folder):
            return PDF_EXTS
        if self.mode == "movies":
            return VIDEO_EXTS
        return DOCUMENT_EXTS if self.mode == "pdfs" else PHOTO_EXTS

    def _set_folder_tree_extensions(self):
        """Keep shared FileCountTree using FTView's current file type set."""
        try:
            self.folder_tree._extensions = {e.lower() for e in self._current_exts(self.root_var.get())}
        except Exception:
            pass

    def _refresh_folder_tree_counts_for_paths(self, paths):
        """Delegate folder count refresh to shared FileCountTree."""
        try:
            self.folder_tree.refresh_after_file_ops(paths)
        except Exception:
            pass

    def _current_file_word(self):
        if self.mode == "movies":
            return "video files"
        return "PDF files" if (self.mode == "pdfs" or self._is_contact_sheets_folder()) else "image files"

    def _empty_folder_message(self):
        """Return the mode-aware message used when the selected folder has no displayable files."""
        if self.mode == "movies":
            return "No Videos"
        return "No PDFs" if (self.mode == "pdfs" or self._is_contact_sheets_folder()) else "No Images"

    def _on_mode_changed(self):
        selected = self.mode_var.get().strip().lower()
        if selected.startswith("pdf"):
            new_mode = "pdfs"
        elif selected.startswith("movie"):
            new_mode = "movies"
        else:
            new_mode = "photos"
        if new_mode == self.mode:
            return
        # Remember the root last used in the previous mode.
        try:
            self.roots_by_mode[self.mode] = self.root_var.get().strip()
        except Exception:
            pass
        self.mode = new_mode
        self.root_var.set(self.roots_by_mode.get(self.mode, DEFAULT_ROOT))
        try:
            self._set_folder_tree_extensions()
        except Exception:
            pass
        self._activate_viewer(self.mode)
        self.clear_files(message=f"Select a {self.mode_var.get()} folder")
        try:
            _save_ftview_ini(self.roots_by_mode.get("photos", DEFAULT_ROOT),
                             self.roots_by_mode.get("pdfs", DEFAULT_ROOT),
                             self.mode,
                             self.roots_by_mode.get("movies", DEFAULT_ROOT))
        except Exception:
            pass
        root = self.root_var.get().strip()
        if os.path.isdir(root):
            self.set_root(root, ask_save=False)

    # Root/tree

    def browse_root(self):
        folder = filedialog.askdirectory(parent=self, title="Select root folder")
        if folder:
            self.root_var.set(folder)
            self.set_root(folder, ask_save=True)

    def _initial_root(self):
        root = self.root_var.get().strip()
        if os.path.isdir(root):
            self.set_root(root)
        else:
            self.status_var.set(f"Root not found: {root}")

    def set_root(self, root, ask_save=False):
        if not root or not os.path.isdir(root):
            messagebox.showwarning("Root not found", f"Folder not found:\n{root}", parent=self)
            return
        root = _ui_path(root)
        previous = self.roots_by_mode.get(self.mode, DEFAULT_ROOT)
        self.root_var.set(root)
        self.roots_by_mode[self.mode] = root
        # Always silently save root to INI when it changes — no dialog needed
        if os.path.normcase(root) != os.path.normcase(previous):
            try:
                _save_ftview_ini(self.roots_by_mode.get("photos", DEFAULT_ROOT),
                                 self.roots_by_mode.get("pdfs", DEFAULT_ROOT),
                                 self.mode,
                                 self.roots_by_mode.get("movies", DEFAULT_ROOT))
            except Exception:
                pass
        try:
            self._set_folder_tree_extensions()
        except Exception:
            pass
        self.clear_files(message=f"No {self._current_file_word()} selected")
        self.current_folder = ""
        self.folder_tree.set_root(root)
        self.status_var.set(root)

    def _folder_has_images(self, folder):
        """Return True when folder directly contains displayable image files."""
        return _count_files(folder, self._current_exts()) > 0

    def _has_files_below(self, folder):
        """Return True if folder or any descendant contains JPG/JPEG files."""
        if self._folder_has_images(folder):
            return True
        try:
            entries = sorted(
                [e for e in os.scandir(_longpath(folder)) if e.is_dir()],
                key=lambda e: e.name.lower()
            )
        except Exception:
            return False
        for entry in entries:
            if self._has_files_below(entry.path):
                return True
        return False

    def _find_first_populated(self, folder):
        """Find the first folder at or below folder that directly contains images."""
        folder = os.path.normpath(folder)
        if self._folder_has_images(folder):
            return folder
        try:
            entries = sorted(
                [e for e in os.scandir(_longpath(folder)) if e.is_dir()],
                key=lambda e: e.name.lower()
            )
        except Exception:
            return None
        for entry in entries:
            found = self._find_first_populated(entry.path)
            if found:
                return os.path.normpath(found)
        return None

    def _path_chain_from_root(self, target):
        """Return filesystem ancestor paths from current root to target."""
        root = os.path.normpath(getattr(self.folder_tree, "_root_path", "") or self.root_var.get().strip())
        target = os.path.normpath(target)
        try:
            if os.path.normcase(os.path.commonpath([root, target])) != os.path.normcase(root):
                return [target]
        except Exception:
            return [target]

        chain = [root]
        rel = os.path.relpath(target, root)
        if rel in (".", ""):
            return chain
        cur = root
        for part in rel.split(os.sep):
            cur = os.path.normpath(os.path.join(cur, part))
            chain.append(cur)
        return chain

    def _force_build_tree_path(self, target):
        """Build, expand, reveal and select target in the lazy ft_widgets tree."""
        target = os.path.normpath(target)
        chain = self._path_chain_from_root(target)

        root = chain[0] if chain else target
        if not self.tree.exists(root) and os.path.isdir(root):
            self.folder_tree.set_root(root)

        for idx, path in enumerate(chain):
            if not self.tree.exists(path):
                parent = chain[idx - 1] if idx > 0 else ""
                if parent and self.tree.exists(parent):
                    try:
                        self.folder_tree._on_node_open(parent)
                    except Exception:
                        pass

            if self.tree.exists(path):
                try:
                    self.tree.item(path, open=True)
                    self.folder_tree._ensure_children(path)
                except Exception:
                    pass
                try:
                    self.folder_tree._fill_children_of(path)
                except Exception:
                    pass

        if self.tree.exists(target):
            self.tree.selection_set(target)
            self.tree.focus(target)
            self.tree.see(target)
            return target
        return None

    def on_tree_click(self, event=None):
        """Load clicked folder, unless Copy/Move target dialog is active.

        When the target dialog is open, clicking the main folder tree should
        choose the target folder only.  It must not navigate FTView away from
        the current source folder being copied/moved.
        """
        if event is None:
            return
        item = self.tree.identify_row(event.y)
        if not item or self.folder_tree.PLACEHOLDER in item or not os.path.isdir(item):
            return

        element = self.tree.identify_element(event.x, event.y)
        if "indicator" in str(element).lower():
            return

        target = _ui_path(item)

        if getattr(self, "_destination_dialog_active", False):
            var = getattr(self, "_destination_folder_var", None)
            if var is not None:
                try:
                    var.set(target)
                    self.status_var.set(f"Target folder: {target}")
                except Exception:
                    pass
            return

        if target == self.current_folder:
            return

        self.load_folder(target)

    # Files/thumbs

    def clear_files(self, message=None):
        if message is None:
            message = self._empty_folder_message()
        self.files = []
        self.selected_idx = None
        self.selected_files.clear()
        self.file_list.delete(*self.file_list.get_children(""))
        self.clear_thumbs()
        try:
            self._active_viewer.set_file_list([], None)
            self._active_viewer.show_message(message)
        except Exception:
            self.zoom.set_message(message)

    def clear_thumbs(self):
        # Invalidate any thumbnail work still pending from a previous folder/refresh.
        self.thumb_generation += 1
        self.thumb_queue = None
        self.thumb_request_queue = None
        self.thumb_requested.clear()
        self.thumb_loaded.clear()
        self.thumb_loading.clear()
        for w in self.thumb_inner.winfo_children():
            w.destroy()
        # Clear previous grid column sizing so changing column count cannot
        # leave old wider geometry behind.
        for c in range(50):
            self.thumb_inner.grid_columnconfigure(c, minsize=0, weight=0)
        self.thumb_refs.clear()
        self.thumb_cells.clear()
        self.thumb_labels.clear()
        self.thumb_total_h = 1
        self.thumb_canvas.yview_moveto(0)
        self.thumb_canvas.configure(scrollregion=(0, 0, self.THUMB_CONTENT_W, 1))
        self._update_nav_status()

    def load_folder(self, folder):
        folder = _ui_path(folder)
        self.current_folder = folder
        self.all_files = _scan_files(folder, self._current_exts(folder))
        self.files = list(self.all_files)
        self.selected_idx = None
        self.last_selected_idx = None
        self.selected_files.clear()
        self.view_filter = "all"

        self.apply_file_sort()
        self.rebuild_file_list()
        # Update viewer file list immediately
        try:
            if self.files:
                self._active_viewer.set_file_list(self.files, None)
                self._active_viewer.show_message("No file selected")
            else:
                self._active_viewer.set_file_list([], None)
                self._active_viewer.show_message(self._empty_folder_message())
        except Exception:
            pass
        self.status_var.set(f"{folder}   ({len(self.files)} files)")
        # Defer thumb generation so file list renders first
        self.after(50, self.refresh_thumbs)

    def rebuild_file_list(self):
        self.file_list.delete(*self.file_list.get_children(""))
        for idx, p in enumerate(self.files):
            # Show file size immediately — duration loaded in background for movies
            col2 = _file_size_text(p)
            self.file_list.insert("", "end", iid=str(idx), values=(_file_display_name(p), col2))
        # Update durations in background for movies (ffprobe is slow)
        if self.mode == "movies" and HAVE_FT_MOVIE:
            self.after(100, self._update_movie_durations)

    def _update_movie_durations(self):
        """Update file list duration column using a small bounded thread pool.

        Previously spawned one thread per file, which caused hundreds of
        simultaneous ffprobe processes and saturated the CPU before any
        thumbnails could load.  Now uses 2 workers sharing a queue so the
        system stays responsive while durations trickle in.
        """
        import queue as _q
        files = list(self.files)
        generation = self.thumb_generation
        work_q = _q.Queue()
        for idx, path in enumerate(files):
            work_q.put((idx, path))

        def _worker():
            while True:
                try:
                    idx, path = work_q.get_nowait()
                except _q.Empty:
                    return
                if self.thumb_generation != generation:
                    return
                try:
                    info = get_video_info(path, _longpath)
                    dur  = _fmt_video_duration(info.get("duration_s", 0))
                except Exception:
                    dur = ""
                def _apply(i=idx, d=dur, g=generation):
                    if self.thumb_generation != g:
                        return
                    try:
                        self.file_list.set(str(i), "#2", d)
                    except Exception:
                        pass
                self.after(0, _apply)

        import threading
        for _ in range(2):   # 2 workers — enough to fill the column without hammering I/O
            threading.Thread(target=_worker, daemon=True).start()

    def _on_file_list_mouse_down(self, event):
        """Windows-style filename edit: first click selects/previews, second click renames stem."""
        try:
            row = self.file_list.identify_row(event.y)
            col = self.file_list.identify_column(event.x)
            if not row or col not in ("#1", "#2"):
                return None
            if row in self.file_list.selection():
                self.after(1, lambda iid=row: self._begin_file_list_rename(iid))
                return "break"
        except Exception:
            pass
        return None

    def _begin_file_list_rename(self, iid):
        """Overlay an Entry on the selected file-list row and rename the filename stem only."""
        try:
            idx = int(iid)
            if idx < 0 or idx >= len(self.files):
                return
            old_path = self.files[idx]
            folder = os.path.dirname(old_path)
            base = os.path.basename(old_path)
            stem, ext = os.path.splitext(base)
            try:
                old_editor = getattr(self, "_file_rename_entry", None)
                if old_editor:
                    old_editor.destroy()
            except Exception:
                pass

            bbox = self.file_list.bbox(iid, "#1")
            if not bbox:
                return
            x, y, w, h = bbox
            entry = tk.Entry(self.file_list, font=("Segoe UI", 10), relief="solid", bd=1)
            self._file_rename_entry = entry
            entry.insert(0, stem)
            entry.select_range(0, "end")
            entry.place(x=x, y=y, width=w, height=h)
            entry.focus_set()

            def cancel(event=None):
                try: entry.destroy()
                except Exception: pass
                if getattr(self, "_file_rename_entry", None) is entry:
                    self._file_rename_entry = None

            def commit(event=None):
                new_stem = entry.get().strip()
                cancel()
                if not new_stem or new_stem == stem:
                    return
                if any(ch in new_stem for ch in r'\/:*?"<>|'):
                    messagebox.showerror("Rename file", "Filename cannot contain: \\ / : * ? \" < > |", parent=self)
                    return
                new_path = os.path.join(folder, new_stem + ext)
                if os.path.normcase(os.path.normpath(new_path)) == os.path.normcase(os.path.normpath(old_path)):
                    return
                if os.path.exists(new_path):
                    messagebox.showerror("Rename file", f"A file already exists with this name:\n\n{os.path.basename(new_path)}", parent=self)
                    return
                try:
                    os.rename(old_path, new_path)
                except Exception as e:
                    messagebox.showerror("Rename file", f"Could not rename file:\n\n{e}", parent=self)
                    return

                def repl(seq):
                    return [new_path if os.path.normcase(os.path.normpath(p)) == os.path.normcase(os.path.normpath(old_path)) else p for p in seq]
                self.all_files = repl(getattr(self, "all_files", []))
                self.files = repl(getattr(self, "files", []))
                try:
                    if old_path in self.selected_files:
                        self.selected_files.discard(old_path)
                        self.selected_files.add(new_path)
                except Exception:
                    pass
                self.apply_file_sort()
                self.rebuild_file_list()
                self.refresh_thumbs()
                if new_path in self.files:
                    self.select_index(self.files.index(new_path), from_thumb=False)
                try:
                    tree = getattr(self, "folder_tree", None)
                    if tree and hasattr(tree, "refresh_after_file_ops"):
                        tree.refresh_after_file_ops([folder])
                except Exception:
                    pass

            entry.bind("<Return>", commit)
            entry.bind("<Escape>", cancel)
            entry.bind("<FocusOut>", commit)
        except Exception as e:
            print(f"_begin_file_list_rename error: {e}")

    def _sort_key(self, p):
        if self.file_sort_column == "size":
            return (_file_size_value(p), os.path.basename(p).lower())
        return os.path.basename(p).lower()

    def _apply_view_filter(self):
        source = list(self.all_files)
        if self.view_filter == "selected":
            source = [p for p in source if p in self.selected_files]
        self.files = sorted(source, key=self._sort_key, reverse=self.file_sort_reverse)

    def apply_file_sort(self):
        self.all_files = sorted(list(self.all_files), key=self._sort_key, reverse=self.file_sort_reverse)
        self._apply_view_filter()

    def _show_sort_menu(self):
        """Show sort popup menu anchored to the Sort button."""
        if show_file_sort_menu is None:
            return
        show_file_sort_menu(
            self._sort_btn,
            columns=[("Name", "file"), ("Size", "size")],
            sort_column=self.file_sort_column,
            sort_reverse=self.file_sort_reverse,
            callback=self._set_file_sort,
        )

    def _set_file_sort(self, column: str, reverse: bool):
        try:
            self._sort_btn.config(text=_sort_btn_label(column, reverse))
        except Exception:
            pass
        self.sort_files(column, reverse=reverse)

    def sort_files(self, column, reverse=None):
        self.show_busy("Sorting and rebuilding thumbnail grid...")
        selected_path = self.files[self.selected_idx] if self.selected_idx is not None and 0 <= self.selected_idx < len(self.files) else None

        if reverse is not None:
            self.file_sort_column  = column
            self.file_sort_reverse = reverse
        elif self.file_sort_column == column:
            self.file_sort_reverse = not self.file_sort_reverse
        else:
            self.file_sort_column  = column
            self.file_sort_reverse = False

        self.apply_file_sort()
        self.rebuild_file_list()
        self.refresh_thumbs()

        if selected_path in self.files:
            self.select_index(self.files.index(selected_path))

        direction = "descending" if self.file_sort_reverse else "ascending"
        label = "size" if self.file_sort_column == "size" else "filename"
        self.status_var.set(f"Sorted by {label}, {direction}")

    def refresh_current(self):
        if self.current_folder:
            self.load_folder(self.current_folder)
        else:
            self.set_root(self.root_var.get().strip())

    def refresh_thumbs(self):
        self.status_var.set("Building thumbnails...")
        self.clear_thumbs()
        if not self.files:
            msg = tk.Label(
                self.thumb_inner,
                text=self._empty_folder_message(),
                bg=BG, fg=DIM,
                font=("Segoe UI", 24, "bold"),
                anchor="center",
            )
            msg.place(x=0, y=0, width=max(1, self.THUMB_CONTENT_W), height=max(1, self.thumb_canvas.winfo_height()))
            self.thumb_inner.configure(width=self.THUMB_CONTENT_W, height=max(1, self.thumb_canvas.winfo_height()))
            self.thumb_total_h = max(1, self.thumb_canvas.winfo_height())
            self.thumb_canvas.configure(scrollregion=(0, 0, self.THUMB_CONTENT_W, self.thumb_total_h))
            self._update_nav_status()
            try:
                self._active_viewer.set_file_list([], None)
                self._active_viewer.show_message(self._empty_folder_message())
            except Exception:
                pass
            self.hide_busy()
            return

        cols = self._safe_int_var(self.cols_var, 6)
        rows = max(1, (len(self.files) + cols - 1) // cols)
        self._set_rows_without_refresh(rows)

        # The thumbnail GRID content width excludes the scrollbar.
        thumb_area_w = self.THUMB_CONTENT_W
        self.thumb_canvas.itemconfigure(self.thumb_window, width=self.THUMB_CONTENT_W)

        # Shared thumbnail layout policy.
        # Geometry belongs in ft_thumb_layout.py so FTView, FT.py and other
        # apps can use the same rules instead of each app inventing its own.
        gap = 6
        layout = calculate_thumb_layout(
            panel_width=thumb_area_w,
            panel_height=940,
            item_count=len(self.files),
            columns=cols,
            gap=gap,
            boundary_gap=3,
            cell_ratio_w_to_h=0.85,
            image_margin=5,
        )
        # Console layout diagnostics disabled for normal use.
        self._thumb_layout = layout
        self._thumb_cell_w_for_drag = layout.cell_w

        cell_w = layout.cell_w_px
        cell_h = layout.cell_h_px
        img_x = layout.image_x_px
        img_y = layout.image_y_px
        thumb_w = layout.image_w_px
        thumb_h = layout.image_h_px

        self.thumb_cols = cols
        self.thumb_box = (thumb_w, thumb_h)
        self.thumb_cell_h = cell_h + gap

        # Batch-and-breathe: build cells in groups of 10, yielding to the
        # event loop between batches so clicks/scrolls work immediately.
        generation = self.thumb_generation

        # Set scroll region now so scrollbar is correct before cells appear
        for c in range(cols):
            self.thumb_inner.grid_columnconfigure(c, minsize=0, weight=0)
        total_h = max(1, int(round(layout.total_h)))
        self.thumb_total_h = total_h
        self.thumb_inner.configure(width=self.THUMB_CONTENT_W, height=total_h)
        self.thumb_canvas.configure(scrollregion=(0, 0, thumb_area_w, total_h))
        self._clamp_thumb_scroll()
        self._update_nav_status()
        self.hide_busy()

        def _build_batch(start_i):
            if self.thumb_generation != generation:
                return
            end_i = min(start_i + 10, len(self.files))
            for idx in range(start_i, end_i):
                path = self.files[idx]
                row, col = divmod(idx, cols)
                cell = tk.Frame(self.thumb_inner, bg="white",
                                width=cell_w, height=cell_h,
                                highlightthickness=0, bd=0)
                left_gap = int(round(layout.boundary_gap)) if col == 0 else int(round(layout.gap))
                top_gap  = int(round(layout.boundary_gap)) if row == 0 else int(round(layout.gap))
                cell.grid(row=row, column=col, padx=(left_gap, 0), pady=(top_gap, 0), sticky="nw")
                cell.grid_propagate(False)

                img_lbl = tk.Label(cell, text="", bg="white", highlightthickness=0, bd=0)
                img_lbl.place(x=img_x, y=img_y, width=thumb_w, height=thumb_h)

                selected_lbl = tk.Label(cell, text="SELECTED", bg="#111111", fg="white",
                                        font=("Segoe UI", 8, "bold"), padx=3, pady=1)

                name_y = img_y + thumb_h + 6
                name_h = max(12, cell_h - name_y - 2)
                name_lbl = tk.Label(cell, text=os.path.basename(path),
                                    bg="white", fg=TEXT, font=("Segoe UI", 8),
                                    anchor="center", wraplength=max(60, cell_w - 8))
                name_lbl.place(x=3, y=name_y, width=cell_w - 6, height=name_h)

                self.thumb_cells[idx]      = cell
                self.thumb_labels[idx]     = (img_lbl, name_lbl)
                self.thumb_watermarks[idx] = selected_lbl
                self._set_thumb_watermark(idx, path in self.selected_files)

                for widget in (cell, img_lbl, name_lbl, selected_lbl):
                    widget.bind("<Button-1>",   lambda e, i=idx: self.on_thumb_click(i, e))
                    widget.bind("<Button-3>",   lambda e, i=idx: self.show_thumb_menu(e, i))
                    widget.bind("<MouseWheel>", self.on_thumb_wheel)

            if end_i < len(self.files):
                # Yield to event loop for 10ms then continue next batch
                self.after(10, lambda s=end_i: _build_batch(s))
            else:
                # All cells built — load visible thumbnails
                self.after(10, self.schedule_visible_thumbs)

        _build_batch(0)

    def schedule_visible_thumbs(self):
        """Request thumbnails only for the visible part of the thumbnail panel.

        Large folders may contain thousands of files, but only the images near
        the current scroll position are decoded. Clicking another folder
        increments thumb_generation, cancelling old pending work before it can
        update the UI.
        """
        if not self.files or not self.thumb_labels:
            return

        generation = self.thumb_generation
        try:
            y0 = self.thumb_canvas.canvasy(0)
            y1 = y0 + max(1, self.thumb_canvas.winfo_height())
        except Exception:
            y0, y1 = 0, 900

        # Load roughly two screenfuls: the visible screen plus one buffer screen.
        buffer = max(200, y1 - y0)
        start_row = max(0, int((y0 - buffer) // max(1, self.thumb_cell_h)))
        end_row = int((y1 + buffer) // max(1, self.thumb_cell_h)) + 1
        start_idx = start_row * self.thumb_cols
        end_idx = min(len(self.files), (end_row + 1) * self.thumb_cols)

        wanted = []
        for idx in range(start_idx, end_idx):
            if idx in self.thumb_loaded or idx in self.thumb_loading or idx in self.thumb_requested:
                continue
            if self.thumb_labels.get(idx):
                wanted.append(idx)

        if not wanted:
            return

        self.status_var.set("Generating visible thumbnails...")
        if self.thumb_request_queue is None:
            self.thumb_request_queue = queue.Queue()

        for idx in wanted:
            self.thumb_requested.add(idx)
            self.thumb_request_queue.put((generation, idx, self.files[idx], self.thumb_labels[idx][0], self.thumb_box[0], self.thumb_box[1]))

        while self.thumb_loader_workers < self.MAX_THUMB_WORKERS:
            self.thumb_loader_workers += 1
            threading.Thread(target=self.thumb_loader_worker, daemon=True).start()

    def thumb_loader_worker(self):
        while True:
            q = self.thumb_request_queue
            if q is None:
                self.thumb_loader_workers = max(0, self.thumb_loader_workers - 1)
                if self.thumb_loader_workers == 0:
                    self.after(0, lambda: self.status_var.set(f"{self.current_folder}   ({len(self.files)} files)" if self.current_folder else "Ready"))
                return
            try:
                item = q.get(timeout=0.4)
            except queue.Empty:
                self.thumb_loader_workers = max(0, self.thumb_loader_workers - 1)
                if self.thumb_loader_workers == 0:
                    self.after(0, lambda: self.status_var.set(f"{self.current_folder}   ({len(self.files)} files)" if self.current_folder else "Ready"))
                return

            generation, idx, path, label, bw, bh = item
            if generation != self.thumb_generation:
                continue

            def mark_loading(g=generation, i=idx):
                if g == self.thumb_generation:
                    self.thumb_requested.discard(i)
                    self.thumb_loading.add(i)
            self.after(0, mark_loading)

            # Shared viewer thumbnail pipeline. Handles photos and PDFs for FTView
            # without changing ft_thumbs.py or other apps during testing.
            thumb_size = max(1, min(int(bw), int(bh)))

            # Cache-first: reuse thumbnail stored by FTMod or FTVideo
            img = None
            if _HAVE_THUMB_CACHE:
                try:
                    jpeg_bytes = _ft_thumb_cache.get_thumb(_ui_path(path))
                    if jpeg_bytes:
                        stored = Image.open(_BytesIO(jpeg_bytes)).convert("RGB")
                        stored.thumbnail((thumb_size, thumb_size), Image.LANCZOS)
                        img = stored
                except Exception as _cache_ex:
                    print(f"FTView cache thumb error ({os.path.basename(path)}): {_cache_ex}")
                    img = None

            if img is None:
                # Generate at store size so the cached entry is useful to FTMod too
                store_size = max(thumb_size, _THUMB_STORE_SIZE)
                if self.mode == "movies" and HAVE_FT_MOVIE:
                    img, ok, err = make_movie_thumbnail_fast(
                        path, MoviePlayerPanel.THUMB_POSITION, store_size, longpath_func=_longpath
                    )
                else:
                    img, ok, err = make_preview_thumbnail(path, store_size, longpath_func=_longpath)

                # Store in global cache before scaling for display
                if img is not None and _HAVE_THUMB_CACHE:
                    try:
                        buf = _BytesIO()
                        img.save(buf, "JPEG", quality=85)
                        _ft_thumb_cache.put_thumb(_ui_path(path), buf.getvalue())
                    except Exception:
                        pass

                # Scale to display size
                if img is not None:
                    img.thumbnail((thumb_size, thumb_size), Image.LANCZOS)

            self._queue_thumb_apply(generation, idx, label, img)

    def _queue_thumb_apply(self, generation, idx, label, img):
        """Queue a thumbnail for UI application — drained in batches of 5 every 50ms."""
        if not hasattr(self, '_thumb_apply_queue'):
            self._thumb_apply_queue = []
            self._thumb_apply_running = False
        self._thumb_apply_queue.append((generation, idx, label, img))
        if not self._thumb_apply_running:
            self._thumb_apply_running = True
            self.after(10, self._drain_thumb_apply_queue)

    def _drain_thumb_apply_queue(self):
        """Apply up to 5 pending thumbnails then yield to the event loop."""
        if not hasattr(self, '_thumb_apply_queue'):
            self._thumb_apply_running = False
            return
        batch = self._thumb_apply_queue[:5]
        self._thumb_apply_queue = self._thumb_apply_queue[5:]
        for generation, idx, label, img in batch:
            self.apply_thumb(generation, idx, label, img)
        if self._thumb_apply_queue:
            self.after(10, self._drain_thumb_apply_queue)
        else:
            self._thumb_apply_running = False

    def apply_thumb(self, generation, idx, label, img):
        if generation != self.thumb_generation:
            return
        self.thumb_loading.discard(idx)
        self.thumb_requested.discard(idx)
        try:
            if not label.winfo_exists():
                return
            photo = ImageTk.PhotoImage(img)
            self.thumb_refs.append(photo)
            self.thumb_loaded.add(idx)
            label.image = photo
            label.configure(image=photo)
            if idx < len(self.files):
                self._set_thumb_watermark(idx, self.files[idx] in self.selected_files)
        except Exception:
            pass

    def _draw_selected_watermark(self, img):
        """Return a copy of img with a SELECTED watermark. Runs in worker threads."""
        base = img.convert("RGBA")
        overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
        from PIL import ImageDraw, ImageFont
        draw = ImageDraw.Draw(overlay)
        text = "SELECTED"
        size = max(14, min(base.size) // 6)
        try:
            font = ImageFont.truetype("arialbd.ttf", size)
        except Exception:
            font = ImageFont.load_default()
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        except Exception:
            tw, th = draw.textsize(text, font=font)
        pad = max(4, size // 3)
        x = max(0, (base.size[0] - tw) // 2)
        y = max(0, (base.size[1] - th) // 2)
        draw.rectangle((x - pad, y - pad, x + tw + pad, y + th + pad), fill=(0, 0, 0, 120))
        draw.text((x, y), text, font=font, fill=(255, 255, 255, 230))
        return Image.alpha_composite(base, overlay).convert("RGB")

    def _set_thumb_watermark(self, idx, selected):
        """Show/hide the SELECTED overlay without regenerating the thumbnail."""
        lbl = self.thumb_watermarks.get(idx)
        if lbl is None:
            return
        try:
            if selected:
                # Centre over the image area.  Use place so this does not affect
                # grid geometry or canvas scroll position.
                cell = self.thumb_cells.get(idx)
                labels = self.thumb_labels.get(idx, ())
                img_lbl = labels[0] if labels else None
                if img_lbl is not None:
                    wm_w, wm_h = 74, 16
                    x = img_lbl.winfo_x() + max(0, (img_lbl.winfo_width()  - wm_w) // 2)
                    y = img_lbl.winfo_y() + max(0,  img_lbl.winfo_height() - wm_h - 4)
                else:
                    wm_w, wm_h = 74, 16
                    x, y = 8, 8
                lbl.place(x=x, y=y, width=wm_w, height=wm_h)
                lbl.lift()
            else:
                lbl.place_forget()
        except Exception:
            pass

    def _refresh_thumb_image(self, idx):
        """Refresh selection display for one visible thumbnail cell.

        This deliberately does NOT rebuild the thumbnail image.  Rebuilding the
        PIL image for every click was the cause of slow watermarking and could
        disturb the canvas while async thumbnail work completed.
        """
        if idx < 0 or idx >= len(self.files):
            return
        self._set_thumb_watermark(idx, self.files[idx] in self.selected_files)

    def _clamp_thumb_scroll(self):
        """Keep thumbnail canvas inside the real row-model bounds."""
        try:
            total_h = max(1, int(self.thumb_total_h))
            visible_h = max(1, int(self.thumb_canvas.winfo_height()))
            max_y = max(0, total_h - visible_h)
            y = int(self.thumb_canvas.canvasy(0))
            y = max(0, min(y, max_y))
            self.thumb_canvas.yview_moveto(0 if total_h <= visible_h else y / total_h)
        except Exception:
            pass

    def on_thumb_wheel(self, event):
        try:
            total_h = max(1, int(self.thumb_total_h))
            visible_h = max(1, int(self.thumb_canvas.winfo_height()))
            max_y = max(0, total_h - visible_h)
            current_y = int(self.thumb_canvas.canvasy(0))
            direction = -1 if event.delta > 0 else 1
            row_step = max(1, int(self.thumb_cell_h))
            new_y = max(0, min(current_y + direction * row_step, max_y))
            self.thumb_canvas.yview_moveto(0 if total_h <= visible_h else new_y / total_h)
        except Exception:
            self.thumb_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
            self._clamp_thumb_scroll()
        self.after_idle(self._after_thumb_scroll)
        return "break"

    # Selection/zoom

    def on_file_select(self, event=None):
        if getattr(self, "_syncing_file_selection", False):
            return
        if getattr(self, "_ignore_file_select_until_idle", False):
            return
        sel = self.file_list.selection()
        if not sel:
            return
        try:
            idx = int(sel[0])
        except Exception:
            return
        self.select_index(idx, from_thumb=False)

    def select_index(self, idx, from_thumb=False):
        if idx < 0 or idx >= len(self.files):
            return
        previous_idx = self.selected_idx
        if self.selected_idx == idx:
            # Even if selection is unchanged, make sure both panes are visible.
            if not from_thumb:
                try:
                    self.file_list.see(str(idx))
                except Exception:
                    pass

                self._scroll_thumb_to_index(idx, select=False)
            self.highlight_thumb(idx)
            return

        self.selected_idx = idx

        # If the user selected from the file list or viewer buttons, scroll the
        # thumbnail canvas so the matching thumbnail is visible.  If the user
        # clicked a thumbnail, it is already visible.
        if not from_thumb:
            self._scroll_thumb_to_index(idx, select=False)

        try:
            iid = str(idx)
            self._syncing_file_selection = True
            if from_thumb:
                # Treeview can deliver <<TreeviewSelect>> after this method
                # returns.  If that late event is handled as a file-list click,
                # it calls select_index(..., from_thumb=False) and scrolls the
                # thumbnail grid so the clicked item jumps to the top row.
                # Ignore that synthetic file-list event; real file-list clicks
                # still work normally.
                self._ignore_file_select_until_idle = True
                self.after(300, lambda: setattr(self, "_ignore_file_select_until_idle", False))
            self.file_list.selection_set(iid)
            self.file_list.focus(iid)
            if not from_thumb:
                self.file_list.see(iid)
        except Exception:
            pass
        finally:
            self._syncing_file_selection = False

        if previous_idx is not None and previous_idx != idx:
            self.highlight_thumb(previous_idx)
        self.highlight_thumb(idx)
        try:
            self._active_viewer.set_file_list(self.files, idx)
        except Exception:
            self.load_zoom_async(self.files[idx])
        self.status_var.set(os.path.basename(self.files[idx]))

    def highlight_thumb(self, idx):
        # Deliberately update only the affected cell.  Full-grid loops caused
        # visible stalls in large folders.
        try:
            idx = int(idx)
        except Exception:
            return
        cell = self.thumb_cells.get(idx)
        if cell is None:
            return
        active = idx == self.selected_idx
        # Multi-selection is shown by the SELECTED watermark only.  Keep cell
        # background changes limited to the single active/viewer item.
        bg = SELECT_BG if active else "white"
        try:
            cell.configure(bg=bg)
            labels = self.thumb_labels.get(idx, ())
            for lbl in labels:
                lbl.configure(bg=bg)
        except Exception:
            pass

    def on_thumb_click(self, idx, event=None):
        """Thumbnail click handling.

        Plain click only displays the image and synchronises the Files list.
        Ctrl-click toggles selection for one thumbnail.
        Shift-click applies the previous Ctrl/Shift selection action across the range.
        """
        state = getattr(event, "state", 0) if event is not None else 0
        is_shift = bool(state & 0x0001)
        is_ctrl = bool(state & 0x0004)

        if is_shift or is_ctrl:
            return self.toggle_select(idx, from_thumb=True, event=event)

        # Normal click is view-only.  It must not add/remove selection watermarks.
        if idx < 0 or idx >= len(self.files):
            return "break"
        try:
            old_yview = self.thumb_canvas.yview()
        except Exception:
            old_yview = None
        self.select_index(idx, from_thumb=True)
        if old_yview is not None:
            def restore_thumb_y(y=old_yview):
                try:
                    self.thumb_canvas.yview_moveto(y[0])
                    self._update_nav_status()
                except Exception:
                    pass
            self.after_idle(restore_thumb_y)
            self.after(50, restore_thumb_y)
        return "break"

    def toggle_select(self, idx, from_thumb=True, event=None):
        """Ctrl/Shift thumbnail selection without disturbing the viewer path."""
        if idx < 0 or idx >= len(self.files):
            return "break"

        try:
            old_yview = self.thumb_canvas.yview()
        except Exception:
            old_yview = None

        self.select_index(idx, from_thumb=from_thumb)
        if old_yview is not None:
            try:
                self.thumb_canvas.yview_moveto(old_yview[0])
            except Exception:
                pass

        state = getattr(event, "state", 0) if event is not None else 0
        is_shift = bool(state & 0x0001)
        changed = set()

        if is_shift and self.last_selected_idx is not None and 0 <= self.last_selected_idx < len(self.files):
            # Shift-click repeats the previous Ctrl/Shift selection action.
            action_select = bool(getattr(self, "last_selection_action", True))
            a, b = sorted((self.last_selected_idx, idx))
            for i in range(a, b + 1):
                path_i = self.files[i]
                if action_select:
                    self.selected_files.add(path_i)
                else:
                    self.selected_files.discard(path_i)
                changed.add(i)
            self.last_selected_idx = idx
            self.last_selection_action = action_select
        else:
            # Ctrl-click toggles exactly one thumbnail and sets the Shift anchor.
            path = self.files[idx]
            if path in self.selected_files:
                self.selected_files.remove(path)
                self.last_selection_action = False
            else:
                self.selected_files.add(path)
                self.last_selection_action = True
            self.last_selected_idx = idx
            changed.add(idx)

        if getattr(self, "selected_idx", None) is not None:
            self.highlight_thumb(self.selected_idx)
        for i in changed:
            self.highlight_thumb(i)
            self._refresh_thumb_image(i)

        if old_yview is not None:
            def restore_thumb_y(y=old_yview):
                try:
                    self.thumb_canvas.yview_moveto(y[0])
                    self._update_nav_status()
                except Exception:
                    pass
            self.after_idle(restore_thumb_y)
            self.after(50, restore_thumb_y)
            self.after(150, restore_thumb_y)

        self.status_var.set(f"{len(self.selected_files)} selected")
        return "break"


    def _print_selected_files(self, files):
        """Route selected files to the appropriate print workflow.

        Images use the existing Contact Sheet workflow.
        PDF/DOCX documents are combined into one temporary PDF and opened
        in the system PDF viewer for one print action.
        """
        try:
            files = [os.path.normpath(f) for f in (files or []) if f and os.path.isfile(_longpath(f))]
            if not files:
                messagebox.showinfo("Print selected", "No selected files.", parent=self)
                return

            image_files = [f for f in files if os.path.splitext(f)[1].lower() in PHOTO_EXTS]
            doc_files = [f for f in files if os.path.splitext(f)[1].lower() in DOCUMENT_EXTS]
            other_files = [f for f in files if f not in image_files and f not in doc_files]

            if image_files and not doc_files and not other_files:
                old_selected = set(getattr(self, "selected_files", set()))
                try:
                    self.selected_files = set(image_files)
                    return self.create_contact_sheet_from_selection()
                finally:
                    self.selected_files = old_selected

            if doc_files and not image_files and not other_files:
                result = ft_print.print_documents_as_combined_pdf(
                    doc_files,
                    parent=self,
                    title="FTView selected documents"
                )
                if not result.get("ok"):
                    messagebox.showerror("Print selected", result.get("message", "Print preparation failed."), parent=self)
                return

            messagebox.showwarning(
                "Print selected",
                "The selection contains mixed images/documents or unsupported files.\n\n"
                "Print images and documents separately.",
                parent=self
            )
        except Exception as e:
            messagebox.showerror("Print selected", str(e), parent=self)

    def show_file_list_menu(self, event):
        """Right-click menu from the file list, sharing the thumbnail menu logic."""
        try:
            row = self.file_list.identify_row(event.y)
            if not row:
                return "break"
            idx = int(row)
            if idx < 0 or idx >= len(self.files):
                return "break"
            return self.show_thumb_menu(event, idx)
        except Exception as e:
            messagebox.showerror("File menu", str(e), parent=self)
            return "break"


    def show_thumb_menu(self, event, idx):
        if idx < 0 or idx >= len(self.files):
            return "break"
        try:
            old_yview = self.thumb_canvas.yview()
        except Exception:
            old_yview = None
        self.select_index(idx, from_thumb=True)
        if old_yview is not None:
            self.after_idle(lambda y=old_yview: self.thumb_canvas.yview_moveto(y[0]))
        path = self.files[idx]
        # Use Ctrl-selected set; if empty, implicitly target right-clicked item (no selection change)
        op_files = set(self.selected_files) if self.selected_files else {path}
        n = len(op_files)
        menu = tk.Menu(self, tearoff=0)
        ext = os.path.splitext(path)[1].lower()
        if ext in {".jpg", ".jpeg"}:
            menu.add_command(label="Edit JPG in FTImgedit", command=lambda p=path: self.edit_jpg_in_ftimgedit(p))
            menu.add_separator()
        elif ext == ".pdf":
            pdf_targets = [p for p in self.files if p in op_files and os.path.splitext(p)[1].lower() == ".pdf"]
            if not pdf_targets:
                pdf_targets = [path]
            menu.add_command(label="Edit", command=lambda files=list(pdf_targets): self._show_pdf_edit_dialog(files))
            menu.add_separator()
        else:
            menu.add_command(label="Edit JPG in FTImgedit", state="disabled")
            menu.add_separator()
        op_list = list(op_files)
        menu.add_command(label=f"Copy selected ({n})", command=lambda f=op_list: self.copy_selected_files(f))
        menu.add_command(label=f"Move selected ({n})", command=lambda f=op_list: self.move_selected_files(f))
        menu.add_command(label=f"Contact sheet selected ({n})", command=lambda f=op_list: self.create_contact_sheet_from_selection(f))
        menu.add_command(label=f"Print selected ({n})", command=lambda f=op_list: self._print_selected_files(f))
        menu.add_separator()
        menu.add_command(label=f"Delete selected ({n})", command=lambda f=op_list: self.delete_selected_files(f))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"



    def _show_pdf_edit_dialog(self, paths):
        """Show the shared PDF edit choices for the clicked/selected PDF files."""
        paths = [os.path.normpath(p) for p in (paths or [])
                 if p and os.path.splitext(p)[1].lower() == ".pdf" and os.path.isfile(p)]
        if not paths:
            messagebox.showinfo("Edit PDF", "No PDF files are selected.", parent=self)
            return

        dlg = tk.Toplevel(self)
        dlg.title("Edit PDF")
        dlg.transient(self)
        dlg.grab_set()
        dlg.resizable(False, False)
        dlg.configure(bg=BG2)

        what = f"{len(paths)} selected PDF files" if len(paths) != 1 else os.path.basename(paths[0])
        tk.Label(
            dlg,
            text=f"Create edited copy for:\n{what}",
            bg=BG2, fg=DIM, font=("Segoe UI", 10, "bold"),
            justify="left", padx=14, pady=12,
        ).pack(fill="x")

        btns = tk.Frame(dlg, bg=BG2)
        btns.pack(fill="x", padx=14, pady=(0, 14))

        def run(mode):
            try:
                dlg.destroy()
            except Exception:
                pass
            self._convert_selected_pdfs(paths, mode)

        tk.Button(btns, text="Grayscale", command=lambda: run("grayscale"),
                  bg=ACCENT, fg="white", padx=12, pady=5).pack(side="left", padx=(0, 8))
        tk.Button(btns, text="B/W", command=lambda: run("bw"),
                  bg=ACCENT, fg="white", padx=12, pady=5).pack(side="left", padx=(0, 8))
        tk.Button(btns, text="Cancel", command=dlg.destroy,
                  padx=12, pady=5).pack(side="right")

        try:
            dlg.update_idletasks()
            x = self.winfo_rootx() + max(40, (self.winfo_width() - dlg.winfo_width()) // 2)
            y = self.winfo_rooty() + max(40, (self.winfo_height() - dlg.winfo_height()) // 2)
            dlg.geometry(f"+{x}+{y}")
        except Exception:
            pass

    def _convert_selected_pdfs(self, paths, mode):
        """Create grayscale or true B/W copies of PDF files, then refresh FTView."""
        paths = [os.path.normpath(p) for p in (paths or [])
                 if p and os.path.splitext(p)[1].lower() == ".pdf" and os.path.isfile(p)]
        if not paths:
            return
        mode_label = "Grayscale" if mode == "grayscale" else "B/W"

        def worker():
            made = []
            errors = []
            for p in paths:
                try:
                    if mode == "grayscale":
                        res = ft_pdf_ops.convert_pdf_to_grayscale(p, zoom=2.0)
                    else:
                        res = ft_pdf_ops.convert_pdf_to_bw(p, zoom=2.0, threshold=128)
                    made.append(res.output_path)
                except Exception as e:
                    errors.append((p, str(e)))

            def finish():
                affected = sorted({os.path.dirname(p) for p in paths + made if p}, key=str.lower)
                try:
                    if affected:
                        self._refresh_folder_tree_counts_for_paths(affected)
                    if self.current_folder and os.path.normcase(os.path.normpath(self.current_folder)) in {os.path.normcase(os.path.normpath(f)) for f in affected}:
                        self.load_folder(self.current_folder)
                    elif made:
                        self.refresh_current()
                except Exception:
                    try:
                        self.refresh_current()
                    except Exception:
                        pass
                lines = [f"Created {len(made)} {mode_label} PDF file(s)."]
                if errors:
                    lines.append("")
                    lines.append(f"Errors: {len(errors)}")
                    for src, err in errors[:8]:
                        lines.append(f"{os.path.basename(src)}: {err}")
                messagebox.showinfo("Edit PDF", "\n".join(lines), parent=self)

            self.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    def edit_jpg_in_ftimgedit(self, path):
        """Launch FTImgedit.py for a single JPG/JPEG file, then refresh FTView after it closes."""
        try:
            path = os.path.normpath(path)
            ext = os.path.splitext(path)[1].lower()
            if ext not in {".jpg", ".jpeg"}:
                messagebox.showinfo("Edit JPG", "Editing is currently available for JPG/JPEG files only.", parent=self)
                return
            if not os.path.exists(path):
                messagebox.showerror("Edit JPG", f"File not found:\n{path}", parent=self)
                return

            app_dir = os.path.dirname(os.path.abspath(__file__))
            editor_path = os.path.join(app_dir, "FTImgedit.py")
            if not os.path.exists(editor_path):
                messagebox.showerror(
                    "Edit JPG",
                    "FTImgedit.py was not found in the same folder as FTView.py.",
                    parent=self,
                )
                return

            proc = subprocess.Popen([sys.executable, editor_path, path], cwd=app_dir)
            self.status_var.set(f"Editing {os.path.basename(path)} in FTImgedit...")

            def waiter():
                try:
                    proc.wait()
                except Exception:
                    pass
                self.after(0, lambda p=path: self._after_ftimgedit_closed(p))

            threading.Thread(target=waiter, daemon=True).start()
        except Exception as e:
            messagebox.showerror("Edit JPG", str(e), parent=self)

    def _after_ftimgedit_closed(self, path):
        """Refresh the affected thumbnail and preview after external editing."""
        try:
            path = os.path.normpath(path)
            changed_idx = self.files.index(path) if path in self.files else None
            if changed_idx is not None:
                self.thumb_loaded.discard(changed_idx)
                self.thumb_loading.discard(changed_idx)
                self.thumb_requested.discard(changed_idx)
                labels = self.thumb_labels.get(changed_idx)
                if labels:
                    try:
                        labels[0].configure(image="", text="")
                        labels[0].image = None
                    except Exception:
                        pass
                self.after_idle(self.schedule_visible_thumbs)
                if self.selected_idx == changed_idx:
                    try:
                        self._active_viewer.show_index(changed_idx)
                    except Exception:
                        self._active_viewer.set_file_list(self.files, changed_idx)
            self.status_var.set(f"Returned from FTImgedit: {os.path.basename(path)}")
        except Exception:
            self.refresh_current()

    def select_all_files(self):
        self.selected_files = set(self.all_files or self.files)
        self.last_selected_idx = 0 if self.files else None
        self.last_selection_action = True
        for idx in list(self.thumb_cells.keys()):
            self.highlight_thumb(idx)
            self._refresh_thumb_image(idx)
        self.status_var.set(f"{len(self.selected_files)} selected")

    def clear_selected_files(self):
        self.selected_files.clear()
        self.last_selected_idx = None
        self.last_selection_action = True
        # Clear always returns the panel to the default All view and redisplays
        # every thumbnail.  This is deliberate: after viewing Selected items,
        # Clear means reset selection and go back to the normal folder view.
        self.view_filter = "all"
        self._rebuild_current_view()
        self._update_view_filter_buttons()
        self.status_var.set("Selection cleared; showing all files")

    def show_filename_filter_dialog(self):
        """Select files whose filenames match entered text criteria."""
        if not self.all_files:
            messagebox.showinfo("Filter", "No files are loaded for the current folder.", parent=self)
            return

        dlg = tk.Toplevel(self)
        dlg.title("Filter filename selection")
        dlg.transient(self)
        dlg.grab_set()
        dlg.resizable(False, False)
        dlg.configure(bg=BG2)

        starts_var = tk.StringVar()
        ends_var = tk.StringVar()
        contains_var = tk.StringVar()
        contains_mode = tk.StringVar(value="any")

        body = tk.Frame(dlg, bg=BG2, padx=12, pady=10)
        body.pack(fill="both", expand=True)

        tk.Label(body, text="Select files where filename:", bg=BG2, fg=TEXT,
                 font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))

        tk.Label(body, text="Starts with", bg=BG2, fg=TEXT, anchor="w").grid(row=1, column=0, sticky="w", pady=3)
        starts_entry = tk.Entry(body, textvariable=starts_var, width=24)
        starts_entry.grid(row=1, column=1, columnspan=3, sticky="we", padx=(8, 0), pady=3)

        tk.Label(body, text="Ends with", bg=BG2, fg=TEXT, anchor="w").grid(row=2, column=0, sticky="w", pady=3)
        tk.Entry(body, textvariable=ends_var, width=24).grid(row=2, column=1, columnspan=3, sticky="we", padx=(8, 0), pady=3)

        tk.Label(body, text="Contains", bg=BG2, fg=TEXT, anchor="w").grid(row=3, column=0, sticky="w", pady=3)
        tk.Entry(body, textvariable=contains_var, width=42).grid(row=3, column=1, columnspan=3, sticky="we", padx=(8, 0), pady=3)

        tk.Label(body, text="Comma-separated strings", bg=BG2, fg=DIM,
                 font=("Segoe UI", 8)).grid(row=4, column=1, columnspan=3, sticky="w", padx=(8, 0), pady=(0, 5))

        tk.Radiobutton(body, text="Any", variable=contains_mode, value="any",
                       bg=BG2, fg=TEXT, activebackground=BG2).grid(row=5, column=1, sticky="w", padx=(8, 0))
        tk.Radiobutton(body, text="All", variable=contains_mode, value="all",
                       bg=BG2, fg=TEXT, activebackground=BG2).grid(row=5, column=2, sticky="w")

        buttons = tk.Frame(body, bg=BG2)
        buttons.grid(row=6, column=0, columnspan=4, sticky="e", pady=(12, 0))

        def apply_filter():
            starts = starts_var.get().strip().lower()
            ends = ends_var.get().strip().lower()
            contains = [s.strip().lower() for s in contains_var.get().split(',') if s.strip()]

            if not starts and not ends and not contains:
                messagebox.showinfo("Filter", "Enter at least one selection criterion.", parent=dlg)
                return

            matched = []
            for path in self.all_files:
                name = os.path.basename(path).lower()
                if starts and not name.startswith(starts):
                    continue
                if ends and not name.endswith(ends):
                    continue
                if contains:
                    if contains_mode.get() == "all":
                        if not all(s in name for s in contains):
                            continue
                    else:
                        if not any(s in name for s in contains):
                            continue
                matched.append(path)

            self.selected_files = set(matched)
            self.last_selected_idx = None
            self.last_selection_action = True
            self.view_filter = "all"
            self._rebuild_current_view()
            self._update_view_filter_buttons()
            self.status_var.set(f"Filter selected {len(matched)} of {len(self.all_files)} files")
            dlg.destroy()

        self._make_toolbar_button(buttons, "Apply", apply_filter, "#704a9e").pack(side="right", padx=(5, 0))
        self._make_toolbar_button(buttons, "Cancel", dlg.destroy, "#555555").pack(side="right", padx=(5, 0))

        body.grid_columnconfigure(1, weight=1)
        starts_entry.focus_set()
        dlg.bind("<Return>", lambda e: apply_filter())
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        self.update_idletasks()
        try:
            x = self.winfo_rootx() + max(40, (self.winfo_width() - dlg.winfo_reqwidth()) // 2)
            y = self.winfo_rooty() + max(40, (self.winfo_height() - dlg.winfo_reqheight()) // 3)
            dlg.geometry(f"+{x}+{y}")
        except Exception:
            pass

    def _rebuild_current_view(self, preserve_path=None):
        """Rebuild the file list/thumb grid for Show All/Show Selected."""
        self._apply_view_filter()
        self.rebuild_file_list()
        self.refresh_thumbs()
        self.selected_idx = None
        if preserve_path in self.files:
            self.select_index(self.files.index(preserve_path), from_thumb=False)
        elif self.files:
            try:
                self._active_viewer.set_file_list(self.files, None)
                self._active_viewer.show_message("No file selected")
            except Exception:
                pass
        else:
            try:
                self._active_viewer.set_file_list([], None)
                self._active_viewer.show_message("No selected files" if self.view_filter == "selected" else self._empty_folder_message())
            except Exception:
                pass

    def show_all_files(self):
        current = self.files[self.selected_idx] if self.selected_idx is not None and 0 <= self.selected_idx < len(self.files) else None
        self.view_filter = "all"
        self._update_view_filter_buttons()
        self._rebuild_current_view(preserve_path=current)
        self.status_var.set(f"Showing all files ({len(self.files)} files, {len(self.selected_files)} selected)")

    def show_selected_files(self):
        current = self.files[self.selected_idx] if self.selected_idx is not None and 0 <= self.selected_idx < len(self.files) else None
        self.view_filter = "selected"
        self._update_view_filter_buttons()
        self._rebuild_current_view(preserve_path=current)
        self.status_var.set(f"Showing selected files ({len(self.files)} of {len(self.selected_files)} selected)")

    def _ftview_contact_sheet_root(self):
        """Shared ft_contactsheet hook: active Photos/PDFs root, not current folder."""
        try:
            return _ui_path(self.root_var.get().strip())
        except Exception:
            return ""

    def _ftview_contact_sheets_dir(self):
        """Shared ft_contactsheet hook: <active root>\_ContactSheets."""
        root = self._ftview_contact_sheet_root()
        out = os.path.join(root, "_ContactSheets") if root else os.path.join(os.getcwd(), "_ContactSheets")
        os.makedirs(out, exist_ok=True)
        return out

    def create_contact_sheet_from_selection(self, files=None):
        """Use the shared FT contact-sheet dialog, pre-filtered to selection."""
        self._ft_contact_sheet_root_func = self._ftview_contact_sheet_root
        self._ft_contact_sheets_dir_func = self._ftview_contact_sheets_dir
        # The shared dialog automatically prefers selected files when a selection exists.
        return ft_contactsheet.contact_sheet_dialog(self)

    def create_contact_sheet(self, selected_only=False):
        """Use the same shared contact-sheet dialog/helper as FT."""
        self._ft_contact_sheet_root_func = self._ftview_contact_sheet_root
        self._ft_contact_sheets_dir_func = self._ftview_contact_sheets_dir
        return ft_contactsheet.contact_sheet_dialog(self)

    def _selected_existing_files(self):
        return [p for p in self.files if p in self.selected_files and os.path.isfile(p)]

    def _choose_destination_dialog(self, title):
        files = self._selected_existing_files()
        if not files:
            messagebox.showinfo(title, "No files are selected.", parent=self)
            return None

        dlg = tk.Toplevel(self)
        dlg.title(title)
        dlg.transient(self)
        # Do not grab the application: while this dialog is open the user must
        # be able to click the main FTView folder tree to choose the target.
        dlg.geometry("720x460")
        dlg.configure(bg=BG2)
        result = {"folder": None}

        tk.Label(
            dlg,
            text=f"Target folder for {len(files)} selected file(s)",
            bg=BG2,
            fg=DIM,
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor="w", padx=10, pady=(10, 4))

        folder_var = tk.StringVar(value=self.current_folder or self.root_var.get().strip())
        self._destination_dialog_active = True
        self._destination_folder_var = folder_var
        row = tk.Frame(dlg, bg=BG2)
        row.pack(fill="x", padx=10, pady=(0, 8))
        tk.Label(row, text="Folder", bg=BG2, fg=DIM).pack(side="left", padx=(0, 6))
        entry = tk.Entry(row, textvariable=folder_var)
        entry.pack(side="left", fill="x", expand=True, padx=(0, 6))

        def browse():
            f = filedialog.askdirectory(parent=dlg, title=title, initialdir=folder_var.get() or self.root_var.get().strip())
            if f:
                folder_var.set(_ui_path(f))

        tk.Button(row, text="Browse", command=browse).pack(side="left")

        tk.Label(
            dlg,
            text="Or click a folder in the tree below, or click a folder in the main FTView folder tree:",
            bg=BG2,
            fg=DIM,
            font=("Segoe UI", 9),
        ).pack(anchor="w", padx=10, pady=(0, 4))

        tree_frame = tk.Frame(dlg, bg=BG2)
        tree_frame.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        tree = ttk.Treeview(tree_frame, show="tree")
        sb = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)

        def add_node(parent, path, text):
            path = _ui_path(path)
            if tree.exists(path):
                return
            tree.insert(parent, "end", iid=path, text=text, open=False)
            try:
                if any(e.is_dir(follow_symlinks=False) for e in os.scandir(_longpath(path))):
                    tree.insert(path, "end", iid=path + "|dummy", text="")
            except Exception:
                pass

        def populate(path):
            path = _ui_path(path)
            dummy = path + "|dummy"
            if tree.exists(dummy):
                tree.delete(dummy)
            try:
                entries = sorted(
                    [e for e in os.scandir(_longpath(path)) if e.is_dir(follow_symlinks=False)],
                    key=lambda e: e.name.lower(),
                )
                for e in entries:
                    child = _ui_path(os.path.join(path, e.name))
                    add_node(path, child, e.name)
            except Exception:
                pass

        root = _ui_path(self.root_var.get().strip() or self.current_folder or os.getcwd())
        if os.path.isdir(root):
            add_node("", root, root)
            tree.item(root, open=True)
            populate(root)

        def on_open(_event=None):
            focus = tree.focus()
            if focus:
                populate(focus)

        def on_select(_event=None):
            sel = tree.selection()
            if sel:
                folder_var.set(_ui_path(sel[0]))

        tree.bind("<<TreeviewOpen>>", on_open)
        tree.bind("<<TreeviewSelect>>", on_select)

        buttons = tk.Frame(dlg, bg=BG2)
        buttons.pack(fill="x", padx=10, pady=(0, 10))

        def close_dialog():
            try:
                self._destination_dialog_active = False
                self._destination_folder_var = None
            except Exception:
                pass
            try:
                dlg.destroy()
            except Exception:
                pass

        def ok():
            folder = _ui_path(folder_var.get().strip())
            if not folder or not os.path.isdir(folder):
                messagebox.showwarning(title, f"Folder not found:\n{folder}", parent=dlg)
                return
            result["folder"] = folder
            close_dialog()

        entry.bind("<Return>", lambda _e: ok())
        dlg.protocol("WM_DELETE_WINDOW", close_dialog)
        tk.Button(buttons, text="Cancel", command=close_dialog).pack(side="right", padx=4)
        tk.Button(buttons, text="OK", command=ok, bg=ACCENT, fg="white").pack(side="right", padx=4)
        entry.focus_set()
        dlg.wait_window()
        self._destination_dialog_active = False
        self._destination_folder_var = None
        return result["folder"]

    def _show_file_op_result(self, title, result):
        lines = [title, ""]
        lines.append(f"Completed: {result.ok_count}")
        if result.skipped_existing:
            lines.append(f"Skipped duplicate filenames: {len(result.skipped_existing)}")
        if result.skipped_missing:
            lines.append(f"Skipped missing files: {len(result.skipped_missing)}")
        if result.errors:
            lines.append(f"Errors: {len(result.errors)}")
            lines.append("")
            for src, err in result.errors[:8]:
                lines.append(f"{os.path.basename(src)}: {err}")
        if result.skipped_existing:
            lines.append("")
            lines.append("Duplicates skipped:")
            for src, dst in result.skipped_existing[:8]:
                lines.append(f"{os.path.basename(src)} already exists in destination")
        messagebox.showinfo(title, "\n".join(lines), parent=self)

    def _finish_file_operation_refresh(self, result, extra_folders=None):
        """Refresh FTView after copy/move/delete without leaving stale selections.

        File operations are synchronous, but Windows/Explorer/antivirus can leave
        directory state appearing stale for a moment.  Refresh immediately and
        then repeat shortly afterwards so moved/deleted files cannot remain as
        phantom thumbnails or selectable stale rows.
        """
        affected = []
        try:
            affected.extend(result.affected_folders())
        except Exception:
            pass
        for folder in extra_folders or []:
            if folder:
                affected.append(folder)
        affected = sorted({os.path.normpath(f) for f in affected if f}, key=str.lower)

        # Reset selection/view state before rebuilding; otherwise a stale selected
        # path can be re-added by right-click code even though it was moved away.
        self.selected_files.clear()
        self.selected_idx = None
        self.last_selected_idx = None
        self.last_selection_action = True
        self.view_filter = "all"
        try:
            self._update_view_filter_buttons()
        except Exception:
            pass

        if affected:
            self._refresh_folder_tree_counts_for_paths(affected)

        def _reload_current_and_counts():
            try:
                if self.current_folder:
                    self.load_folder(self.current_folder)
                else:
                    self.refresh_current()
            finally:
                if affected:
                    self._refresh_folder_tree_counts_for_paths(affected)

        _reload_current_and_counts()
        try:
            self.after(250, _reload_current_and_counts)
            self.after(900, lambda: self._refresh_folder_tree_counts_for_paths(affected) if affected else None)
        except Exception:
            pass

    def copy_selected_files(self, files=None):
        if files is None:
            files = self._selected_existing_files()
        files = [p for p in files if os.path.isfile(p)]
        if not files:
            messagebox.showinfo("Copy selected files", "No files are selected.", parent=self)
            return
        dest = self._choose_destination_dialog("Copy selected files")
        if not dest:
            return
        try:
            result = ft_file_ops.copy_files(files, dest, overwrite=False)
        except Exception as e:
            messagebox.showerror("Copy selected files", str(e), parent=self)
            return
        self._finish_file_operation_refresh(result, extra_folders=[dest, self.current_folder])
        self._show_file_op_result("Copy selected files", result)

    def move_selected_files(self, files=None):
        if files is None:
            files = self._selected_existing_files()
        files = [p for p in files if os.path.isfile(p)]
        if not files:
            messagebox.showinfo("Move selected files", "No files are selected.", parent=self)
            return
        dest = self._choose_destination_dialog("Move selected files")
        if not dest:
            return
        try:
            result = ft_file_ops.move_files(files, dest, overwrite=False)
        except Exception as e:
            messagebox.showerror("Move selected files", str(e), parent=self)
            return
        self._finish_file_operation_refresh(result, extra_folders=[dest, self.current_folder])
        self._show_file_op_result("Move selected files", result)

    def delete_selected_files(self, files=None):
        if files is None:
            files = self._selected_existing_files()
        files = [p for p in files if os.path.isfile(p)]
        if not files:
            messagebox.showinfo("Delete selected files", "No files are selected.", parent=self)
            return
        if not messagebox.askyesno(
            "Delete selected files",
            f"Delete {len(files)} selected file{'s' if len(files)!=1 else ''} from disk?",
            parent=self,
        ):
            return
        result = ft_file_ops.delete_files(files)
        self._finish_file_operation_refresh(result, extra_folders=[self.current_folder])
        self._show_file_op_result("Delete selected files", result)

    def _viewer_select_index(self, idx):
        """Callback from ViewerPanel / MoviePlayerPanel prev/next buttons."""
        if 0 <= idx < len(self.files):
            self.select_index(idx, from_thumb=False)

    def _on_viewer_file_changed(self, path):
        """Callback from ViewerPanel when a file is modified (e.g. rotated).
        Invalidates the cached thumbnail and reloads it in the grid.
        """
        try:
            if not path:
                return
            if _ft_thumb_cache:
                try:
                    _ft_thumb_cache.delete_thumb(_ui_path(path))
                except Exception:
                    pass
            if path in self.files:
                idx = self.files.index(path)
                self._invalidate_thumb(idx)
                self._reload_single_thumb(idx)
        except Exception as e:
            print(f"_on_viewer_file_changed error: {e}")


def main():
    app = FTView()
    app.mainloop()


if __name__ == "__main__":
    main()
