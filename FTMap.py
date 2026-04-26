r"""
FTMap.py  -
Version: 23:40 26-Apr-2026  FileTagger Map Image
--------------------------------------
Standalone GPS map viewer. Same layout as FTEditimg:
  [File list] | [empty ctrl col] | [Selected image] | [Map]

Usage:
  Standalone - folder:  pythonw.exe FTMap.py "S:\Photos\2024"
  Standalone - file:    pythonw.exe FTMap.py "S:\Photos\2024\IMG_001.jpg"
  Embedded:             pythonw.exe FTMap.py --embedded

IPC: <script_dir>\FT_IPC\FTMap_request.csv  {"seq":N,"folder":"...","center":"..."}
"""

import os, sys, math, json, configparser, threading
import tkinter as tk
from FTWidgets import FileCountTree, FolderTreeWidget, TREE_COL_W, TREE_WIDTH, TREE_SCROLL_W, TREE_PAD_R
from tkinter import messagebox

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
    ini = os.path.join(_script_dir(), "FileTagger.ini")
    path = None
    if os.path.exists(ini):
        cfg = configparser.ConfigParser(strict=False)
        cfg.read(ini)
        path = cfg.get("FileTagger", "ipc_folder", fallback="").strip()
    if not path:
        path = os.path.join(_script_dir(), "FT_IPC")
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
# ── FTMapTree — FileCountTree subclass for FTMap ───────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

class FTMapTree(FileCountTree):
    """
    FileCountTree subclass for FTMap.

    Two columns:
        Files — photo files directly in folder
        GPS   — files that have GPS coordinates (via get_gps_count callback)

    Parameters
    ----------
    extensions    : set of str — file extensions e.g. {'.jpg', '.jpeg'}
    get_gps_count : callable(path) -> int — returns count of files with GPS in path
    col_files     : str — heading for files column (default "Files")
    col_gps       : str — heading for GPS column   (default "GPS")

    Usage
    -----
        tree = FTMapTree(
            parent,
            extensions={'.jpg', '.jpeg'},
            get_gps_count=lambda p: my_gps_count(p),
            on_select=my_callback,
            show_root_entry=True,
        )
        tree.pack(fill="y", side="left")
        tree.pack_propagate(False)
        tree.configure(width=tree.actual_width())
    """

    def __init__(self, parent, extensions=None,
                 get_gps_count=None,
                 col_files="Files", col_gps="GPS", **kw):
        self._get_gps_count = get_gps_count or (lambda p: 0)
        kw['columns'] = [
            (col_files, TREE_COL_W, "e"),
            (col_gps,   TREE_COL_W, "e"),
        ]
        self._extensions = {e.lower() for e in (extensions or {'.jpg', '.jpeg'})}
        FolderTreeWidget.__init__(self, parent, **kw)

    def _fill_node(self, path):
        """Fill Files column for a single node — GPS disabled."""
        files = self._count_own(path)
        self.set_col(path, 0, self._fmt(files))
        self.set_col(path, 1, "-")
        if files > 0:
            self._tree.item(path, tags=("has_files",))
        else:
            self._tree.item(path, tags=("empty",))

    def _fill_children_of(self, path):
        """Fill columns for direct children of path."""
        for child in self._tree.get_children(path):
            if self.PLACEHOLDER not in child:
                self._fill_node(child)

    def _populate_root(self, path):
        """Populate root with no counting — fully lazy."""
        super(FileCountTree, self)._populate_root(path)

    def _on_node_open(self, path):
        """Expand node then fill columns for its children."""
        super(FileCountTree, self)._on_node_open(path)
        self._fill_children_of(path)


class FTMap(tk.Tk):

    CTRL_W = 0   # No operation column — set nonzero to restore

    def __init__(self, mode, start_path=None, file_list=None):
        super().__init__()
        self.title("FTMap - FileTagger Map Image")
        self.configure(bg=BG)
        self.minsize(700, 400)

        self._mode       = mode
        self._file_list  = file_list or []
        self._file_idx   = 0
        self._gps_flags  = {}   # path -> bool
        self._gps_data   = {}   # path -> (lat, lon)

        # Map state
        self._tmv           = None
        self.map_widget     = None
        self._map_ready     = False
        self._all_markers   = {}
        self._marker_coords = {}
        self._marker_km     = {}
        self._selected_path = None
        self._pin_red       = None
        self._pin_green     = None
        self._nearby_data   = []

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
            # tree(300) + files(300) + image(600) + map(1000) + sashes
            win_w = min(2220, sw - 40)
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
                # Sash 0: after left_outer (tree+files = 605px)
                self._paned.sash_place(0, 605, 0)
                # Sash 1: after image (605 + 600 = 1205px)
                self._paned.sash_place(1, 1205, 0)
                # Inner sash: tree 300 | files 300
                try:
                    self._left_paned.sash_place(0, 310, 0)  # tree widget actual width at 125% DPI
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
            left_outer = tk.Frame(self._paned, bg=BG2, width=605)
            self._paned.add(left_outer, minsize=400, width=605, stretch="never")

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

            # Folder tree — FTMapTree from FTWidgets
            self._tree_widget = FTMapTree(
                left_paned,
                extensions=PHOTO_EXTS,
                get_gps_count=lambda p: 0,  # GPS disabled — too slow
                on_select=self._on_tree_select_path,
                show_root_entry=False,
                bg=BG2
            )
            left_paned.add(self._tree_widget, minsize=200,
                           width=self._tree_widget.actual_width(), stretch="always")
            self._tree = self._tree_widget.tree()

            # File list inside left_paned
            file_frame = tk.Frame(left_paned, bg=BG2)
            left_paned.add(file_frame, minsize=200, width=300, stretch="never")

        else:
            # Embedded: just a file list pane, no tree or folder browser
            left_outer = tk.Frame(self._paned, bg=BG2, width=300)
            self._paned.add(left_outer, minsize=200, width=300, stretch="never")
            self._folder_var   = None
            self._folder_count = None
            self._tree         = None
            file_frame = left_outer

        # ── File list (both modes) ────────────────────────────────────────────
        tk.Label(file_frame, text="JPG Files", bg=BG2, fg=TEXT_DIM,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=4, pady=(4,0))

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

        # ── Pane 2: operation controls — zero width for FTMapimg ──────────────
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

            # Zoom/recentre bar — packed BEFORE map widget so it sits above it cleanly
            zoom_bar = tk.Frame(map_frame, bg="#222")
            zoom_bar.pack(fill="x", side="bottom")
            _zbtn = dict(bg="#333", fg="white", font=("Segoe UI",12,"bold"),
                         relief="flat", padx=8, pady=2, cursor="hand2",
                         activebackground="#555", activeforeground="white")
            tk.Button(zoom_bar, text="+",
                      command=lambda: self._map_zoom(1),
                      **_zbtn).pack(side="left", padx=(0,1))
            tk.Button(zoom_bar, text="-",
                      command=lambda: self._map_zoom(-1),
                      **_zbtn).pack(side="left")
            tk.Button(zoom_bar, text="Recentre", bg="#1a6655", fg="white",
                      font=("Segoe UI",9,"bold"), relief="flat", padx=8, pady=2,
                      cursor="hand2", activebackground="#227766",
                      command=self._recentre).pack(side="left", padx=(6,0))

            self.map_widget = tmv.TkinterMapView(map_frame, corner_radius=0)
            self.map_widget.pack(fill="both", expand=True)
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
        """Set folder as tree root and populate the tree."""
        if not folder or not os.path.isdir(folder): return
        folder = os.path.normpath(folder)
        self._folder_var.set(folder)
        # Clear all state when root changes
        self._file_list   = []
        self._file_idx    = 0
        self._gps_flags   = {}
        self._gps_data    = {}
        self._selected_path = None
        if self._lb:
            self._lb.delete(0, "end")
        self._folder_count.config(text="Select a root folder")
        if self._tree:
            self._tree_widget.set_root(folder)

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
        """Called by FTMapTree with selected path — also fill GPS column for this node."""
        # Update GPS count for the selected node in the tree
        try:
            gps = _count_gps_in_folder(path)
            self._tree_widget.set_col(path, 1, self._tree_widget._fmt(gps) if hasattr(self._tree_widget, '_fmt') else (str(gps) if gps else '-'))
        except Exception:
            pass
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
        self._populate_list()
        self._load_image(self._file_list[0])
        self.after(200, lambda: self._load_folder_on_map(path))

    def _populate_list(self):
        if self._lb is None: return
        self._lb.delete(0, "end")

        # Calculate space padding to match "[GPS] " width — done once
        try:
            from tkinter import font as tkfont
            f = tkfont.Font(family="Segoe UI", size=9)
            gps_w   = f.measure("[GPS] ")
            space_w = f.measure("\u2007")  # figure space — wider than regular space
            if space_w > 0:
                n = max(1, round(gps_w / space_w))
                no_gps_prefix = "\u2007" * n
            else:
                no_gps_prefix = "          "
        except Exception:
            no_gps_prefix = "          "

        gps_count = 0
        for path in self._file_list:
            coords = _get_gps_coords(path)
            has_gps = coords is not None
            self._gps_flags[path] = has_gps
            if has_gps:
                self._gps_data[path] = coords
                gps_count += 1
            prefix = "[GPS] " if has_gps else no_gps_prefix
            self._lb.insert("end", prefix + os.path.basename(path))
        n = len(self._file_list)
        if self._folder_count:
            self._folder_count.config(
                text=f"{n} file{'s' if n!=1 else ''}  |  {gps_count} with GPS")
        if self._file_list:
            self._lb_select(self._file_idx)

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
        self._load_image(self._file_list[self._file_idx])

    def _navigate(self, direction):
        if not self._file_list: return
        new_idx = self._file_idx + direction
        if new_idx < 0 or new_idx >= len(self._file_list): return
        self._file_idx = new_idx
        self._lb_select(self._file_idx)
        self._load_image(self._file_list[self._file_idx])

    def _load_image(self, path):
        """Called when user selects a file. Updates display AND map marker."""
        self._current_path = path
        self._path_lbl.config(text=path)
        if self._mode == "standalone" and self._file_list:
            self._count_lbl.config(
                text=f"{self._file_idx + 1} of {len(self._file_list)}")
        self.title(f"FTMapimg - {os.path.basename(path)}")
        self._show_image(path)
        # Update map marker — does NOT call back to _load_image
        import pathlib
        p = pathlib.Path(path)
        coords = self._gps_data.get(path) or _get_gps_coords(path)
        if coords:
            self._gps_data[path] = coords
            self._img_info.config(text=f"GPS: {coords[0]:.5f}, {coords[1]:.5f}")
            if self._map_ready:
                self._select_marker(p, coords, update_image=False)
        else:
            self._img_info.config(text="No GPS data")
            if self._map_ready:
                self._sync_list_to(p)

    def _show_image(self, path):
        """Load and display image in the image panel. No map interaction."""
        def _bg():
            try:
                ImageFile.LOAD_TRUNCATED_IMAGES = True
                img = Image.open(_longpath(path))
                img = ImageOps.exif_transpose(img)
                img = img.convert("RGB")
                ImageFile.LOAD_TRUNCATED_IMAGES = False
            except Exception:
                img = None
            self.after(0, lambda: self._set_image(img, path))
        threading.Thread(target=_bg, daemon=True).start()

    def _set_image(self, img, path):
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

    def _build_map(self, center_path, center_coords, nearby_data):
        """Initialise map with markers. Called after map widget is ready."""
        if not self.map_widget: return
        self._map_ready    = True
        self._center_path  = center_path
        self._center_coords = center_coords
        self._nearby_data  = nearby_data

        # Fit map to all markers
        all_lats = [center_coords[0]] + [c[0] for _,c,_ in nearby_data]
        all_lons = [center_coords[1]] + [c[1] for _,c,_ in nearby_data]
        if len(all_lats) > 1:
            self.map_widget.fit_bounding_box(
                (max(all_lats)+0.01, min(all_lons)-0.01),
                (min(all_lats)-0.01, max(all_lons)+0.01))
        else:
            self.map_widget.set_position(center_coords[0], center_coords[1])
            self.map_widget.set_zoom(12)

        # Place markers
        self._all_markers   = {}
        self._marker_coords = {}
        self._marker_km     = {}

        import pathlib
        self._marker_coords[center_path] = center_coords
        self._marker_km[center_path]     = 0.0
        m = self.map_widget.set_marker(
            center_coords[0], center_coords[1],
            text="", icon=self._pin_green, icon_anchor="s",
            command=lambda mk, p=center_path, c=center_coords:
                self._select_marker(p, c))
        self._all_markers[center_path] = m

        for p, coords, km in nearby_data:
            self._marker_coords[p] = coords
            self._marker_km[p]     = km
            mk = self.map_widget.set_marker(
                coords[0], coords[1],
                text="", icon=self._pin_green, icon_anchor="s",
                command=lambda mk, _p=p, _c=coords:
                    self._select_marker(_p, _c))
            self._all_markers[p] = mk

        # Highlight starting selection
        self._select_marker(center_path, center_coords)

        n_gps = 1 + len(nearby_data)
        self._gps_count_lbl.config(
            text=f"{n_gps} image{'s' if n_gps!=1 else ''} with GPS  |  OpenStreetMap")

    def _select_marker(self, path, coords, update_image=True):
        """Make path's marker red (selected), restore previous to green."""
        if not self.map_widget: return

        # Restore old selection to green
        prev = self._selected_path
        if prev and prev in self._all_markers and prev != path:
            prev_coords = self._marker_coords.get(prev)
            try: self._all_markers[prev].delete()
            except: pass
            if prev_coords:
                new_mk = self.map_widget.set_marker(
                    prev_coords[0], prev_coords[1],
                    text="", icon=self._pin_green, icon_anchor="s",
                    command=lambda mk, _p=prev, _c=prev_coords:
                        self._select_marker(_p, _c))
                self._all_markers[prev] = new_mk

        # Make selected marker red and on top
        self._selected_path = path
        if path in self._all_markers:
            try: self._all_markers[path].delete()
            except: pass
        new_mk = self.map_widget.set_marker(
            coords[0], coords[1],
            text="", icon=self._pin_red, icon_anchor="s",
            command=lambda mk, _p=path, _c=coords:
                self._select_marker(_p, _c))
        self._all_markers[path] = new_mk

        self._sync_list_to(path)

        # Update image panel — load folder if needed, then select file
        if update_image:
            str_path = str(path)
            # If file not in current list, load its parent folder first
            if str_path not in self._file_list:
                folder = os.path.dirname(str_path)
                if os.path.isdir(folder):
                    from FTWidgets import _longpath
                    try:
                        import os as _os
                        file_list = [
                            os.path.join(folder, f)
                            for f in os.listdir(folder)
                            if os.path.splitext(f)[1].lower() in PHOTO_EXTS
                        ]
                        file_list.sort()
                        self._file_list = file_list
                        self._file_idx  = 0
                        self._gps_flags = {}
                        self._gps_data  = {}
                        self._populate_list()
                    except Exception:
                        pass
            if str_path in self._file_list:
                try:
                    self._file_idx = self._file_list.index(str_path)
                    self._lb_select(self._file_idx)
                except ValueError:
                    pass
                self._current_path = str_path
                self._path_lbl.config(text=str_path)
                coords2 = self._gps_data.get(str_path, coords)
                self._img_info.config(
                    text=f"GPS: {coords2[0]:.5f}, {coords2[1]:.5f}")
                self._show_image(str_path)

    def _sync_list_to(self, path):
        if self._lb is None: return
        try:
            self._lb.unbind("<<ListboxSelect>>")
            self._lb.selection_clear(0, "end")
            str_path = str(path)
            if str_path in self._file_list:
                idx = self._file_list.index(str_path)
                self._lb.selection_set(idx)
                self._lb.see(idx)
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
        if not self.map_widget: return
        try:
            self.map_widget.set_zoom(self.map_widget.zoom + delta)
        except Exception: pass

    # ── Load a folder onto the map ────────────────────────────────────────────

    def _load_folder_on_map(self, folder, center_path=None):
        """Scan folder for GPS images and build map."""
        if not self.map_widget: return
        import pathlib

        files = _scan_folder(folder)
        # Collect GPS data
        gps_files = []
        for f in files:
            coords = _get_gps_coords(f)
            if coords:
                self._gps_data[f]  = coords
                self._gps_flags[f] = True
                gps_files.append((pathlib.Path(f), coords))
            else:
                self._gps_flags[f] = False

        if not gps_files:
            self._gps_count_lbl.config(text="No GPS images found in folder")
            return

        # Determine centre
        if center_path and os.path.isfile(center_path):
            coords = _get_gps_coords(center_path)
            if coords:
                center = (pathlib.Path(center_path), coords)
            else:
                center = gps_files[0]
        else:
            center = gps_files[0]

        center_p, center_c = center
        nearby = []
        for p, c in gps_files:
            if p == center_p: continue
            km = _haversine_km(center_c[0], center_c[1], c[0], c[1])
            nearby.append((p, c, km))
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
            print(f"FTMapimg poll error: {e}")
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
        # No args — open app with empty list, user clicks ... to browse
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

    messagebox.showerror("FTMapimg", f"Path not found:\n{target}")


if __name__ == "__main__":
    main()
