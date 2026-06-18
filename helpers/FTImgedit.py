r"""
FTImgedit.py  —  FileTagger Image Editor
-------------------------------------
Standalone image editor extracted from FTDBX.
Supports JPG files. Edit tabs: Basic, Crop, Straighten, Transform, Squeeze, FFT Filter, Barrel.

Usage:
  Standalone — single file:
      pythonw.exe FTImgedit.py "S:\Photos\image.jpg"

  Standalone — folder (shows all JPGs in file list):
      pythonw.exe FTImgedit.py "S:\Photos\2024-Holiday"

  Embedded — launched by FTDBX (polls IPC folder for requests):
      pythonw.exe FTImgedit.py --embedded

IPC folder (embedded mode):
  Default: <script_dir>\IPC\
  Override: [FileTagger] ipc_folder = ... in FileTagger.ini alongside this script.

  FTDBX writes:   IPC\FTImgedit_request.csv  {"seq": N, "path": "..."}
  FTEditI writes: IPC\FTImgedit_result.csv   {"seq": N, "outcome": "DISCARDED"|"OVERWRITE"|"SAVED_NEW", "path": "..."}
"""

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import os, sys, math, json, configparser, threading

try:
    from libraries.ft_project_roots import read_project_roots
except Exception:
    def read_project_roots(base_file=None):
        return {"photos": "", "pdfs": "", "project": ""}

try:
    from libraries.ft_widgets import show_file_sort_menu, _sort_btn_label
except ImportError:
    def _sort_btn_label(column="name", reverse=False):  # type: ignore[misc]
        labels = {"name": "Name", "file": "Name"}
        return f"{labels.get(column, column)} {'↓' if reverse else '↑'} ▾"
    show_file_sort_menu = None  # type: ignore[assignment]

import tkinter as tk
from tkinter import messagebox

# ── DPI awareness (Windows) ───────────────────────────────────────────────────
try:
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

# ── PIL imports ───────────────────────────────────────────────────────────────
try:
    from PIL import Image, ImageTk, ImageFile, ImageOps, ImageEnhance
    Image.MAX_IMAGE_PIXELS = None
except ImportError:
    tk.Tk().withdraw()
    messagebox.showerror("Missing library", "Pillow is required.\n\nRun:  pip install Pillow")
    sys.exit(1)

# ── Perspective helper ───────────────────────────────────────────────────────
try:
    from libraries.ft_perspective import perspective_adjust
except Exception:
    perspective_adjust = None

# ── Constants ─────────────────────────────────────────────────────────────────
PHOTO_EXTS = {'.jpg', '.jpeg'}

BG          = "#f0f0f0"   # main background — light grey
BG2         = "#e0e0e0"   # slightly darker — panels, list
BG3         = "#f0f0f0"   # dialogs
TEXT_BRIGHT = "#111111"   # primary text — near black
TEXT_DIM    = "#555555"   # secondary text
TEXT_DARK   = "#111111"   # alias
ACCENT      = "#1a5276"   # active tab — dark blue
GREEN       = "#1e8449"   # save / apply — green
AMBER       = "#a04000"   # overwrite — amber/brown
PURPLE      = "#6c3483"   # undo — purple
NAVY        = "#1a3a5c"   # revert — navy
RED         = "#922b21"   # discard / cancel — red
CANVAS_BG   = "#888888"   # image canvas background — mid grey

# ── Helpers ───────────────────────────────────────────────────────────────────

def _longpath(p):
    """Windows long-path prefix."""
    if os.name == 'nt':
        p = p.replace('/', '\\')
        if not p.startswith('\\\\?\\'):
            return '\\\\?\\' + os.path.abspath(p)
    return p

def _scale_to_fit(img, w, h):
    """Scale PIL image to fit within w×h, preserving aspect ratio."""
    iw, ih = img.size
    if iw == 0 or ih == 0: return img
    scale = min(w / iw, h / ih)
    nw = max(1, int(iw * scale))
    nh = max(1, int(ih * scale))
    if nw == iw and nh == ih: return img
    return img.resize((nw, nh), Image.BILINEAR)

def _script_dir():
    return os.path.dirname(os.path.abspath(__file__))

def _ipc_dir():
    """Return IPC folder path, creating it if needed."""
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

def _scan_folder(folder):
    """Return sorted list of JPG paths in folder."""
    try:
        files = [
            os.path.join(folder, f)
            for f in sorted(os.listdir(folder), key=str.lower)
            if os.path.splitext(f)[1].lower() in PHOTO_EXTS
        ]
        return files
    except Exception:
        return []

def _centre_window(win, w, h):
    win.update_idletasks()
    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()
    win.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")

# ── Main application class ────────────────────────────────────────────────────

class FTImgedit(tk.Tk):
    def __init__(self, mode, start_path=None, file_list=None):
        super().__init__()
        self.title("FTImgedit version 1.1 — FileTagger Edit Image")
        self.configure(bg=BG)
        self.minsize(900, 500)

        # mode: "standalone" or "embedded"
        self._mode       = mode
        self._file_list  = file_list or []   # list of full paths
        self._file_idx   = 0                 # index into _file_list
        self._sort_column  = "name"
        self._sort_reverse = False

        # Edit state — reset on each new image
        self._orig_img   = None   # PIL Image — original, never modified
        self._current    = [None] # current[0] = working PIL Image
        self._history    = []
        self._unsaved    = False

        # IPC state (embedded mode)
        self._ipc_seq    = -1
        self._ipc_dir    = _ipc_dir() if mode == "embedded" else None

        # Image display references (keep alive for tkinter)
        self._working_photo = None
        self._orig_photo    = None

        self._build_ui()

        # Size to 90% screen height, width proportional (capped at screen width)
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        win_h = int(sh * 0.80)
        # tree(300) + files(300) + ctrl(273) + working + original — cap at screen
        win_w = min(int(win_h * 2.5) + 300, sw - 40)
        x = (sw - win_w) // 2
        y = (sh - win_h) // 2
        self.geometry(f"{win_w}x{win_h}+{x}+{y}")
        if self._mode == "standalone":
            self.after(150, self._set_initial_sashes)

        # Load initial image
        if start_path and os.path.isfile(start_path):
            self._load_image(start_path)
        elif self._file_list:
            self._load_image(self._file_list[0])

        # Start IPC polling in embedded mode
        if mode == "embedded":
            self.after(500, self._poll_ipc)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        # Top-level paned window: [tree] | [file list] | [controls] | [working] | [original]
        self._paned = tk.PanedWindow(self, orient="horizontal", bg=BG,
                                     sashwidth=5, sashrelief="flat",
                                     sashpad=2)
        self._paned.pack(fill="both", expand=True)

        # ── Left pane: folder tree + file list (standalone only) ─────────────
        if self._mode == "standalone":
            from tkinter import ttk

            left_outer = tk.Frame(self._paned, bg=BG2, width=605)
            self._paned.add(left_outer, minsize=400, width=605, stretch="never")

            # Root folder entry bar
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

            # Horizontal split: tree | file list
            left_paned = tk.PanedWindow(left_outer, orient="horizontal", bg=BG2,
                                        sashwidth=4, sashrelief="flat")
            left_paned.pack(fill="both", expand=True)
            self._left_paned = left_paned

            # Folder tree
            tree_frame = tk.Frame(left_paned, bg=BG2)
            left_paned.add(tree_frame, minsize=150, width=300, stretch="always")
            tk.Label(tree_frame, text="Folders", bg=BG2, fg=TEXT_DIM,
                     font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=4, pady=(4,0))
            tree_sb = ttk.Scrollbar(tree_frame, orient="vertical")
            style = ttk.Style()
            style.configure("Edit.Treeview", background="white",
                            fieldbackground="white", foreground=TEXT_BRIGHT,
                            font=("Segoe UI", 9))
            self._tree = ttk.Treeview(tree_frame, style="Edit.Treeview",
                                      yscrollcommand=tree_sb.set,
                                      show="tree", selectmode="browse")
            tree_sb.config(command=self._tree.yview)
            self._tree.tag_configure("has_jpg", foreground="#0055cc")
            self._tree.tag_configure("no_jpg",  foreground="#888888")
            tree_sb.pack(side="right", fill="y")
            self._tree.pack(fill="both", expand=True, padx=(4,0), pady=(2,4))
            self._tree.bind("<<TreeviewSelect>>", self._on_tree_select)
            self._tree.bind("<<TreeviewOpen>>",   lambda e: self.after(1, self._do_tree_open))
            self._tree.bind("<MouseWheel>",
                lambda e: self._tree.yview_scroll(-1 if e.delta > 0 else 1, "units"))

            # File list
            file_frame = tk.Frame(left_paned, bg=BG2)
            left_paned.add(file_frame, minsize=150, width=300, stretch="never")

            _fl_hdr = tk.Frame(file_frame, bg=BG2)
            _fl_hdr.pack(fill="x", padx=6, pady=(2, 0))
            tk.Label(_fl_hdr, text="JPG Files", bg=BG2, fg=TEXT_DIM,
                     font=("Segoe UI", 8, "bold")).pack(side="left")
            self._sort_btn = tk.Button(
                _fl_hdr, text=_sort_btn_label(self._sort_column, self._sort_reverse),
                font=("Segoe UI", 8, "bold"), bg=BG2, fg=ACCENT,
                relief="flat", cursor="hand2",
                command=lambda: self._show_sort_menu(self._sort_btn),
            )
            self._sort_btn.pack(side="right", padx=(0, 20))
            sb = tk.Scrollbar(file_frame, orient="vertical")
            self._lb = tk.Listbox(file_frame, bg="white", fg=TEXT_BRIGHT,
                                  selectbackground=ACCENT, selectforeground="white",
                                  font=("Segoe UI", 9), activestyle="none",
                                  yscrollcommand=sb.set, borderwidth=0,
                                  highlightthickness=1,
                                  highlightbackground="#cccccc")
            sb.config(command=self._lb.yview)
            sb.pack(side="right", fill="y")
            self._lb.pack(fill="both", expand=True, padx=(4,0), pady=(2,4))
            self._lb.bind("<<ListboxSelect>>", self._on_list_select)
            self._lb.bind("<MouseWheel>",
                lambda e: self._lb.yview_scroll(-1 if e.delta > 0 else 1, "units"))
            self._lb.bind("<Up>",   lambda e: self._navigate(-1))
            self._lb.bind("<Down>", lambda e: self._navigate(1))
        else:
            self._lb           = None
            self._folder_var   = None
            self._folder_count = None
            self._tree         = None

        # ── Control pane ──────────────────────────────────────────────────────
        ctrl_frame = tk.Frame(self._paned, bg=BG, width=273)
        self._paned.add(ctrl_frame, minsize=230, width=273, stretch="never")
        self._build_ctrl(ctrl_frame)

        # ── Full path title bar ───────────────────────────────────────────────
        path_bar = tk.Frame(self, bg=BG2, height=26)
        path_bar.pack(fill="x", side="top")
        path_bar.pack_propagate(False)
        self._path_lbl = tk.Label(path_bar, text="No file loaded",
                                  bg=BG2, fg="#000000",
                                  font=("Segoe UI", 9, "bold"), anchor="w")
        self._path_lbl.pack(side="left", padx=10, fill="x", expand=True)
        self._count_lbl = tk.Label(path_bar, text="",
                                   bg=BG2, fg=TEXT_DIM,
                                   font=("Segoe UI", 9), anchor="e")
        self._count_lbl.pack(side="right", padx=10)

        # ── Working copy pane ─────────────────────────────────────────────────
        work_frame = tk.Frame(self._paned, bg=CANVAS_BG)
        self._paned.add(work_frame, minsize=200, stretch="always")

        tk.Label(work_frame, text="WORKING COPY", bg=BG2, fg=TEXT_DIM,
                 font=("Segoe UI", 8, "bold")).pack(fill="x", ipady=2)
        self._work_canvas = tk.Canvas(work_frame, bg=CANVAS_BG, highlightthickness=0)
        self._work_canvas.pack(fill="both", expand=True)
        self._work_canvas.bind("<Configure>", lambda e: self._redraw_working())
        self._work_canvas.bind("<ButtonPress-1>",        self._on_press)
        self._work_canvas.bind("<Shift-ButtonPress-1>",  self._on_shift_press)
        self._work_canvas.bind("<B1-Motion>",            self._on_drag)
        self._work_canvas.bind("<ButtonRelease-1>",      self._on_release)
        self._work_canvas.bind("<Motion>",               self._on_motion)

        # _overlay is an alias for _work_canvas — handles/grid drawn on same canvas
        self._overlay  = self._work_canvas
        self._img_off  = [0.0, 0.0]
        self._img_scl  = [1.0]

        # ── Original pane ─────────────────────────────────────────────────────
        orig_frame = tk.Frame(self._paned, bg=CANVAS_BG)
        self._paned.add(orig_frame, minsize=200, stretch="always")

        tk.Label(orig_frame, text="ORIGINAL", bg=BG2, fg=TEXT_DIM,
                 font=("Segoe UI", 8, "bold")).pack(fill="x", ipady=2)
        self._orig_canvas = tk.Canvas(orig_frame, bg=CANVAS_BG, highlightthickness=0)
        self._orig_canvas.pack(fill="both", expand=True)
        self._orig_canvas.bind("<Configure>", lambda e: self._redraw_orig())

        # Activate first tab — must be after _overlay is created
        self._set_tab("Basic")

    def _build_ctrl(self, parent):
        """Control panel: op buttons → fixed action buttons → op controls area."""

        # ── Operation buttons — 2 rows of 3 ──────────────────────────────────
        self._active_tab  = [None]
        self._tab_btns    = {}
        self._tab_frames  = {}

        op_top = tk.Frame(parent, bg=BG2)
        op_top.pack(fill="x")
        row1 = tk.Frame(op_top, bg=BG2); row1.pack(fill="x")
        row2 = tk.Frame(op_top, bg=BG2); row2.pack(fill="x")
        row3 = tk.Frame(op_top, bg=BG2); row3.pack(fill="x")
        rows = [row1, row1, row1, row2, row2, row2, row3, row3]

        for i, name in enumerate(("Basic", "Crop", "Straighten",
                                   "Transform", "Perspective", "Squeeze",
                                   "FFT Filter", "Barrel")):
            b = tk.Button(rows[i], text=name, bg=BG2, fg=TEXT_BRIGHT,
                          font=("Segoe UI", 9, "bold"), relief="flat",
                          padx=4, pady=4, cursor="hand2",
                          command=lambda n=name: self._set_tab(n))
            b.pack(side="left", fill="x", expand=True, padx=1, pady=1)
            self._tab_btns[name] = b

        # ── Separator ─────────────────────────────────────────────────────────
        tk.Frame(parent, bg="#aaaaaa", height=1).pack(fill="x", pady=(4,2))

        # ── Fixed action buttons ──────────────────────────────────────────────
        act = tk.Frame(parent, bg=BG)
        act.pack(fill="x", padx=6, pady=2)

        r1 = tk.Frame(act, bg=BG); r1.pack(fill="x", pady=1)
        self._btn_undo = tk.Button(r1, text="Undo", bg=PURPLE, fg="white",
                                   disabledforeground="#ccaacc",
                                   font=("Segoe UI",8,"bold"), relief="flat",
                                   pady=3, cursor="hand2", state="disabled",
                                   command=self._do_undo)
        self._btn_revert = tk.Button(r1, text="Revert", bg=NAVY, fg="white",
                                     disabledforeground="#aabbcc",
                                     font=("Segoe UI",8,"bold"), relief="flat",
                                     pady=3, cursor="hand2", state="disabled",
                                     command=self._do_revert)
        self._btn_undo.pack(  side="left", fill="x", expand=True, padx=(0,1))
        self._btn_revert.pack(side="left", fill="x", expand=True, padx=(1,0))

        r2 = tk.Frame(act, bg=BG); r2.pack(fill="x", pady=1)
        self._btn_save = tk.Button(r2, text="Save...", bg=GREEN, fg="white",
                                   disabledforeground="#aaccaa",
                                   font=("Segoe UI",8,"bold"), relief="flat",
                                   pady=3, cursor="hand2", state="disabled",
                                   command=self._do_save)
        self._btn_discard = tk.Button(r2, text="Discard", bg=RED, fg="white",
                                      font=("Segoe UI",8,"bold"), relief="flat",
                                      pady=3, cursor="hand2",
                                      command=self._do_discard)
        self._btn_save.pack(   side="left", fill="x", expand=True, padx=(0,1))
        self._btn_discard.pack(side="left", fill="x", expand=True, padx=(1,0))

        if self._mode == "standalone":
            r3 = tk.Frame(act, bg=BG); r3.pack(fill="x", pady=1)
            tk.Button(r3, text="Prev", bg=BG2, fg=TEXT_BRIGHT,
                      font=("Segoe UI",8,"bold"), relief="flat",
                      pady=3, cursor="hand2",
                      command=lambda: self._navigate(-1)).pack(
                          side="left", fill="x", expand=True, padx=(0,1))
            tk.Button(r3, text="Next", bg=BG2, fg=TEXT_BRIGHT,
                      font=("Segoe UI",8,"bold"), relief="flat",
                      pady=3, cursor="hand2",
                      command=lambda: self._navigate(1)).pack(
                          side="left", fill="x", expand=True, padx=(1,0))

        # ── Separator ─────────────────────────────────────────────────────────
        tk.Frame(parent, bg="#aaaaaa", height=1).pack(fill="x", padx=6, pady=(4,2))

        # ── Operation controls area — only active op shown ────────────────────
        self._op_area = tk.Frame(parent, bg=BG)
        self._op_area.pack(fill="x", padx=6, pady=4)

        self._tab_frames["Basic"]      = self._build_basic_tab(self._op_area)
        self._tab_frames["Crop"]       = self._build_crop_tab(self._op_area)
        self._tab_frames["Straighten"] = self._build_straighten_tab(self._op_area)
        self._tab_frames["Transform"]  = self._build_transform_tab(self._op_area)
        self._tab_frames["Perspective"] = self._build_perspective_tab(self._op_area)
        self._tab_frames["Squeeze"]    = self._build_squeeze_tab(self._op_area)
        self._tab_frames["FFT Filter"] = self._build_fft_tab(self._op_area)
        self._tab_frames["Barrel"]     = self._build_barrel_tab(self._op_area)

    # ── Slider keyboard helpers ───────────────────────────────────────────────

    def _bind_slider_keys(self, slider, var, *, step=1.0, big_step=None, callback=None):
        """Allow precise slider changes with keyboard arrows.

        Hover or click a slider, then:
            Left/Right = small nudge
            Shift+Left/Shift+Right = larger nudge
        """
        if big_step is None:
            big_step = step * 10

        def _limits():
            try:
                lo = float(slider.cget("from"))
                hi = float(slider.cget("to"))
            except Exception:
                lo, hi = -100.0, 100.0
            return min(lo, hi), max(lo, hi)

        def _nudge(delta):
            try:
                cur = float(var.get())
            except Exception:
                cur = 0.0
            lo, hi = _limits()
            val = max(lo, min(hi, cur + delta))
            try:
                if abs(step - round(step)) < 1e-9:
                    val = int(round(val))
                var.set(val)
            except Exception:
                pass
            if callback:
                try:
                    callback(val)
                except TypeError:
                    callback(str(val))
            return "break"

        slider.configure(takefocus=1)
        slider.bind("<Enter>", lambda e: slider.focus_set(), add="+")
        slider.bind("<Motion>", lambda e: slider.focus_set(), add="+")
        slider.bind("<ButtonPress-1>", lambda e: slider.focus_set(), add="+")
        slider.bind("<Left>", lambda e: _nudge(-step))
        slider.bind("<Right>", lambda e: _nudge(step))
        slider.bind("<Shift-Left>", lambda e: _nudge(-big_step))
        slider.bind("<Shift-Right>", lambda e: _nudge(big_step))
        return slider


    # ── Tab builders ──────────────────────────────────────────────────────────

    def _build_basic_tab(self, parent):
        f = tk.Frame(parent, bg=BG)
        tk.Label(f, text="Colour / tone adjustments preview live.\nClick Apply to commit.",
                 bg=BG, fg=TEXT_DIM, font=("Segoe UI", 8),
                 justify="left").pack(anchor="w", pady=(2,4))

        self._adj_vars = {}
        self._adj_labels = {}

        def _add_slider(key, label, frm, to, res, value, fmt="{:+.0f}"):
            row = tk.Frame(f, bg=BG); row.pack(fill="x", pady=(1,0))
            tk.Label(row, text=label + ":", bg=BG, fg=TEXT_DIM,
                     font=("Segoe UI", 8)).pack(side="left")
            var = tk.DoubleVar(value=value)
            self._adj_vars[key] = var
            val_lbl = tk.Label(row, text=fmt.format(value), bg=BG, fg=TEXT_BRIGHT,
                               font=("Segoe UI", 8, "bold"), width=6, anchor="e")
            val_lbl.pack(side="right")
            self._adj_labels[key] = (val_lbl, fmt)
            def _on_change(v, k=key):
                try:
                    val = float(v)
                    lbl, ff = self._adj_labels[k]
                    lbl.config(text=ff.format(val))
                except Exception:
                    pass
                self._redraw_working()
            sl = tk.Scale(f, from_=frm, to=to, resolution=res, orient="horizontal",
                          variable=var, bg=BG, fg=TEXT_BRIGHT, troughcolor="#bbbbbb",
                          highlightthickness=0, showvalue=False, command=_on_change)
            sl.pack(fill="x")
            self._bind_slider_keys(sl, var, step=res, big_step=res * 10, callback=_on_change)

        _add_slider("brightness", "Brightness", -100, 100, 1, 0)
        _add_slider("contrast",   "Contrast",   -100, 100, 1, 0)
        _add_slider("saturation", "Saturation", -100, 100, 1, 0)
        _add_slider("sharpness",  "Sharpness",  -100, 100, 1, 0)
        _add_slider("gamma",      "Gamma",      0.50, 2.50, 0.05, 1.00, "{:.2f}")
        _add_slider("red",        "Red",        -50, 50, 1, 0)
        _add_slider("green",      "Green",      -50, 50, 1, 0)
        _add_slider("blue",       "Blue",       -50, 50, 1, 0)

        tk.Frame(f, bg="#cccccc", height=1).pack(fill="x", pady=(8,4))
        tk.Button(f, text="Auto Contrast", bg=NAVY, fg="white",
                  font=("Segoe UI",9,"bold"), relief="flat", padx=10, pady=3,
                  cursor="hand2", command=self._adjust_auto_contrast).pack(fill="x", pady=1)
        tk.Button(f, text="Reset Basic", bg=BG2, fg=TEXT_BRIGHT,
                  font=("Segoe UI",9,"bold"), relief="flat", padx=10, pady=3,
                  cursor="hand2", command=self._adjust_reset).pack(fill="x", pady=1)
        tk.Button(f, text="Apply Basic", bg=ACCENT, fg="white",
                  font=("Segoe UI",9,"bold"), relief="flat", padx=10, pady=4,
                  cursor="hand2", command=self._adjust_apply).pack(fill="x", pady=(4,1))
        return f

    def _build_crop_tab(self, parent):
        f = tk.Frame(parent, bg=BG)

        tk.Label(f, text="Draw a crop marquee on the image.\n"
                         "Drag handles to resize.",
                 bg=BG, fg=TEXT_DIM, font=("Segoe UI",8),
                 justify="left").pack(anchor="w", pady=(2,4))

        ar_row = tk.Frame(f, bg=BG); ar_row.pack(fill="x", pady=(2,0))
        tk.Label(ar_row, text="Aspect:", bg=BG, fg=TEXT_DIM,
                 font=("Segoe UI",9)).pack(side="left", padx=(0,6))
        self._ar_var = tk.StringVar(value="Free")
        ar_menu = tk.OptionMenu(ar_row, self._ar_var, "Free","1:1","3:2","4:3","16:9",
                              command=lambda _v: self._on_crop_aspect_changed())
        ar_menu.config(bg=BG2, fg=TEXT_BRIGHT, font=("Segoe UI",9),
                       relief="flat", highlightthickness=0,
                       activebackground=ACCENT, activeforeground="white")
        ar_menu["menu"].config(bg=BG2, fg=TEXT_BRIGHT, font=("Segoe UI",9))
        ar_menu.pack(side="left")
        self._ar_orient = tk.StringVar(value="Landscape")
        self._btn_orient = tk.Button(ar_row, text="Landscape", bg=NAVY, fg="white",
                                     font=("Segoe UI",9), relief="flat", padx=6,
                                     cursor="hand2", command=self._tog_orient)
        self._btn_orient.pack(side="left", padx=4)

        tk.Button(f, text="Show Full Marquee", bg=BG2, fg=TEXT_BRIGHT,
                  font=("Segoe UI",9), relief="flat", padx=6, pady=2,
                  cursor="hand2",
                  command=self._show_crop_marquee).pack(fill="x", pady=(6,1))
        tk.Button(f, text="Clear Marquee", bg=BG2, fg=TEXT_BRIGHT,
                  font=("Segoe UI",9), relief="flat", padx=6, pady=2,
                  cursor="hand2",
                  command=self._clear_crop_marquee).pack(fill="x", pady=1)

        tk.Frame(f, bg="#cccccc", height=1).pack(fill="x", pady=(6,4))
        tk.Button(f, text="Apply Crop", bg=ACCENT, fg="white",
                  font=("Segoe UI",9,"bold"), relief="flat", padx=10, pady=4,
                  cursor="hand2", command=self._apply_crop).pack(fill="x")

        self._CS_MOVE_R     = 10
        self._cs_crop_rect   = [None]
        self._cs_drag        = [None]  # None or dict: {'mode': 'new'|'move', ...}
        self._cs_handle_idx  = [None]
        return f

    def _build_straighten_tab(self, parent):
        f = tk.Frame(parent, bg=BG)

        tk.Label(f, text="Adjust angle to straighten horizon\nor correct camera tilt.",
                 bg=BG, fg=TEXT_DIM, font=("Segoe UI",8),
                 justify="left").pack(anchor="w", pady=(2,4))

        self._angle_var = tk.DoubleVar(value=0.0)

        def _on_angle(v):
            self._angle_lbl.config(text=f"{float(v):+.1f}deg")
            self._redraw_working()

        sl_row = tk.Frame(f, bg=BG); sl_row.pack(fill="x", pady=(2,0))
        tk.Label(sl_row, text="Angle:", bg=BG, fg=TEXT_DIM,
                 font=("Segoe UI",9)).pack(side="left")
        self._angle_lbl = tk.Label(sl_row, text="0.0deg", bg=BG, fg=TEXT_BRIGHT,
                                   font=("Segoe UI",9,"bold"), width=7)
        self._angle_lbl.pack(side="right")
        sl = tk.Scale(f, from_=-15, to=15, resolution=0.1, orient="horizontal",
                      variable=self._angle_var, bg=BG, fg=TEXT_BRIGHT, troughcolor="#bbbbbb",
                      highlightthickness=0, showvalue=False,
                      command=_on_angle)
        sl.pack(fill="x")
        self._bind_slider_keys(sl, self._angle_var, step=0.1, big_step=1.0, callback=_on_angle)
        hint = tk.Frame(f, bg=BG); hint.pack(fill="x")
        tk.Label(hint, text="CCW", bg=BG, fg=TEXT_DIM,
                 font=("Segoe UI",8)).pack(side="left")
        tk.Label(hint, text="CW", bg=BG, fg=TEXT_DIM,
                 font=("Segoe UI",8)).pack(side="right")

        tk.Frame(f, bg="#cccccc", height=1).pack(fill="x", pady=(8,4))
        tk.Button(f, text="Apply Straighten", bg=ACCENT, fg="white",
                  font=("Segoe UI",9,"bold"), relief="flat", padx=10, pady=4,
                  cursor="hand2", command=self._apply_straighten).pack(fill="x")
        return f

    def _build_transform_tab(self, parent):
        f = tk.Frame(parent, bg=BG)
        tk.Label(f, text="Drag the 4 corner handles to the corners\n"
                          "of the subject that should be rectangular.",
                 bg=BG, fg=TEXT_DIM, font=("Segoe UI",8),
                 justify="left").pack(anchor="w", padx=4, pady=(4,2))

        self._INSET      = 0.05
        self._HANDLE_R   = 9
        self._HANDLE_COL = "#00ffcc"
        self._GRID_N     = 6
        self._tf_handles  = self._default_tf_handles()
        self._tf_drag_idx = [None]

        tk.Button(f, text="Apply Transform", bg=ACCENT, fg="white",
                  font=("Segoe UI",9,"bold"), relief="flat", padx=10, pady=3,
                  cursor="hand2", command=self._apply_transform).pack(pady=(4,2), fill="x")
        return f

    def _build_perspective_tab(self, parent):
        f = tk.Frame(parent, bg=BG)
        tk.Label(f, text="Correct perspective / keystone distortion.\n"
                         "This is separate from 4-point Transform.",
                 bg=BG, fg=TEXT_DIM, font=("Segoe UI",8),
                 justify="left", wraplength=230).pack(anchor="w", padx=4, pady=(4,2))

        self._persp_axis_var = tk.StringVar(value="Vertical")
        axis_row = tk.Frame(f, bg=BG)
        axis_row.pack(fill="x", pady=(4,0))
        tk.Label(axis_row, text="Axis:", bg=BG, fg=TEXT_DIM,
                 font=("Segoe UI",9)).pack(side="left", padx=(0,6))
        tk.Radiobutton(axis_row, text="Vertical", variable=self._persp_axis_var,
                       value="Vertical", bg=BG, fg=TEXT_BRIGHT,
                       selectcolor=BG2, font=("Segoe UI",8),
                       command=self._redraw_working).pack(side="left")
        tk.Radiobutton(axis_row, text="Horizontal", variable=self._persp_axis_var,
                       value="Horizontal", bg=BG, fg=TEXT_BRIGHT,
                       selectcolor=BG2, font=("Segoe UI",8),
                       command=self._redraw_working).pack(side="left")

        self._persp_var = tk.DoubleVar(value=0.0)

        def _on_persp(v):
            try:
                val = float(v)
            except Exception:
                val = 0.0
            self._persp_lbl.config(text=f"{val:+.0f}")
            self._redraw_working()

        row = tk.Frame(f, bg=BG)
        row.pack(fill="x", pady=(6,0))
        tk.Label(row, text="Amount:", bg=BG, fg=TEXT_DIM,
                 font=("Segoe UI",9)).pack(side="left")
        self._persp_lbl = tk.Label(row, text="+0", bg=BG, fg=TEXT_BRIGHT,
                                   font=("Segoe UI",9,"bold"), width=5)
        self._persp_lbl.pack(side="right")

        sl = tk.Scale(f, from_=-100, to=100, resolution=1, orient="horizontal",
                      variable=self._persp_var, bg=BG, fg=TEXT_BRIGHT,
                      troughcolor="#bbbbbb", highlightthickness=0, showvalue=False,
                      command=_on_persp)
        sl.pack(fill="x")
        self._bind_slider_keys(sl, self._persp_var, step=1, big_step=10, callback=_on_persp)

        hint = tk.Frame(f, bg=BG)
        hint.pack(fill="x")
        self._persp_left_hint = tk.Label(hint, text="Top inward", bg=BG, fg=TEXT_DIM,
                                         font=("Segoe UI",8))
        self._persp_left_hint.pack(side="left")
        self._persp_right_hint = tk.Label(hint, text="Top outward", bg=BG, fg=TEXT_DIM,
                                          font=("Segoe UI",8))
        self._persp_right_hint.pack(side="right")

        def _axis_hint_update(*_):
            if self._persp_axis_var.get() == "Horizontal":
                self._persp_left_hint.config(text="Left inward")
                self._persp_right_hint.config(text="Left outward")
            else:
                self._persp_left_hint.config(text="Top inward")
                self._persp_right_hint.config(text="Top outward")
        self._persp_axis_var.trace_add("write", _axis_hint_update)
        _axis_hint_update()

        tk.Button(f, text="Reset Perspective", bg=BG2, fg=TEXT_BRIGHT,
                  font=("Segoe UI",9,"bold"), relief="flat", padx=10, pady=3,
                  cursor="hand2", command=self._perspective_reset).pack(fill="x", pady=(8,1))
        tk.Button(f, text="Apply Perspective", bg=ACCENT, fg="white",
                  font=("Segoe UI",9,"bold"), relief="flat", padx=10, pady=4,
                  cursor="hand2", command=self._apply_perspective).pack(fill="x", pady=(3,1))
        return f


    def _build_squeeze_tab(self, parent):
        f = tk.Frame(parent, bg=BG)
        tk.Label(f, text="Squeeze both sides inward to correct\n"
                          "wide-angle edge distortion.",
                 bg=BG, fg=TEXT_DIM, font=("Segoe UI",8),
                 justify="left", wraplength=220).pack(anchor="w", padx=4, pady=(4,2))

        self._sq_var = tk.DoubleVar(value=0.0)

        def _on_sq(v):
            self._sq_lbl.config(text=f"{int(float(v))}%")
            self._redraw_working()

        row = tk.Frame(f, bg=BG); row.pack(fill="x", pady=(4,0))
        tk.Label(row, text="Squeeze:", bg=BG, fg=TEXT_DIM,
                 font=("Segoe UI",9)).pack(side="left")
        self._sq_lbl = tk.Label(row, text="0%", bg=BG, fg=TEXT_BRIGHT,
                                font=("Segoe UI",9,"bold"), width=4)
        self._sq_lbl.pack(side="right")
        sl = tk.Scale(f, from_=0, to=100, resolution=1, orient="horizontal",
                      variable=self._sq_var, bg=BG, fg=TEXT_BRIGHT, troughcolor="#bbbbbb",
                      highlightthickness=0, showvalue=False,
                      command=_on_sq)
        sl.pack(fill="x")
        self._bind_slider_keys(sl, self._sq_var, step=1, big_step=10, callback=_on_sq)
        tk.Button(f, text="Apply Squeeze", bg=ACCENT, fg="white",
                  font=("Segoe UI",9,"bold"), relief="flat", padx=10, pady=3,
                  cursor="hand2", command=self._apply_squeeze).pack(pady=(8,2), fill="x")
        return f

    def _build_fft_tab(self, parent):
        f = tk.Frame(parent, bg=BG)
        tk.Label(f, text="Compute spectrum, edit patches, preview,\n"
                          "return to dots, then Apply when satisfied.",
                 bg=BG, fg=TEXT_DIM, font=("Segoe UI",8),
                 justify="left").pack(anchor="w", padx=4, pady=(4,2))

        ctrl_row = tk.Frame(f, bg=BG); ctrl_row.pack(fill="x", padx=4)
        tk.Label(ctrl_row, text="Radius:", bg=BG, fg=TEXT_DIM,
                 font=("Segoe UI",9)).pack(side="left")
        self._fft_radius_var = tk.IntVar(value=12)
        self._fft_radius_lbl = tk.Label(ctrl_row, text="12", bg=BG, fg=TEXT_BRIGHT,
                                        font=("Segoe UI",9,"bold"), width=3)
        self._fft_radius_lbl.pack(side="left", padx=(2,0))
        def _on_fft_radius(v):
            self._fft_radius_lbl.config(text=str(int(float(v))))
        sl = tk.Scale(ctrl_row, from_=2, to=40, resolution=1, orient="horizontal",
                      variable=self._fft_radius_var, bg=BG, fg=TEXT_BRIGHT, troughcolor="#bbbbbb",
                      highlightthickness=0, length=120, showvalue=False,
                      command=_on_fft_radius)
        sl.pack(side="left", padx=4)
        self._bind_slider_keys(sl, self._fft_radius_var, step=1, big_step=5, callback=_on_fft_radius)

        mode_row = tk.Frame(f, bg=BG); mode_row.pack(fill="x", padx=4, pady=(2,0))
        tk.Label(mode_row, text="Click mode:", bg=BG, fg=TEXT_DIM,
                 font=("Segoe UI",8)).pack(side="left")
        self._fft_mode_var = tk.StringVar(value="Add")
        tk.Radiobutton(mode_row, text="Add patch", variable=self._fft_mode_var, value="Add",
                       bg=BG, fg=TEXT_BRIGHT, selectcolor=BG2,
                       font=("Segoe UI",8), command=self._fft_update_status).pack(side="left")
        tk.Radiobutton(mode_row, text="Remove patch", variable=self._fft_mode_var, value="Remove",
                       bg=BG, fg=TEXT_BRIGHT, selectcolor=BG2,
                       font=("Segoe UI",8), command=self._fft_update_status).pack(side="left")
        tk.Radiobutton(mode_row, text="Replace", variable=self._fft_mode_var, value="Replace",
                       bg=BG, fg=TEXT_BRIGHT, selectcolor=BG2,
                       font=("Segoe UI",8), command=self._fft_update_status).pack(side="left")

        btn_row = tk.Frame(f, bg=BG); btn_row.pack(fill="x", padx=4, pady=2)
        tk.Button(btn_row, text="Compute Spectrum", bg=ACCENT, fg="white",
                  font=("Segoe UI",9,"bold"), relief="flat", padx=6, cursor="hand2",
                  command=self._fft_compute).pack(fill="x", pady=1)
        tk.Button(btn_row, text="Back to Edit Dots", bg=BG2, fg=TEXT_BRIGHT,
                  font=("Segoe UI",9,"bold"), relief="flat", padx=6, cursor="hand2",
                  command=self._fft_show_spectrum).pack(fill="x", pady=1)
        tk.Button(btn_row, text="Undo Patch", bg=PURPLE, fg="white",
                  font=("Segoe UI",9,"bold"), relief="flat", padx=6, cursor="hand2",
                  command=self._fft_undo_notch).pack(fill="x", pady=1)
        tk.Button(btn_row, text="Clear Patches", bg=NAVY, fg="white",
                  font=("Segoe UI",9,"bold"), relief="flat", padx=6, cursor="hand2",
                  command=self._fft_clear_patches).pack(fill="x", pady=1)
        tk.Button(btn_row, text="Clear Spectrum", bg=BG2, fg=TEXT_BRIGHT,
                  font=("Segoe UI",9,"bold"), relief="flat", padx=6, cursor="hand2",
                  command=self._fft_clear).pack(fill="x", pady=1)
        tk.Button(btn_row, text="Preview Filter", bg=ACCENT, fg="white",
                  font=("Segoe UI",9,"bold"), relief="flat", padx=6, cursor="hand2",
                  command=self._fft_preview).pack(fill="x", pady=1)
        tk.Button(btn_row, text="Apply Filter", bg=GREEN, fg="white",
                  font=("Segoe UI",9,"bold"), relief="flat", padx=6, cursor="hand2",
                  command=self._fft_apply).pack(fill="x", pady=1)

        self._fft_status = tk.Label(f, text="Click Compute Spectrum to begin",
                                    bg=BG, fg="#666", font=("Segoe UI",8))
        self._fft_status.pack(anchor="w", padx=4, pady=(0,2))

        # FFT state
        self._fft_notches    = []
        self._fft_spec_photo = [None]
        self._fft_F_cache    = [None]
        self._fft_img_shape  = [None]
        self._fft_disp       = {"sz": 300, "ox": 0, "oy": 0}
        self._FFT_MAX_PX     = 1200
        self._fft_click_bound = [False]
        self._fft_preview_img = [None]
        self._fft_preview_mode = False
        self._fft_line_anchor = None   # (sx, sy) awaiting shift-click line end

        return f

    def _build_barrel_tab(self, parent):
        f = tk.Frame(parent, bg=BG)

        tk.Label(f, text="Correct barrel or pincushion lens distortion.\n"
                         "Negative = barrel (bulge), Positive = pincushion.",
                 bg=BG, fg=TEXT_DIM, font=("Segoe UI",8),
                 justify="left").pack(anchor="w", pady=(2,4))

        self._barrel_var = tk.DoubleVar(value=0.0)

        def _on_barrel(v):
            self._barrel_lbl.config(text=f"{float(v):+.1f}")
            self._redraw_working()

        sl_row = tk.Frame(f, bg=BG); sl_row.pack(fill="x", pady=(2,0))
        tk.Label(sl_row, text="Amount:", bg=BG, fg=TEXT_DIM,
                 font=("Segoe UI",9)).pack(side="left")
        self._barrel_lbl = tk.Label(sl_row, text="0.0", bg=BG, fg=TEXT_BRIGHT,
                                    font=("Segoe UI",9,"bold"), width=5)
        self._barrel_lbl.pack(side="right")
        sl = tk.Scale(f, from_=-5, to=5, resolution=0.5, orient="horizontal",
                      variable=self._barrel_var, bg=BG, fg=TEXT_BRIGHT, troughcolor="#bbbbbb",
                      highlightthickness=0, showvalue=False,
                      command=_on_barrel)
        sl.pack(fill="x")
        self._bind_slider_keys(sl, self._barrel_var, step=0.5, big_step=2.5, callback=_on_barrel)
        hint = tk.Frame(f, bg=BG); hint.pack(fill="x")
        tk.Label(hint, text="Barrel", bg=BG, fg=TEXT_DIM,
                 font=("Segoe UI",8)).pack(side="left")
        tk.Label(hint, text="Pincushion", bg=BG, fg=TEXT_DIM,
                 font=("Segoe UI",8)).pack(side="right")

        # Axis selector
        axis_row = tk.Frame(f, bg=BG); axis_row.pack(fill="x", pady=(6,0))
        tk.Label(axis_row, text="Axis:", bg=BG, fg=TEXT_DIM,
                 font=("Segoe UI",9)).pack(side="left", padx=(0,6))
        self._barrel_axis = tk.StringVar(value="Both")
        ax_menu = tk.OptionMenu(axis_row, self._barrel_axis,
                                "Both", "Horizontal", "Vertical",
                                command=lambda _: self._redraw_working())
        ax_menu.config(bg=BG2, fg=TEXT_BRIGHT, font=("Segoe UI",9),
                       relief="flat", highlightthickness=0,
                       activebackground=ACCENT, activeforeground="white")
        ax_menu["menu"].config(bg=BG2, fg=TEXT_BRIGHT, font=("Segoe UI",9))
        ax_menu.pack(side="left")

        tk.Frame(f, bg="#cccccc", height=1).pack(fill="x", pady=(8,4))
        tk.Button(f, text="Apply Barrel", bg=ACCENT, fg="white",
                  font=("Segoe UI",9,"bold"), relief="flat", padx=10, pady=4,
                  cursor="hand2", command=self._apply_barrel).pack(fill="x")
        return f

    # ── Tab switching ─────────────────────────────────────────────────────────

    def _set_tab(self, name):
        prev = self._active_tab[0]
        self._active_tab[0] = name

        for n, b in self._tab_btns.items():
            b.config(bg=ACCENT if n == name else BG2,
                     fg="white" if n == name else TEXT_BRIGHT,
                     relief="flat")

        # Cleanup leaving old tab
        if prev == "Transform" and name != "Transform":
            self._overlay.delete("tf_grid", "tf_quad", "tf_handle", "tf_label")
        if prev == "Perspective" and name != "Perspective":
            self._overlay.delete("persp_grid")
        if prev == "Crop" and name != "Crop":
            self._cs_crop_rect[0] = None
            self._overlay.delete("cs_grid", "cs_rect")

        for n, fr in self._tab_frames.items():
            fr.pack_forget()

        if name == "FFT Filter":
            self._fft_bind()
            self._tab_frames[name].pack(fill="x")
            self._redraw_working()
        else:
            self._fft_unbind()
            self._tab_frames[name].pack(fill="x")
            if name == "Transform":
                self.after(50, self._reset_tf_handles)
            elif name == "Crop":
                self._show_crop_marquee()
            self._redraw_working()
            # Second redraw after layout settles — ensures grid visible immediately
            self.after(80, self._redraw_working)

    # ── Image loading ─────────────────────────────────────────────────────────

    def _load_image(self, path):
        """Load a new image. Resets all edit state."""
        try:
            ImageFile.LOAD_TRUNCATED_IMAGES = True
            img = Image.open(_longpath(path))
            img.load()
            try:
                img = ImageOps.exif_transpose(img)
            except Exception:
                pass
            img = img.convert("RGB")
            ImageFile.LOAD_TRUNCATED_IMAGES = False
        except Exception as e:
            ImageFile.LOAD_TRUNCATED_IMAGES = False
            messagebox.showerror("Cannot open", str(e), parent=self)
            return

        self._current_path = path
        self._orig_img     = img
        self._current      = [img.copy()]
        self._history      = []
        self._unsaved      = False
        self._tf_handles   = self._default_tf_handles()
        self._cs_crop_rect = [None]
        self._clear_live_previews()
        self._angle_var.set(0.0)
        self._angle_lbl.config(text="0.0deg")
        self._sq_var.set(0.0)
        self._sq_lbl.config(text="0%")
        try:
            self._persp_var.set(0.0)
            self._persp_lbl.config(text="+0")
            self._persp_axis_var.set("Vertical")
        except Exception:
            pass
        self._fft_clear_state()
        self._adjust_reset(redraw=False)
        self._angle_var.set(0.0)
        self._angle_lbl.config(text="0.0deg")

        self._update_btns()
        self._update_status()
        self.title(f"FTEDITI — {os.path.basename(path)}")
        self.after(50, self._redraw_all)

    def _reset_edit_state(self):
        self._history  = []
        self._unsaved  = False
        self._update_btns()

    # ── Drawing ───────────────────────────────────────────────────────────────

    def _redraw_all(self):
        self._redraw_working()
        self._redraw_orig()

    def _redraw_working(self, event=None):
        if self._current[0] is None: return
        c = self._work_canvas
        try: c.update_idletasks()
        except: return
        cw = c.winfo_width(); ch = c.winfo_height()
        if cw < 2 or ch < 2: return

        img = self._current[0]
        tab = self._active_tab[0]
        if tab == "Basic":
            img = self._adjust_preview_image()
        elif tab == "Perspective":
            img = self._perspective_preview_image()
        elif tab == "FFT Filter" and getattr(self, '_fft_preview_mode', False) and getattr(self, '_fft_preview_img', [None])[0] is not None:
            img = self._fft_preview_img[0]
        iw, ih = img.size
        scale = min(cw / iw, ch / ih)
        self._img_scl[0] = scale
        ox = (cw - iw * scale) / 2
        oy = (ch - ih * scale) / 2
        self._img_off[0] = ox
        self._img_off[1] = oy

        disp = _scale_to_fit(img, cw, ch)
        self._working_photo = ImageTk.PhotoImage(disp)
        c.delete("all")
        c.create_image(ox, oy, anchor="nw", image=self._working_photo, tags="work_img")

        # Draw overlay for current tab on same canvas, above image
        tab = self._active_tab[0]
        if tab == "Crop":
            self._draw_cs_overlay()
        elif tab == "Straighten":
            self._draw_straighten_overlay()
        elif tab == "Transform":
            self._draw_tf_overlay()
        elif tab == "Perspective":
            self._draw_perspective_overlay()
        elif tab == "Squeeze":
            self._draw_squeeze_overlay()
        elif tab == "Barrel":
            self._draw_barrel_overlay()
        elif tab == "FFT Filter" and self._fft_spec_photo[0] and not getattr(self, '_fft_preview_mode', False):
            self._fft_redraw_spec()

    def _redraw_orig(self, event=None):
        if self._orig_img is None: return
        c = self._orig_canvas
        try: c.update_idletasks()
        except: return
        cw = c.winfo_width(); ch = c.winfo_height()
        if cw < 2 or ch < 2: return
        disp = _scale_to_fit(self._orig_img, cw, ch)
        iw, ih = self._orig_img.size
        scale = min(cw / iw, ch / ih)
        ox = (cw - iw * scale) / 2
        oy = (ch - ih * scale) / 2
        self._orig_photo = ImageTk.PhotoImage(disp)
        c.delete("all")
        c.create_image(ox, oy, anchor="nw", image=self._orig_photo)

    # ── Image coord helpers ───────────────────────────────────────────────────

    def _i2c(self, ix, iy):
        s = self._img_scl[0]
        return ix * s + self._img_off[0], iy * s + self._img_off[1]

    def _c2i(self, cx, cy):
        s = self._img_scl[0]
        return (cx - self._img_off[0]) / s, (cy - self._img_off[1]) / s

    # ── Basic adjustment tab logic ───────────────────────────────────────────────────────

    def _adjust_factor(self, key):
        """Map -100..+100 slider values to Pillow enhancement factors."""
        try:
            v = float(self._adj_vars[key].get())
        except Exception:
            return 1.0
        if key in ("brightness", "contrast", "saturation", "sharpness"):
            return max(0.0, 1.0 + (v / 100.0))
        return 1.0

    def _adjust_preview_image(self):
        """Return adjusted preview image without changing history/current image."""
        img = self._current[0]
        if img is None:
            return img
        try:
            out = img.copy().convert("RGB")
            out = ImageEnhance.Brightness(out).enhance(self._adjust_factor("brightness"))
            out = ImageEnhance.Contrast(out).enhance(self._adjust_factor("contrast"))
            out = ImageEnhance.Color(out).enhance(self._adjust_factor("saturation"))
            out = ImageEnhance.Sharpness(out).enhance(self._adjust_factor("sharpness"))

            gamma = float(self._adj_vars.get("gamma", tk.DoubleVar(value=1.0)).get())
            if abs(gamma - 1.0) > 0.001:
                inv = 1.0 / max(0.05, gamma)
                lut = [max(0, min(255, int(((i / 255.0) ** inv) * 255.0 + 0.5))) for i in range(256)]
                out = out.point(lut * 3)

            rfac = 1.0 + float(self._adj_vars.get("red", tk.DoubleVar(value=0)).get()) / 100.0
            gfac = 1.0 + float(self._adj_vars.get("green", tk.DoubleVar(value=0)).get()) / 100.0
            bfac = 1.0 + float(self._adj_vars.get("blue", tk.DoubleVar(value=0)).get()) / 100.0
            if abs(rfac-1.0) > 0.001 or abs(gfac-1.0) > 0.001 or abs(bfac-1.0) > 0.001:
                r, g, b = out.split()
                r = r.point(lambda i: max(0, min(255, int(i * rfac))))
                g = g.point(lambda i: max(0, min(255, int(i * gfac))))
                b = b.point(lambda i: max(0, min(255, int(i * bfac))))
                out = Image.merge("RGB", (r, g, b))
            return out
        except Exception as e:
            print(f"Basic preview error: {e}")
            return img

    def _adjust_reset(self, redraw=True):
        defaults = {
            "brightness": 0, "contrast": 0, "saturation": 0, "sharpness": 0,
            "gamma": 1.0, "red": 0, "green": 0, "blue": 0,
        }
        if not hasattr(self, '_adj_vars'):
            return
        for key, value in defaults.items():
            try:
                self._adj_vars[key].set(value)
                lbl, fmt = self._adj_labels[key]
                lbl.config(text=fmt.format(value))
            except Exception:
                pass
        if redraw:
            self._redraw_working()

    def _adjust_auto_contrast(self):
        if self._current[0] is None: return
        self._push(ImageOps.autocontrast(self._current[0].convert("RGB")))
        self._adjust_reset(redraw=False)
        self._redraw_working()
        self._redraw_orig()

    def _adjust_apply(self):
        if self._current[0] is None: return
        adjusted = self._adjust_preview_image()
        if adjusted is None: return
        self._push(adjusted)
        self._adjust_reset(redraw=False)

    # ── Perspective tab logic ─────────────────────────────────────────────────

    def _perspective_axis(self):
        try:
            axis = self._persp_axis_var.get()
        except Exception:
            axis = "Vertical"
        return "horizontal" if axis == "Horizontal" else "vertical"

    def _perspective_amount(self):
        try:
            return float(self._persp_var.get()) / 100.0
        except Exception:
            return 0.0

    def _perspective_preview_image(self):
        img = self._current[0]
        if img is None:
            return img
        amount = self._perspective_amount()
        if abs(amount) < 0.001:
            return img
        if perspective_adjust is None:
            return img
        try:
            return perspective_adjust(img, amount, axis=self._perspective_axis(), keep_size=True, border=(128, 128, 128))
        except Exception as e:
            print(f"Perspective preview error: {e}")
            return img

    def _perspective_reset(self):
        try:
            self._persp_var.set(0.0)
            self._persp_lbl.config(text="+0")
        except Exception:
            pass
        self._redraw_working()

    def _apply_perspective(self):
        if self._current[0] is None:
            return
        amount = self._perspective_amount()
        if abs(amount) < 0.001:
            return
        if perspective_adjust is None:
            messagebox.showerror(
                "Perspective failed",
                "Perspective helper is not available.\n\n"
                "Required file/libraries:\n"
                "  ft_perspective.py\n"
                "  opencv-python",
                parent=self,
            )
            return
        try:
            out = perspective_adjust(self._current[0], amount, axis=self._perspective_axis(), keep_size=False, border=(128, 128, 128))
        except Exception as e:
            messagebox.showerror("Perspective failed", str(e), parent=self)
            return

        # Clear the live slider BEFORE pushing, because _push redraws immediately.
        self._clear_live_previews()
        self._push(out)

    def _clear_live_previews(self):
        """Clear non-committed live preview controls.

        Perspective preview is generated on redraw from the current slider value.
        If the slider remains non-zero, Undo/Revert can appear not to work
        because the preview is immediately re-applied visually.
        """
        try:
            self._persp_var.set(0.0)
            self._persp_lbl.config(text="+0")
        except Exception:
            pass

    # ── History ───────────────────────────────────────────────────────────────

    def _push(self, new_img):
        self._history.append(self._current[0].copy())
        self._current[0] = new_img
        self._unsaved = True
        self._update_btns()
        self._redraw_working()
        self.after(80, self._redraw_working)

    def _do_undo(self):
        if not self._history: return
        self._clear_live_previews()
        self._current[0] = self._history.pop()
        self._unsaved = bool(self._history)
        self._update_btns()
        self._redraw_working()

    def _do_revert(self):
        if self._orig_img is None: return
        self._clear_live_previews()
        self._history.clear()
        self._current[0] = self._orig_img.copy()
        self._unsaved = False
        self._update_btns()
        self._redraw_working()

    def _update_btns(self):
        has = bool(self._history)
        st  = "normal" if has else "disabled"
        self._btn_undo.config(state=st)
        self._btn_revert.config(state=st)
        self._btn_save.config(state="normal" if has else "disabled")

    # ── Save / Discard ────────────────────────────────────────────────────────

    def _do_save(self):
        if self._current[0] is None: return
        path = self._current_path

        dlg = tk.Toplevel(self)
        dlg.title("Save Image")
        dlg.configure(bg=BG3)
        dlg.resizable(False, False)
        dlg.grab_set()
        dlg.transient(self)
        _centre_window(dlg, 380, 160)

        tk.Label(dlg, text="Save edited image as:",
                 bg=BG3, fg=TEXT_DARK,
                 font=("Segoe UI", 10, "bold")).pack(pady=(16,8))
        bf = tk.Frame(dlg, bg=BG3); bf.pack(pady=4)

        def _save_overwrite():
            dlg.destroy()
            try:
                self._current[0].save(_longpath(path), quality=92, optimize=True)
            except Exception as ex:
                messagebox.showerror("Save failed", str(ex), parent=self); return
            self._history.clear()
            self._unsaved = False          # clear BEFORE writing result
            self._orig_img = self._current[0].copy()
            self._update_btns()
            self._redraw_orig()
            self._write_result("OVERWRITE", path)
            self._path_lbl.config(text=f"Saved: {os.path.basename(path)}")

        def _save_new():
            dlg.destroy()
            base, ext = os.path.splitext(path)
            save_path = base + "_ed" + ext
            n = 1
            while os.path.exists(_longpath(save_path)):
                save_path = base + f"_ed{n}" + ext; n += 1
            try:
                self._current[0].save(_longpath(save_path), quality=92, optimize=True)
            except Exception as ex:
                messagebox.showerror("Save failed", str(ex), parent=self); return
            # Stay on saved version — reset history, update orig to saved
            self._history.clear()
            self._unsaved = False          # clear BEFORE writing result
            self._orig_img = self._current[0].copy()
            self._current_path = save_path
            self.title(f"FTEDITI — {os.path.basename(save_path)}")
            self._update_btns()
            self._redraw_orig()
            self._write_result("SAVED_NEW", save_path)
            self._path_lbl.config(text=save_path)

        tk.Button(bf, text="Overwrite original", bg=AMBER, fg="white",
                  font=("Segoe UI",9,"bold"), relief="flat", padx=10, pady=4,
                  cursor="hand2", command=_save_overwrite).pack(side="left", padx=6)
        tk.Button(bf, text="Save as new file", bg=GREEN, fg="white",
                  font=("Segoe UI",9,"bold"), relief="flat", padx=10, pady=4,
                  cursor="hand2", command=_save_new).pack(side="left", padx=6)
        tk.Button(bf, text="Cancel", bg=BG2, fg=TEXT_BRIGHT,
                  font=("Segoe UI",9), relief="flat", padx=8, pady=4,
                  cursor="hand2", command=dlg.destroy).pack(side="left", padx=6)

    def _do_discard(self):
        self._clear_live_previews()
        if self._unsaved:
            if not messagebox.askyesno("Discard edits",
                    f"Discard all edits to {os.path.basename(self._current_path)}?",
                    parent=self):
                return
        self._do_revert()
        self._write_result("DISCARDED", self._current_path)
        self._path_lbl.config(text="Edits discarded.")

    def _write_result(self, outcome, path):
        """Write IPC result file (embedded mode only)."""
        if self._mode != "embedded": return
        try:
            rpath = os.path.join(self._ipc_dir, "FTImgedit_result.csv")
            with open(rpath, "w", encoding="utf-8") as f:
                f.write(f"SEQ,{self._ipc_seq}\n")
                f.write(f"OUTCOME,{outcome}\n")
                f.write(f"{path}\n")
        except Exception as e:
            print(f"FTImgedit: could not write result: {e}")

    # ── Navigation (standalone) ───────────────────────────────────────────────

    def _navigate(self, direction):
        if not self._file_list: return
        if self._unsaved:
            resp = messagebox.askyesnocancel(
                "Unsaved edits",
                f"Save edits to {os.path.basename(self._current_path)} before moving on?",
                parent=self)
            if resp is None: return      # Cancel
            if resp:                     # Yes — open save dialog, then navigate
                self._do_save()
                if self._unsaved: return # save was cancelled
        new_idx = self._file_idx + direction
        if new_idx < 0 or new_idx >= len(self._file_list): return
        self._file_idx = new_idx
        self._lb_select(self._file_idx)
        self._load_image(self._file_list[self._file_idx])

    def _on_list_select(self, event=None):
        if self._lb is None: return
        sel = self._lb.curselection()
        if not sel: return
        new_idx = sel[0]
        if new_idx == self._file_idx: return
        if self._unsaved:
            resp = messagebox.askyesnocancel(
                "Unsaved edits",
                f"Save edits to {os.path.basename(self._current_path)} before moving on?",
                parent=self)
            if resp is None:
                self._lb_select(self._file_idx); return
            if resp:
                self._do_save()
                if self._unsaved:
                    self._lb_select(self._file_idx); return
        self._file_idx = new_idx
        self._load_image(self._file_list[self._file_idx])

    def _lb_select(self, idx):
        """Highlight listbox row without firing <<ListboxSelect>>."""
        if self._lb is None: return
        self._lb.unbind("<<ListboxSelect>>")
        self._lb.selection_clear(0, "end")
        self._lb.selection_set(idx)
        self._lb.see(idx)
        self._lb.bind("<<ListboxSelect>>", self._on_list_select)

    def _set_initial_sashes(self):
        """Position sashes: tree(300) | files(300) | ctrl(273) | working | original."""
        try:
            self.update_idletasks()
            self._paned.sash_place(0, 605, 0)   # after left_outer
            self._paned.sash_place(1, 605 + 273, 0)  # after ctrl
            try: self._left_paned.sash_place(0, 300, 0)
            except Exception: pass
        except Exception: pass

    def _browse_root(self):
        import tkinter.filedialog as fd
        folder = fd.askdirectory(parent=self, title="Select root folder")
        if not folder: return
        self._set_tree_root(folder)

    def _set_tree_root(self, folder):
        if not folder or not os.path.isdir(folder): return
        folder = os.path.normpath(folder)
        self._folder_var.set(folder)
        if self._tree:
            self._populate_tree(folder)

    def _populate_tree(self, root_dir):
        if not self._tree: return
        for item in self._tree.get_children(""): self._tree.delete(item)
        root_dir = os.path.normpath(root_dir)
        name = os.path.basename(root_dir) or root_dir
        tag = "has_jpg" if self._folder_has_jpgs(root_dir) else "no_jpg"
        self._tree.insert("", "end", iid=root_dir, text=f"  {name}",
                          open=True, tags=(tag,))
        self._insert_tree_subdirs(root_dir, root_dir)

    def _insert_tree_subdirs(self, parent_iid, path):
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
            if self._folder_has_subdirs(dp):
                self._tree.insert(dp, "end", iid=dp + "/__ph__", text="")

    def _folder_has_jpgs(self, path):
        try:
            return any(
                os.path.splitext(e.name)[1].lower() in {'.jpg', '.jpeg'}
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

    def _on_tree_select(self, event=None):
        if not self._tree: return
        sel = self._tree.selection()
        if not sel: return
        path = sel[0]
        if "__ph__" in path: return
        if not os.path.isdir(path): return
        self._folder_count.config(text="Scanning...")
        self._lb.delete(0, "end")
        self._file_list = []
        import threading
        threading.Thread(target=self._load_folder_bg, args=(path,), daemon=True).start()

    def _load_folder_bg(self, path):
        """Background thread — scan folder and populate list without blocking UI."""
        file_list = _scan_folder(path)
        self.after(0, lambda: self._on_folder_loaded(path, file_list))

    def _on_folder_loaded(self, path, file_list):
        """Called on main thread once folder scan is complete."""
        if not file_list:
            self._folder_count.config(text="No JPGs in this folder")
            self._lb.delete(0, "end")
            self._file_list = []
            return
        self._file_list = file_list
        self._file_idx  = 0
        n = len(file_list)
        self._folder_count.config(text=f"{n} file{'s' if n!=1 else ''}")
        self._populate_list()
        self._load_image(self._file_list[0])

    def _show_sort_menu(self, btn):
        if show_file_sort_menu is None:
            return
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
        if self._lb is None: return
        self._lb.delete(0, "end")
        for path in self._file_list:
            self._lb.insert("end", os.path.basename(path))
        if self._file_list:
            self._lb_select(self._file_idx)

    def _populate_list_fast(self):
        self._populate_list()

    def _scan_gps_background(self, *a): pass
    def _update_gps_prefix(self, *a): pass

    def _update_status(self):
        if not hasattr(self, '_current_path'): return
        self._path_lbl.config(text=self._current_path)
        if self._mode == "standalone" and self._file_list:
            self._count_lbl.config(text=f"{self._file_idx + 1} of {len(self._file_list)}")
        else:
            self._count_lbl.config(text="")

    # ── IPC polling (embedded mode) ───────────────────────────────────────────

    def _poll_ipc(self):
        try:
            req_path = os.path.join(self._ipc_dir, "FTImgedit_request.csv")
            if os.path.exists(req_path):
                with open(req_path, encoding="utf-8") as f:
                    lines = [l.strip() for l in f if l.strip()]
                seq   = int(lines[0].split(",", 1)[1]) if lines and lines[0].startswith("SEQ,") else 0
                center = ""
                files  = []
                for line in lines[1:]:
                    if line.startswith("CENTER,"):
                        center = line.split(",", 1)[1]
                    else:
                        files.append(line)
                # First valid file to open
                path = center if center and os.path.isfile(center) else (files[0] if files else "")
                if seq != self._ipc_seq and path and os.path.isfile(path):
                    self._ipc_seq = seq
                    os.remove(req_path)
                    def _load():
                        # Update file list with all files from request
                        valid = [f for f in files if os.path.isfile(f)]
                        if valid:
                            self._file_list = valid
                            self._file_idx  = valid.index(path) if path in valid else 0
                            if self._lb is not None:
                                self._populate_list()
                        self._load_image(path)
                        self.deiconify()
                        self.lift()
                        self.attributes("-topmost", True)
                        self.after(100, lambda: self.attributes("-topmost", False))
                        self.focus_force()
                    if self._unsaved:
                        resp = messagebox.askyesnocancel(
                            "Unsaved edits",
                            f"New files requested. Save edits to "
                            f"{os.path.basename(self._current_path)} first?",
                            parent=self)
                        if resp is None:
                            pass   # ignore request
                        elif resp:
                            self._do_save(); _load()
                        else:
                            self._do_revert(); _load()
                    else:
                        _load()
        except Exception as e:
            print(f"FTImgedit poll error: {e}")
        self.after(500, self._poll_ipc)

    # ── Window close ─────────────────────────────────────────────────────────

    def _on_close(self):
        if self._unsaved:
            resp = messagebox.askyesnocancel(
                "Unsaved edits",
                f"Save edits to {os.path.basename(self._current_path)} before closing?",
                parent=self)
            if resp is None: return
            if resp:
                self._do_save()
                if self._unsaved: return
            else:
                self._write_result("DISCARDED", self._current_path)
        self.destroy()

    # ── Crop / Straighten tab logic ───────────────────────────────────────────

    def _on_crop_aspect_changed(self):
        """When aspect changes, reshape the existing marquee immediately."""
        if self._active_tab[0] == "Crop" and self._cs_crop_rect[0] is not None:
            self._cs_crop_rect[0] = self._cs_force_aspect_from_centre(self._cs_crop_rect[0])
            self._redraw_working()

    def _tog_orient(self):
        new = "Portrait" if self._ar_orient.get() == "Landscape" else "Landscape"
        self._ar_orient.set(new)
        self._btn_orient.config(text=new)
        if self._active_tab[0] == "Crop" and self._cs_crop_rect[0] is not None:
            self._cs_crop_rect[0] = self._cs_force_aspect_from_centre(self._cs_crop_rect[0])
            self._redraw_working()

    def _cs_aspect_ratio(self):
        """Return selected crop aspect ratio as width/height, or None for Free."""
        try:
            val = self._ar_var.get()
        except Exception:
            return None
        if not val or val == "Free" or ":" not in val:
            return None
        try:
            w, h = [float(x) for x in val.split(":", 1)]
            if w <= 0 or h <= 0:
                return None
            ratio = w / h
            if self._ar_orient.get() == "Portrait" and ratio != 1.0:
                ratio = 1.0 / ratio
            return ratio
        except Exception:
            return None

    def _cs_bounds(self):
        if self._current[0] is None:
            return 0.0, 0.0
        iw, ih = self._current[0].size
        return float(iw), float(ih)

    def _cs_norm_rect(self, r):
        x0, y0, x1, y1 = r
        return [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]

    def _cs_clamp_rect(self, r):
        """Shift rectangle back inside the image without changing its size."""
        iw, ih = self._cs_bounds()
        x0, y0, x1, y1 = self._cs_norm_rect(r)
        w = max(1.0, x1 - x0)
        h = max(1.0, y1 - y0)
        if w > iw:
            x0, x1 = 0.0, iw
        else:
            if x0 < 0:
                x1 -= x0; x0 = 0.0
            if x1 > iw:
                x0 -= (x1 - iw); x1 = iw
        if h > ih:
            y0, y1 = 0.0, ih
        else:
            if y0 < 0:
                y1 -= y0; y0 = 0.0
            if y1 > ih:
                y0 -= (y1 - ih); y1 = ih
        return [max(0.0, x0), max(0.0, y0), min(iw, x1), min(ih, y1)]

    def _cs_rect_contains_point(self, r, ix, iy):
        if not r:
            return False
        x0, y0, x1, y1 = self._cs_norm_rect(r)
        return x0 <= ix <= x1 and y0 <= iy <= y1

    def _cs_move_handle_hit(self, r, ix, iy):
        """Return True only when pointer is on the centre move handle."""
        if not r:
            return False
        x0, y0, x1, y1 = self._cs_norm_rect(r)
        cx = (x0 + x1) / 2.0
        cy = (y0 + y1) / 2.0
        rr = getattr(self, "_CS_MOVE_R", 10)
        return (ix - cx) ** 2 + (iy - cy) ** 2 <= rr ** 2

    def _cs_move_handle_hit_canvas(self, r, cx_canvas, cy_canvas):
        """Return True when pointer is on the centre move handle.

        The centre handle is drawn in canvas pixels, so this hit-test also
        uses canvas pixels. This prevents a centre-handle click from being
        misread as a new crop marquee when the image is zoomed.
        """
        if not r:
            return False
        x0, y0, x1, y1 = self._cs_norm_rect(r)
        hx, hy = self._i2c((x0 + x1) / 2.0, (y0 + y1) / 2.0)
        rr = getattr(self, "_CS_MOVE_R", 10) + 4
        return (cx_canvas - hx) ** 2 + (cy_canvas - hy) ** 2 <= rr ** 2

    def _cs_move_rect(self, r, dx, dy):
        x0, y0, x1, y1 = self._cs_norm_rect(r)
        return self._cs_clamp_rect([x0 + dx, y0 + dy, x1 + dx, y1 + dy])

    def _cs_rect_from_anchor(self, ax, ay, px, py):
        """Create a new rect from anchor to pointer, enforcing selected aspect ratio."""
        iw, ih = self._cs_bounds()
        px = max(0.0, min(px, iw)); py = max(0.0, min(py, ih))
        ratio = self._cs_aspect_ratio()
        if not ratio:
            return self._cs_clamp_rect([ax, ay, px, py])

        sx = 1.0 if px >= ax else -1.0
        sy = 1.0 if py >= ay else -1.0
        w = abs(px - ax)
        h = abs(py - ay)
        if w < 1 and h < 1:
            return [ax, ay, ax, ay]
        # Fit the largest selected-ratio rectangle inside the user's drag box.
        if h <= 0 or (w / max(h, 1e-9)) > ratio:
            w = h * ratio
        else:
            h = w / ratio
        x1 = ax + sx * w
        y1 = ay + sy * h
        return self._cs_clamp_rect([ax, ay, x1, y1])

    def _cs_force_aspect_from_centre(self, r):
        """Adjust an existing rect to the selected aspect ratio around its centre."""
        ratio = self._cs_aspect_ratio()
        if not ratio:
            return self._cs_clamp_rect(r)
        iw, ih = self._cs_bounds()
        x0, y0, x1, y1 = self._cs_norm_rect(r)
        cx = (x0 + x1) / 2.0
        cy = (y0 + y1) / 2.0
        w = max(1.0, x1 - x0)
        h = max(1.0, y1 - y0)
        if w / h > ratio:
            w = h * ratio
        else:
            h = w / ratio
        w = min(w, iw)
        h = min(h, ih)
        return self._cs_clamp_rect([cx - w/2, cy - h/2, cx + w/2, cy + h/2])

    def _cs_resize_rect_by_handle(self, r, handle_idx, ix, iy):
        """Resize crop rect using a handle. Fixed ratios are enforced when selected."""
        ratio = self._cs_aspect_ratio()
        iw, ih = self._cs_bounds()
        lx, ty, rx, by = self._cs_norm_rect(r)
        ix = max(0.0, min(ix, iw)); iy = max(0.0, min(iy, ih))

        if not ratio:
            mx0, my0, mx1, my1 = self._CS_HANDLE_AXES[handle_idx]
            if mx0: lx = ix
            if my0: ty = iy
            if mx1: rx = ix
            if my1: by = iy
            return self._cs_clamp_rect([lx, ty, rx, by])

        cx = (lx + rx) / 2.0
        cy = (ty + by) / 2.0

        # Corner handles: opposite corner is fixed.
        if handle_idx == 0:   # top-left, anchor bottom-right
            return self._cs_rect_from_anchor(rx, by, ix, iy)
        if handle_idx == 2:   # top-right, anchor bottom-left
            return self._cs_rect_from_anchor(lx, by, ix, iy)
        if handle_idx == 4:   # bottom-right, anchor top-left
            return self._cs_rect_from_anchor(lx, ty, ix, iy)
        if handle_idx == 6:   # bottom-left, anchor top-right
            return self._cs_rect_from_anchor(rx, ty, ix, iy)

        # Side handles: opposite side remains fixed; the perpendicular dimension
        # expands/contracts around the rectangle centre.
        if handle_idx == 1:   # top edge
            h = max(1.0, by - iy)
            w = h * ratio
            return self._cs_clamp_rect([cx - w/2, by - h, cx + w/2, by])
        if handle_idx == 5:   # bottom edge
            h = max(1.0, iy - ty)
            w = h * ratio
            return self._cs_clamp_rect([cx - w/2, ty, cx + w/2, ty + h])
        if handle_idx == 3:   # right edge
            w = max(1.0, ix - lx)
            h = w / ratio
            return self._cs_clamp_rect([lx, cy - h/2, lx + w, cy + h/2])
        if handle_idx == 7:   # left edge
            w = max(1.0, rx - ix)
            h = w / ratio
            return self._cs_clamp_rect([rx - w, cy - h/2, rx, cy + h/2])

        return self._cs_clamp_rect(r)

    def _show_straighten(self):
        pass  # straighten tab is now its own operation — nothing extra needed

    def _show_crop_marquee(self):
        if self._current[0] is None: return
        iw, ih = self._current[0].size
        r = [10, 10, iw - 10, ih - 10]
        self._cs_crop_rect[0] = self._cs_force_aspect_from_centre(r)
        self._redraw_working()

    def _clear_crop_marquee(self):
        self._cs_crop_rect[0] = None
        self._redraw_working()

    def _apply_straighten(self):
        if self._current[0] is None: return
        a = self._angle_var.get()
        if abs(a) < 0.01:
            messagebox.showinfo("Nothing to apply",
                "Move the angle slider first.", parent=self); return
        result = self._current[0].rotate(-a, expand=True, resample=Image.BILINEAR,
                                          fillcolor=(0,0,0))
        self._angle_var.set(0.0)
        self._angle_lbl.config(text="0.0deg")
        self._push(result)

    def _apply_crop(self):
        if self._current[0] is None: return
        img = self._current[0]
        r = self._cs_crop_rect[0]
        if not r:
            messagebox.showinfo("Nothing to apply",
                "Draw a crop marquee first.", parent=self); return
        x0,y0,x1,y1 = [int(v) for v in r]
        rw, rh = img.size
        x0=max(0,x0); y0=max(0,y0)
        x1=min(rw,x1); y1=min(rh,y1)
        if x1 <= x0 or y1 <= y0:
            messagebox.showinfo("Nothing to apply",
                "Crop region is too small.", parent=self); return
        self._cs_crop_rect[0] = None
        self._push(img.crop((x0,y0,x1,y1)))

    def _draw_cs_overlay(self):
        oc = self._overlay
        if self._current[0] is None: return
        img = self._current[0]
        iw, ih = img.size
        a = self._angle_var.get()
        if abs(a) > 0.01:
            N = 5
            cos_a = math.cos(math.radians(-a))
            sin_a = math.sin(math.radians(-a))
            cx_i, cy_i = iw/2, ih/2
            def rot(x,y):
                return (cx_i + x*cos_a - y*sin_a, cy_i + x*sin_a + y*cos_a)
            for i in range(1, N):
                t = i / N
                p0 = self._i2c(*rot(iw*t - cx_i, -ih/2))
                p1 = self._i2c(*rot(iw*t - cx_i,  ih/2))
                oc.create_line(*p0, *p1, fill="#00ffcc", width=1, tags="cs_grid")
                p0 = self._i2c(*rot(-iw/2, ih*t - cy_i))
                p1 = self._i2c(*rot( iw/2, ih*t - cy_i))
                oc.create_line(*p0, *p1, fill="#00ffcc", width=1, tags="cs_grid")
        r = self._cs_crop_rect[0]
        if r:
            x0,y0,x1,y1 = r
            lx,ty = min(x0,x1), min(y0,y1)
            rx,by = max(x0,x1), max(y0,y1)
            cx0,cy0 = self._i2c(lx,ty)
            cx1,cy1 = self._i2c(rx,by)
            mx = (cx0+cx1)/2; my = (cy0+cy1)/2
            oc.create_rectangle(cx0,cy0,cx1,cy1, outline="#ffffff", width=2, tags="cs_rect")
            H = 6
            for hx,hy in [(cx0,cy0),(mx,cy0),(cx1,cy0),(cx1,my),
                           (cx1,cy1),(mx,cy1),(cx0,cy1),(cx0,my)]:
                oc.create_rectangle(hx-H,hy-H,hx+H,hy+H,
                                    fill="#ffffff", outline="#666", width=1, tags="cs_rect")
            # Centre move handle. Drag this circle to move the crop marquee.
            try:
                cxm, cym = self._i2c((lx + rx) / 2.0, (ty + by) / 2.0)
                rr = getattr(self, "_CS_MOVE_R", 10)
                oc.create_oval(cxm - rr, cym - rr, cxm + rr, cym + rr,
                               outline="#ffffff", fill="#00ffcc",
                               width=2, tags=("cs_rect", "cs_move"))
                oc.create_line(cxm - rr + 3, cym, cxm + rr - 3, cym,
                               fill="#003333", width=2, tags=("cs_rect", "cs_move"))
                oc.create_line(cxm, cym - rr + 3, cxm, cym + rr - 3,
                               fill="#003333", width=2, tags=("cs_rect", "cs_move"))
            except Exception:
                pass

    def _draw_straighten_overlay(self):
        """Draw a rotated grid showing the straighten angle."""
        oc = self._overlay
        if self._current[0] is None: return
        a = self._angle_var.get()
        iw, ih = self._current[0].size
        N = 6
        cos_a = math.cos(math.radians(-a))
        sin_a = math.sin(math.radians(-a))
        cx_i, cy_i = iw / 2.0, ih / 2.0

        def rot(x, y):
            return (cx_i + x * cos_a - y * sin_a,
                    cy_i + x * sin_a + y * cos_a)

        for i in range(1, N):
            t = i / N
            # Horizontal grid lines
            p0 = self._i2c(*rot(-iw / 2, ih * t - cy_i))
            p1 = self._i2c(*rot( iw / 2, ih * t - cy_i))
            oc.create_line(*p0, *p1, fill="#00ffcc", width=1, tags="st_grid")
            # Vertical grid lines
            p0 = self._i2c(*rot(iw * t - cx_i, -ih / 2))
            p1 = self._i2c(*rot(iw * t - cx_i,  ih / 2))
            oc.create_line(*p0, *p1, fill="#00ffcc", width=1, tags="st_grid")

        # Horizon reference line through centre
        p0 = self._i2c(*rot(-iw / 2, 0))
        p1 = self._i2c(*rot( iw / 2, 0))
        oc.create_line(*p0, *p1, fill="#ffcc00", width=2, tags="st_grid")

    def _draw_squeeze_overlay(self):
        """Draw a barrel-like grid preview of the squeeze distortion."""
        oc = self._overlay
        if self._current[0] is None: return
        s = self._sq_var.get() / 100.0
        if abs(s) < 0.01:
            # Draw plain undistorted grid at zero
            iw, ih = self._current[0].size
            N = 8
            for i in range(N + 1):
                py = ih * i / N
                p0 = self._i2c(0, py); p1 = self._i2c(iw, py)
                oc.create_line(*p0, *p1, fill="#00ffcc", width=1, tags="sq_grid")
                px = iw * i / N
                p0 = self._i2c(px, 0); p1 = self._i2c(px, ih)
                oc.create_line(*p0, *p1, fill="#00ffcc", width=1, tags="sq_grid")
            return
        iw, ih = self._current[0].size
        N = 8
        k = s * 0.15
        cx_i = iw / 2.0

        def squeeze_x(px):
            """Map destination x to source x (same formula as _apply_squeeze)."""
            nx = (px - cx_i) / cx_i
            src_nx = nx * (1.0 + k * nx ** 2)
            return src_nx * cx_i + cx_i

        STEPS = 20
        # Horizontal lines — squeeze distorts x, so sample along each row
        for i in range(N + 1):
            py = ih * i / N
            pts = []
            for s2 in range(STEPS + 1):
                px_dst = iw * s2 / STEPS
                px_src = squeeze_x(px_dst)
                cx2, cy2 = self._i2c(px_src, py)
                pts += [cx2, cy2]
            if len(pts) >= 4:
                oc.create_line(pts, fill="#00ffcc", width=1, tags="sq_grid")
        # Vertical lines — straight vertical in squeeze (only x is distorted)
        for i in range(N + 1):
            px_dst = iw * i / N
            px_src = squeeze_x(px_dst)
            p0 = self._i2c(px_src, 0)
            p1 = self._i2c(px_src, ih)
            oc.create_line(*p0, *p1, fill="#00ffcc", width=1, tags="sq_grid")

    def _cs_handles_canvas(self):
        r = self._cs_crop_rect[0]
        if not r: return None
        x0,y0,x1,y1 = r
        lx,ty = min(x0,x1), min(y0,y1)
        rx,by = max(x0,x1), max(y0,y1)
        cx0,cy0 = self._i2c(lx,ty)
        cx1,cy1 = self._i2c(rx,by)
        mx=(cx0+cx1)/2; my=(cy0+cy1)/2
        return [(cx0,cy0),(mx,cy0),(cx1,cy0),(cx1,my),
                (cx1,cy1),(mx,cy1),(cx0,cy1),(cx0,my)]

    _CS_HANDLE_AXES = [
        (True, True,  False,False),
        (False,True,  False,False),
        (False,True,  True, False),
        (False,False, True, False),
        (False,False, True, True ),
        (False,False, False,True ),
        (True, False, False,True ),
        (True, False, False,False),
    ]
    _H_HIT = 10

    # ── Transform tab logic ───────────────────────────────────────────────────

    def _default_tf_handles(self):
        if self._current is None or self._current[0] is None:
            return [[10,10],[90,10],[90,90],[10,90]]
        iw, ih = self._current[0].size
        i = self._INSET if hasattr(self, '_INSET') else 0.05
        return [
            [iw*i,      ih*i],
            [iw*(1-i),  ih*i],
            [iw*(1-i),  ih*(1-i)],
            [iw*i,      ih*(1-i)],
        ]

    def _reset_tf_handles(self):
        self._tf_handles = self._default_tf_handles()
        self._redraw_working()

    def _bilerp(self, t, u, quad):
        tl,tr,br,bl = quad
        top = (tl[0]+(tr[0]-tl[0])*t, tl[1]+(tr[1]-tl[1])*t)
        bot = (bl[0]+(br[0]-bl[0])*t, bl[1]+(br[1]-bl[1])*t)
        return (top[0]+(bot[0]-top[0])*u, top[1]+(bot[1]-top[1])*u)

    def _solve_np(self, src4, dst4):
        import numpy as _np
        A, bv = [], []
        for (x,y),(X,Y) in zip(src4, dst4):
            A.append([x,y,1,0,0,0,-X*x,-X*y])
            A.append([0,0,0,x,y,1,-Y*x,-Y*y])
            bv += [X, Y]
        try:
            return _np.linalg.solve(
                _np.array(A, dtype=_np.float64),
                _np.array(bv, dtype=_np.float64))
        except _np.linalg.LinAlgError:
            return None

    def _apply_barrel(self):
        import numpy as _np
        if self._current[0] is None: return
        k = self._barrel_var.get() / 5.0    # slider -5..+5 → k -1..+1
        if abs(k) < 0.005:
            messagebox.showinfo("Nothing to apply",
                "Move the slider first.", parent=self); return
        try:
            img = self._current[0]
            w, h = img.size
            arr = _np.array(img, dtype=_np.float32)
            axis = self._barrel_axis.get()
            cx, cy = w / 2.0, h / 2.0

            # Vectorised — fast but uses more memory
            xs = (_np.arange(w, dtype=_np.float32) - cx) / cx
            ys = (_np.arange(h, dtype=_np.float32) - cy) / cy
            xg, yg = _np.meshgrid(xs, ys)
            if axis == "Horizontal":
                r2 = xg ** 2
            elif axis == "Vertical":
                r2 = yg ** 2
            else:
                r2 = xg ** 2 + yg ** 2
            factor = 1.0 + k * 0.5 * r2
            src_x = _np.clip(xg * factor * cx + cx, 0, w - 1)
            src_y = _np.clip(yg * factor * cy + cy, 0, h - 1)
            x0 = src_x.astype(_np.int32); x1 = _np.clip(x0 + 1, 0, w - 1)
            y0 = src_y.astype(_np.int32); y1 = _np.clip(y0 + 1, 0, h - 1)
            dx = (src_x - x0)[:, :, _np.newaxis].astype(_np.float32)
            dy = (src_y - y0)[:, :, _np.newaxis].astype(_np.float32)
            result = (arr[y0, x0] * (1-dx) * (1-dy) +
                      arr[y0, x1] * dx     * (1-dy) +
                      arr[y1, x0] * (1-dx) * dy     +
                      arr[y1, x1] * dx     * dy)

            self._barrel_var.set(0.0)
            self._barrel_lbl.config(text="0.0")
            self._push(Image.fromarray(_np.clip(result, 0, 255).astype(_np.uint8)))

        except MemoryError:
            messagebox.showerror("Memory error",
                "Image too large for barrel distortion on 32-bit Python.\n"
                "Try reducing image size first.", parent=self)
        except Exception as e:
            messagebox.showerror("Barrel failed", str(e), parent=self)

    def _apply_transform(self):
        import numpy as _np
        if self._current[0] is None: return
        img = self._current[0]
        w, h = img.size
        quad = [tuple(hh) for hh in self._tf_handles]

        # The handles define the SOURCE quadrilateral to be straightened.
        # Do NOT crop the result to that quadrilateral.  Instead, keep the
        # original full image canvas and map the source quadrilateral into the
        # rectangle that bounds it on that same canvas.  This makes Transform a
        # perspective correction only; cropping remains a separate Crop action.
        xs = [p[0] for p in quad]
        ys = [p[1] for p in quad]
        left   = max(0.0, min(xs))
        top    = max(0.0, min(ys))
        right  = min(float(w), max(xs))
        bottom = min(float(h), max(ys))

        if right - left < 2 or bottom - top < 2:
            messagebox.showerror("Transform failed",
                "Transform rectangle is too small — check handle positions.", parent=self)
            return

        # Destination rectangle stays inside the original image dimensions.
        # Image.transform() needs backward coefficients: destination -> source.
        dst4 = [(left, top), (right, top), (right, bottom), (left, bottom)]
        bwd_c = self._solve_np(dst4, quad)
        if bwd_c is None:
            messagebox.showerror("Transform failed",
                "Could not compute transform — check handle positions.", parent=self)
            return

        result = img.transform(
            (w, h), Image.PERSPECTIVE,
            tuple(float(v) for v in bwd_c), Image.BICUBIC)

        # Reset handles to the full transformed image area.  The output canvas
        # remains the original size, not the quadrilateral size.
        i = self._INSET
        self._tf_handles = [
            [w * i,       h * i],
            [w * (1 - i), h * i],
            [w * (1 - i), h * (1 - i)],
            [w * i,       h * (1 - i)],
        ]
        self._push(result)

    def _draw_tf_overlay(self):
        oc = self._overlay
        if self._current[0] is None: return
        N = self._GRID_N; segs = N + 1
        quad = self._tf_handles
        HC   = self._HANDLE_COL
        HR   = self._HANDLE_R
        for i in range(segs):
            t = i / N
            pts = []
            for j in range(segs):
                ix2,iy2 = self._bilerp(t, j/N, quad)
                cx2,cy2 = self._i2c(ix2,iy2)
                pts += [cx2,cy2]
            if len(pts) >= 4:
                oc.create_line(pts, fill=HC, width=1, tags="tf_grid")
            pts = []
            for j in range(segs):
                ix2,iy2 = self._bilerp(j/N, t, quad)
                cx2,cy2 = self._i2c(ix2,iy2)
                pts += [cx2,cy2]
            if len(pts) >= 4:
                oc.create_line(pts, fill=HC, width=1, tags="tf_grid")
        quad_c = [self._i2c(hx,hy) for hx,hy in quad]
        flat = [v for pt in quad_c+[quad_c[0]] for v in pt]
        oc.create_line(flat, fill=HC, width=2, tags="tf_quad")
        for i,(hx,hy) in enumerate(quad):
            cx2,cy2 = self._i2c(hx,hy)
            oc.create_oval(cx2-HR,cy2-HR,cx2+HR,cy2+HR,
                           fill=HC, outline="#ffffff", width=2, tags="tf_handle")
            oc.create_text(cx2, cy2-HR-8,
                           text=["TL","TR","BR","BL"][i],
                           fill=HC, font=("Segoe UI",8,"bold"), tags="tf_label")

    def _draw_perspective_overlay(self):
        """Draw a fixed rectangular graticule for judging perspective correction."""
        if self._current[0] is None:
            return
        c = self._overlay
        c.delete("persp_grid")
        iw, ih = self._current[0].size
        if iw <= 0 or ih <= 0:
            return
        x0, y0 = self._i2c(0, 0)
        x1, y1 = self._i2c(iw, ih)
        c.create_rectangle(x0, y0, x1, y1, outline="#00ffcc", width=2, tags="persp_grid")
        n = 6
        for i in range(1, n):
            t = i / n
            x = x0 + (x1 - x0) * t
            y = y0 + (y1 - y0) * t
            c.create_line(x, y0, x, y1, fill="#00ffcc", width=1, dash=(4, 4), tags="persp_grid")
            c.create_line(x0, y, x1, y, fill="#00ffcc", width=1, dash=(4, 4), tags="persp_grid")
        cx = (x0 + x1) / 2
        cy = (y0 + y1) / 2
        c.create_line(cx, y0, cx, y1, fill="#ffffff", width=1, dash=(6, 4), tags="persp_grid")
        c.create_line(x0, cy, x1, cy, fill="#ffffff", width=1, dash=(6, 4), tags="persp_grid")


    def _draw_barrel_overlay(self):
        """Draw a warped grid preview showing the barrel/pincushion distortion."""
        if self._current[0] is None: return
        k = getattr(self, '_barrel_var', None)
        if k is None: return
        k = k.get() / 5.0    # scale: slider -5..+5 → k -1..+1
        oc = self._overlay
        img = self._current[0]
        iw, ih = img.size
        axis = self._barrel_axis.get() if hasattr(self, '_barrel_axis') else "Both"
        N = 8
        col = "#00ffcc"
        cx, cy = iw / 2.0, ih / 2.0

        if abs(k) < 0.005:
            # Draw plain undistorted grid at zero
            for i in range(N + 1):
                p0 = self._i2c(iw * i / N, 0); p1 = self._i2c(iw * i / N, ih)
                oc.create_line(*p0, *p1, fill=col, width=1, tags="barrel_grid")
                p0 = self._i2c(0, ih * i / N); p1 = self._i2c(iw, ih * i / N)
                oc.create_line(*p0, *p1, fill=col, width=1, tags="barrel_grid")
            return

        def distort(px, py):
            """Apply barrel distortion to image-space point, return canvas coords."""
            nx = (px - cx) / cx
            ny = (py - cy) / cy
            if axis == "Horizontal":
                r2 = nx ** 2
            elif axis == "Vertical":
                r2 = ny ** 2
            else:
                r2 = nx ** 2 + ny ** 2
            f = 1.0 + k * 0.5 * r2
            dx = nx * f * cx + cx
            dy = ny * f * cy + cy
            return self._i2c(dx, dy)

        STEPS = 20  # points per line for smooth curve

        # Horizontal lines
        for row in range(N + 1):
            py = ih * row / N
            pts = []
            for s in range(STEPS + 1):
                px = iw * s / STEPS
                cx2, cy2 = distort(px, py)
                pts += [cx2, cy2]
            if len(pts) >= 4:
                oc.create_line(pts, fill=col, width=1, tags="barrel_grid")

        # Vertical lines
        for col_i in range(N + 1):
            px = iw * col_i / N
            pts = []
            for s in range(STEPS + 1):
                py = ih * s / STEPS
                cx2, cy2 = distort(px, py)
                pts += [cx2, cy2]
            if len(pts) >= 4:
                oc.create_line(pts, fill=col, width=1, tags="barrel_grid")

    # ── Squeeze tab logic ─────────────────────────────────────────────────────

    def _apply_squeeze(self):
        import numpy as _np
        if self._current[0] is None: return
        s = self._sq_var.get() / 100.0
        if s < 0.005:
            messagebox.showinfo("Nothing to apply",
                "Move the Squeeze slider first.", parent=self); return
        img = self._current[0]
        w, h = img.size
        arr = _np.array(img)
        cx = w / 2.0
        x_dst = (_np.arange(w) - cx) / cx
        k = s * 0.15
        x_src_raw = x_dst * (1.0 + k * x_dst**2)
        x_src = x_src_raw * cx + cx
        left_crop = int(_np.ceil(k * cx))
        x_src_c = _np.clip(x_src, 0, w - 1)
        x_idx = _np.arange(w)
        result = _np.zeros_like(arr)
        for c in range(3):
            for row in range(h):
                result[row,:,c] = _np.interp(x_src_c, x_idx, arr[row,:,c])
        if left_crop > 0 and w - 2*left_crop > 10:
            result = result[:, left_crop:w-left_crop, :]
        self._sq_var.set(0.0)
        self._sq_lbl.config(text="0%")
        self._push(Image.fromarray(result.astype(_np.uint8)))

    # ── FFT tab logic ─────────────────────────────────────────────────────────

    def _fft_clear_state(self):
        self._fft_notches     = []
        self._fft_spec_photo  = [None]
        self._fft_F_cache     = [None]
        self._fft_img_shape   = [None]
        self._fft_disp        = {"sz": 300, "ox": 0, "oy": 0}
        self._fft_preview_img = [None]
        self._fft_preview_mode = False
        self._fft_line_anchor = None
        if hasattr(self, '_fft_mode_var'):
            try: self._fft_mode_var.set("Add")
            except Exception: pass
        if hasattr(self, '_fft_status'):
            self._fft_status.config(text="Click Compute Spectrum to begin", fg="#666")

    def _fft_bind(self):
        self._fft_click_bound[0] = True

    def _fft_unbind(self):
        self._fft_click_bound[0] = False

    def _fft_compute(self):
        if self._current[0] is None: return
        self._fft_status.config(text="Computing spectrum…", fg=TEXT_DIM)
        self._overlay.delete("overlay")
        self._fft_notches.clear()
        self._fft_preview_img[0] = None
        self._fft_preview_mode = False
        self._fft_F_cache[0] = None

        def _worker():
            import numpy as _np
            img = self._current[0]
            iw, ih = img.size
            scale = min(1.0, self._FFT_MAX_PX / max(iw,ih))
            if scale < 1.0:
                sw,sh = int(iw*scale), int(ih*scale)
                img_fft = img.resize((sw,sh), Image.BILINEAR)
            else:
                img_fft = img; sw,sh = iw,ih
            grey = _np.array(img_fft.convert("L"), dtype=_np.float32)
            h, w = grey.shape
            F_grey = _np.fft.fftshift(_np.fft.fft2(grey))
            mag = _np.log1p(_np.abs(F_grey))
            mag -= mag.min()
            if mag.max() > 0: mag /= mag.max()
            spec_arr = (mag * 255).astype(_np.uint8)
            spec_pil = Image.fromarray(spec_arr, "L").convert("RGB")
            oc = self._overlay
            try: oc.update_idletasks()
            except: return
            cw2 = oc.winfo_width(); ch2 = oc.winfo_height()
            sz = min(max(min(cw2,ch2), 300), 600)
            spec_pil = spec_pil.resize((sz,sz), Image.BILINEAR)
            arr = _np.array(img_fft, dtype=_np.float32)
            F_ch = [_np.fft.fftshift(_np.fft.fft2(arr[:,:,c])) for c in range(3)]
            self._fft_F_cache[0] = F_ch
            self._fft_img_shape[0] = (h, w)

            def _done():
                cw3 = oc.winfo_width(); ch3 = oc.winfo_height()
                ox = (cw3-sz)//2; oy = (ch3-sz)//2
                photo = ImageTk.PhotoImage(spec_pil)
                self._fft_spec_photo[0] = photo
                self._fft_disp.update(sz=sz, ox=ox, oy=oy)
                note = f" (downsampled to {sw}×{sh})" if scale < 1.0 else ""
                self._fft_status.config(
                    text=f"Image {iw}×{ih}{note}. Click to place notches.",
                    fg=TEXT_DIM)
                # Redraw via the normal path — ensures canvas is fully consistent
                # before any clicks are processed. Direct canvas manipulation here
                # caused stale _fft_disp bugs when _redraw_working fired mid-update.
                self._redraw_working()
            self.after(0, _done)

        threading.Thread(target=_worker, daemon=True).start()

    def _fft_redraw_spec(self):
        if not self._fft_spec_photo[0]: return
        oc = self._overlay
        d = self._fft_disp
        oc.create_image(d["ox"], d["oy"], anchor="nw",
                        image=self._fft_spec_photo[0], tags="fft_spec")
        self._fft_draw_notches()

    def _fft_draw_notches(self):
        oc = self._overlay
        oc.delete("fft_notch")
        oc.delete("fft_anchor")
        oc.delete("fft_rubberband")
        d = self._fft_disp
        sz,ox,oy = d["sz"], d["ox"], d["oy"]
        for cx,cy,r in self._fft_notches:
            ax,ay = cx+ox, cy+oy
            oc.create_oval(ax-r,ay-r,ax+r,ay+r,
                           outline="#ff4444", width=2, tags="fft_notch")
            mx,my = (sz-cx)+ox, (sz-cy)+oy
            oc.create_oval(mx-r,my-r,mx+r,my+r,
                           outline="#ff8888", width=1, dash=(4,3), tags="fft_notch")
        # Draw line-mode anchor if set
        anc = getattr(self, '_fft_line_anchor', None)
        if anc is not None:
            ax2, ay2 = anc[0]+ox, anc[1]+oy
            arm = 10
            oc.create_line(ax2-arm, ay2, ax2+arm, ay2, fill="#ffdd00", width=2, tags="fft_anchor")
            oc.create_line(ax2, ay2-arm, ax2, ay2+arm, fill="#ffdd00", width=2, tags="fft_anchor")
            oc.create_oval(ax2-4, ay2-4, ax2+4, ay2+4,
                           outline="#ffdd00", width=2, tags="fft_anchor")

    def _fft_click(self, e):
        if self._fft_F_cache[0] is None:
            self._fft_status.config(text="Compute spectrum first.", fg="#ff6666"); return
        # Preview mode is deliberately temporary. A click while previewing first
        # returns to the editable spectrum; the user can then click again to add,
        # remove, or replace patches. This avoids accidentally editing invisible
        # spectrum coordinates while the filtered photo is shown.
        if getattr(self, '_fft_preview_mode', False) or self._fft_preview_img[0] is not None:
            self._fft_show_spectrum()
            return
        # Cancel any pending line anchor
        if self._fft_line_anchor is not None:
            self._fft_line_anchor = None
            self._overlay.delete("fft_anchor")
            self._overlay.delete("fft_rubberband")
        d  = self._fft_disp
        sx = e.x - d["ox"]; sy = e.y - d["oy"]
        if not (0 <= sx < d["sz"] and 0 <= sy < d["sz"]): return
        mode = self._fft_mode_var.get() if hasattr(self, '_fft_mode_var') else "Add"
        if mode == "Remove":
            self._fft_remove_nearest(sx, sy)
            return
        if mode == "Replace":
            self._fft_remove_nearest(sx, sy, silent_if_miss=True)
        r = self._fft_radius_var.get()
        self._fft_notches.append((sx, sy, r))
        self._fft_draw_notches()
        self._fft_update_status(prefix="Patch replaced. " if mode == "Replace" else "")

    def _fft_shift_click(self, e):
        """Shift+click: set line anchor on first call, draw line on second."""
        if self._fft_F_cache[0] is None:
            self._fft_status.config(text="Compute spectrum first.", fg="#ff6666"); return
        if getattr(self, '_fft_preview_mode', False) or self._fft_preview_img[0] is not None:
            self._fft_show_spectrum()
            return
        d  = self._fft_disp
        sx = e.x - d["ox"]; sy = e.y - d["oy"]
        if not (0 <= sx < d["sz"] and 0 <= sy < d["sz"]): return
        if self._fft_line_anchor is None:
            # First shift-click — store anchor
            self._fft_line_anchor = (sx, sy)
            self._fft_draw_notches()
            self._fft_update_status(prefix="Anchor set. ")
        else:
            # Second shift-click — draw line of patches
            ax, ay = self._fft_line_anchor
            r = self._fft_radius_var.get()
            self._fft_add_line(ax, ay, sx, sy, r)
            self._fft_line_anchor = None
            self._overlay.delete("fft_anchor")
            self._overlay.delete("fft_rubberband")
            self._fft_draw_notches()
            self._fft_update_status(prefix="Line added. ")

    def _fft_add_line(self, x1, y1, x2, y2, r):
        """Place overlapping circular notches from (x1,y1) to (x2,y2)."""
        import math as _math
        dist = _math.hypot(x2 - x1, y2 - y1)
        step = max(r * 1.4, 1.0)
        n    = max(int(dist / step) + 1, 2)
        mode = self._fft_mode_var.get() if hasattr(self, '_fft_mode_var') else "Add"
        for i in range(n):
            t  = i / (n - 1)
            cx = x1 + t * (x2 - x1)
            cy = y1 + t * (y2 - y1)
            if mode == "Replace":
                self._fft_remove_nearest(int(cx), int(cy), silent_if_miss=True)
            self._fft_notches.append((cx, cy, r))

    def _fft_draw_rubberband(self, ex, ey):
        """Draw a preview line from anchor to current mouse position."""
        oc = self._overlay
        oc.delete("fft_rubberband")
        anc = getattr(self, '_fft_line_anchor', None)
        if anc is None: return
        d = self._fft_disp
        ox, oy = d["ox"], d["oy"]
        ax2, ay2 = anc[0] + ox, anc[1] + oy
        oc.create_line(ax2, ay2, ex, ey,
                       fill="#ffdd00", width=1, dash=(6, 4), tags="fft_rubberband")

    def _fft_remove_nearest(self, sx, sy, silent_if_miss=False):
        if not self._fft_notches:
            if not silent_if_miss:
                self._fft_status.config(text="No patches to remove.", fg="#ff6666")
            return False
        best_i = None; best_d2 = None
        for i, (cx, cy, r) in enumerate(self._fft_notches):
            d2 = (sx-cx)**2 + (sy-cy)**2
            if best_d2 is None or d2 < best_d2:
                best_i, best_d2 = i, d2
        if best_i is not None and best_d2 <= (max(18, self._fft_notches[best_i][2] * 2) ** 2):
            self._fft_notches.pop(best_i)
            self._fft_draw_notches()
            if not silent_if_miss:
                self._fft_update_status(prefix="Patch removed. ")
            return True
        if not silent_if_miss:
            self._fft_status.config(text="No patch near click.", fg="#ff6666")
        return False

    def _fft_update_status(self, prefix=""):
        n = len(self._fft_notches)
        mode = self._fft_mode_var.get() if hasattr(self, '_fft_mode_var') else "Add"
        anc = getattr(self, '_fft_line_anchor', None)
        if anc is not None:
            self._fft_status.config(
                text=f"{prefix}{n} patch{'es' if n!=1 else ''}. Anchor set — Shift+click end point to draw line. Plain click cancels.",
                fg="#ffdd00")
        else:
            self._fft_status.config(
                text=f"{prefix}{n} patch{'es' if n!=1 else ''}. Mode: {mode}. Shift+click twice to draw a notch line.",
                fg=TEXT_DIM)

    def _fft_undo_notch(self):
        if self._fft_notches:
            self._fft_notches.pop()
            self._fft_draw_notches()
            self._fft_update_status(prefix="Undo patch. ")

    def _fft_clear_patches(self):
        self._fft_notches.clear()
        self._fft_preview_img[0] = None
        self._fft_preview_mode = False
        self._fft_draw_notches()
        self._fft_status.config(text="Patches cleared. Spectrum remains available.", fg=TEXT_DIM)
        if self._fft_spec_photo[0]:
            self._fft_show_spectrum()

    def _fft_show_spectrum(self):
        if not self._fft_spec_photo[0]:
            self._fft_status.config(text="Compute spectrum first.", fg="#ff6666")
            return
        self._fft_preview_img[0] = None
        self._fft_preview_mode = False
        self._redraw_working()
        self._fft_update_status(prefix="Back to editable dots. ")

    def _fft_make_filtered_image(self):
        import numpy as _np
        if self._fft_F_cache[0] is None:
            raise RuntimeError("Compute spectrum first.")
        if not self._fft_notches:
            raise RuntimeError("Place at least one patch on the spectrum.")
        h,w = self._fft_img_shape[0]
        sz  = self._fft_disp["sz"]
        mask = _np.ones((h,w), dtype=_np.float32)
        scx = w/sz; scy = h/sz
        Y,X = _np.mgrid[0:h, 0:w]
        for cx_d,cy_d,r_d in self._fft_notches:
            cx_f = cx_d*scx; cy_f = cy_d*scy
            r_f  = r_d*min(scx,scy)
            mx_f = w-cx_f;  my_f = h-cy_f
            mask[_np.sqrt((X-cx_f)**2+(Y-cy_f)**2) <= r_f] = 0
            mask[_np.sqrt((X-mx_f)**2+(Y-my_f)**2) <= r_f] = 0
        result = _np.zeros((h,w,3), dtype=_np.float32)
        for c in range(3):
            filtered = _np.fft.ifft2(
                _np.fft.ifftshift(self._fft_F_cache[0][c]*mask)).real
            result[:,:,c] = _np.clip(filtered, 0, 255)
        filt_pil = Image.fromarray(result.astype(_np.uint8))
        orig_w,orig_h = self._current[0].size
        if (h,w) != (orig_h,orig_w):
            filt_pil = filt_pil.resize((orig_w,orig_h), Image.BILINEAR)
        return filt_pil

    def _fft_preview(self):
        try:
            self._fft_status.config(text="Building filtered preview…", fg=TEXT_DIM)
            def _worker():
                try:
                    filt = self._fft_make_filtered_image()
                except Exception as ex:
                    self.after(0, lambda e=ex: messagebox.showinfo("FFT preview", str(e), parent=self))
                    return
                def _done():
                    self._fft_preview_img[0] = filt
                    self._fft_preview_mode = True
                    self._fft_status.config(text="Filtered preview shown. Use Back to Edit Dots to change patches, or Apply Filter to commit.", fg="#88cc88")
                    self._redraw_working()
                self.after(0, _done)
            threading.Thread(target=_worker, daemon=True).start()
        except Exception as e:
            messagebox.showerror("FFT preview failed", str(e), parent=self)

    def _fft_clear(self):
        self._fft_notches.clear()
        self._fft_preview_img[0] = None
        self._fft_preview_mode = False
        self._fft_spec_photo[0] = None
        self._fft_F_cache[0] = None
        self._fft_img_shape[0] = None
        self._overlay.delete("fft_notch","fft_spec")
        self._fft_status.config(text="Cleared. Compute Spectrum to restart.", fg=TEXT_DIM)
        self._redraw_working()

    def _fft_apply(self):
        if self._fft_F_cache[0] is None:
            messagebox.showinfo("No spectrum", "Compute spectrum first.", parent=self); return
        if not self._fft_notches:
            messagebox.showinfo("No patches",
                "Place at least one patch on the spectrum.", parent=self); return
        self._fft_status.config(text="Applying filter…", fg=TEXT_DIM)

        def _worker():
            try:
                # If the user has already built a preview, commit that exact
                # image. Otherwise build the filtered image now.
                filt_pil = self._fft_preview_img[0] if self._fft_preview_img[0] is not None else self._fft_make_filtered_image()
            except Exception as ex:
                self.after(0, lambda e=ex: messagebox.showerror("FFT failed", str(e), parent=self))
                return

            def _done():
                self._fft_notches.clear()
                self._fft_F_cache[0] = None
                self._fft_img_shape[0] = None
                self._fft_spec_photo[0] = None
                self._fft_preview_img[0] = None
                self._fft_preview_mode = False
                self._fft_status.config(
                    text="Filter applied. Compute Spectrum to continue.", fg="#88cc88")
                self._fft_unbind()
                self._push(filt_pil)
            self.after(0, _done)

        threading.Thread(target=_worker, daemon=True).start()

    # ── Mouse event binding ───────────────────────────────────────────────────

    def _on_shift_press(self, e):
        tab = self._active_tab[0]
        if tab == "FFT Filter":
            if self._fft_click_bound[0]:
                self._fft_shift_click(e)
            return

    def _on_motion(self, e):
        if self._active_tab[0] == "FFT Filter" and self._fft_click_bound[0]:
            if getattr(self, '_fft_line_anchor', None) is not None:
                self._fft_draw_rubberband(e.x, e.y)

    def _on_press(self, e):
        tab = self._active_tab[0]
        if tab == "FFT Filter":
            if self._fft_click_bound[0]:
                self._fft_click(e)
            return
        if tab == "Transform":
            for i,(hx,hy) in enumerate(self._tf_handles):
                px,py = self._i2c(hx,hy)
                if (e.x-px)**2+(e.y-py)**2 <= (self._HANDLE_R+4)**2:
                    self._tf_drag_idx[0] = i; return
            self._tf_drag_idx[0] = None
        elif tab == "Crop":
            ix, iy = self._c2i(e.x, e.y)
            if self._current[0]:
                iw, ih = self._current[0].size
                ix = max(0, min(ix, iw)); iy = max(0, min(iy, ih))

            handles = self._cs_handles_canvas()
            if handles:
                for i,(hx,hy) in enumerate(handles):
                    if abs(e.x-hx) <= self._H_HIT and abs(e.y-hy) <= self._H_HIT:
                        self._cs_handle_idx[0] = i
                        self._cs_drag[0] = None
                        return

            r = self._cs_crop_rect[0]
            shift_down = bool(getattr(e, "state", 0) & 0x0001)

            # Move is now deliberate only:
            #   - drag the centre circle handle, or
            #   - Shift+drag inside the marquee.
            if r and (self._cs_move_handle_hit_canvas(r, e.x, e.y) or (shift_down and self._cs_rect_contains_point(r, ix, iy))):
                self._cs_handle_idx[0] = None
                self._cs_drag[0] = {
                    'mode': 'move',
                    'start': (ix, iy),
                    'rect': self._cs_norm_rect(r),
                }
                return

            # Normal click-drag creates a fresh marquee, even if clicking inside the old one.
            self._cs_handle_idx[0] = None
            self._cs_drag[0] = {'mode': 'new', 'start': (ix, iy)}
            self._cs_crop_rect[0] = [ix, iy, ix, iy]
            self._redraw_working()

    def _on_drag(self, e):
        tab = self._active_tab[0]
        if tab == "Transform":
            i = self._tf_drag_idx[0]
            if i is None: return
            ix,iy = self._c2i(e.x,e.y)
            if self._current[0]:
                iw,ih = self._current[0].size
                self._tf_handles[i] = [max(0,min(ix,iw)), max(0,min(iy,ih))]
            self._redraw_working()
        elif tab == "Crop":
            ix,iy = self._c2i(e.x,e.y)
            if self._current[0]:
                iw,ih = self._current[0].size
                ix = max(0,min(ix,iw)); iy = max(0,min(iy,ih))
            if self._cs_handle_idx[0] is not None:
                r = self._cs_crop_rect[0]
                if not r: return
                self._cs_crop_rect[0] = self._cs_resize_rect_by_handle(
                    r, self._cs_handle_idx[0], ix, iy)
                self._redraw_working()
            elif self._cs_drag[0]:
                drag = self._cs_drag[0]
                if isinstance(drag, dict) and drag.get('mode') == 'move':
                    sx, sy = drag['start']
                    self._cs_crop_rect[0] = self._cs_move_rect(drag['rect'], ix - sx, iy - sy)
                elif isinstance(drag, dict) and drag.get('mode') == 'new':
                    sx, sy = drag['start']
                    self._cs_crop_rect[0] = self._cs_rect_from_anchor(sx, sy, ix, iy)
                else:
                    # Compatibility with very old state where _cs_drag stored a tuple.
                    sx, sy = drag
                    self._cs_crop_rect[0] = self._cs_rect_from_anchor(sx, sy, ix, iy)
                self._redraw_working()

    def _on_release(self, e):
        self._tf_drag_idx[0] = None
        self._cs_drag[0] = None
        self._cs_handle_idx[0] = None
        if self._cs_crop_rect[0]:
            x0,y0,x1,y1 = self._cs_norm_rect(self._cs_crop_rect[0])
            if abs(x1-x0) < 5 or abs(y1-y0) < 5:
                self._cs_crop_rect[0] = None
            else:
                self._cs_crop_rect[0] = [x0,y0,x1,y1]
                self._redraw_working()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]

    if "--embedded" in args:
        app = FTImgedit(mode="embedded")
        app.mainloop()
        return

    if not args:
        # No arguments — default to the active project Photos root when
        # Projects.ini exists.  If no project root exists, leave the root box
        # empty so the user can enter/select a folder.
        app = FTImgedit(mode="standalone", start_path=None, file_list=[])
        roots = read_project_roots(__file__)
        photos_root = roots.get("photos", "")
        if photos_root and os.path.isdir(photos_root):
            app.after(100, lambda: app._set_tree_root(photos_root))
        app.mainloop()
        return

    target = args[0]

    if os.path.isdir(target):
        app = FTImgedit(mode="standalone", start_path=None, file_list=[])
        app.after(100, lambda: app._set_tree_root(target))
        app.mainloop()
        return

    if os.path.isfile(target):
        folder = os.path.dirname(target)
        file_list = _scan_folder(folder)
        if target not in file_list:
            file_list.insert(0, target)
        app = FTImgedit(mode="standalone",
                      start_path=target,
                      file_list=file_list)
        app._file_idx = file_list.index(target)
        app._lb_select(app._file_idx)
        app.after(100, lambda: app._set_tree_root(folder))
        app.mainloop()
        return

    messagebox.showerror("FTImgedit", f"Path not found:\n{target}")


if __name__ == "__main__":
    main()