"""
FTFiler.py — FileTagger File Operations tool for FileTagger suite.

Layout (standalone):
  [Folder Tree 300px] | [File List 300px] | [Rename Controls 380px] | [Preview]

Usage:
  pythonw.exe FTFiler.py
  pythonw.exe FTFiler.py "S:\\Photos\\2024-Holiday"
  pythonw.exe FTFiler.py "S:\\Photos\\IMG_001.JPG"

Dependencies: Python 3.11 32-bit, Pillow, (pymupdf for PDF preview)
FTCategories.json must be alongside this script.
"""

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import os, sys, re, json
import tkinter as tk
from tkinter import ttk, messagebox
import tkinter.filedialog as fd

try:
    from libraries.ft_project_roots import read_project_roots
except Exception:
    def read_project_roots(base_file=None):
        return {"photos": "", "pdfs": "", "project": ""}

try:
    from libraries.ft_widgets import show_file_sort_menu, _sort_btn_label
except ImportError:
    def _sort_btn_label(column="name", reverse=False):  # type: ignore[misc]
        labels = {"name": "Name", "date_taken": "Date", "file": "Name", "size": "Size"}
        return f"{labels.get(column, column)} {'↓' if reverse else '↑'} ▾"
    show_file_sort_menu = None  # type: ignore[assignment]
from datetime import date as _date

# ── Attempt optional imports ───────────────────────────────────────────────────
try:
    from PIL import Image, ImageTk
    _PIL = True
except ImportError:
    _PIL = False

try:
    import fitz as _fitz
    _FITZ = True
except ImportError:
    _FITZ = False

# ── Constants ─────────────────────────────────────────────────────────────────
PHOTO_EXTS = {'.jpg', '.jpeg'}
PDF_EXTS   = {'.pdf'}

BG       = "#f0f0f0"
BG2      = "#e8e8e8"
ACCENT   = "#4f8ef7"
TEXT_BRIGHT = "#111111"
TEXT_DIM    = "#777777"
HDR_BG   = "#1a3a5c"
HDR_FG   = "white"
GREEN    = "#1a6b2a"
AMBER    = "#c87800"
RED      = "#8b1a1a"

# ── Helpers ───────────────────────────────────────────────────────────────────

def _longpath(p):
    if sys.platform == "win32" and not p.startswith("\\\\?\\"):
        return "\\\\?\\" + os.path.abspath(p)
    return p

def _script_dir():
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except NameError:
        return os.path.dirname(os.path.abspath(sys.argv[0]))

_DATE_PAT = re.compile(r'^\d{4}-\d{2}-\d{2}')

def _scan_folder(folder, exts):
    try:
        entries = [e for e in os.scandir(folder)
                   if e.is_file() and os.path.splitext(e.name)[1].lower() in exts]
        def _sort_key(path):
            fname = os.path.basename(path)
            m = _DATE_PAT.match(fname)
            if m:
                # Dated — sort newest first (negate char codes)
                return (0, tuple(~ord(c) for c in m.group(0)))
            # Undated — alphabetical after dated files
            return (1, fname.lower())
        return sorted([e.path for e in entries], key=_sort_key)
    except Exception:
        return []

def _load_ft_categories(parent=None):
    p = os.path.join(_script_dir(), "FTCategories.json")
    empty = {"who": [], "categories": {}}
    if not os.path.exists(p):
        msg = f"FTCategories.json not found in:\n{_script_dir()}\n\nCategories will be unavailable."
        if parent:
            messagebox.showwarning("FTCategories.json missing", msg, parent=parent)
        else:
            messagebox.showwarning("FTCategories.json missing", msg)
        return empty
    try:
        with open(p, 'r', encoding='utf-8') as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        msg = (f"FTCategories.json has a JSON error and could not be loaded:\n\n"
               f"{e}\n\n"
               f"File: {p}\n\n"
               f"Fix the JSON file and restart.")
        if parent:
            messagebox.showerror("FTCategories.json error", msg, parent=parent)
        else:
            messagebox.showerror("FTCategories.json error", msg)
        return empty
    except Exception as e:
        msg = f"Could not read FTCategories.json:\n\n{e}\n\nFile: {p}"
        if parent:
            messagebox.showerror("FTCategories.json error", msg, parent=parent)
        else:
            messagebox.showerror("FTCategories.json error", msg)
        return empty

def _fit_text(text, max_px, font_obj):
    """Truncate text to fit within max_px using the given tkfont."""
    while text and font_obj.measure(text) > max_px:
        text = text[:-1]
    return text

def _centre_window(win, w, h):
    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()
    win.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")

def _clean_stem(s, maxlen=60):
    s = s.strip()[:maxlen]
    s = re.sub(r'[\s/\\:*?"<>|]+', '_', s)
    return s.strip('_')

def _add_tooltip(widget, text):
    """Show a simple tooltip on hover."""
    tip = [None]
    def _show(e):
        tip[0] = tk.Toplevel(widget)
        tip[0].overrideredirect(True)
        tip[0].configure(bg="#ffffe0")
        tk.Label(tip[0], text=text, bg="#ffffe0", fg="#111111",
                 font=("Segoe UI", 8), padx=6, pady=3,
                 relief="solid", bd=1).pack()
        tip[0].geometry(f"+{e.x_root+12}+{e.y_root+20}")
    def _hide(e):
        if tip[0]:
            try: tip[0].destroy()
            except: pass
            tip[0] = None
    widget.bind("<Enter>", _show)
    widget.bind("<Leave>", _hide)


# ── Main app ──────────────────────────────────────────────────────────────────

class FTFiler(tk.Tk):

    def __init__(self, start_path=None, embedded=False, start_mode=None):
        super().__init__()
        self.title("FTFiler — File Rename")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.minsize(900, 600)

        # State
        self._mode          = "photos"
        self._file_list     = []
        self._file_idx      = 0
        self._filtered_list = None
        self._sort_column   = "name"
        self._sort_reverse  = False
        self._renamed       = {}
        self._tree          = None
        self._left_paned    = None
        self._cats          = {"who": [], "categories": {}}
        self._folder_var    = None
        # IPC
        self._embedded      = embedded
        self._ipc_mode      = start_mode or "RENAME"
        self._ipc_seq       = -1
        self._ipc_dir       = self._get_ipc_dir()

        # Hide window immediately if starting in RENAME mode — show after IPC received
        if embedded and start_mode == "RENAME":
            self.withdraw()

        # Size window
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        win_h = int(sh * 0.80)
        a4_preview_w = int(win_h * 210 / 297) + 40
        win_w_min = 610 + 380 + a4_preview_w
        win_w = min(max(win_w_min, 1600), sw - 40)
        x = (sw - win_w) // 2
        y = (sh - win_h) // 2
        self.geometry(f"{win_w}x{win_h}+{x}+{y}")

        self._build_ui()
        self._cats = _load_ft_categories(parent=self)
        self._reload_categories()
        self.after(150, self._set_initial_sashes)

        if self._embedded:
            self.after(100, self._poll_ipc)   # poll quickly — FTDB wrote request before launch
            return

        if start_path:
            if os.path.isfile(start_path):
                folder = os.path.dirname(start_path)
                self.after(200, lambda: self._set_tree_root(folder))
                # Select that file after tree loads
                self.after(500, lambda: self._open_file(start_path))
            elif os.path.isdir(start_path):
                self.after(200, lambda: self._set_tree_root(start_path))

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Mode toggle bar ───────────────────────────────────────────────────
        mode_bar = tk.Frame(self, bg=HDR_BG)
        mode_bar.pack(fill="x")
        tk.Label(mode_bar, text="FTFiler", bg=HDR_BG, fg="white",
                 font=("Segoe UI", 10, "bold"), padx=12).pack(side="left")

        self._mode_var = tk.StringVar(value="photos")
        for val, label in (("photos", "📷  Photos"), ("pdfs", "📄  PDFs")):
            tk.Radiobutton(mode_bar, text=label, variable=self._mode_var, value=val,
                           bg=HDR_BG, fg="white", selectcolor="#2a5a8a",
                           activebackground=HDR_BG, activeforeground="white",
                           font=("Segoe UI", 9, "bold"),
                           command=self._on_mode_change).pack(side="left", padx=8, pady=4)

        # ── Main paned window ─────────────────────────────────────────────────
        self._paned = tk.PanedWindow(self, orient="horizontal", bg=BG2,
                                     sashwidth=6, sashrelief="groove", sashpad=2,
                                     handlesize=8)
        self._paned.pack(fill="both", expand=True)

        # ── Tree outer: folder root entry + tree (hidden in RENAME mode) ──────
        from tkinter import ttk
        tree_outer = tk.Frame(self._paned, bg=BG2, width=260)
        self._tree_outer = tree_outer
        self._paned.add(tree_outer, minsize=200, width=260, stretch="never")

        # Root folder entry
        folder_bar = tk.Frame(tree_outer, bg=BG2)
        folder_bar.pack(fill="x", padx=4, pady=(6, 2))
        tk.Label(folder_bar, text="Root folder", bg=BG2, fg=TEXT_DIM,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w")
        fe_frame = tk.Frame(folder_bar, bg=BG2)
        fe_frame.pack(fill="x")
        self._folder_var = tk.StringVar()
        self._folder_entry = tk.Entry(fe_frame, textvariable=self._folder_var,
                                      bg="white", fg=TEXT_BRIGHT,
                                      font=("Segoe UI", 8), relief="solid", bd=1)
        self._folder_entry.pack(side="left", fill="x", expand=True)
        self._folder_entry.bind("<Return>",
            lambda e: self._set_tree_root(self._folder_var.get().strip()))
        tk.Button(fe_frame, text="...", bg=ACCENT, fg="white",
                  font=("Segoe UI", 9, "bold"), relief="flat", padx=6, cursor="hand2",
                  command=self._browse_root).pack(side="left", padx=(2, 0))
        self._folder_count = tk.Label(folder_bar, text="Select a root folder",
                                      bg=BG2, fg=TEXT_DIM, font=("Segoe UI", 7), anchor="w")
        self._folder_count.pack(anchor="w", pady=(2, 0))

        tk.Frame(tree_outer, bg="#aaaaaa", height=1).pack(fill="x", pady=(4, 0))

        # Folder tree
        tree_sb = ttk.Scrollbar(tree_outer, orient="vertical")
        style = ttk.Style()
        style.configure("Rename.Treeview", background="white",
                        fieldbackground="white", foreground=TEXT_BRIGHT,
                        font=("Segoe UI", 9))
        self._tree = ttk.Treeview(tree_outer, style="Rename.Treeview",
                                  yscrollcommand=tree_sb.set,
                                  show="tree", selectmode="browse")
        tree_sb.config(command=self._tree.yview)
        self._tree.tag_configure("has_file", foreground="#0055cc")
        self._tree.tag_configure("no_file",  foreground="#888888")
        tree_sb.pack(side="right", fill="y")
        self._tree.pack(fill="both", expand=True, padx=(4, 0), pady=(2, 4))
        self._tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self._tree.bind("<<TreeviewOpen>>",   lambda e: self.after(1, self._do_tree_open))
        self._tree.bind("<MouseWheel>",
            lambda e: self._tree.yview_scroll(-1 if e.delta > 0 else 1, "units"))

        # ── File list pane (always visible — direct child of _paned) ──────────
        file_frame = tk.Frame(self._paned, bg=BG2, width=300)
        self._file_frame = file_frame
        self._paned.add(file_frame, minsize=150, width=300, stretch="never")

        # Files header row: label + sort button
        _fl_hdr = tk.Frame(file_frame, bg=BG2)
        _fl_hdr.pack(fill="x", padx=4, pady=(4, 0))
        tk.Label(_fl_hdr, text="Files", bg=BG2, fg=TEXT_DIM,
                 font=("Segoe UI", 8, "bold")).pack(side="left")
        self._sort_btn = tk.Button(
            _fl_hdr, text=_sort_btn_label(self._sort_column, self._sort_reverse),
            font=("Segoe UI", 8, "bold"), bg=BG2, fg=ACCENT,
            relief="flat", cursor="hand2",
            command=lambda: self._show_sort_menu(self._sort_btn),
        )
        self._sort_btn.pack(side="right", padx=(0, 20))

        # Filter entry row
        filt_entry_frame = tk.Frame(file_frame, bg=BG2)
        filt_entry_frame.pack(fill="x", padx=4, pady=(2, 1))
        self._filter_var = tk.StringVar()
        filt_entry = tk.Entry(filt_entry_frame, textvariable=self._filter_var,
                              bg="white", fg=TEXT_BRIGHT, insertbackground=TEXT_BRIGHT,
                              font=("Segoe UI", 9), relief="solid", bd=1)
        filt_entry.pack(fill="x")
        filt_entry.bind("<Return>", lambda e: self._apply_filter())

        # Any / All + Apply + Clear row
        filt_btn_frame = tk.Frame(file_frame, bg=BG2)
        filt_btn_frame.pack(fill="x", padx=4, pady=(1, 2))

        self._filter_mode = tk.StringVar(value="Any")

        def _set_mode(m):
            self._filter_mode.set(m)
            btn_any.config(bg="#225588" if m == "Any" else BG2,
                           fg="white"   if m == "Any" else TEXT_DIM,
                           relief="flat" if m == "Any" else "groove")
            btn_all.config(bg="#225588" if m == "All" else BG2,
                           fg="white"   if m == "All" else TEXT_DIM,
                           relief="flat" if m == "All" else "groove")

        btn_any = tk.Button(filt_btn_frame, text="Match Any", bg="#225588", fg="white",
                            font=("Segoe UI", 8, "bold"), relief="flat",
                            padx=6, pady=2, cursor="hand2",
                            command=lambda: _set_mode("Any"))
        btn_any.pack(side="left", padx=(0, 2))

        btn_all = tk.Button(filt_btn_frame, text="Match All", bg=BG2, fg=TEXT_DIM,
                            font=("Segoe UI", 8, "bold"), relief="groove",
                            padx=6, pady=2, cursor="hand2",
                            command=lambda: _set_mode("All"))
        btn_all.pack(side="left", padx=(0, 6))

        tk.Button(filt_btn_frame, text="Apply Filter", bg=ACCENT, fg="white",
                  font=("Segoe UI", 8, "bold"), relief="flat",
                  padx=6, pady=2, cursor="hand2",
                  command=self._apply_filter).pack(side="left", padx=(0, 2))

        tk.Button(filt_btn_frame, text="Clear Filter", bg="#666666", fg="white",
                  font=("Segoe UI", 8), relief="flat",
                  padx=6, pady=2, cursor="hand2",
                  command=self._clear_filter).pack(side="left")

        self._filter_label = tk.Label(file_frame, text="", bg=BG2, fg=ACCENT,
                                      font=("Segoe UI", 7), anchor="w")
        self._filter_label.pack(anchor="w", padx=4)
        lb_sb = tk.Scrollbar(file_frame, orient="vertical")
        self._lb = tk.Listbox(file_frame, bg="white", fg=TEXT_BRIGHT,
                              selectbackground=ACCENT, selectforeground="white",
                              font=("Segoe UI", 9), activestyle="none",
                              yscrollcommand=lb_sb.set, borderwidth=0,
                              highlightthickness=1, highlightbackground="#cccccc")
        lb_sb.config(command=self._lb.yview)
        lb_sb.pack(side="right", fill="y")
        self._lb.pack(fill="both", expand=True, padx=(4, 0), pady=(2, 4))
        self._lb.bind("<<ListboxSelect>>", self._on_list_select)
        self._lb.bind("<MouseWheel>",
            lambda e: self._lb.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        self._lb.bind("<Up>",   lambda e: (self._navigate(-1), "break")[1])
        self._lb.bind("<Down>", lambda e: (self._navigate(1),  "break")[1])

        # ── Rename controls pane ──────────────────────────────────────────────
        ctrl_frame = tk.Frame(self._paned, bg=BG, width=380)
        self._paned.add(ctrl_frame, minsize=320, width=380, stretch="never")
        self._build_rename_controls(ctrl_frame)

        # ── Preview pane — A4 portrait proportions (210:297 ≈ 1:1.414) ───────
        # Width is fixed so the preview always shows a proper A4 page shape.
        # Height drives width: pane_h / 1.414 + padding gives the target width.
        prev_outer = tk.Frame(self._paned, bg="#222222")
        self._paned.add(prev_outer, minsize=380, width=480, stretch="always")

        # Filename bar + zoom controls — dark blue header above image
        self._preview_name_var = tk.StringVar(value="")
        preview_hdr = tk.Frame(prev_outer, bg=HDR_BG)
        preview_hdr.pack(fill="x")
        tk.Label(preview_hdr, textvariable=self._preview_name_var,
                 bg=HDR_BG, fg="white",
                 font=("Segoe UI", 9, "bold"),
                 anchor="w", padx=8, pady=4).pack(side="left", fill="x", expand=True)

        self._zoom_level_var = tk.StringVar(value="Fit")
        tk.Label(preview_hdr, textvariable=self._zoom_level_var,
                 bg=HDR_BG, fg="#aaccff",
                 font=("Segoe UI", 8), padx=4, pady=4).pack(side="right")

        def _zoom_btn(text, cmd, tip):
            b = tk.Button(preview_hdr, text=text, bg="#2a5a8a", fg="white",
                          font=("Segoe UI", 9, "bold"), relief="flat",
                          padx=7, pady=1, cursor="hand2",
                          activebackground="#3a7abb", activeforeground="white",
                          command=cmd)
            b.pack(side="right", padx=2, pady=2)
            _add_tooltip(b, tip)
            return b

        _zoom_btn("Fit",  lambda: self._preview_canvas.zoom_fit(),  "Reset to fit whole image in panel  (double-click image)")
        _zoom_btn("−",    lambda: self._preview_canvas.zoom_out(),  "Zoom out  (mouse wheel down)")
        _zoom_btn("+",    lambda: self._preview_canvas.zoom_in(),   "Zoom in  (mouse wheel up)")

        # Separator
        tk.Frame(preview_hdr, bg="#4a7aaa", width=1).pack(side="right", fill="y", pady=3, padx=2)

        self._open_viewer_btn = tk.Button(preview_hdr, text="Open in Viewer",
                                          bg="#446644", fg="white",
                                          font=("Segoe UI", 9, "bold"), relief="flat",
                                          padx=8, pady=1, cursor="hand2",
                                          activebackground="#558855", activeforeground="white",
                                          state="disabled",
                                          command=self._open_in_viewer)
        self._open_viewer_btn.pack(side="right", padx=2, pady=2)
        _add_tooltip(self._open_viewer_btn, "Open current file in the system PDF viewer")

        # Canvas — ZoomableCanvas handles zoom/pan/fit internally
        from libraries.ft_zoom_canvas import ZoomableCanvas
        self._preview_canvas = ZoomableCanvas(prev_outer, bg="#222222")
        self._preview_canvas.pack(fill="both", expand=True)
        self._preview_canvas.set_level_var(self._zoom_level_var)
        self._preview_canvas.bind("<Double-Button-1>", lambda e: self._preview_canvas.zoom_fit())

    def _build_rename_controls(self, parent):
        """Build the structured rename fields in the controls pane."""
        cats     = self._cats
        who_list = cats.get("who", [])
        cat_dict = cats.get("categories", {})
        cat_names = list(cat_dict.keys())
        today    = _date.today()

        DBG  = BG
        DFLD = "white"
        DFG  = TEXT_BRIGHT
        DDIM = TEXT_DIM
        PADX = 12

        # ── Header ────────────────────────────────────────────────────────────
        hdr = tk.Frame(parent, bg=HDR_BG)
        hdr.pack(fill="x")
        tk.Label(hdr, text="Structured Rename", bg=HDR_BG, fg=HDR_FG,
                 font=("Segoe UI", 10, "bold"), padx=PADX, pady=6).pack(anchor="w")

        # ── From / To display ─────────────────────────────────────────────────
        name_frame = tk.Frame(parent, bg=DBG)
        name_frame.pack(fill="x", padx=PADX, pady=(8, 4))
        tk.Label(name_frame, text="From:", bg=DBG, fg=DDIM,
                 font=("Segoe UI", 8), width=5, anchor="w").grid(row=0, column=0, sticky="w")
        self._from_var = tk.StringVar(value="—")
        tk.Label(name_frame, textvariable=self._from_var, bg=DBG, fg="#aa2200",
                 font=("Segoe UI", 9, "bold"), wraplength=300, justify="left",
                 anchor="w").grid(row=0, column=1, sticky="ew", padx=(4, 0))
        tk.Label(name_frame, text="To:", bg=DBG, fg=DDIM,
                 font=("Segoe UI", 8), width=5, anchor="w").grid(row=1, column=0, sticky="w")
        self._to_var = tk.StringVar(value="—")
        tk.Label(name_frame, textvariable=self._to_var, bg=DBG, fg=GREEN,
                 font=("Segoe UI", 9, "bold"), wraplength=300, justify="left",
                 anchor="w").grid(row=1, column=1, sticky="ew", padx=(4, 0))
        name_frame.columnconfigure(1, weight=1)

        tk.Frame(parent, bg="#cccccc", height=1).pack(fill="x", padx=PADX, pady=(4, 0))

        # ── Scrollable fields area ────────────────────────────────────────────
        fields_outer = tk.Frame(parent, bg=DBG)
        fields_outer.pack(fill="both", expand=True)
        fields_cv = tk.Canvas(fields_outer, bg=DBG, highlightthickness=0)
        fields_sb = tk.Scrollbar(fields_outer, orient="vertical", command=fields_cv.yview)
        fields_cv.configure(yscrollcommand=fields_sb.set)
        fields_sb.pack(side="right", fill="y")
        fields_cv.pack(side="left", fill="both", expand=True)
        fields_fr = tk.Frame(fields_cv, bg=DBG)
        fw = fields_cv.create_window((0, 0), window=fields_fr, anchor="nw")
        fields_fr.bind("<Configure>",
            lambda e: fields_cv.configure(scrollregion=fields_cv.bbox("all")))
        fields_cv.bind("<Configure>",
            lambda e: fields_cv.itemconfig(fw, width=e.width))
        fields_cv.bind("<MouseWheel>",
            lambda e: fields_cv.yview_scroll(-1 if e.delta > 0 else 1, "units"))

        def lbl(text):
            tk.Label(fields_fr, text=text, bg=DBG, fg=DDIM,
                     font=("Segoe UI", 8), anchor="w").pack(
                     fill="x", padx=PADX, pady=(8, 1))

        def combo(var, values, width=28):
            cb = ttk.Combobox(fields_fr, textvariable=var, values=values,
                              font=("Segoe UI", 10), width=width, state="normal")
            cb.pack(anchor="w", padx=PADX, pady=(0, 2))
            return cb

        def entry(var):
            e = tk.Entry(fields_fr, textvariable=var, bg=DFLD, fg=DFG,
                         insertbackground=DFG, font=("Segoe UI", 10),
                         relief="solid", bd=1)
            e.pack(fill="x", padx=PADX, pady=(0, 2))
            return e

        # Date row
        lbl("Date")
        date_frame = tk.Frame(fields_fr, bg=DBG)
        date_frame.pack(fill="x", padx=PADX)
        self._dd_var   = tk.StringVar(value="")
        self._mm_var   = tk.StringVar(value="")
        self._yyyy_var = tk.StringVar(value="")
        for var, label, vals, w in [
            (self._dd_var,   "Day",   [""] + [str(i) for i in range(1, 32)], 4),
            (self._mm_var,   "Month", [""] + [str(i) for i in range(1, 13)], 4),
            (self._yyyy_var, "Year",  [""] + [str(i) for i in range(2000, today.year + 6)], 6),
        ]:
            f = tk.Frame(date_frame, bg=DBG); f.pack(side="left", padx=(0, 8))
            tk.Label(f, text=label, bg=DBG, fg=DDIM, font=("Segoe UI", 7)).pack(anchor="w")
            ttk.Combobox(f, textvariable=var, values=vals,
                         font=("Segoe UI", 9), width=w).pack()
        tk.Button(date_frame, text="Today", bg="#dddddd", fg=DFG,
                  font=("Segoe UI", 8), relief="flat", padx=6, cursor="hand2",
                  command=lambda: (
                      self._dd_var.set(str(today.day)),
                      self._mm_var.set(str(today.month)),
                      self._yyyy_var.set(str(today.year))
                  )).pack(side="left", pady=(14, 0))

        # Who
        lbl("Who")
        self._who_var = tk.StringVar()
        self._who_cb = combo(self._who_var, who_list)

        # Category
        lbl("Category")
        self._cat_var = tk.StringVar()
        self._cat_cb = combo(self._cat_var, cat_names)

        # Type — updated when category changes
        lbl("Type")
        self._typ_var = tk.StringVar()
        self._typ_cb  = combo(self._typ_var, [])

        def _on_cat_change(*_):
            cat      = self._cat_var.get().strip()
            cat_dict = self._cats.get("categories", {})
            types    = cat_dict.get(cat, {}).get("types", [])
            self._typ_cb["values"] = types
            if self._typ_var.get() not in types:
                self._typ_var.set(types[0] if types else "")
            self._update_preview()

        self._cat_var.trace_add("write", _on_cat_change)

        # Description
        lbl("Description")
        self._desc_var = tk.StringVar()
        self._desc_entry = entry(self._desc_var)

        # Prefix (optional override)
        lbl("Prefix (optional — prepended before date)")
        self._prefix_var = tk.StringVar()
        entry(self._prefix_var)

        # Trace all fields for live preview
        for v in (self._dd_var, self._mm_var, self._yyyy_var,
                  self._who_var, self._cat_var, self._typ_var,
                  self._desc_var, self._prefix_var):
            v.trace_add("write", lambda *_: self._update_preview())

        # ── Status ────────────────────────────────────────────────────────────
        self._status_var = tk.StringVar()
        tk.Label(parent, textvariable=self._status_var, bg=DBG, fg="#226644",
                 font=("Segoe UI", 9, "italic"), anchor="w", padx=PADX).pack(
                 fill="x", pady=(4, 0))

        # ── Action buttons ────────────────────────────────────────────────────
        self._btn_frame = tk.Frame(parent, bg=DBG)
        self._btn_frame.pack(fill="x", padx=PADX, pady=(6, 10))
        btn_frame = self._btn_frame

        tk.Button(btn_frame, text="✔  Apply & Next", bg=GREEN, fg="white",
                  font=("Segoe UI", 10, "bold"), relief="flat", padx=12, pady=6,
                  cursor="hand2", command=self._apply_rename).pack(
                  side="left", padx=(0, 6))
        tk.Button(btn_frame, text="→  Skip", bg="#445566", fg="white",
                  font=("Segoe UI", 10, "bold"), relief="flat", padx=12, pady=6,
                  cursor="hand2", command=self._skip).pack(side="left", padx=(0, 6))
        tk.Button(btn_frame, text="↺  Clear", bg="#666666", fg="white",
                  font=("Segoe UI", 9), relief="flat", padx=8, pady=6,
                  cursor="hand2", command=self._clear_fields).pack(side="left")

    # ── Tree methods (same pattern as FTMapimg / FTEditimg) ───────────────────

    def _reload_categories(self):
        """Push loaded category data into the combobox widgets."""
        cats      = self._cats
        who_list  = cats.get("who", [])
        cat_dict  = cats.get("categories", {})
        cat_names = list(cat_dict.keys())
        try:
            self._who_cb["values"] = who_list
            self._cat_cb["values"] = cat_names
        except AttributeError:
            pass  # widgets not yet built

    def _set_initial_sashes(self):
        try:
            self.update_idletasks()
            win_w = self.winfo_width()
            win_h = self.winfo_height()
            preview_h = win_h - 40
            preview_w = int(preview_h * 210 / 297) + 40
            preview_w = max(preview_w, 440)
            # 4 panes: tree(310) | files(300) | controls(380) | preview
            sash0 = 260
            sash1 = 310 + 300
            sash2 = max(sash1 + 380, win_w - preview_w)
            self._paned.sash_place(0, sash0, 0)
            self._paned.sash_place(1, sash1, 0)
            self._paned.sash_place(2, sash2, 0)
        except Exception: pass

    def _browse_root(self):
        folder = fd.askdirectory(parent=self, title="Select root folder")
        if not folder: return
        self._set_tree_root(folder)

    def _set_tree_root(self, folder):
        if not folder or not os.path.isdir(folder): return
        folder = os.path.normpath(folder)
        self._folder_var.set(folder)
        self._populate_tree(folder)

    def _populate_tree(self, root_dir):
        if not self._tree: return
        for item in self._tree.get_children(""): self._tree.delete(item)
        root_dir = os.path.normpath(root_dir)
        name = os.path.basename(root_dir) or root_dir
        tag  = "has_file" if self._folder_has_files(root_dir) else "no_file"
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
            tag = "has_file" if self._folder_has_files(dp) else "no_file"
            self._tree.insert(parent_iid, "end", iid=dp,
                              text=f"  {d.name}", tags=(tag,))
            if self._folder_has_subdirs(dp):
                self._tree.insert(dp, "end", iid=dp + "/__ph__", text="")

    def _folder_has_files(self, path):
        exts = PHOTO_EXTS if self._mode == "photos" else PDF_EXTS
        try:
            return any(os.path.splitext(e.name)[1].lower() in exts
                       for e in os.scandir(path) if e.is_file())
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
            ch  = self._tree.get_children(iid)
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
        if "__ph__" in path or not os.path.isdir(path): return
        exts = PHOTO_EXTS if self._mode == "photos" else PDF_EXTS
        file_list = _scan_folder(path, exts)
        n = len(file_list)
        self._folder_count.config(
            text=f"{n} file{'s' if n!=1 else ''}" if n else "No files in this folder")
        self._file_list     = file_list
        self._file_idx      = 0
        self._filtered_list = None
        self._filter_var.set("")
        self._filter_label.config(text="")
        self._populate_list()
        if file_list:
            self._open_file(file_list[0])

    def _on_mode_change(self):
        self._mode = self._mode_var.get()
        # Refresh tree colours and file list
        root = self._folder_var.get().strip() if self._folder_var else ""
        if root and os.path.isdir(root):
            self._populate_tree(root)
        # Re-scan current folder if tree has selection
        if self._tree:
            sel = self._tree.selection()
            if sel and not "__ph__" in sel[0]:
                self._on_tree_select()

    # ── File list ─────────────────────────────────────────────────────────────

    def _apply_filter(self):
        """Filter the file list to show only files matching the filter terms."""
        raw = self._filter_var.get().strip()
        if not raw:
            self._clear_filter()
            return
        terms  = [t.strip().lower() for t in raw.split(",") if t.strip()]
        mode   = self._filter_mode.get()
        self._filtered_list = []
        for path in self._file_list:
            name = os.path.basename(path).lower()
            hit  = any(t in name for t in terms) if mode == "Any" else all(t in name for t in terms)
            if hit:
                self._filtered_list.append(path)
        n = len(self._filtered_list)
        total = len(self._file_list)
        self._filter_label.config(
            text=f"{n} of {total} files  ({mode}: {', '.join(terms)})",
            fg=ACCENT if n < total else TEXT_DIM)
        self._populate_list()
        if self._filtered_list:
            self._file_idx = 0
            self._lb_select(0)
            self._open_file(self._filtered_list[0])

    def _clear_filter(self):
        """Remove filter and show all files."""
        self._filter_var.set("")
        self._filtered_list = None
        self._filter_label.config(text="")
        self._populate_list()

    def _active_list(self):
        """Return filtered list if active, otherwise full file list."""
        if getattr(self, "_filtered_list", None) is not None:
            return self._filtered_list
        return self._file_list

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
        key = lambda p: os.path.basename(p).lower()
        self._file_list.sort(key=key, reverse=reverse)
        if self._filtered_list is not None:
            self._filtered_list.sort(key=key, reverse=reverse)
        self._populate_list()

    def _populate_list(self):
        self._lb.delete(0, "end")
        for path in self._active_list():
            renamed = path in self._renamed
            marker  = "✓ " if renamed else "  "
            self._lb.insert("end", marker + os.path.basename(path))
        if self._active_list():
            self._lb_select(min(self._file_idx, len(self._active_list()) - 1))

    def _lb_select(self, idx):
        self._lb.selection_clear(0, "end")
        self._lb.selection_set(idx)
        self._lb.activate(idx)
        self._lb.see(idx)

    def _on_list_select(self, event=None):
        sel = self._lb.curselection()
        if not sel: return
        idx = sel[0]
        al  = self._active_list()
        if 0 <= idx < len(al):
            self._file_idx = idx
            self._open_file(al[idx])

    def _navigate(self, direction):
        al      = self._active_list()
        new_idx = self._file_idx + direction
        if 0 <= new_idx < len(al):
            self._file_idx = new_idx
            self._lb_select(new_idx)
            self._open_file(al[new_idx])

    # ── File open / preview ───────────────────────────────────────────────────

    def _open_in_viewer(self):
        path = getattr(self, "_current_path", None)
        if not path or not os.path.isfile(path): return
        try:
            os.startfile(_longpath(path))
        except Exception as e:
            messagebox.showerror("Open failed", str(e), parent=self)

    def _open_file(self, path):
        if not os.path.isfile(path): return
        self._current_path = path
        stem, ext = os.path.splitext(os.path.basename(path))
        self._current_ext = ext

        # Update filename bar and From: label
        self._preview_name_var.set(os.path.basename(path))
        self._from_var.set(os.path.basename(path))

        # Enable/disable Open in Viewer button
        is_pdf = ext.lower() in PDF_EXTS
        try:
            self._open_viewer_btn.config(state="normal" if is_pdf else "disabled")
        except Exception: pass

        # Parse existing stem into fields
        self._parse_stem_into_fields(stem)
        self._update_preview()

        # Load preview image
        self._load_preview(path)

    def _load_preview(self, path):
        """Load image into ZoomableCanvas preview."""
        ext = os.path.splitext(path)[1].lower()
        self._preview_canvas.set_a4_mode(ext in PDF_EXTS)
        self._preview_canvas.load_path(path)

    # ── Rename fields ─────────────────────────────────────────────────────────

    def _parse_stem_into_fields(self, stem):
        cats      = self._cats
        who_list  = cats.get("who", [])
        cat_dict  = cats.get("categories", {})
        cat_names = list(cat_dict.keys())
        today     = _date.today()

        parts = stem.split("_")
        date_str = who = cat = typ = desc = ""

        if parts and re.match(r'^\d{4}-\d{2}-\d{2}$', parts[0]):
            date_str = parts[0]; parts = parts[1:]
        if parts and parts[0] in who_list:
            who = parts[0]; parts = parts[1:]
        if parts and parts[0] in cat_names:
            cat   = parts[0]
            types = cat_dict[cat].get("types", [])
            parts = parts[1:]
            if parts and parts[0] in types:
                typ = parts[0]; parts = parts[1:]
        desc = " ".join(parts).replace("_", " ")

        if date_str:
            try:
                d = _date.fromisoformat(date_str)
                dd, mm, yyyy = str(d.day), str(d.month), str(d.year)
            except Exception:
                dd, mm, yyyy = "", "", ""
        else:
            dd, mm, yyyy = "", "", ""

        self._dd_var.set(dd); self._mm_var.set(mm); self._yyyy_var.set(yyyy)
        self._who_var.set(who)
        self._cat_var.set(cat)
        # Trigger type list update via trace
        types = cat_dict.get(cat, {}).get("types", [])
        self._typ_cb["values"] = types
        self._typ_var.set(typ if typ in types else (types[0] if types else ""))
        self._desc_var.set(desc)
        self._prefix_var.set("")

    def _build_new_stem(self):
        try:
            date_s = f"{int(self._yyyy_var.get()):04d}-{int(self._mm_var.get()):02d}-{int(self._dd_var.get()):02d}"
        except Exception:
            date_s = ""
        parts = [p for p in [
            _clean_stem(self._prefix_var.get(), 20),
            date_s,
            _clean_stem(self._who_var.get(),  20),
            _clean_stem(self._cat_var.get(),  20),
            _clean_stem(self._typ_var.get(),  20),
            _clean_stem(self._desc_var.get(), 80),
        ] if p]
        return "_".join(parts)

    def _update_preview(self, *_):
        stem = self._build_new_stem()
        ext  = getattr(self, "_current_ext", "")
        if stem:
            self._to_var.set(stem + ext)
        else:
            self._to_var.set("(fill in fields to build name)")

    def _clear_fields(self):
        self._dd_var.set("")
        self._mm_var.set("")
        self._yyyy_var.set("")
        self._who_var.set("")
        self._cat_var.set("")
        self._typ_var.set("")
        self._desc_var.set("")
        self._prefix_var.set("")
        self._desc_entry.focus_set()

    # ── Actions ───────────────────────────────────────────────────────────────

    def _apply_rename(self):
        if not hasattr(self, "_current_path") or not self._current_path:
            self._status_var.set("No file selected.")
            return
        new_stem = self._build_new_stem()
        if not new_stem:
            self._status_var.set("⚠  Fill in at least a date to build a name.")
            return

        old_path = self._current_path
        ext      = self._current_ext
        new_name = new_stem + ext
        new_path = os.path.join(os.path.dirname(old_path), new_name)

        if new_path == old_path:
            self._status_var.set("Name unchanged — skipping.")
            self._skip()
            return

        if os.path.exists(new_path):
            if not messagebox.askyesno("File exists",
                    f"A file named '{new_name}' already exists.\n\nOverwrite?",
                    parent=self): return

        try:
            os.rename(_longpath(old_path), _longpath(new_path))
        except Exception as e:
            messagebox.showerror("Rename failed", str(e), parent=self)
            return

        # Update file list in place
        idx = self._file_idx
        self._renamed[old_path] = new_path
        self._file_list[idx]    = new_path
        self._current_path      = new_path
        self._from_var.set(new_name)
        self._status_var.set(f"✓  Renamed: {os.path.basename(old_path)} → {new_name}")
        self._populate_list()
        self._lb_select(idx)

        # Advance to next file
        self._skip()

    def _skip(self):
        if self._file_idx + 1 < len(self._file_list):
            self._navigate(1)
        else:
            self._status_var.set("End of file list.")


# ── Entry point ───────────────────────────────────────────────────────────────

    # ── IPC (embedded mode) ───────────────────────────────────────────────────

    def _get_ipc_dir(self):
        """Return FT_IPC folder path, creating it if needed."""
        import configparser as _cp
        ini = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "FileTagger.ini")
        path = None
        if os.path.exists(ini):
            cfg = _cp.ConfigParser(strict=False)
            cfg.read(ini)
            path = cfg.get("FileTagger", "ipc_folder", fallback="").strip()
        if not path:
            path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "FT_IPC")
        os.makedirs(path, exist_ok=True)
        return path

    def _poll_ipc(self):
        """Poll for FTFiler_request.csv every 500ms."""
        try:
            req_path = os.path.join(self._ipc_dir, "FTFiler_request.csv")
            if os.path.exists(req_path):
                with open(req_path, encoding="utf-8") as f:
                    lines = [l.strip() for l in f if l.strip()]
                seq  = int(lines[0].split(",", 1)[1]) if lines and lines[0].startswith("SEQ,") else 0
                if seq != self._ipc_seq:
                    self._ipc_seq = seq
                    os.remove(req_path)
                    # Parse keywords and file list
                    mode = "RENAME"; root = ""; folder = ""
                    files = []
                    for line in lines[1:]:
                        if line.startswith("MODE,"):    mode   = line.split(",",1)[1]
                        elif line.startswith("ROOT,"):  root   = line.split(",",1)[1]
                        elif line.startswith("FOLDER,"): folder = line.split(",",1)[1]
                        elif os.path.isfile(line):      files.append(line)

                    if mode == "RENAME":
                        self._set_rename_mode(files)
                    else:  # FILEMGMT
                        self._set_filemgmt_mode(root, folder)

                    self.deiconify()
                    self.lift()
                    self.attributes("-topmost", True)
                    self.after(100, lambda: self.attributes("-topmost", False))
                    self.focus_force()
        except Exception as e:
            print(f"FTFiler poll error: {e}")
        self.after(500, self._poll_ipc)

    def _set_rename_mode(self, files):
        """Switch to RENAME mode — hide tree panel only, file list stays visible."""
        self._ipc_mode      = "RENAME"
        self._file_list     = files
        self._file_idx      = 0
        self._filtered_list = None
        self._renamed       = {}
        # Hide tree panel only — file list remains as first pane
        try:
            self._paned.forget(self._tree_outer)
        except Exception: pass
        # Resize window — standalone width minus tree column (310px)
        try:
            self.update_idletasks()
            sw    = self.winfo_screenwidth()
            sh    = self.winfo_screenheight()
            win_h = int(sh * 0.80)
            a4_preview_w = int(win_h * 210 / 297) + 40
            win_w = min(max(300 + 380 + a4_preview_w, 1000), sw - 40)
            x = (sw - win_w) // 2
            y = (sh - win_h) // 2
            self.geometry(f"{win_w}x{win_h}+{x}+{y}")
        except Exception: pass
        self._populate_list()
        if files:
            ext = os.path.splitext(files[0])[1].lower()
            self._mode_var.set("pdfs" if ext in PDF_EXTS else "photos")
            self._mode = "pdfs" if ext in PDF_EXTS else "photos"
            self._open_file(files[0])
        self._show_send_btn()
        self.after(150, self._set_initial_sashes_rename)
        # Reveal window now it's configured correctly
        self.deiconify()
        self.lift()
        self.focus_force()

    def _set_initial_sashes_rename(self):
        """Sash positions for RENAME mode — files(300) | controls(380) | preview."""
        try:
            self.update_idletasks()
            win_w = self.winfo_width()
            win_h = self.winfo_height()
            preview_h = win_h - 40
            preview_w = int(preview_h * 210 / 297) + 40
            preview_w = max(preview_w, 440)
            # 3 panes: files(300) | controls(380) | preview
            sash0 = 300
            sash1 = max(sash0 + 380, win_w - preview_w)
            self._paned.sash_place(0, sash0, 0)
            self._paned.sash_place(1, sash1, 0)
        except Exception: pass

    def _set_filemgmt_mode(self, root, folder):
        """Switch to FILEMGMT mode — full tree, navigate to folder, no Send button."""
        self._ipc_mode = "FILEMGMT"
        self._renamed  = {}
        # Restore tree panel if hidden
        try:
            panes = list(self._paned.panes())
            if str(self._tree_outer) not in panes:
                self._paned.add(self._tree_outer, minsize=200, width=260,
                                stretch="never", before=panes[0])
        except Exception: pass
        if root and os.path.isdir(root):
            self._set_tree_root(root)
            if folder and os.path.isdir(folder):
                self.after(300, lambda: self._navigate_tree_to(folder))
        self._hide_send_btn()
        self.after(150, self._set_initial_sashes)

    def _show_send_btn(self):
        """Show Send to FTDB button in rename controls."""
        try:
            if not hasattr(self, '_send_btn') or not self._send_btn.winfo_exists():
                self._send_btn = tk.Button(
                    self._btn_frame, text="✔  Send to FTDB",
                    bg="#1a6b2a", fg="white",
                    font=("Segoe UI", 10, "bold"), relief="flat",
                    padx=12, pady=6, cursor="hand2",
                    command=self._write_result)
                self._send_btn.pack(side="left", padx=(12, 0))
        except Exception: pass

    def _hide_send_btn(self):
        try:
            if hasattr(self, '_send_btn') and self._send_btn.winfo_exists():
                self._send_btn.pack_forget()
        except Exception: pass

    def _navigate_tree_to(self, folder):
        """Expand tree and select the given folder."""
        folder = os.path.normpath(folder)
        if self._tree and self._tree.exists(folder):
            self._tree.selection_set(folder)
            self._tree.see(folder)
            self._on_tree_select()

    def _write_result(self):
        """Write FTFiler_result.csv with all renames made this session."""
        if not self._embedded: return
        if not self._renamed:
            messagebox.showinfo("Nothing to send",
                "No files have been renamed yet.", parent=self)
            return
        try:
            res_path = os.path.join(self._ipc_dir, "FTFiler_result.csv")
            with open(res_path, "w", encoding="utf-8") as f:
                f.write(f"SEQ,{self._ipc_seq}\n")
                for old, new in self._renamed.items():
                    f.write(f"{old},{new}\n")
            n = len(self._renamed)
            messagebox.showinfo("Sent to FTDB",
                f"{n} rename{'s' if n!=1 else ''} sent to FileTagger.", parent=self)
        except Exception as e:
            messagebox.showerror("Send failed", str(e), parent=self)


def main():
    args = sys.argv[1:]

    if "--rename" in args:
        # Embedded rename mode — start hidden, poll immediately, show only when ready
        app = FTFiler(start_path=None, embedded=True, start_mode="RENAME")
        app.mainloop()
        return

    if "--embedded" in args:
        app = FTFiler(start_path=None, embedded=True)
        app.title("FTFiler — Embedded")
        app.mainloop()
        return

    start_path = args[0] if args else None

    # Standalone default: if Projects.ini exists, open the active project Photos root.
    # If Projects.ini/root is absent, leave the root box empty for manual entry.
    if start_path is None:
        roots = read_project_roots(__file__)
        photos_root = roots.get("photos", "")
        if photos_root and os.path.isdir(photos_root):
            start_path = photos_root

    app = FTFiler(start_path=start_path)
    app.mainloop()

if __name__ == "__main__":
    main()
