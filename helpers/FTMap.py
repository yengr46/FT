r"""
FTMapB.py  -
Version: 23:40 26-Apr-2026  FileTagger Map Image - ESRI Satellite
--------------------------------------
Standalone GPS map viewer. Same layout as FTEditimg:
  [File list] | [empty ctrl col] | [Selected image] | [Map]

Usage:
  Standalone - folder:  pythonw.exe FTMapB.py "S:\Photos\2024"
  Standalone - file:    pythonw.exe FTMapB.py "S:\Photos\2024\IMG_001.jpg"
  Embedded:             pythonw.exe FTMapB.py --embedded

IPC: <script_dir>\FT_IPC\FTMap_request.csv  {"seq":N,"folder":"...","center":"..."}
"""

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import os, sys, math, json, configparser, threading
import tkinter as tk
from libraries.ft_widgets import FileCountTree, FolderTreeWidget, TREE_COL_W, TREE_WIDTH, TREE_SCROLL_W, TREE_PAD_R, show_file_sort_menu, _sort_btn_label
from tkinter import messagebox

try:
    from libraries.ft_project_roots import read_project_roots
except Exception:
    def read_project_roots(base_file=None):
        return {"photos": "", "pdfs": "", "project": ""}

try:
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

try:
    from PIL import Image, ImageTk, ImageFile, ImageOps, ImageDraw
    Image.MAX_IMAGE_PIXELS = None
except ImportError:
    tk.Tk().withdraw()
    messagebox.showerror("Missing library", "Pillow is required.\n\nRun:  pip install Pillow")
    sys.exit(1)

# ── Constants ─────────────────────────────────────────────────────────────────
PHOTO_EXTS  = {'.jpg', '.jpeg'}
# Match FTView's standalone navigation widths so the shared folder tree
# has enough horizontal space for both Files and Tree columns.
FTVIEW_TREE_W = 420
FTMAP_FILE_LIST_W = 300
FTMAP_NAV_W = FTVIEW_TREE_W + FTMAP_FILE_LIST_W
BG          = "#f0f0f0"
BG2         = "#e0e0e0"
BG3         = "#f0f0f0"
TEXT_BRIGHT = "#111111"
TEXT_DARK   = "#111111"
TEXT_DIM    = "#555555"
ACCENT      = "#1a5276"
CANVAS_BG   = "#888888"

# ── Helpers ───────────────────────────────────────────────────────────────────

def _longpath(p):
    if os.name == 'nt':
        p = p.replace('/', '\\')
        if not p.startswith('\\\\?\\'):
            return '\\\\?\\' + os.path.abspath(p)
    return p

def _script_dir():
    return os.path.dirname(os.path.abspath(__file__))

def _ipc_dir():
    ini = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "FileTagger.ini")
    path = None
    if os.path.exists(ini):
        cfg = configparser.ConfigParser(strict=False)
        cfg.read(ini)
        path = cfg.get("FileTagger", "ipc_folder", fallback="").strip()
    if not path:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "FT_IPC")
    os.makedirs(path, exist_ok=True)
    return path

def _centre_window(win, w, h):
    win.update_idletasks()
    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()
    win.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")

def _scan_folder(folder):
    try:
        return sorted([
            os.path.join(folder, f)
            for f in os.listdir(folder)
            if os.path.splitext(f)[1].lower() in PHOTO_EXTS
        ], key=str.lower)
    except Exception:
        return []

# ── GPS helpers ───────────────────────────────────────────────────────────────
_gps_cache = {}

def _get_gps_coords(path):
    key = str(path)
    if key in _gps_cache:
        return _gps_cache[key]
    try:
        img = Image.open(str(path))
        exif = img.getexif()
        if not exif:
            _gps_cache[key] = None; return None
        gps = exif.get_ifd(34853)
        if not gps or 2 not in gps or 4 not in gps:
            _gps_cache[key] = None; return None
        def _dms(v): return float(v[0]) + float(v[1])/60 + float(v[2])/3600
        lat = _dms(gps[2]); lon = _dms(gps[4])
        if gps.get(1, "N") == "S": lat = -lat
        if gps.get(3, "E") == "W": lon = -lon
        result = None if (lat == 0.0 and lon == 0.0) else (lat, lon)
        _gps_cache[key] = result
        return result
    except Exception:
        _gps_cache[key] = None
        return None

def _count_gps_in_folder(path):
    """Count files with GPS coordinates directly in path."""
    try:
        return sum(
            1 for e in os.scandir(path)
            if e.is_file()
            and os.path.splitext(e.name)[1].lower() in PHOTO_EXTS
            and _get_gps_coords(e.path) is not None
        )
    except Exception:
        return 0

def _haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2-lat1); dl = math.radians(lon2-lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return R * 2 * math.asin(math.sqrt(a))

def _make_pin(color, size=18):
    try:
        w, h = size, int(size * 1.4)
        img = Image.new("RGBA", (w, h), (0,0,0,0))
        d = ImageDraw.Draw(img)
        d.ellipse([1,1,w-2,w-2], fill=color, outline="#ffffff", width=1)
        d.polygon([(w//2,h-1),(w//2-3,w-4),(w//2+3,w-4)], fill=color)
        return ImageTk.PhotoImage(img)
    except Exception:
        return None

# ── Main application ──────────────────────────────────────────────────────────

# ═══════════════════════════════════════════════════════════════════════════════
# ── FTMapTree — shared Files/Tree folder tree for FTMap ───────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

class FTMapTree(FileCountTree):
    """FTMap folder tree using the shared FileCountTree implementation.

    FTMap deliberately shares the same folder-tree counting logic as FTView and
    FTmod: two columns, Files and Tree.  The old FTMap-specific GPS column made
    the tree visually and behaviourally different from the rest of the FT suite.
    GPS counts remain part of the map/file logic, not the folder-tree structure.
    """

    def __init__(self, parent, extensions=None, get_gps_count=None, **kw):
        # get_gps_count is accepted for backward compatibility with older
        # FTMap construction code, but the shared tree now exposes only
        # Files/Tree columns like FTView/FTmod.
        super().__init__(
            parent,
            extensions=extensions or PHOTO_EXTS,
            col_own="Files",
            col_child="Tree",
            **kw,
        )


class FTMap(tk.Tk):

    CTRL_W = 0   # No operation column — set nonzero to restore

    def __init__(self, mode, start_path=None, file_list=None):
        super().__init__()
        self.title("FTMapB version 1.1 - FileTagger Map Image - ESRI Satellite")
        self.configure(bg=BG)
        self.minsize(700, 400)

        self._mode       = mode
        self._file_list  = file_list or []
        self._file_idx   = 0
        self._sort_column  = "name"
        self._sort_reverse = False
        self._gps_flags  = {}   # path -> bool
        self._gps_data   = {}   # path -> (lat, lon)
        self._gps_scan_gen = 0   # cancels stale background GPS scans

        # Map state
        self._tmv           = None
        self.map_widget     = None
        self._map_ready     = False
        self._all_markers   = {}
        self._marker_coords = {}
        self._marker_km     = {}
        self._selected_path = None
        self._image_load_token = 0  # prevents stale background image loads from replacing the current image
        self._pin_red       = None
        self._pin_green     = None
        self._nearby_data   = []
        self._map_mode      = "osm"
        self._pins_visible  = True

        # Image display — keep strong references to prevent GC
        self._img_photo   = None
        self._photo_refs  = []   # permanent store — prevents GC on 32-bit

        # IPC
        self._ipc_seq = -1
        self._ipc_dir = _ipc_dir() if mode == "embedded" else None

        self._build_ui()

        # Create pin images AFTER window exists (PhotoImage needs a live Tk root)
        self._pin_red   = _make_pin("#cc0000", 20)
        self._pin_green = _make_pin("#1a9977", 18)
        # Keep permanent refs so GC never collects them
        self._photo_refs.extend([self._pin_red, self._pin_green])

        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        win_h = int(sh * 0.80)
        if self._mode == "standalone":
            # tree(420, matching FTView) + files(300) + image(600) + map + sashes
            win_w = min(2340, sw - 40)
        else:
            # files(300) + image(600) + map(1000) + sashes
            win_w = min(1920, sw - 40)
        x = (sw - win_w) // 2
        y = (sh - win_h) // 2
        self.geometry(f"{win_w}x{win_h}+{x}+{y}")
        self.after(150, self._set_initial_sashes)

        if start_path and os.path.isfile(start_path):
            self._load_image(start_path)
        elif self._file_list:
            self._load_image(self._file_list[0])

        if mode == "embedded":
            self.after(500, self._poll_ipc)

        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def _set_initial_sashes(self):
        """Set sash positions to match specified column widths."""
        try:
            self.update_idletasks()
            if self._mode == "standalone":
                # Sash 0: after left navigation area (tree + files).
                self._paned.sash_place(0, FTMAP_NAV_W, 0)
                # Sash 1: after selected-image pane.
                self._paned.sash_place(1, FTMAP_NAV_W + 600, 0)
                # Inner sash: tree width matches FTView so Files/Tree columns show.
                try:
                    self._left_paned.sash_place(0, FTVIEW_TREE_W, 0)
                except Exception: pass
            else:
                # Embedded: file list (300) | image (600) | map
                self._paned.sash_place(0, 300, 0)
                self._paned.sash_place(1, 900, 0)
        except Exception:
            pass

    # ── UI — mirrors FTEditimg exactly ───────────────────────────────────────

    def _build_ui(self):
        self._paned = tk.PanedWindow(self, orient="horizontal", bg=BG,
                                     sashwidth=5, sashrelief="flat", sashpad=2)
        self._paned.pack(fill="both", expand=True)

        # ── Pane 1: left nav panel ────────────────────────────────────────────
        from tkinter import ttk

        if self._mode == "standalone":
            left_outer = tk.Frame(self._paned, bg=BG2, width=FTMAP_NAV_W)
            self._paned.add(left_outer, minsize=FTMAP_NAV_W, width=FTMAP_NAV_W, stretch="never")

            # ── Folder root entry bar ─────────────────────────────────────────
            folder_bar = tk.Frame(left_outer, bg=BG2)
            folder_bar.pack(fill="x", padx=4, pady=(6,2))
            tk.Label(folder_bar, text="Root folder", bg=BG2, fg=TEXT_DIM,
                     font=("Segoe UI", 8, "bold")).pack(anchor="w")
            fe_frame = tk.Frame(folder_bar, bg=BG2)
            fe_frame.pack(fill="x")
            self._folder_var = tk.StringVar()
            self._folder_entry = tk.Entry(fe_frame, textvariable=self._folder_var,
                                          bg="white", fg=TEXT_BRIGHT,
                                          font=("Segoe UI", 8),
                                          relief="solid", bd=1)
            self._folder_entry.pack(side="left", fill="x", expand=True)
            self._folder_entry.bind("<Return>",
                lambda e: self._set_tree_root(self._folder_var.get().strip()))
            tk.Button(fe_frame, text="...", bg=ACCENT, fg="white",
                      font=("Segoe UI", 9, "bold"), relief="flat",
                      padx=6, cursor="hand2",
                      command=self._browse_root).pack(side="left", padx=(2,0))
            self._folder_count = tk.Label(folder_bar, text="Select a root folder",
                                          bg=BG2, fg=TEXT_DIM,
                                          font=("Segoe UI", 7), anchor="w")
            self._folder_count.pack(anchor="w", pady=(2,0))

            tk.Frame(left_outer, bg="#aaaaaa", height=1).pack(fill="x", pady=(4,0))

            # ── Horizontal split: tree (left) | file list (right) ────────────
            left_paned = tk.PanedWindow(left_outer, orient="horizontal", bg=BG2,
                                        sashwidth=4, sashrelief="flat")
            left_paned.pack(fill="both", expand=True)
            self._left_paned = left_paned

            # Folder tree — FTMapTree from ft_widgets
            self._tree_widget = FTMapTree(
                left_paned,
                extensions=PHOTO_EXTS,
                get_gps_count=lambda p: 0,  # GPS disabled — too slow
                on_select=self._on_tree_select_path,
                show_root_entry=False,
                bg=BG2
            )
            left_paned.add(self._tree_widget, minsize=FTVIEW_TREE_W,
                           width=FTVIEW_TREE_W, stretch="never")
            try:
                self._tree_widget.configure(width=FTVIEW_TREE_W)
                self._tree_widget.pack_propagate(False)
            except Exception:
                pass
            self._tree = self._tree_widget.tree()

            # File list inside left_paned
            file_frame = tk.Frame(left_paned, bg=BG2)
            left_paned.add(file_frame, minsize=FTMAP_FILE_LIST_W, width=FTMAP_FILE_LIST_W, stretch="never")

        else:
            # Embedded: just a file list pane, no tree or folder browser
            left_outer = tk.Frame(self._paned, bg=BG2, width=300)
            self._paned.add(left_outer, minsize=200, width=300, stretch="never")
            self._folder_var   = None
            self._folder_count = None
            self._tree         = None
            file_frame = left_outer

        # ── File list (both modes) ────────────────────────────────────────────
        _files_hdr = tk.Frame(file_frame, bg=BG2)
        _files_hdr.pack(fill="x")
        tk.Label(_files_hdr, text="JPG FILES", bg=BG2, fg=TEXT_DIM,
                 font=("Segoe UI", 8, "bold")).pack(side="left", padx=(4, 0), pady=2)
        self._sort_btn = tk.Button(
            _files_hdr, text=_sort_btn_label(self._sort_column, self._sort_reverse),
            font=("Segoe UI", 8, "bold"), bg=BG2, fg=ACCENT,
            relief="flat", cursor="hand2",
            command=lambda: self._show_sort_menu(self._sort_btn),
        )
        self._sort_btn.pack(side="right", padx=(0, 20))

        lb_sb = tk.Scrollbar(file_frame, orient="vertical")
        self._lb = tk.Listbox(file_frame, bg="white", fg=TEXT_BRIGHT,
                              selectbackground=ACCENT, selectforeground="white",
                              font=("Segoe UI", 9), activestyle="none",
                              yscrollcommand=lb_sb.set, borderwidth=0,
                              highlightthickness=1,
                              highlightbackground="#cccccc")
        self._lb_font = ("Segoe UI", 9)
        lb_sb.config(command=self._lb.yview)
        lb_sb.pack(side="right", fill="y")
        self._lb.pack(fill="both", expand=True, padx=(4,0), pady=(2,4))
        self._lb.bind("<<ListboxSelect>>", self._on_list_select)
        self._lb.bind("<MouseWheel>",
            lambda e: self._lb.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        self._lb.bind("<Up>",   lambda e: self._navigate(-1))
        self._lb.bind("<Down>", lambda e: self._navigate(1))

        # ── Pane 2: operation controls — zero width for FTMapB ──────────────
        if self.CTRL_W > 0:
            ctrl_frame = tk.Frame(self._paned, bg=BG, width=self.CTRL_W)
            self._paned.add(ctrl_frame, minsize=self.CTRL_W, width=self.CTRL_W,
                            stretch="never")

        # ── Path title bar ────────────────────────────────────────────────────
        path_bar = tk.Frame(self, bg=BG2, height=26)
        path_bar.pack(fill="x", side="top")
        path_bar.pack_propagate(False)
        self._path_lbl = tk.Label(path_bar, text="No file selected",
                                  bg=BG2, fg="#000000",
                                  font=("Segoe UI", 9, "bold"), anchor="w")
        self._path_lbl.pack(side="left", padx=10, fill="x", expand=True)
        self._count_lbl = tk.Label(path_bar, text="",
                                   bg=BG2, fg=TEXT_DIM,
                                   font=("Segoe UI", 9), anchor="e")
        self._count_lbl.pack(side="right", padx=10)

        # ── Pane 3: selected image — fixed width, not stretching ─────────────
        img_frame = tk.Frame(self._paned, bg=CANVAS_BG, width=600)
        self._paned.add(img_frame, minsize=300, width=600, stretch="never")

        tk.Label(img_frame, text="SELECTED IMAGE", bg=BG2, fg=TEXT_DIM,
                 font=("Segoe UI", 8, "bold")).pack(fill="x", ipady=2)
        self._img_canvas = tk.Canvas(img_frame, bg=CANVAS_BG, highlightthickness=0)
        self._img_canvas.pack(fill="both", expand=True)
        self._img_canvas.bind("<Configure>", lambda e: self._redraw_image())

        # Info bar below image
        info_bar = tk.Frame(img_frame, bg=BG2, height=20)
        info_bar.pack(fill="x", side="bottom")
        info_bar.pack_propagate(False)
        self._img_info = tk.Label(info_bar, text="", bg=BG2, fg=TEXT_DIM,
                                  font=("Segoe UI", 8), anchor="w")
        self._img_info.pack(side="left", padx=6)

        # ── Pane 4: map ───────────────────────────────────────────────────────
        map_frame = tk.Frame(self._paned, bg="#111")
        self._paned.add(map_frame, minsize=400, width=1000, stretch="always")

        tk.Label(map_frame, text="MAP", bg=BG2, fg=TEXT_DIM,
                 font=("Segoe UI", 8, "bold")).pack(fill="x", ipady=2)

        try:
            import tkintermapview as tmv
            self._tmv = tmv

            # Map command bar.  TkinterMapView already provides its own
            # built-in +/- zoom controls, so FTMap must not add a second pair.
            # Keep only FT-specific navigation helpers here.
            zoom_bar = tk.Frame(map_frame, bg="#222")
            zoom_bar.pack(fill="x", side="bottom")
            _nav_btn = dict(bg="#1a6655", fg="white",
                            font=("Segoe UI", 9, "bold"), relief="flat",
                            padx=8, pady=2, cursor="hand2",
                            activebackground="#227766", activeforeground="white")
            tk.Button(zoom_bar, text="Recentre",
                      command=self._recentre,
                      **_nav_btn).pack(side="left", padx=(4, 2), pady=2)
            tk.Button(zoom_bar, text="Zoom All",
                      command=self._zoom_all,
                      **_nav_btn).pack(side="left", padx=(2, 2), pady=2)

            tk.Button(zoom_bar, text="Map Provider",
                      command=self._toggle_map_mode,
                      **_nav_btn).pack(side="left", padx=(2, 2), pady=2)

            tk.Button(zoom_bar, text="Pins On/Off",
                      command=self._toggle_pins,
                      **_nav_btn).pack(side="left", padx=(2, 4), pady=2)

            self.map_widget = tmv.TkinterMapView(map_frame, corner_radius=0)
            self.map_widget.pack(fill="both", expand=True)
            # Default startup map view: whole of Australia.
            # TkinterMapView otherwise defaults to Berlin until a GPS image is selected.
            try:
                self.map_widget.set_position(-25.2744, 133.7751)
                self.map_widget.set_zoom(4)
            except Exception:
                pass
        except ImportError:
            tk.Label(map_frame,
                     text="tkintermapview not installed.\n\nRun:  pip install tkintermapview",
                     bg="#111", fg="white", font=("Segoe UI",13), justify="center"
                     ).pack(expand=True)

        # GPS count label bottom of map
        self._gps_count_lbl = tk.Label(map_frame, text="",
                                       bg="#111", fg="#888",
                                       font=("Segoe UI", 8))
        self._gps_count_lbl.pack(side="bottom", pady=2)

    # ── Folder / file loading ─────────────────────────────────────────────────

    def _browse_root(self):
        """Browse for a root folder to set as tree root."""
        import tkinter.filedialog as fd
        folder = fd.askdirectory(parent=self, title="Select root folder")
        if not folder: return
        self._set_tree_root(folder)

    def _set_tree_root(self, folder):
        """Set folder as tree root, then immediately load that root folder."""
        if not folder or not os.path.isdir(folder): return
        folder = os.path.normpath(folder)
        self._folder_var.set(folder)

        # Clear all state when root changes
        self._file_list   = []
        self._file_idx    = 0
        self._gps_flags   = {}
        self._gps_data    = {}
        self._gps_scan_gen += 1
        self._selected_path = None
        if self._lb:
            self._lb.delete(0, "end")
        self._folder_count.config(text="Select a root folder")

        if self._tree:
            self._tree_widget.set_root(folder)
            try:
                self._tree.selection_set(folder)
                self._tree.focus(folder)
                self._tree.see(folder)
            except Exception:
                pass

        # FolderTreeWidget.set_root() only builds the tree; it does not fire
        # <<TreeviewSelect>>. Load the root explicitly so entering/browsing a
        # root folder actually populates the file list and map immediately.
        self._on_tree_select_path(folder)

    def _populate_tree(self, root_dir):
        """Build tree from root_dir down."""
        if not self._tree: return
        for item in self._tree.get_children(""): self._tree.delete(item)
        root_dir = os.path.normpath(root_dir)
        name = os.path.basename(root_dir) or root_dir
        tag = "has_jpg" if self._folder_has_jpgs(root_dir) else "no_jpg"
        self._tree.insert("", "end", iid=root_dir, text=f"  {name}",
                          open=True, tags=(tag,))
        self._insert_tree_subdirs(root_dir, root_dir)

    def _insert_tree_subdirs(self, parent_iid, path):
        """Insert immediate subdirectories of path under parent_iid."""
        try:
            dirs = sorted([e for e in os.scandir(path) if e.is_dir()],
                          key=lambda e: e.name.lower())
        except PermissionError:
            return
        for d in dirs:
            dp = os.path.normpath(d.path)
            if self._tree.exists(dp): continue
            tag = "has_jpg" if self._folder_has_jpgs(dp) else "no_jpg"
            self._tree.insert(parent_iid, "end", iid=dp,
                              text=f"  {d.name}", tags=(tag,))
            # Add placeholder if has subdirs (for expand arrow)
            if self._folder_has_subdirs(dp):
                self._tree.insert(dp, "end", iid=dp + "/__ph__", text="")

    def _folder_has_jpgs(self, path):
        """Return True if path directly contains any JPG files."""
        try:
            return any(
                os.path.splitext(e.name)[1].lower() in PHOTO_EXTS
                for e in os.scandir(path) if e.is_file()
            )
        except Exception:
            return False

    def _folder_has_subdirs(self, path):
        try:
            return any(e.is_dir() for e in os.scandir(path))
        except Exception:
            return False

    def _do_tree_open(self):
        """Expand tree node — replace placeholder with real subdirs."""
        if not self._tree: return
        def _check(iid):
            if "__ph__" in iid: return
            if not self._tree.item(iid, "open"): return
            ch = self._tree.get_children(iid)
            phs = [c for c in ch if c.endswith("/__ph__")]
            if phs and len(ch) == len(phs):
                for ph in phs: self._tree.delete(ph)
                self._insert_tree_subdirs(iid, iid)
                return
            for child in ch: _check(child)
        for iid in self._tree.get_children(""): _check(iid)

    def _on_tree_select_path(self, path):
        """Called by the shared folder tree when a folder is selected.

        Important: do not do any GPS or recursive counting here. Folder-tree
        expansion/selection must return immediately, especially on large NAS
        folders. GPS scanning is handled later in a background worker after the
        file list has been displayed.
        """
        self._on_tree_select(path=path)

    def _on_tree_select(self, event=None, path=None):
        """User clicked a folder in the tree — load its JPGs into the file list."""
        if not self._tree: return
        if path is None:
            sel = self._tree.selection()
            if not sel: return
            path = sel[0]
        if "__ph__" in path: return
        if not os.path.isdir(path): return
        file_list = _scan_folder(path)
        if not file_list:
            self._folder_count.config(text="No JPGs in this folder")
            self._lb.delete(0, "end")
            self._file_list = []
            return
        self._file_list = file_list
        self._file_idx  = 0
        self._gps_flags = {}
        self._gps_data  = {}
        self._gps_scan_gen += 1
        self._populate_list()
        self._load_image(self._file_list[0])
        # GPS/map scanning is started by _populate_list() in the background.

    def _show_sort_menu(self, btn):
        show_file_sort_menu(btn, [("Name", "name")],
                            self._sort_column, self._sort_reverse, self._set_sort)

    def _set_sort(self, column: str, reverse: bool):
        self._sort_column  = column
        self._sort_reverse = reverse
        try:
            self._sort_btn.config(text=_sort_btn_label(column, reverse))
        except Exception:
            pass
        self._file_list.sort(key=lambda p: os.path.basename(p).lower(), reverse=reverse)
        self._populate_list()

    def _populate_list(self):
        """Populate the file list immediately, then find GPS data in background.

        The old FTMap code opened every JPEG and read EXIF synchronously while
        selecting/expanding folders. On a large NAS folder that makes Tk appear
        to be "Not Responding". This version keeps the UI responsive: it lists
        filenames first, loads the first image, and updates [GPS] prefixes as
        the background scan completes.
        """
        if self._lb is None:
            return
        self._lb.delete(0, "end")

        try:
            from tkinter import font as tkfont
            f = tkfont.Font(family="Segoe UI", size=9)
            gps_w = f.measure("[GPS] ")
            space_w = f.measure("\u2007")
            if space_w > 0:
                n = max(1, round(gps_w / space_w))
                self._no_gps_prefix = "\u2007" * n
            else:
                self._no_gps_prefix = "          "
        except Exception:
            self._no_gps_prefix = "          "

        for path in self._file_list:
            self._gps_flags[path] = False
            self._lb.insert("end", self._no_gps_prefix + os.path.basename(path))

        n = len(self._file_list)
        if self._folder_count:
            self._folder_count.config(text=f"{n} file{'s' if n!=1 else ''}  |  scanning GPS…")
        if self._file_list:
            self._lb_select(self._file_idx)

        self._start_gps_scan_async(list(self._file_list))

    def _start_gps_scan_async(self, files):
        """Scan EXIF GPS data off the Tk thread and update the UI safely."""
        self._gps_scan_gen += 1
        gen = self._gps_scan_gen
        files = list(files or [])

        def worker():
            gps_count = 0
            for idx, path in enumerate(files):
                if gen != self._gps_scan_gen:
                    return
                coords = _get_gps_coords(path)
                if coords is not None:
                    gps_count += 1
                try:
                    self.after(0, lambda i=idx, p=path, c=coords, n=gps_count, g=gen:
                               self._on_gps_scan_item(g, i, p, c, n))
                except Exception:
                    return
            try:
                self.after(0, lambda n=gps_count, g=gen: self._on_gps_scan_done(g, n))
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _on_gps_scan_item(self, gen, idx, path, coords, gps_count):
        if gen != self._gps_scan_gen:
            return
        if idx < 0 or idx >= len(self._file_list):
            return
        if self._file_list[idx] != path:
            return
        has_gps = coords is not None
        self._gps_flags[path] = has_gps
        if has_gps:
            self._gps_data[path] = coords
            prefix = "[GPS] "
        else:
            prefix = getattr(self, "_no_gps_prefix", "          ")
        try:
            self._lb.delete(idx)
            self._lb.insert(idx, prefix + os.path.basename(path))
            if idx == self._file_idx:
                self._lb_select(idx)
        except Exception:
            pass
        if self._folder_count:
            n = len(self._file_list)
            self._folder_count.config(text=f"{n} file{'s' if n!=1 else ''}  |  {gps_count} with GPS")

    def _on_gps_scan_done(self, gen, gps_count):
        if gen != self._gps_scan_gen:
            return
        if self._folder_count:
            n = len(self._file_list)
            self._folder_count.config(text=f"{n} file{'s' if n!=1 else ''}  |  {gps_count} with GPS")
        folder = ""
        try:
            folder = os.path.dirname(self._file_list[0]) if self._file_list else ""
        except Exception:
            folder = ""
        if folder:
            # Preserve the user's current image/marker selection when the
            # asynchronous GPS scan finishes.  Rebuilding the map with no
            # centre path used to silently re-select the first GPS image, which
            # made the image panel appear to stick on an old file.
            center = getattr(self, "_current_path", None)
            self._load_folder_on_map(folder, center)

    def _lb_select(self, idx):
        if self._lb is None: return
        self._lb.unbind("<<ListboxSelect>>")
        self._lb.selection_clear(0, "end")
        self._lb.selection_set(idx)
        self._lb.see(idx)
        self._lb.bind("<<ListboxSelect>>", self._on_list_select)

    def _on_list_select(self, event=None):
        if self._lb is None: return
        sel = self._lb.curselection()
        if not sel: return
        new_idx = sel[0]
        if new_idx == self._file_idx: return
        self._file_idx = new_idx
        path = self._file_list[self._file_idx]
        coords = self._gps_data.get(path) or _get_gps_coords(path)
        self._set_selected(path, coords)

    def _navigate(self, direction):
        if not self._file_list: return
        new_idx = self._file_idx + direction
        if new_idx < 0 or new_idx >= len(self._file_list): return
        self._file_idx = new_idx
        path = self._file_list[self._file_idx]
        coords = self._gps_data.get(path) or _get_gps_coords(path)
        self._set_selected(path, coords)

    def _load_image(self, path):
        """Load a file into the image panel and update labels only.

        Selection/list/map synchronisation is handled by _set_selected().
        """
        self._current_path = path
        self._path_lbl.config(text=path)
        if self._mode == "standalone" and self._file_list:
            self._count_lbl.config(
                text=f"{self._file_idx + 1} of {len(self._file_list)}")
        self.title(f"FTMapB - {os.path.basename(path)}")
        self._show_image(path)

        coords = self._gps_data.get(path) or _get_gps_coords(path)
        if coords:
            self._gps_data[path] = coords
            self._img_info.config(text=f"GPS: {coords[0]:.5f}, {coords[1]:.5f}")
        else:
            self._img_info.config(text="No GPS data")

    def _show_image(self, path):
        """Load and display image in the image panel. No map interaction.

        Image loading happens in background threads.  When the user clicks map
        pins quickly, an older thread can finish after a newer one.  The token
        check prevents that stale image from replacing the current selection.
        """
        self._image_load_token = getattr(self, "_image_load_token", 0) + 1
        token = self._image_load_token

        def _bg():
            try:
                ImageFile.LOAD_TRUNCATED_IMAGES = True
                img = Image.open(_longpath(path))
                img = ImageOps.exif_transpose(img)
                img = img.convert("RGB")
                ImageFile.LOAD_TRUNCATED_IMAGES = False
            except Exception:
                img = None
            self.after(0, lambda: self._set_image(img, path, token))
        threading.Thread(target=_bg, daemon=True).start()

    def _set_image(self, img, path, token=None):
        if token is not None and token != getattr(self, "_image_load_token", None):
            return
        if path != getattr(self, "_current_path", path):
            return
        self._current_img = img
        self._redraw_image()

    def _redraw_image(self, event=None):
        img = getattr(self, '_current_img', None)
        if img is None: return
        c = self._img_canvas
        try: c.update_idletasks()
        except: return
        cw = c.winfo_width(); ch = c.winfo_height()
        if cw < 2 or ch < 2: return
        iw, ih = img.size
        scale = min(cw/iw, ch/ih)
        nw = max(1, int(iw*scale)); nh = max(1, int(ih*scale))
        ox = (cw-nw)//2; oy = (ch-nh)//2
        disp = img.resize((nw,nh), Image.BILINEAR)
        photo = ImageTk.PhotoImage(disp)
        self._img_photo = photo
        self._photo_refs.append(photo)   # prevent GC on 32-bit Python
        if len(self._photo_refs) > 20:   # trim old refs
            self._photo_refs = self._photo_refs[-10:]
            self._photo_refs.extend([self._pin_red, self._pin_green])
        c.delete("all")
        c.create_image(ox, oy, anchor="nw", image=self._img_photo)

    # ── Map logic (ported from _MapWindow) ────────────────────────────────────

    def _clear_map_markers(self):
        """Remove every marker from the map widget and reset marker objects.

        TkinterMapView can leave old marker drawings behind if only our local
        dictionaries are overwritten.  Use the widget-level clear first, then
        defensively delete any marker objects we still know about.
        """
        if not self.map_widget:
            self._all_markers = {}
            return
        try:
            self.map_widget.delete_all_marker()
        except Exception:
            pass
        for mk in list(getattr(self, "_all_markers", {}).values()):
            try:
                mk.delete()
            except Exception:
                pass
        self._all_markers = {}

    def _build_map(self, center_path, center_coords, nearby_data):
        """Initialise map with markers. Called after map widget is ready."""
        if not self.map_widget: return
        self._map_ready     = True
        self._center_path   = center_path
        self._center_coords = center_coords
        self._nearby_data   = nearby_data

        # Fit map to all markers
        all_lats = [center_coords[0]] + [c[0] for _, c, _ in nearby_data]
        all_lons = [center_coords[1]] + [c[1] for _, c, _ in nearby_data]
        if len(all_lats) > 1:
            self.map_widget.fit_bounding_box(
                (max(all_lats)+0.01, min(all_lons)-0.01),
                (min(all_lats)-0.01, max(all_lons)+0.01))
        else:
            self.map_widget.set_position(center_coords[0], center_coords[1])
            self.map_widget.set_zoom(12)

        # Clear previous marker objects before rebuilding.  This must clear the
        # map widget itself as well as FTMap's marker dictionary, otherwise old
        # red selected pins can remain visible after fast clicks/rebuilds.
        self._clear_map_markers()

        # Place markers using stable string keys so old red pins are always found.
        self._marker_coords = {}
        self._marker_km     = {}
        self._selected_path = None

        center_key = self._marker_key(center_path)
        self._marker_coords[center_key] = center_coords
        self._marker_km[center_key]     = 0.0
        m = self.map_widget.set_marker(
            center_coords[0], center_coords[1],
            text="", icon=self._pin_green, icon_anchor="s",
            command=lambda mk, p=center_key, c=center_coords:
                self._set_selected(p, c))
        self._all_markers[center_key] = m

        for p, coords, km in nearby_data:
            p_key = self._marker_key(p)
            self._marker_coords[p_key] = coords
            self._marker_km[p_key]     = km
            mk = self.map_widget.set_marker(
                coords[0], coords[1],
                text="", icon=self._pin_green, icon_anchor="s",
                command=lambda mk, _p=p_key, _c=coords:
                    self._set_selected(_p, _c))
            self._all_markers[p_key] = mk

        # Highlight starting selection
        self._set_selected(center_key, center_coords)

        n_gps = 1 + len(nearby_data)
        self._gps_count_lbl.config(
            text=f"{n_gps} image{'s' if n_gps!=1 else ''} with GPS  |  OpenStreetMap / ESRI Satellite")

    def _marker_key(self, path):
        """Return a stable string key for marker dictionaries."""
        return os.path.normcase(os.path.normpath(str(path)))

    def _set_selected(self, path, coords=None):
        """Single selection path for list, image and map marker updates."""
        key = self._marker_key(path)

        if coords and self._map_ready:
            self._select_marker(key, coords, update_image=False)

        file_lookup = {self._marker_key(p): p for p in self._file_list}
        actual_path = file_lookup.get(key)

        if actual_path:
            try:
                self._file_idx = self._file_list.index(actual_path)
                self._lb_select(self._file_idx)
            except ValueError:
                pass
            self._load_image(actual_path)
        else:
            self._sync_list_to(key)

    def _select_marker(self, path, coords, update_image=True):
        """Make exactly one marker red and all other visible markers green.

        The safest way with tkintermapview is to delete and rebuild the visible
        marker layer on each selection.  This avoids orphaned old red markers
        when marker objects are recreated after map refreshes or quick clicks.
        """
        if not self.map_widget:
            return

        path_key = self._marker_key(path)
        self._selected_path = path_key
        self._marker_coords[path_key] = coords

        # Delete every existing marker object first; otherwise old selected
        # markers can remain visible even after their dictionary entry changes.
        self._clear_map_markers()

        marker_rows = []
        try:
            if getattr(self, "_center_path", None) and getattr(self, "_center_coords", None):
                marker_rows.append((self._marker_key(self._center_path), self._center_coords))
            for p, c, _km in getattr(self, "_nearby_data", []) or []:
                marker_rows.append((self._marker_key(p), c))
        except Exception:
            marker_rows = []

        # Ensure the selected path is present even if it came from the file list
        # before nearby data finished refreshing.
        if not any(k == path_key for k, _c in marker_rows):
            marker_rows.append((path_key, coords))

        # Draw order matters in tkintermapview: later markers are painted on
        # top of earlier markers.  Therefore all normal green markers are drawn
        # first and the single selected red marker is drawn last.  This prevents
        # a nearby/overlapping green marker from covering the red selected pin.
        seen = set()
        selected_row = None
        normal_rows = []
        for key, c in marker_rows:
            if not c or key in seen:
                continue
            seen.add(key)
            if key == path_key:
                selected_row = (key, c)
            else:
                normal_rows.append((key, c))

        def _draw_marker(key, c, selected=False):
            icon = self._pin_red if selected else self._pin_green
            mk = self.map_widget.set_marker(
                c[0], c[1],
                text="", icon=icon, icon_anchor="s",
                command=lambda marker, _p=key, _c=c:
                    self._set_selected(_p, _c))
            self._all_markers[key] = mk
            self._marker_coords[key] = c

        for key, c in normal_rows:
            _draw_marker(key, c, selected=False)

        # Red selected marker is always created last so it stays visually on top.
        if selected_row is not None:
            _draw_marker(selected_row[0], selected_row[1], selected=True)

        if update_image:
            self._set_selected(path_key, coords)

    def _sync_list_to(self, path):
        if self._lb is None: return
        try:
            self._lb.unbind("<<ListboxSelect>>")
            self._lb.selection_clear(0, "end")
            path_key = self._marker_key(path)
            for idx, item_path in enumerate(self._file_list):
                if self._marker_key(item_path) == path_key:
                    self._lb.selection_set(idx)
                    self._lb.see(idx)
                    break
        except Exception: pass
        finally:
            self._lb.bind("<<ListboxSelect>>", self._on_list_select)

    def _recentre(self):
        if not self.map_widget or not self._selected_path: return
        coords = self._marker_coords.get(self._selected_path)
        if not coords: return
        try:
            z = self.map_widget.zoom
            self.map_widget.set_position(coords[0], coords[1])
            self.map_widget.after(50, lambda: self.map_widget.set_zoom(z))
        except Exception: pass

    def _map_zoom(self, delta):
        """Zoom around the current map centre.

        This is kept for compatibility with any older key/menu bindings, but
        the visible bottom +/- buttons were removed because TkinterMapView
        already supplies its own zoom controls.
        """
        if not self.map_widget:
            return
        try:
            self.map_widget.set_zoom(self.map_widget.zoom + delta)
        except Exception:
            pass

    def _zoom_all(self):
        """Fit the map to all currently known GPS markers.

        Use this when the selected file/marker is outside the current map view.
        Recentre keeps the current zoom and centres on the selected marker;
        Zoom All changes the view so every GPS marker in the loaded folder is
        visible again.
        """
        if not self.map_widget:
            return

        coords = []
        seen = set()

        def _add(c):
            if not c:
                return
            try:
                lat = float(c[0]); lon = float(c[1])
            except Exception:
                return
            key = (round(lat, 7), round(lon, 7))
            if key not in seen:
                seen.add(key)
                coords.append((lat, lon))

        # Prefer marker_coords because it matches what is actually drawn now.
        for c in list(getattr(self, "_marker_coords", {}).values()):
            _add(c)

        # Include any GPS data that has been scanned but not currently drawn.
        for c in list(getattr(self, "_gps_data", {}).values()):
            _add(c)

        if not coords:
            return

        try:
            if len(coords) == 1:
                self.map_widget.set_position(coords[0][0], coords[0][1])
                self.map_widget.set_zoom(12)
                return

            lats = [c[0] for c in coords]
            lons = [c[1] for c in coords]
            pad = 0.01
            self.map_widget.fit_bounding_box(
                (max(lats) + pad, min(lons) - pad),
                (min(lats) - pad, max(lons) + pad),
            )
        except Exception:
            pass


    def _toggle_map_mode(self):
        """Cycle map provider: OpenStreetMap -> Google Satellite -> ESRI Satellite."""
        if not self.map_widget:
            return

        try:
            lat, lon = self.map_widget.get_position()
        except Exception:
            lat, lon = (-25.2744, 133.7751)

        try:
            zoom = self.map_widget.zoom
        except Exception:
            zoom = 4

        providers = ["osm", "google_satellite", "esri_satellite"]
        current = getattr(self, "_map_mode", "osm")
        if current == "map":
            current = "osm"
        try:
            next_mode = providers[(providers.index(current) + 1) % len(providers)]
        except ValueError:
            next_mode = "osm"

        try:
            if next_mode == "osm":
                self.map_widget.set_tile_server(
                    "https://a.tile.openstreetmap.org/{z}/{x}/{y}.png"
                )
                self._map_mode = "osm"
                label = "OpenStreetMap"

            elif next_mode == "google_satellite":
                self.map_widget.set_tile_server(
                    "https://mt0.google.com/vt/lyrs=s&hl=en&x={x}&y={y}&z={z}&s=Ga",
                    max_zoom=22
                )
                self._map_mode = "google_satellite"
                label = "Google Satellite"

            else:
                self.map_widget.set_tile_server(
                    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
                    max_zoom=19
                )
                self._map_mode = "esri_satellite"
                label = "ESRI Satellite"

            self.map_widget.set_position(lat, lon)
            self.map_widget.set_zoom(zoom)

            try:
                self._gps_count_lbl.config(text=f"Map provider: {label}")
            except Exception:
                pass

        except Exception as e:
            print("Map provider switch failed:", e)




    def _toggle_pins(self):
        """Toggle map pins visibility without changing loaded GPS data."""
        self._pins_visible = not getattr(self, "_pins_visible", True)

        if not self.map_widget:
            return

        if not self._pins_visible:
            self._clear_map_markers()
            try:
                self._gps_count_lbl.config(text="Pins hidden")
            except Exception:
                pass
            return

        try:
            center_path = getattr(self, "_center_path", None)
            center_coords = getattr(self, "_center_coords", None)
            nearby_data = getattr(self, "_nearby_data", [])
            if center_path and center_coords:
                self._build_map(center_path, center_coords, nearby_data)
        except Exception as e:
            print("Pin toggle failed:", e)


    # ── Load a folder onto the map ────────────────────────────────────────────

    def _load_folder_on_map(self, folder, center_path=None):
        """Build map from GPS data already collected by the background scan."""
        if not self.map_widget:
            return
        import pathlib

        files = [f for f in self._file_list if os.path.dirname(os.path.normpath(f)) == os.path.normpath(folder)]
        if not files:
            files = _scan_folder(folder)

        gps_files = []
        for f in files:
            coords = self._gps_data.get(f)
            if coords:
                gps_files.append((pathlib.Path(f), coords))

        if not gps_files:
            self._gps_count_lbl.config(text="No GPS images found in folder")
            return

        if center_path and os.path.isfile(center_path) and self._gps_data.get(center_path):
            center = (pathlib.Path(center_path), self._gps_data[center_path])
        else:
            center = gps_files[0]

        center_p, center_c = center
        nearby = []
        for pth, coords in gps_files:
            if pth == center_p:
                continue
            km = _haversine_km(center_c[0], center_c[1], coords[0], coords[1])
            nearby.append((pth, coords, km))
        nearby.sort(key=lambda x: x[2])
        self.after(0, lambda: self._build_map(center_p, center_c, nearby))

    # ── IPC (embedded mode) ───────────────────────────────────────────────────

    def _poll_ipc(self):
        try:
            req_path = os.path.join(self._ipc_dir, "FTMap_request.csv")
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
                folder = os.path.dirname(files[0]) if files else ""
                if seq != self._ipc_seq and files:
                    self._ipc_seq = seq
                    os.remove(req_path)
                    self._folder    = folder
                    self._file_list = [f for f in files if os.path.isfile(f)]
                    self._file_idx  = 0
                    self._gps_flags = {}
                    self._gps_data  = {}
                    if self._lb:
                        self._populate_list()
                    start = center if center and os.path.isfile(center) else (self._file_list[0] if self._file_list else None)
                    if start:
                        self._load_image(start)
                    self._load_folder_on_map(folder, center or None)
                    self.deiconify()
                    self.lift()
                    self.attributes("-topmost", True)
                    self.after(100, lambda: self.attributes("-topmost", False))
                    self.focus_force()
        except Exception as e:
            print(f"FTMapB poll error: {e}")
        self.after(500, self._poll_ipc)

    def _on_close(self):
        self.destroy()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]

    if "--embedded" in args:
        app = FTMap(mode="embedded")
        # Delay map init — no folder yet
        app.mainloop()
        return

    if not args:
        # No arguments: default to the active project Photos root when Projects.ini exists.
        # If no project root exists, leave the root box empty for manual entry.
        roots = read_project_roots(__file__)
        photos_root = roots.get("photos", "")
        if photos_root and os.path.isdir(photos_root):
            file_list = _scan_folder(photos_root)
            app = FTMap(mode="standalone",
                        start_path=file_list[0] if file_list else None,
                        file_list=file_list)
            app.after(100, lambda: app._set_tree_root(photos_root))
            if file_list:
                app.after(300, lambda: app._load_folder_on_map(photos_root))
            app.mainloop()
            return

        app = FTMap(mode="standalone", file_list=[])
        app.mainloop()
        return

    target = args[0]

    if os.path.isdir(target):
        file_list = _scan_folder(target)
        app = FTMap(mode="standalone",
                       start_path=file_list[0] if file_list else None,
                       file_list=file_list)
        app.after(100, lambda: app._set_tree_root(target))
        if file_list:
            app.after(300, lambda: app._load_folder_on_map(target))
        app.mainloop()
        return

    if os.path.isfile(target):
        folder = os.path.dirname(target)
        file_list = _scan_folder(folder)
        if target not in file_list:
            file_list.insert(0, target)
        app = FTMap(mode="standalone",
                       start_path=target,
                       file_list=file_list)
        try:
            app._file_idx = file_list.index(target)
        except ValueError:
            pass
        app.after(100, lambda: app._set_tree_root(folder))
        app._lb_select(app._file_idx)
        app.after(300, lambda: app._load_folder_on_map(folder, target))
        app.mainloop()
        return

    messagebox.showerror("FTMapB", f"Path not found:\n{target}")


if __name__ == "__main__":
    main()
