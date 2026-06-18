
"""
FTVideo.py — Video clip browser and player helper for the FT ecosystem.

Layout (standalone):
    Header
    Folder tree | File list | Thumbnail grid | Divider | MoviePlayerPanel

Layout (embedded, launched by FTMod):
    Header
    File list | Thumbnail grid | Divider | MoviePlayerPanel

Usage:
  Standalone:   python FTVideo.py
                python FTVideo.py "S:\\Movies"
  Embedded:     python FTVideo.py --embedded

IPC (embedded mode):
  Reads  FT_IPC\\FTVideo_request.csv   (SEQ,N / CENTER,path / one-path-per-line)
  No result file written (phase 1).
"""

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

# ── Crash logger ──────────────────────────────────────────────────────────────
def _ftvideo_excepthook(exc_type, exc_val, exc_tb):
    import traceback, datetime
    _log = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "FTVideo_error.log")
    with open(_log, "a") as _f:
        _f.write(f"\n--- {datetime.datetime.now()} ---\n")
        traceback.print_exception(exc_type, exc_val, exc_tb, file=_f)
    try:
        import tkinter as _tk; from tkinter import messagebox as _mb
        _r = _tk.Tk(); _r.withdraw()
        _mb.showerror("FTVideo error",
                      f"{exc_type.__name__}: {exc_val}\n\nSee FTVideo_error.log in helpers/ for details.")
        _r.destroy()
    except Exception:
        pass
    _sys.__excepthook__(exc_type, exc_val, exc_tb)
_sys.excepthook = _ftvideo_excepthook
# ─────────────────────────────────────────────────────────────────────────────

import os
import sys
import io as _io
import threading
import queue
import configparser
import ctypes
import subprocess
import shutil
try:
    import ctypes.wintypes as wintypes
except Exception:
    wintypes = None

# ── DPI awareness ─────────────────────────────────────────────────────────────
def _set_process_dpi_awareness():
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

_set_process_dpi_awareness()
# ─────────────────────────────────────────────────────────────────────────────

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ── Pillow ────────────────────────────────────────────────────────────────────
try:
    from PIL import Image, ImageTk, ImageFile
    Image.MAX_IMAGE_PIXELS = None
    ImageFile.LOAD_TRUNCATED_IMAGES = True
except ImportError:
    tk.Tk().withdraw()
    messagebox.showerror("Missing library", "Pillow is required.\n\nRun: pip install Pillow")
    sys.exit(1)

# ── ft_movie ──────────────────────────────────────────────────────────────────
try:
    from libraries.ft_movie import (
        MoviePlayerPanel, make_movie_thumbnail_fast, get_video_info,
        _fmt_duration as _fmt_video_duration,
    )
    HAVE_FT_MOVIE = True
except ImportError as _e:  # noqa: F841
    HAVE_FT_MOVIE = False
    print(f"FTVideo: ft_movie not available: {_e}")
    tk.Tk().withdraw()
    messagebox.showerror("Missing module",
                         "libraries/ft_movie.py is required.\n\nCould not import it.")
    sys.exit(1)

# ── ffprobe duration (no cv2/COM) ─────────────────────────────────────────────
_FFPROBE_EXE = shutil.which("ffprobe")

def _duration_ffprobe(path: str) -> float:
    """Return video duration in seconds using ffprobe subprocess (no cv2/COM)."""
    if not _FFPROBE_EXE:
        return 0.0
    try:
        r = subprocess.run(
            [_FFPROBE_EXE, "-v", "error",
             "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=8,
            creationflags=0x08000000 if os.name == "nt" else 0)  # CREATE_NO_WINDOW
        return float(r.stdout.decode().strip())
    except Exception:
        return 0.0

# ── ft_thumb_layout ───────────────────────────────────────────────────────────
try:
    from libraries.ft_thumb_layout import calculate_thumb_layout
except ImportError:
    tk.Tk().withdraw()
    messagebox.showerror("Missing module", "libraries/ft_thumb_layout.py is required.")
    sys.exit(1)

# ── ft_widgets ────────────────────────────────────────────────────────────────
try:
    from libraries.ft_widgets import FileCountTree, SortableFileList, show_file_sort_menu, _sort_btn_label
    HAVE_FILE_COUNT_TREE = True
except ImportError:
    HAVE_FILE_COUNT_TREE = False
    SortableFileList = None  # type: ignore[assignment,misc]
    show_file_sort_menu = None  # type: ignore[assignment]
    def _sort_btn_label(column="name", reverse=False):  # type: ignore[misc]
        labels = {"name": "Name", "date_taken": "Date", "file": "Name", "size": "Size"}
        return f"{labels.get(column, column)} {'↓' if reverse else '↑'} ▾"

# ── Global per-PC thumbnail cache ─────────────────────────────────────────────
try:
    from libraries import ft_thumb_cache as _ft_thumb_cache
    _HAVE_THUMB_CACHE = True
except ImportError:
    _ft_thumb_cache = None
    _HAVE_THUMB_CACHE = False

try:
    from libraries import ft_file_ops as _ft_file_ops
except ImportError:
    _ft_file_ops = None  # type: ignore[assignment]

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".avi", ".mkv", ".wmv", ".mpg", ".mpeg", ".mts", ".m2ts"}

# Match FTMod's storage size so thumbs are shared between FTMod and FTVideo
_THUMB_STORE_SIZE = 250

BG         = "#dddddd"
BG2        = "#eeeeee"
ACCENT     = "#1a5276"
SELECT_BG  = "#d9ecff"
TEXT       = "#111111"
DIM        = "#555555"
CANVAS_BG  = "#777777"

DEFAULT_ROOT = ""
INI_FILE     = "FTVideo.ini"


# ─────────────────────────────────────────────────────────────────────────────
# Path helpers
# ─────────────────────────────────────────────────────────────────────────────

def _script_dir():
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except Exception:
        return os.getcwd()




def _ini_path():
    return os.path.join(_script_dir(), INI_FILE)


def _longpath(path):
    if not path:
        return path
    path = _ui_path(path)
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
    if not path:
        return path
    path = str(path)
    if path.startswith("\\\\?\\"):
        path = path[4:]
    path = os.path.normpath(path)
    if os.name == "nt" and len(path) == 2 and path[1] == ":":
        path += "\\"
    return path


def _ipc_dir():
    """Return FT_IPC folder path (from FileTagger.ini or default)."""
    ini = os.path.join(os.path.dirname(_script_dir()), "FileTagger.ini")
    path = None
    if os.path.exists(ini):
        cfg = configparser.ConfigParser(strict=False)
        cfg.read(ini)
        path = cfg.get("FileTagger", "ipc_folder", fallback="").strip()
    if not path:
        path = os.path.join(os.path.dirname(_script_dir()), "FT_IPC")
    os.makedirs(path, exist_ok=True)
    return path




# ─────────────────────────────────────────────────────────────────────────────
# INI load/save
# ─────────────────────────────────────────────────────────────────────────────

def _load_ini():
    cfg = configparser.ConfigParser()
    data = {"movies_root": DEFAULT_ROOT}
    path = _ini_path()
    if os.path.exists(path):
        try:
            cfg.read(path, encoding="utf-8")
            data["movies_root"] = cfg.get("Roots", "MoviesRoot", fallback="").strip()
        except Exception:
            pass
    return data


def _save_ini(movies_root):
    cfg = configparser.ConfigParser()
    cfg["Roots"] = {"MoviesRoot": movies_root or DEFAULT_ROOT}
    try:
        with open(_ini_path(), "w", encoding="utf-8") as f:
            cfg.write(f)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# File helpers
# ─────────────────────────────────────────────────────────────────────────────

def _scan_files(folder):
    folder = _ui_path(folder)
    try:
        files = []
        for e in os.scandir(_longpath(folder)):
            if e.is_file() and os.path.splitext(e.name)[1].lower() in VIDEO_EXTS:
                files.append(_ui_path(os.path.join(folder, e.name)))
        return sorted(files, key=lambda p: os.path.basename(p).lower())
    except Exception:
        return []


def _count_files(folder):
    folder = _ui_path(folder)
    try:
        return sum(
            1 for e in os.scandir(_longpath(folder))
            if e.is_file() and os.path.splitext(e.name)[1].lower() in VIDEO_EXTS
        )
    except Exception:
        return 0


def _file_display_name(path):
    ext = os.path.splitext(str(path))[1].lower()
    tag = ext.lstrip(".").upper()
    return f"[{tag}]  {os.path.basename(str(path))}"


def _file_size_value(path):
    try:
        return os.path.getsize(path)
    except Exception:
        return -1


def _file_size_text(path):
    size = _file_size_value(path)
    if size < 0:
        return "? kb"
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} mb"
    return f"{size / 1024:.1f} kb"


# ─────────────────────────────────────────────────────────────────────────────
# Main application
# ─────────────────────────────────────────────────────────────────────────────

class _ThumbDragGhost:
    def __init__(self, root_widget, photo, count=0):
        self._win = tk.Toplevel(root_widget)
        self._win.overrideredirect(True)
        try:
            self._win.attributes("-alpha", 0.7)
            self._win.attributes("-topmost", True)
        except Exception:
            pass
        self._win.configure(bg="#1a3a5c")
        if count > 1:
            tk.Label(self._win, text=f"\U0001f3ac  {count} clips",
                     bg="#1a3a5c", fg="white",
                     font=("Segoe UI", 13, "bold"), padx=14, pady=8).pack()
        elif photo:
            tk.Label(self._win, image=photo, bg="#1a3a5c",
                     relief="solid", bd=1).pack()
        else:
            tk.Label(self._win, text="[clip]", bg="#1a3a5c", fg="white",
                     font=("Segoe UI", 11), padx=10, pady=6).pack()
        self._photo = photo
        self._destroyed = False

    def move(self, x_root, y_root):
        if not self._destroyed:
            try:
                self._win.geometry(f"+{x_root + 14}+{y_root + 14}")
            except Exception:
                pass

    def destroy(self):
        if not self._destroyed:
            self._destroyed = True
            try:
                self._win.destroy()
            except Exception:
                pass


class FTVideo(tk.Tk):
    TREE_W         = 420
    FILES_W        = 260
    FILES_MIN_W    = 120
    THUMBS_W       = 810
    THUMB_CONTENT_W = 800
    THUMB_SCROLL_W = 10
    DIVIDER_W      = 6
    ZOOM_MIN_W     = 300

    def __init__(self, embedded=False, initial_files=None, initial_root=None):
        super().__init__()
        self.title("FTVideo version 1.73")
        self.configure(bg=BG)
        self._embedded = embedded

        self._maximize()
        self.minsize(900, 600)

        ini = _load_ini()
        self.root_var = tk.StringVar(value=initial_root or ini.get("movies_root", DEFAULT_ROOT))
        self.cols_var = tk.IntVar(value=6)
        self.rows_var = tk.IntVar(value=6)
        self.nav_var  = tk.StringVar(value="0 of 0")
        self.goto_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="Ready")

        self.current_folder      = ""
        self.all_files           = []
        self.files               = []
        self.selected_idx        = None
        self._file_sort_column   = "name"
        self._file_sort_reverse  = False
        self._syncing_file_selection    = False
        self._ignore_file_select_until_idle = False
        self._dragging_thumb_divider    = False
        self._dragging_files_divider    = False
        self._drag_files_start_x        = 0
        self._drag_files_start_w        = 0
        self._syncing_spinners          = False

        # Thumbnail state
        self.thumb_refs           = []
        self.thumb_cells          = {}
        self.thumb_labels         = {}
        self.thumb_watermarks     = {}
        self.thumb_generation     = 0
        self.thumb_request_queue  = None
        self.thumb_loader_workers = 0
        self.MAX_THUMB_WORKERS    = 2
        self.thumb_loaded         = set()
        self.thumb_loading        = set()
        self.thumb_requested      = set()
        self.thumb_box            = (80, 80)
        self.thumb_cols           = 6
        self.thumb_cell_h         = 120
        self.thumb_total_h        = 1
        self._thumb_cell_w_for_drag = None
        self._thumb_layout        = None

        # Thumbnail JPEG bytes cache (combine strip drag)
        self._thumb_jpeg  = {}

        # Drag-to-combine-strip state
        self._drag_active  = False
        self._drag_press_x = 0
        self._drag_press_y = 0
        self._drag_idx     = None
        self._drag_paths   = []      # list of (idx, path) for current drag
        self._press_defer_select = False  # defer plain-click clear when on multi-selected thumb
        self._drag_ghost   = None

        # Multi-select for drag
        self._thumb_selected   = set()   # indices checked for multi-drag
        self._thumb_sel_anchor = None    # shift-click anchor (updated by Ctrl-click only)
        self._last_ctrl_op    = 'select' # last Ctrl-click operation for Shift-click range

        # IPC (embedded mode)
        self._ipc_seq  = -1
        self._ipc_dir  = _ipc_dir() if embedded else None

        self._build_ui()

        self._maximize()
        self.after(1,   self._maximize)
        self.after(50,  self._maximize)
        self.after(200, self._maximize)
        self.after(120, self._layout_body_panels)
        self.after(300, lambda: self._initial_load(initial_files, initial_root))

        if embedded:
            self.after(500, self._poll_ipc)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── Win32 maximize ────────────────────────────────────────────────────────

    def _windows_work_area(self):
        if os.name == "nt" and wintypes is not None:
            try:
                rect = wintypes.RECT()
                ok = ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(rect), 0)
                if ok:
                    left, top = int(rect.left), int(rect.top)
                    w = int(rect.right - rect.left)
                    h = int(rect.bottom - rect.top)
                    if w > 100 and h > 100:
                        return left, top, w, h
            except Exception:
                pass
        return 0, 0, int(self.winfo_screenwidth()), max(600, int(self.winfo_screenheight()) - 60)

    def _top_level_hwnd(self):
        hwnd = int(self.winfo_id())
        if os.name != "nt":
            return hwnd
        try:
            parent = ctypes.windll.user32.GetParent(hwnd)
            if parent:
                hwnd = int(parent)
        except Exception:
            pass
        return hwnd

    def _window_rect(self):
        if os.name != "nt" or wintypes is None:
            return None
        try:
            rect = wintypes.RECT()
            if ctypes.windll.user32.GetWindowRect(self._top_level_hwnd(), ctypes.byref(rect)):
                return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
        except Exception:
            pass
        return None

    def _maximize(self):
        try:
            if os.name == "nt":
                left, top, width, height = self._windows_work_area()
                try: self.state("normal")
                except Exception: pass
                try: self.update_idletasks()
                except Exception: pass
                hwnd = self._top_level_hwnd()
                SWP = 0x0004 | 0x0010 | 0x0040
                ctypes.windll.user32.SetWindowPos(hwnd, None, left, top, width, height, SWP)
                try: self.update_idletasks()
                except Exception: pass
                rect = self._window_rect()
                if rect:
                    r_l, r_t, r_r, r_b = rect
                    wa_r = left + width
                    wa_b = top + height
                    dx = (left - r_l if r_l < left else wa_r - r_r if r_r > wa_r else 0)
                    dy = (top - r_t if r_t < top else wa_b - r_b if r_b > wa_b else 0)
                    if dx or dy:
                        ctypes.windll.user32.SetWindowPos(
                            hwnd, None,
                            r_l + dx, r_t + dy, r_r - r_l, r_b - r_t, SWP)
            else:
                self.geometry(f"{self.winfo_screenwidth()}x{self.winfo_screenheight()}+0+0")
        except Exception:
            try: self.state("zoomed")
            except Exception: pass

    # ── UI construction ───────────────────────────────────────────────────────

    def _make_toolbar_button(self, parent, text, command, colour):
        return tk.Button(
            parent, text=text, command=command,
            bg=colour, fg="white",
            activebackground=colour, activeforeground="white",
            font=("Segoe UI", 9, "bold"),
            relief="raised", bd=1, padx=6, pady=1,
        )

    def _build_ui(self):
        # ── Header bar ────────────────────────────────────────────────────────
        header = tk.Frame(self, bg=BG2, height=38)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        if not self._embedded:
            tk.Label(header, text="Root", bg=BG2, fg=DIM, font=("Segoe UI", 9, "bold")).pack(side="left", padx=(8, 4))
            root_entry = tk.Entry(header, textvariable=self.root_var, width=50)
            root_entry.pack(side="left", padx=(0, 4))
            root_entry.bind("<Return>", lambda e: self.set_root(self.root_var.get().strip()))
            self._make_toolbar_button(header, "...", self._browse_root, ACCENT).pack(side="left", padx=(0, 8))

        tk.Label(header, text="Cols", bg=BG2, fg=DIM).pack(side="left")
        self.cols_spin = tk.Spinbox(header, from_=1, to=20, width=3,
                                    textvariable=self.cols_var, command=self._on_cols_changed)
        self.cols_spin.pack(side="left", padx=(2, 5))
        self.cols_spin.bind("<Return>",   lambda e: self._on_cols_changed())
        self.cols_spin.bind("<FocusOut>", lambda e: self._on_cols_changed())

        self._make_toolbar_button(header, "Refresh", self._refresh_current, "#555555").pack(side="left", padx=(2, 4))

        nav_group = tk.Frame(header, bg=BG2, highlightbackground="black",
                             highlightcolor="black", highlightthickness=1, bd=0)
        nav_group.pack(side="left", padx=(4, 2), pady=4)
        self._make_toolbar_button(nav_group, "First",     self._nav_first,     "#405b77").pack(side="left", padx=2, pady=2)
        self._make_toolbar_button(nav_group, "Page Up",   self._nav_page_up,   "#405b77").pack(side="left", padx=2, pady=2)
        tk.Label(nav_group, textvariable=self.nav_var, bg=BG2, fg=DIM, width=11,
                 anchor="center", font=("Segoe UI", 9, "bold")).pack(side="left", padx=3, pady=2)
        self._make_toolbar_button(nav_group, "Page Down", self._nav_page_down, "#405b77").pack(side="left", padx=2, pady=2)
        self._make_toolbar_button(nav_group, "Last",      self._nav_last,      "#405b77").pack(side="left", padx=2, pady=2)
        tk.Label(nav_group, text="Goto", bg=BG2, fg=DIM).pack(side="left", padx=(5, 2), pady=2)
        goto_e = tk.Entry(nav_group, textvariable=self.goto_var, width=5)
        goto_e.pack(side="left", padx=(0, 2), pady=2)
        goto_e.bind("<Return>", lambda e: self._nav_goto())

        tk.Label(header, textvariable=self.status_var, bg=BG2, fg=DIM, anchor="e").pack(side="right", padx=8)

        # ── Body ──────────────────────────────────────────────────────────────
        self.body = tk.Frame(self, bg=BG)
        self.body.pack(fill="both", expand=True)
        self.body.bind("<Configure>", self._layout_body_panels)

        # Folder tree (standalone only)
        self.tree_frame = None
        self.folder_tree = None
        self.tree = None
        if not self._embedded and HAVE_FILE_COUNT_TREE:
            self.tree_frame = tk.Frame(self.body, bg=BG2)
            self.tree_frame.place(x=0, y=0, width=self.TREE_W, height=1)
            self.tree_frame.pack_propagate(False)
            self.folder_tree = FileCountTree(
                self.tree_frame,
                extensions=VIDEO_EXTS,
                col_own="Files",
                col_child="Tree",
                show_root_entry=False,
                bg=BG2,
            )
            self.folder_tree.pack(fill="both", expand=True)
            self.tree = self.folder_tree.tree()
            self.tree.bind("<ButtonRelease-1>", self._on_tree_click, add="+")

        # Files list
        files_x = self.TREE_W if (not self._embedded and self.tree_frame) else 0
        self.files_frame = tk.Frame(self.body, bg=BG2)
        self.files_frame.place(x=files_x, y=0, width=self.FILES_W, height=1)
        self.files_frame.pack_propagate(False)

        # FILES header row: label + sort button
        _files_hdr = tk.Frame(self.files_frame, bg=BG2)
        _files_hdr.pack(fill="x")
        tk.Label(_files_hdr, text="FILES", bg=BG2, fg=DIM,
                 font=("Segoe UI", 8, "bold")).pack(side="left", padx=(4, 0), pady=2)
        self._sort_btn = tk.Button(
            _files_hdr, text=_sort_btn_label(self._file_sort_column, self._file_sort_reverse),
            font=("Segoe UI", 8, "bold"), bg=BG2, fg=ACCENT,
            relief="flat", cursor="hand2",
            command=self._show_file_sort_menu,
        )
        self._sort_btn.pack(side="right", padx=(0, 20))

        self.file_list_widget = SortableFileList(
            self.files_frame,
            on_select=self._on_sfl_select,
            on_click=self._on_sfl_click,
            duration_getter=self._get_duration_for_sfl,
        )
        self.file_list_widget.pack(fill="both", expand=True, pady=(0, 4))

        # Divider between file list and thumbnails
        self.files_thumb_divider = tk.Frame(self.body, bg="#999999", cursor="sb_h_double_arrow")
        self.files_thumb_divider.place(x=files_x + self.FILES_W, y=0, width=self.DIVIDER_W, height=1)
        self.files_thumb_divider.bind("<ButtonPress-1>",  self._start_files_divider_drag)
        self.files_thumb_divider.bind("<B1-Motion>",      self._drag_files_divider)
        self.files_thumb_divider.bind("<ButtonRelease-1>", self._end_files_divider_drag)

        # Thumbnails
        thumbs_x = files_x + self.FILES_W + self.DIVIDER_W
        self.thumbs_frame = tk.Frame(self.body, bg=BG)
        self.thumbs_frame.place(x=thumbs_x, y=0, width=self.THUMBS_W, height=1)
        self.thumbs_frame.pack_propagate(False)
        self.thumbs_frame.grid_propagate(False)
        tk.Label(self.thumbs_frame, text="THUMBNAILS", bg=BG2, fg=DIM,
                 font=("Segoe UI", 8, "bold")).pack(fill="x", ipady=2)

        self.thumb_canvas = tk.Canvas(self.thumbs_frame, bg=BG, highlightthickness=0,
                                      width=self.THUMB_CONTENT_W)
        self.thumb_canvas.place(x=0, y=22, width=self.THUMB_CONTENT_W, height=1)

        self.thumb_scrollbar = ttk.Scrollbar(self.thumbs_frame, orient="vertical",
                                             command=self._thumb_yview)
        self.thumb_canvas.configure(yscrollcommand=self._on_thumb_yscroll)
        self.thumb_scrollbar.place(x=self.THUMB_CONTENT_W, y=22,
                                   width=self.THUMB_SCROLL_W, height=1)

        self.thumb_inner = tk.Frame(self.thumb_canvas, bg=BG, width=self.THUMB_CONTENT_W)
        self.thumb_window = self.thumb_canvas.create_window(
            (0, 0), window=self.thumb_inner, anchor="nw", width=self.THUMB_CONTENT_W)
        self.thumb_inner.grid_propagate(False)
        self.thumb_canvas.bind("<MouseWheel>", self._on_thumb_wheel)
        self.thumb_canvas.bind("<Configure>", self._on_thumb_canvas_configure)

        # Divider between thumbnails and player
        divider_x = thumbs_x + self.THUMBS_W
        self.thumb_divider = tk.Frame(self.body, bg="#999999", cursor="sb_h_double_arrow")
        self.thumb_divider.place(x=divider_x, y=0, width=self.DIVIDER_W, height=1)
        self.thumb_divider.bind("<ButtonPress-1>",  self._start_divider_drag)
        self.thumb_divider.bind("<B1-Motion>",      self._drag_divider)
        self.thumb_divider.bind("<ButtonRelease-1>", self._end_divider_drag)

        # Movie player panel
        player_x = divider_x + self.DIVIDER_W
        self.player_frame = tk.Frame(self.body, bg=CANVAS_BG)
        self.player_frame.place(x=player_x, y=0)

        self.movie_player = MoviePlayerPanel(
            self.player_frame,
            bg=CANVAS_BG,
            longpath_func=_longpath,
            on_select_index=self._on_player_select_index,
        )
        self.movie_player.pack(fill="both", expand=True)

    # ── Layout ────────────────────────────────────────────────────────────────

    def _tree_w(self):
        """Tree width — 0 in embedded mode or when tree not available."""
        if self._embedded or self.tree_frame is None:
            return 0
        return self.TREE_W

    def _layout_body_panels(self, event=None):
        try:
            total_w = max(1, self.body.winfo_width())
            total_h = max(1, self.body.winfo_height())
            tree_w = self._tree_w()

            left_fixed  = tree_w + self.FILES_W + self.DIVIDER_W
            max_thumbs_w = max(self.THUMB_CONTENT_W + self.THUMB_SCROLL_W,
                               total_w - left_fixed - self.DIVIDER_W - self.ZOOM_MIN_W)
            self.THUMBS_W = max(self.THUMB_CONTENT_W + self.THUMB_SCROLL_W,
                                min(self.THUMBS_W, max_thumbs_w))
            self.THUMB_CONTENT_W = max(80, self.THUMBS_W - self.THUMB_SCROLL_W)

            x_tree      = 0
            x_files     = tree_w
            x_files_div = tree_w + self.FILES_W
            x_thumbs    = x_files_div + self.DIVIDER_W
            x_div       = x_thumbs + self.THUMBS_W
            x_player    = x_div + self.DIVIDER_W
            player_w    = max(self.ZOOM_MIN_W, total_w - x_player)
            panel_h     = max(1, total_h)

            if self.tree_frame is not None:
                self.tree_frame.place(x=x_tree, y=0, width=self.TREE_W, height=total_h)
            self.files_frame.place(x=x_files,          y=0, width=self.FILES_W,  height=total_h)
            self.files_thumb_divider.place(x=x_files_div, y=0, width=self.DIVIDER_W, height=total_h)
            self.thumbs_frame.place(x=x_thumbs,        y=0, width=self.THUMBS_W, height=total_h)
            self.thumb_divider.place(x=x_div,          y=0, width=self.DIVIDER_W, height=total_h)
            self.player_frame.place(x=x_player,        y=0, width=player_w, height=panel_h)
            self.files_thumb_divider.lift()
            self.thumb_divider.lift()
            self.player_frame.configure(width=player_w, height=panel_h)

            self.thumb_canvas.place(x=0, y=22,
                                    width=self.THUMB_CONTENT_W, height=max(1, panel_h - 22))
            self.thumb_canvas.configure(width=self.THUMB_CONTENT_W,
                                        height=max(1, panel_h - 22))
            self.thumb_canvas.itemconfigure(self.thumb_window, width=self.THUMB_CONTENT_W)
            self.thumb_scrollbar.place(x=self.THUMB_CONTENT_W, y=22,
                                       width=self.THUMB_SCROLL_W, height=max(1, panel_h - 22))
            self.after_idle(self._update_nav_status)
        except Exception:
            pass

    # ── Divider drag ──────────────────────────────────────────────────────────

    def _start_divider_drag(self, event):
        self._dragging_thumb_divider = True
        self._drag_start_x = event.x_root
        self._drag_start_thumbs_w = self.THUMBS_W
        return "break"

    def _drag_divider(self, event):
        try:
            dx = event.x_root - self._drag_start_x
            tree_w   = self._tree_w()
            total_w  = max(1, self.body.winfo_width())
            min_w    = 160
            max_w    = max(min_w, total_w - tree_w - self.FILES_W - self.DIVIDER_W - self.ZOOM_MIN_W)
            self.THUMBS_W = max(min_w, min(max_w, self._drag_start_thumbs_w + dx))
            self.THUMB_CONTENT_W = max(80, self.THUMBS_W - self.THUMB_SCROLL_W)
            self._layout_body_panels()
        except Exception:
            pass
        return "break"

    def _end_divider_drag(self, event):
        self._dragging_thumb_divider = False
        if self.files:
            self.refresh_thumbs()
        return "break"

    def _start_files_divider_drag(self, event):
        self._dragging_files_divider = True
        self._drag_files_start_x = event.x_root
        self._drag_files_start_w = self.FILES_W
        return "break"

    def _drag_files_divider(self, event):
        try:
            dx = event.x_root - self._drag_files_start_x
            tree_w  = self._tree_w()
            total_w = max(1, self.body.winfo_width())
            max_w = max(self.FILES_MIN_W,
                        total_w - tree_w - self.DIVIDER_W
                        - (self.THUMB_CONTENT_W + self.THUMB_SCROLL_W)
                        - self.DIVIDER_W - self.ZOOM_MIN_W)
            self.FILES_W = max(self.FILES_MIN_W, min(max_w, self._drag_files_start_w + dx))
            self._layout_body_panels()
        except Exception:
            pass
        return "break"

    def _end_files_divider_drag(self, event):
        self._dragging_files_divider = False
        return "break"

    # ── Scroll helpers ────────────────────────────────────────────────────────

    def _thumb_yview(self, *args):
        self.thumb_canvas.yview(*args)
        self.after_idle(self._after_thumb_scroll)

    def _on_thumb_yscroll(self, first, last):
        try:
            self.thumb_scrollbar.set(first, last)
        except Exception:
            pass
        self.after_idle(self._update_nav_status)

    def _on_thumb_canvas_configure(self, event=None):
        try:
            self._clamp_thumb_scroll()
        except Exception:
            pass
        self.after_idle(self._after_thumb_scroll)

    def _after_thumb_scroll(self):
        self._update_nav_status()
        self._schedule_visible_thumbs()

    def _clamp_thumb_scroll(self):
        try:
            total_h   = max(1, int(self.thumb_total_h))
            visible_h = max(1, int(self.thumb_canvas.winfo_height()))
            max_y     = max(0, total_h - visible_h)
            y         = max(0, min(int(self.thumb_canvas.canvasy(0)), max_y))
            self.thumb_canvas.yview_moveto(0 if total_h <= visible_h else y / total_h)
        except Exception:
            pass

    def _on_thumb_wheel(self, event):
        try:
            total_h   = max(1, int(self.thumb_total_h))
            visible_h = max(1, int(self.thumb_canvas.winfo_height()))
            max_y     = max(0, total_h - visible_h)
            cur_y     = int(self.thumb_canvas.canvasy(0))
            direction = -1 if event.delta > 0 else 1
            new_y     = max(0, min(cur_y + direction * max(1, int(self.thumb_cell_h)), max_y))
            self.thumb_canvas.yview_moveto(0 if total_h <= visible_h else new_y / total_h)
        except Exception:
            self.thumb_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
            self._clamp_thumb_scroll()
        self.after_idle(self._after_thumb_scroll)
        return "break"

    # ── Navigation ────────────────────────────────────────────────────────────

    def _update_nav_status(self):
        total = len(self.files)
        if total <= 0:
            self.nav_var.set("0 of 0")
            return
        try:
            y0     = self.thumb_canvas.canvasy(0)
            row    = max(0, int(y0 // max(1, self.thumb_cell_h)))
            first  = min(total - 1, row * max(1, self.thumb_cols)) + 1
        except Exception:
            first = 1
        self.nav_var.set(f"{first} of {total}")

    def _visible_thumb_count(self):
        if not self.files:
            return 1
        try:
            visible_h = max(1, int(self.thumb_canvas.winfo_height()))
            row_h     = max(1, int(self.thumb_cell_h))
            cols      = max(1, int(self.thumb_cols))
            full_rows = max(1, visible_h // row_h)
            return max(1, full_rows * cols)
        except Exception:
            return max(1, int(getattr(self, "thumb_cols", 1) or 1))

    def _first_visible_thumb_index(self):
        if not self.files:
            return 0
        try:
            y0  = max(0, int(self.thumb_canvas.canvasy(0)))
            row = max(0, int(y0 // max(1, self.thumb_cell_h)))
            return min(len(self.files) - 1, row * max(1, self.thumb_cols))
        except Exception:
            return 0

    def _scroll_thumb_to_index(self, idx):
        if not self.files:
            return
        idx = max(0, min(int(idx), len(self.files) - 1))
        try:
            row     = idx // max(1, self.thumb_cols)
            y       = row * max(1, self.thumb_cell_h)
            total_h = max(1, int(self.thumb_total_h))
            try:
                visible_h = max(1, int(self.thumb_canvas.winfo_height()))
            except Exception:
                visible_h = 1
            max_y = max(0, total_h - visible_h)
            y = max(0, min(y, max_y))
            self.thumb_canvas.yview_moveto(0 if total_h <= visible_h else y / total_h)
        except Exception:
            pass
        self.after_idle(self._after_thumb_scroll)

    def _nav_first(self):
        self._scroll_thumb_to_index(0)

    def _nav_last(self):
        if self.files:
            self._scroll_thumb_to_index(len(self.files) - 1)

    def _nav_page_up(self):
        if not self.files:
            return
        first = self._first_visible_thumb_index()
        self._scroll_thumb_to_index(max(0, first - self._visible_thumb_count()))

    def _nav_page_down(self):
        if not self.files:
            return
        first = self._first_visible_thumb_index()
        self._scroll_thumb_to_index(min(len(self.files) - 1, first + self._visible_thumb_count()))

    def _nav_goto(self):
        text = self.goto_var.get().strip()
        if not text:
            return
        try:
            idx = int(text) - 1
        except Exception:
            messagebox.showwarning("Goto", "Enter a file number starting at 1.", parent=self)
            return
        if not self.files:
            return
        idx = max(0, min(idx, len(self.files) - 1))
        self.goto_var.set(str(idx + 1))
        self._scroll_thumb_to_index(idx)
        self.select_index(idx)

    # ── Spinners ──────────────────────────────────────────────────────────────

    def _safe_int_var(self, var, default=1):
        try:
            return max(1, int(var.get()))
        except Exception:
            try: var.set(default)
            except Exception: pass
            return default

    def _on_cols_changed(self):
        if self._syncing_spinners:
            return
        self.refresh_thumbs()

    # ── Root / tree ───────────────────────────────────────────────────────────

    def _browse_root(self):
        folder = filedialog.askdirectory(parent=self, title="Select video root folder")
        if folder:
            self.root_var.set(folder)
            self.set_root(folder)

    def set_root(self, root):
        if not root or not os.path.isdir(root):
            messagebox.showwarning("Root not found", f"Folder not found:\n{root}", parent=self)
            return
        root = _ui_path(root)
        self.root_var.set(root)
        _save_ini(root)
        self.clear_files("No video file selected")
        self.current_folder = ""
        if self.folder_tree is not None:
            try:
                self.folder_tree._extensions = {e.lower() for e in VIDEO_EXTS}
                self.folder_tree.set_root(root)
            except Exception:
                pass
        self.status_var.set(root)

    def _on_tree_click(self, event=None):
        if event is None:
            return
        if self.folder_tree is None or self.tree is None:
            return
        item = self.tree.identify_row(event.y)
        if not item or not os.path.isdir(item):
            return
        element = self.tree.identify_element(event.x, event.y)
        if "indicator" in str(element).lower():
            return
        target = _ui_path(item)
        if target == self.current_folder:
            return
        self.load_folder(target)

    def _initial_load(self, initial_files, initial_root):
        if initial_files:
            self._load_file_list(initial_files)
        elif initial_root and os.path.isdir(initial_root):
            self.set_root(initial_root)
        else:
            root = self.root_var.get().strip()
            if root and os.path.isdir(root):
                self.set_root(root)

    # ── File loading ──────────────────────────────────────────────────────────

    def _load_file_list(self, files, center_path=None):
        """Load an explicit list of files (embedded mode / IPC)."""
        self.all_files = [_ui_path(p) for p in files if os.path.isfile(p)]
        self.files     = list(self.all_files)
        self.current_folder = os.path.dirname(self.files[0]) if self.files else ""
        self.selected_idx = None
        self._apply_file_sort()
        self._rebuild_file_list()
        try:
            self.movie_player.set_file_list(self.files, None)
            self.movie_player.show_message("No file selected")
        except Exception:
            pass
        self.status_var.set(f"{len(self.files)} files")
        self.after(50, self.refresh_thumbs)
        if center_path:
            target = _ui_path(center_path)
            if target in self.files:
                self.after(200, lambda: self.select_index(self.files.index(target)))
        elif self.files:
            self.after(200, lambda: self.select_index(0))

    def load_folder(self, folder):
        folder = _ui_path(folder)
        self.current_folder = folder
        self.all_files = _scan_files(folder)
        self.files = list(self.all_files)
        self.selected_idx = None
        self._apply_file_sort()
        self._rebuild_file_list()
        try:
            if self.files:
                self.movie_player.set_file_list(self.files, None)
                self.movie_player.show_message("No file selected")
            else:
                self.movie_player.set_file_list([], None)
                self.movie_player.show_message("No Videos")
        except Exception:
            pass
        self.status_var.set(f"{folder}   ({len(self.files)} files)")
        self.after(50, self.refresh_thumbs)

    def _refresh_current(self):
        if self.current_folder:
            self.load_folder(self.current_folder)
        else:
            root = self.root_var.get().strip()
            if root:
                self.set_root(root)

    def _rebuild_file_list(self):
        """Reload the SortableFileList with the current self.files order."""
        self.file_list_widget.set_files(self.files)

    @staticmethod
    def _get_duration_for_sfl(path: str) -> str:
        """Duration getter passed to SortableFileList — runs in a worker thread."""
        try:
            secs = _duration_ffprobe(path)
            return _fmt_video_duration(secs) if secs > 0 else ""
        except Exception:
            return ""

    def _show_file_sort_menu(self):
        show_file_sort_menu(
            self._sort_btn,
            columns=[("Name", "name"), ("Date taken", "date_taken")],
            sort_column=self._file_sort_column,
            sort_reverse=self._file_sort_reverse,
            callback=self._set_file_sort,
        )

    def _set_file_sort(self, column: str, reverse: bool):
        self._file_sort_column  = column
        self._file_sort_reverse = reverse
        self._sort_btn.config(text=_sort_btn_label(column, reverse))
        self._apply_file_sort()
        self._rebuild_file_list()
        self.refresh_thumbs()

    def _apply_file_sort(self):
        """Sort self.all_files → self.files using current sort preference."""
        try:
            try:
                from libraries.ft_file_ops import sort_files
            except ImportError:
                from ft_file_ops import sort_files  # type: ignore[no-redef]
            self.files = sort_files(
                self.all_files,
                column=self._file_sort_column,
                reverse=self._file_sort_reverse,
            )
        except Exception:
            self.files = sorted(self.all_files,
                                key=lambda p: os.path.basename(p).lower(),
                                reverse=self._file_sort_reverse)

    def _on_sfl_select(self, idx: int):
        """SortableFileList on_select — new row selected by user."""
        self.select_index(idx, from_thumb=False)

    def _on_sfl_click(self, idx: int):
        """SortableFileList on_click — ButtonRelease-1 (handles re-click same row)."""
        self.select_index(idx, from_thumb=False)

    # ── Thumbnail system ──────────────────────────────────────────────────────

    def clear_files(self, message="No Videos"):
        self.files = []
        self.selected_idx = None
        self.file_list_widget.clear()
        self._clear_thumbs()
        try:
            self.movie_player.set_file_list([], None)
            self.movie_player.show_message(message)
        except Exception:
            pass

    def _clear_thumbs(self):
        self.thumb_generation += 1
        self.thumb_request_queue = None
        self.thumb_requested.clear()
        self.thumb_loaded.clear()
        self.thumb_loading.clear()
        for w in self.thumb_inner.winfo_children():
            w.destroy()
        for c in range(50):
            self.thumb_inner.grid_columnconfigure(c, minsize=0, weight=0)
        self.thumb_refs.clear()
        self.thumb_cells.clear()
        self.thumb_labels.clear()
        self.thumb_watermarks.clear()
        self.thumb_total_h = 1
        self.thumb_canvas.yview_moveto(0)
        self.thumb_canvas.configure(scrollregion=(0, 0, self.THUMB_CONTENT_W, 1))
        self._update_nav_status()

    def refresh_thumbs(self):
        self.status_var.set("Building thumbnails...")
        self._clear_thumbs()
        self._show_thumb_progress()   # shows "Generating previews 0 / N"
        if not self.files:
            msg = tk.Label(self.thumb_inner, text="No Videos", bg=BG, fg=DIM,
                           font=("Segoe UI", 24, "bold"), anchor="center")
            msg.place(x=0, y=0,
                      width=max(1, self.THUMB_CONTENT_W),
                      height=max(1, self.thumb_canvas.winfo_height()))
            self.thumb_inner.configure(
                width=self.THUMB_CONTENT_W,
                height=max(1, self.thumb_canvas.winfo_height()))
            self.thumb_total_h = max(1, self.thumb_canvas.winfo_height())
            self.thumb_canvas.configure(
                scrollregion=(0, 0, self.THUMB_CONTENT_W, self.thumb_total_h))
            self._update_nav_status()
            return

        cols = self._safe_int_var(self.cols_var, 6)
        gap  = 6
        layout = calculate_thumb_layout(
            panel_width=self.THUMB_CONTENT_W,
            panel_height=940,
            item_count=len(self.files),
            columns=cols,
            gap=gap,
            boundary_gap=3,
            cell_ratio_w_to_h=0.85,
            image_margin=5,
        )
        self._thumb_layout = layout
        self._thumb_cell_w_for_drag = layout.cell_w

        cell_w = layout.cell_w_px
        cell_h = layout.cell_h_px
        img_x  = layout.image_x_px
        img_y  = layout.image_y_px
        thumb_w = layout.image_w_px
        thumb_h = layout.image_h_px

        self.thumb_cols   = cols
        self.thumb_box    = (thumb_w, thumb_h)
        self.thumb_cell_h = cell_h + gap

        for c in range(cols):
            self.thumb_inner.grid_columnconfigure(c, minsize=0, weight=0)
        total_h = max(1, int(round(layout.total_h)))
        self.thumb_total_h = total_h
        self.thumb_inner.configure(width=self.THUMB_CONTENT_W, height=total_h)
        self.thumb_canvas.configure(scrollregion=(0, 0, self.THUMB_CONTENT_W, total_h))
        self._clamp_thumb_scroll()
        self._update_nav_status()

        generation = self.thumb_generation

        def _build_batch(start_i):
            if self.thumb_generation != generation:
                return
            end_i = min(start_i + 10, len(self.files))
            for idx in range(start_i, end_i):
                path    = self.files[idx]
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

                name_y = img_y + thumb_h + 6
                name_h = max(12, cell_h - name_y - 2)
                name_lbl = tk.Label(cell, text=os.path.basename(path),
                                    bg="white", fg=TEXT, font=("Segoe UI", 8),
                                    anchor="center",
                                    wraplength=max(60, cell_w - 8))
                name_lbl.place(x=3, y=name_y, width=cell_w - 6, height=name_h)

                sel_lbl = tk.Label(cell, text="SELECTED", bg="#111111", fg="white",
                                   font=("Segoe UI", 8, "bold"), padx=3, pady=1)

                self.thumb_cells[idx]      = cell
                self.thumb_labels[idx]     = (img_lbl, name_lbl)
                self.thumb_watermarks[idx] = sel_lbl
                self._set_thumb_watermark(idx, idx in self._thumb_selected)

                for widget in (cell, img_lbl, name_lbl, sel_lbl):
                    widget.bind("<ButtonPress-1>",
                                lambda e, i=idx: self._thumb_press(i, e))
                    widget.bind("<B1-Motion>",
                                lambda e: self._thumb_drag_motion(e))
                    widget.bind("<ButtonRelease-1>",
                                lambda e, i=idx: self._thumb_release(i, e))
                    widget.bind("<MouseWheel>", self._on_thumb_wheel)
                    widget.bind("<ButtonPress-3>",
                                lambda e, i=idx: self._thumb_right_click(i, e))

            if end_i < len(self.files):
                self.after(10, lambda s=end_i: _build_batch(s))
            else:
                self.after(10, self._schedule_visible_thumbs)

        _build_batch(0)

    def _schedule_visible_thumbs(self):
        if not self.files or not self.thumb_labels:
            return
        generation = self.thumb_generation
        try:
            y0 = self.thumb_canvas.canvasy(0)
            y1 = y0 + max(1, self.thumb_canvas.winfo_height())
        except Exception:
            y0, y1 = 0, 900
        buffer    = max(200, y1 - y0)
        start_row = max(0, int((y0 - buffer) // max(1, self.thumb_cell_h)))
        end_row   = int((y1 + buffer) // max(1, self.thumb_cell_h)) + 1
        start_idx = start_row * self.thumb_cols
        end_idx   = min(len(self.files), (end_row + 1) * self.thumb_cols)

        wanted = [idx for idx in range(start_idx, end_idx)
                  if idx not in self.thumb_loaded
                  and idx not in self.thumb_loading
                  and idx not in self.thumb_requested
                  and self.thumb_labels.get(idx)]
        if not wanted:
            return

        if self.thumb_request_queue is None:
            self.thumb_request_queue = queue.Queue()
        for idx in wanted:
            self.thumb_requested.add(idx)
            self.thumb_request_queue.put((
                generation, idx, self.files[idx],
                self.thumb_labels[idx][0],
                self.thumb_box[0], self.thumb_box[1],
            ))
        while self.thumb_loader_workers < self.MAX_THUMB_WORKERS:
            self.thumb_loader_workers += 1
            threading.Thread(target=self._thumb_loader_worker, daemon=True).start()

    def _thumb_loader_worker(self):
        while True:
            q = self.thumb_request_queue
            if q is None:
                self.thumb_loader_workers = max(0, self.thumb_loader_workers - 1)
                return
            try:
                item = q.get(timeout=0.4)
            except queue.Empty:
                self.thumb_loader_workers = max(0, self.thumb_loader_workers - 1)
                if self.thumb_loader_workers == 0:
                    self.after(0, self._show_thumb_progress)  # updates status bar to done/folder line
                return

            generation, idx, path, label, bw, bh = item
            if generation != self.thumb_generation:
                continue

            def _mark_loading(g=generation, i=idx):
                if g == self.thumb_generation:
                    self.thumb_requested.discard(i)
                    self.thumb_loading.add(i)
            self.after(0, _mark_loading)

            thumb_size = max(1, min(int(bw), int(bh)))

            # Cache-first: reuse thumbnail stored in the global per-PC cache
            img = None
            _db_source = "none"
            if _HAVE_THUMB_CACHE:
                try:
                    jpeg_bytes = _ft_thumb_cache.get_thumb(_ui_path(path))
                    if jpeg_bytes:
                        stored = Image.open(_io.BytesIO(jpeg_bytes)).convert("RGB")
                        stored.thumbnail((thumb_size, thumb_size), Image.LANCZOS)
                        img = stored
                        _db_source = "cache"
                except Exception as _cache_ex:
                    print(f"FTVideo cache thumb error ({os.path.basename(path)}): {_cache_ex}")
                    img = None

            if img is None:
                img, ok, err = make_movie_thumbnail_fast(
                    path, MoviePlayerPanel.THUMB_POSITION, _THUMB_STORE_SIZE, longpath_func=_longpath)
                _db_source = "generated" if img is not None else f"FAILED({err})"
                # Store at full size in global cache so FTMod and future FTVideo launches can reuse it
                if img is not None and _HAVE_THUMB_CACHE:
                    try:
                        buf = _io.BytesIO()
                        img.save(buf, "JPEG", quality=82)
                        _ft_thumb_cache.put_thumb(_ui_path(path), buf.getvalue())
                    except Exception:
                        pass
                # Scale to display size, preserving aspect ratio
                if img is not None:
                    img.thumbnail((thumb_size, thumb_size), Image.LANCZOS)

            print(f"FTVideo thumb [{_db_source}]: {os.path.basename(path)}")
            self._queue_thumb_apply(generation, idx, label, img)

    def _queue_thumb_apply(self, generation, idx, label, img):
        if not hasattr(self, "_thumb_apply_queue"):
            self._thumb_apply_queue = []
            self._thumb_apply_running = False
        self._thumb_apply_queue.append((generation, idx, label, img))
        if not self._thumb_apply_running:
            self._thumb_apply_running = True
            self.after(10, self._drain_thumb_apply_queue)

    def _drain_thumb_apply_queue(self):
        if not hasattr(self, "_thumb_apply_queue"):
            self._thumb_apply_running = False
            return
        # While video is playing, defer all UI updates — avoid stealing event-loop
        # time from the video frame callbacks.
        try:
            if self.movie_player.is_playing:
                self.after(300, self._drain_thumb_apply_queue)
                return
        except Exception:
            pass
        batch = self._thumb_apply_queue[:5]
        self._thumb_apply_queue = self._thumb_apply_queue[5:]
        for generation, idx, label, img in batch:
            self._apply_thumb(generation, idx, label, img)
        if self._thumb_apply_queue:
            self.after(10, self._drain_thumb_apply_queue)
        else:
            self._thumb_apply_running = False

    def _apply_thumb(self, generation, idx, label, img):
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
        except Exception:
            pass
        self._apply_thumb_jpeg_cache(idx, img)
        self._show_thumb_progress()

    # Drag-to-combine-strip / multi-select methods

    def _thumb_press(self, idx, event):
        """Unified ButtonPress-1: record drag origin + handle selection.

        Left click  — preview only; selection unchanged.
        Ctrl+click  — toggle Ctrl-selection; update preview; set shift anchor.
        Shift+click — apply last Ctrl-click op (select/deselect) to range
                      from Ctrl anchor to here; update preview.
        """
        self._drag_press_x = event.x_root
        self._drag_press_y = event.y_root
        self._drag_idx     = idx
        self._drag_active  = False
        self._drag_ghost   = None
        self._press_defer_select = False

        ctrl  = bool(event.state & 0x4)
        shift = bool(event.state & 0x1)

        if ctrl:
            # Toggle Ctrl-selection; anchor moves here
            if idx in self._thumb_selected:
                self._thumb_selected.discard(idx)
                self._last_ctrl_op = "deselect"
            else:
                self._thumb_selected.add(idx)
                self._last_ctrl_op = "select"
            self._thumb_sel_anchor = idx
            self._redraw_thumb_select(idx)
            self._update_file_list_ctrl_selection()
            self._load_video_for_index(idx)

        elif shift:
            # Range select/deselect matching last Ctrl-click operation
            anchor = self._thumb_sel_anchor if self._thumb_sel_anchor is not None else 0
            a, b = sorted([anchor, idx])
            op = self._last_ctrl_op
            changed = set()
            for i in range(a, b + 1):
                if op == "select":
                    self._thumb_selected.add(i)
                else:
                    self._thumb_selected.discard(i)
                changed.add(i)
            for i in changed:
                self._redraw_thumb_select(i)
            self._update_file_list_ctrl_selection()
            self._load_video_for_index(idx)

        else:
            # Plain left-click: preview only — selection state unchanged
            self._load_video_for_index(idx)

    def _thumb_drag_motion(self, event):
        if self._drag_idx is None:
            return
        dx = abs(event.x_root - self._drag_press_x)
        dy = abs(event.y_root - self._drag_press_y)
        if not self._drag_active and (dx > 8 or dy > 8):
            self._drag_active = True
            # Build path list: all selected if drag-idx is in selection, else just this one
            idx = self._drag_idx
            if self._thumb_selected and idx in self._thumb_selected:
                self._drag_paths = [(i, self.files[i])
                                    for i in sorted(self._thumb_selected)
                                    if 0 <= i < len(self.files)]
            else:
                path = self.files[idx] if 0 <= idx < len(self.files) else None
                self._drag_paths = [(idx, path)] if path else []
            # Build ghost
            photo = None
            n = len(self._drag_paths)
            if n == 1:
                try:
                    lbl_pair = self.thumb_labels.get(self._drag_paths[0][0])
                    if lbl_pair:
                        photo = getattr(lbl_pair[0], "image", None)
                except Exception:
                    pass
            self._drag_ghost = _ThumbDragGhost(self, photo,
                                               count=n if n > 1 else 0)
        if self._drag_active:
            if self._drag_ghost:
                self._drag_ghost.move(event.x_root, event.y_root)
            over = self._over_combine_strip(event.x_root, event.y_root)
            try:
                if self.movie_player.combine_strip:
                    self.movie_player.combine_strip.set_drop_highlight(over)
            except Exception:
                pass

    def _thumb_release(self, idx, event):
        was_drag = self._drag_active
        ghost    = self._drag_ghost
        paths    = list(self._drag_paths)
        self._drag_ghost         = None
        self._drag_active        = False
        self._drag_paths         = []
        self._drag_idx           = None
        self._press_defer_select = False
        if ghost:
            ghost.destroy()
        try:
            if self.movie_player.combine_strip:
                self.movie_player.combine_strip.set_drop_highlight(False)
        except Exception:
            pass
        if was_drag and paths:
            if self._over_combine_strip(event.x_root, event.y_root):
                # Pass all clips as one ordered batch so they land in grid order.
                try:
                    pairs = [(path, self._thumb_jpeg.get(i)) for i, path in paths]
                    self.movie_player.add_clips_to_strip(pairs)
                except Exception as e:
                    print(f"add_clips_to_strip: {e}")

    def _update_file_list_ctrl_selection(self):
        """Sync the file-list ctrl-selection highlight with _thumb_selected."""
        try:
            self.file_list_widget.set_ctrl_selected(self._thumb_selected)
        except Exception:
            pass

    def _redraw_thumb_select(self, idx):
        """Update one thumbnail's background and SELECTED watermark."""
        cell = self.thumb_cells.get(idx)
        if cell is None:
            return
        is_ctrl_sel = idx in self._thumb_selected
        if idx == self.selected_idx:
            bg = SELECT_BG
        elif is_ctrl_sel:
            bg = "#c8e6ff"   # ctrl-selection tint
        else:
            bg = "white"
        try:
            cell.configure(bg=bg)
            for lbl in self.thumb_labels.get(idx, ()):
                lbl.configure(bg=bg)
        except Exception:
            pass
        self._set_thumb_watermark(idx, is_ctrl_sel)

    def _set_thumb_watermark(self, idx, selected):
        """Show/hide the SELECTED overlay near the bottom of the image area."""
        lbl = self.thumb_watermarks.get(idx)
        if lbl is None:
            return
        try:
            if selected:
                labels = self.thumb_labels.get(idx, ())
                img_lbl = labels[0] if labels else None
                wm_w, wm_h = 74, 16
                x, y = 4, 4
                if img_lbl is not None:
                    info = img_lbl.place_info()
                    iw = int(info.get('width',  '') or 0)
                    ih = int(info.get('height', '') or 0)
                    if iw > 0 and ih > 0:
                        x = int(info.get('x', '') or 0) + max(0, (iw - wm_w) // 2)
                        y = int(info.get('y', '') or 0) + ih - wm_h - 4
                lbl.place(x=x, y=y, width=wm_w, height=wm_h)
                lbl.lift()
            else:
                lbl.place_forget()
        except Exception:
            pass

    def _over_combine_strip(self, x_root, y_root):
        try:
            strip = self.movie_player.combine_strip
            if strip is None:
                return False
            return (strip.winfo_rootx() <= x_root <= strip.winfo_rootx() + strip.winfo_width()
                    and strip.winfo_rooty() <= y_root <= strip.winfo_rooty() + strip.winfo_height())
        except Exception:
            return False

    def _apply_thumb_jpeg_cache(self, idx, img):
        try:
            import io as _io_local
            buf = _io_local.BytesIO()
            small = img.copy()
            small.thumbnail((160, 120))
            small.save(buf, "JPEG", quality=82)
            self._thumb_jpeg[idx] = buf.getvalue()
        except Exception:
            self._thumb_jpeg[idx] = None

    # ── Selection ─────────────────────────────────────────────────────────────

    # _on_file_list_click and _on_file_select have been absorbed into
    # SortableFileList (on_click / on_select callbacks + internal guards).

    def _thumb_right_click(self, idx, event):
        """Right-click — show context menu without changing selection state.

        If no items are Ctrl-selected, the right-clicked item is the implicit
        target (count = 1) but is NOT added to the selection set.
        """
        # Determine operation targets
        if self._thumb_selected:
            op_indices = sorted(self._thumb_selected)
        else:
            op_indices = [idx] if 0 <= idx < len(self.files) else []
        n = len(op_indices)
        op_paths = [self.files[i] for i in op_indices if 0 <= i < len(self.files)]
        label = f"Add {n} clips to timeline" if n > 1 else "Add to timeline"

        menu = tk.Menu(self, tearoff=0)
        menu.add_command(
            label=label,
            command=lambda: self._add_selected_to_strip(op_indices))
        menu.add_separator()
        menu.add_command(
            label=f"Copy selected ({n})",
            command=lambda p=op_paths: self._copy_selected_files(p))
        menu.add_command(
            label=f"Move selected ({n})",
            command=lambda p=op_paths: self._move_selected_files(p))
        menu.add_separator()
        menu.add_command(
            label=f"Delete selected ({n})",
            command=lambda p=op_paths: self._delete_selected_files(p))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _load_video_for_index(self, idx):
        """Load idx into the video player (preview), preserving thumb scroll position."""
        if idx < 0 or idx >= len(self.files):
            return
        try:
            old_yview = self.thumb_canvas.yview()
        except Exception:
            old_yview = None
        self.select_index(idx, from_thumb=True)
        if old_yview is not None:
            def _restore(y=old_yview):
                try:
                    self.thumb_canvas.yview_moveto(y[0])
                    self._update_nav_status()
                except Exception:
                    pass
            self.after_idle(_restore)
            self.after(50, _restore)

    def _add_selected_to_strip(self, indices):
        """Add clips at the given sorted indices to the timeline strip."""
        pairs = [(self.files[i], self._thumb_jpeg.get(i))
                 for i in indices
                 if 0 <= i < len(self.files)]
        if pairs:
            try:
                self.movie_player.add_clips_to_strip(pairs)
            except Exception as e:
                print(f"add_clips_to_strip: {e}")

    # ── File operations on selected thumbnails ────────────────────────────────

    def _file_op_result_msg(self, title, result):
        lines = [f"Completed: {result.ok_count}"]
        if result.skipped_existing:
            lines.append(f"Skipped duplicates: {len(result.skipped_existing)}")
        if result.skipped_missing:
            lines.append(f"Skipped missing: {len(result.skipped_missing)}")
        if result.errors:
            lines.append(f"Errors: {len(result.errors)}")
            for src, err in result.errors[:6]:
                lines.append(f"  {os.path.basename(src)}: {err}")
        messagebox.showinfo(title, "\n".join(lines), parent=self)

    def _refresh_after_file_op(self):
        """Reload the current folder so moved/deleted files disappear."""
        self._thumb_selected.clear()
        folder = self.current_folder
        if folder:
            self.after(150, lambda: self.load_folder(folder))

    def _copy_selected_files(self, paths):
        if not paths:
            return
        if _ft_file_ops is None:
            messagebox.showerror("Copy", "ft_file_ops not available.", parent=self)
            return
        dest = filedialog.askdirectory(title="Copy to folder", parent=self)
        if not dest:
            return
        try:
            result = _ft_file_ops.copy_files(paths, dest, overwrite=False)
        except Exception as e:
            messagebox.showerror("Copy selected", str(e), parent=self)
            return
        self._file_op_result_msg("Copy selected", result)

    def _move_selected_files(self, paths):
        if not paths:
            return
        if _ft_file_ops is None:
            messagebox.showerror("Move", "ft_file_ops not available.", parent=self)
            return
        dest = filedialog.askdirectory(title="Move to folder", parent=self)
        if not dest:
            return
        try:
            result = _ft_file_ops.move_files(paths, dest, overwrite=False)
        except Exception as e:
            messagebox.showerror("Move selected", str(e), parent=self)
            return
        self._file_op_result_msg("Move selected", result)
        self._refresh_after_file_op()

    def _delete_selected_files(self, paths):
        if not paths:
            return
        if _ft_file_ops is None:
            messagebox.showerror("Delete", "ft_file_ops not available.", parent=self)
            return
        n = len(paths)
        if not messagebox.askyesno(
            "Delete selected",
            f"Permanently delete {n} file{'s' if n != 1 else ''} from disk?",
            parent=self,
        ):
            return
        result = _ft_file_ops.delete_files(paths)
        self._file_op_result_msg("Delete selected", result)
        self._refresh_after_file_op()

    # ─────────────────────────────────────────────────────────────────────────

    def _on_thumb_click(self, idx, event=None):
        """Kept for any external callers; delegates to _thumb_press logic."""
        if idx < 0 or idx >= len(self.files):
            return
        self.select_index(idx, from_thumb=True)

    def _on_player_select_index(self, idx):
        """Callback from MoviePlayerPanel prev/next navigation."""
        if 0 <= idx < len(self.files):
            self.select_index(idx, from_thumb=False)

    def select_index(self, idx, from_thumb=False):
        if idx < 0 or idx >= len(self.files):
            return
        prev_idx = self.selected_idx
        self.selected_idx = idx

        if not from_thumb:
            self._scroll_thumb_to_index(idx)

        try:
            self.file_list_widget.select_index(idx, scroll=not from_thumb)
        except Exception:
            pass

        if prev_idx is not None and prev_idx != idx:
            self._highlight_thumb(prev_idx, False)
        self._highlight_thumb(idx, True)

        # Pause thumbnail loading for 2 s so playback ffmpeg gets disk priority.
        # With 238 clips and 3 thumb workers each running ffprobe+ffmpeg, IO
        # contention can starve the playback pipe.  We drain the queue and let
        # workers exit, then reschedule visible thumbs after the pause.
        self.thumb_request_queue = None   # workers exit after finishing current job
        # Clear pending sets so _resume_thumb_loading can re-queue them after the pause
        self.thumb_requested.clear()
        self.thumb_loading.clear()
        if hasattr(self, "_thumb_resume_id") and self._thumb_resume_id:
            try:
                self.after_cancel(self._thumb_resume_id)
            except Exception:
                pass
        self._thumb_resume_id = self.after(2000, self._resume_thumb_loading)

        try:
            self.movie_player.set_file_list(self.files, idx)
        except Exception:
            pass
        # Show progress if still generating; otherwise show selected filename.
        if self.thumb_loaded and len(self.thumb_loaded) < len(self.files):
            self._show_thumb_progress()
        else:
            self.status_var.set(os.path.basename(self.files[idx]))

    def _resume_thumb_loading(self):
        # Keep paused while video is actively playing — re-check every 500 ms.
        try:
            if self.movie_player.is_playing:
                self._thumb_resume_id = self.after(500, self._resume_thumb_loading)
                return
        except Exception:
            pass
        self._thumb_resume_id = None
        self.thumb_loader_workers = 0
        self._schedule_visible_thumbs()

    def _show_thumb_progress(self):
        """Update the status bar with thumbnail generation progress."""
        total    = len(self.files)
        n_loaded = len(self.thumb_loaded)
        if total == 0:
            return
        if n_loaded >= total:
            # Restore normal folder line — same as what _thumb_loader_worker does
            self.status_var.set(
                f"{self.current_folder}   ({total} files)"
                if self.current_folder else "Ready")
        else:
            self.status_var.set(
                f"Generating previews  {n_loaded} / {total}"
                f"   —   use the file list to play while loading")

    def _highlight_thumb(self, idx, active):
        """Mark idx as the active (playing) thumbnail.  Multi-select tint is
        handled separately by _redraw_thumb_select."""
        cell = self.thumb_cells.get(idx)
        if cell is None:
            return
        if active:
            bg = SELECT_BG
        elif idx in self._thumb_selected:
            bg = "#c8e6ff"
        else:
            bg = "white"
        try:
            cell.configure(bg=bg)
            labels = self.thumb_labels.get(idx, ())
            for lbl in labels:
                lbl.configure(bg=bg)
        except Exception:
            pass

    # ── IPC (embedded mode) ───────────────────────────────────────────────────

    def _poll_ipc(self):
        try:
            req_path = os.path.join(self._ipc_dir, "FTVideo_request.csv")
            if os.path.exists(req_path):
                with open(req_path, encoding="utf-8") as f:
                    lines = [l.strip() for l in f if l.strip()]
                seq    = int(lines[0].split(",", 1)[1]) if lines and lines[0].startswith("SEQ,") else 0
                center = ""
                files  = []
                for line in lines[1:]:
                    if line.startswith("CENTER,"):
                        center = line.split(",", 1)[1]
                    else:
                        files.append(line)
                if seq != self._ipc_seq and files:
                    self._ipc_seq = seq
                    os.remove(req_path)
                    self._load_file_list(files, center_path=center or None)
        except Exception as e:
            print(f"FTVideo poll error: {e}")
        self.after(500, self._poll_ipc)

    # ── Window close ──────────────────────────────────────────────────────────

    def _on_close(self):
        try:
            self.movie_player.stop()
        except Exception:
            pass
        self.destroy()


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    embedded     = "--embedded" in args
    initial_root = None
    initial_files = None

    remaining = [a for a in args if a != "--embedded"]
    if remaining:
        p = remaining[0]
        if os.path.isdir(p):
            initial_root = p
        elif os.path.isfile(p) and os.path.splitext(p)[1].lower() in VIDEO_EXTS:
            initial_files = remaining
        else:
            initial_root = p   # will show "folder not found" warning
    app = FTVideo(embedded=embedded, initial_files=initial_files, initial_root=initial_root)
    app.mainloop()


if __name__ == "__main__":
    main()
