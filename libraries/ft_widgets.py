"""
ft_widgets.py  —  Shared widget library
Version: 09:44 AEST 27-Apr-2026
Version: 08:00 AEST 27-Apr-2026
Version: 03:54 AEST 27-Apr-2026 for the FileTagger suite.

Provides:
    FolderTreeWidget   — standardised folder tree with optional data columns
    ZoomableCanvas     — embedded canvas with built-in zoom and pan

Usage:
    from ft_widgets import FolderTreeWidget, ZoomableCanvas

Both widgets have a consistent appearance across all FT apps.
FolderTreeWidget can be subclassed to add columns, right-click menus
and custom colour coding (as in FTDBXnew.py).
"""

import os
import threading
import tkinter as tk
from tkinter import ttk
from tkinter import messagebox

try:
    from PIL import Image, ImageTk, ImageOps, ImageFile
    _PIL = True
except ImportError:
    _PIL = False

try:
    import fitz as _fitz
    _FITZ = True
except ImportError:
    _FITZ = False

PHOTO_EXTS = {'.jpg', '.jpeg'}
PDF_EXTS   = {'.pdf'}

# ── Shared colours and fonts ───────────────────────────────────────────────────

TREE_BG       = "#ffffff"
TREE_FG       = "#111111"
TREE_SEL_BG   = "#1a4a8a"
TREE_SEL_FG   = "#ffffff"
TREE_HDR_BG   = "#1a3a5c"
TREE_HDR_FG   = "#ffffff"
TREE_FONT     = ("Segoe UI", 9)
TREE_FONT_B   = ("Segoe UI", 9, "bold")
TREE_HAS_FILE = "#0055cc"   # folder contains files
TREE_EMPTY    = "#555555"   # folder is empty / context-only folder
TREE_STYLE_ID = "FT.Treeview"
TREE_WIDTH    = 250          # 250 screen px / 1.25 DPI scale
TREE_COL_W    = 44           # 55 screen px / 1.25 DPI scale
TREE_TREE_COL_EXTRA_W = 15           # FileTagger Tree column extra width
TREE_COLS_W   = TREE_COL_W * 3 + TREE_TREE_COL_EXTRA_W
TREE_SCROLL_W = 18           # 22 screen px / 1.25 DPI scale
TREE_PAD_R    = 10           # right padding between last column and scrollbar
TREE_LEFT_W   = 327  # FileTagger folder panel widened by 15px for Tree column


def _longpath(p):
    import sys
    if sys.platform == "win32" and not p.startswith("\\\\?\\"):
        return "\\\\?\\" + os.path.abspath(p)
    return p

def _has_subdirs(path):
    """
    Return True if path has at least one real child directory.

    Symlinked/junction-like entries are ignored so leaf folders do not show
    misleading expand markers in the UI.
    """
    try:
        for entry in os.scandir(_longpath(path)):
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    return True
            except OSError:
                continue
        return False
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# ── FolderTreeWidget ───────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

def _ui_path(p):
    """Return a normal UI/storage path; reserve Windows long-path prefix for disk I/O only."""
    if not p:
        return p
    p = str(p)
    if p.startswith("\\\\?\\"):
        p = p[4:]
    return os.path.normpath(p)


class FolderTreeWidget(tk.Frame):
    """
    Standardised folder tree widget used across all FT apps.

    Appearance matches the helper apps (FTFiler, FTMap, FTImgedit):
    white background, Segoe UI 9pt, blue selection, + expand icons.

    Optional data columns (e.g. file count, tagged count) are added via
    the `columns` parameter:
        columns = [("JPGs", 50), ("Tagged", 55), ("✓", 30)]

    Each column value is set via:
        tree_widget.set_col(node_id, col_index, value)

    Subclassing:
        Override _on_node_open(path)  — called when a node is expanded
        Override _on_select(path)     — called when a node is selected
        Override _populate_root(root) — called when set_root() is called
        Override _make_context_menu(path) — return a tk.Menu or None

    Lazy population:
        By default nodes are populated lazily. A placeholder child "__ph__"
        is inserted so the expand arrow appears. Override _on_node_open(path)
        to replace the placeholder with real children.
    """

    PLACEHOLDER = "__ph__"

    def __init__(self, parent, columns=None, on_select=None,
                 on_delete_folder=None, on_folders_changed=None,
                 show_root_entry=True, bg=None, **kw):
        """
        Parameters
        ----------
        parent             : tk parent widget
        columns            : list of (header_label, width_px[, anchor]) or None
        on_select          : callback(path) when a folder is selected
        on_delete_folder   : callback(path) after a folder is deleted
        on_folders_changed : callback(path) after any folder operation
        show_root_entry    : if True, show a path entry + Browse button above tree
        bg                 : background colour for the frame (default matches parent)
        """
        super().__init__(parent, bg=bg or parent.cget("bg"), **kw)
        self._on_select_cb       = on_select
        self._on_delete_folder   = on_delete_folder
        self._on_folders_changed = on_folders_changed
        self._col_defs           = columns or []
        self._show_root_entry    = show_root_entry
        self._root_path          = ""
        self._build()

    # ── Build ──────────────────────────────────────────────────────────────────

    def _build(self):
        bg = self.cget("bg")

        if self._show_root_entry:
            self._build_root_entry(bg)

        self._build_tree(bg)

    def _build_root_entry(self, bg):
        """Path entry bar above the tree."""
        import tkinter.filedialog as fd
        bar = tk.Frame(self, bg=bg)
        bar.pack(fill="x", padx=4, pady=(6, 2))
        tk.Label(bar, text="Root folder", bg=bg, fg="#888888",
                 font=("Segoe UI", 8, "bold")).pack(anchor="w")
        row = tk.Frame(bar, bg=bg)
        row.pack(fill="x")
        self._root_var = tk.StringVar()
        self._root_entry = tk.Entry(row, textvariable=self._root_var,
                                    bg="white", fg="#111111",
                                    font=TREE_FONT, relief="solid", bd=1)
        self._root_entry.pack(side="left", fill="none", expand=False)
        self._root_entry.config(width=40)
        self._root_entry.bind("<Return>",
            lambda e: self.set_root(self._root_var.get().strip()))
        tk.Button(row, text="…", bg="#1a3a5c", fg="white",
                  font=("Segoe UI", 9, "bold"), relief="flat",
                  padx=6, cursor="hand2",
                  command=lambda: self._browse(fd)).pack(
                      side="left", padx=(2, 0))
        self._root_count = tk.Label(bar, text="Select a root folder",
                                    bg=bg, fg="#888888",
                                    font=("Segoe UI", 7), anchor="w")
        self._root_count.pack(anchor="w", pady=(2, 0))
        tk.Frame(self, bg="#aaaaaa", height=1).pack(fill="x", pady=(4, 0))

    def _browse(self, fd):
        folder = fd.askdirectory(title="Select root folder",
                                 initialdir=self._root_path or os.path.expanduser("~"))
        if folder:
            self.set_root(os.path.normpath(folder))

    def _build_tree(self, bg):
        """Build the ttk.Treeview with optional columns and manual header row."""
        self._apply_style()

        import tkinter.font as tkfont
        hfont = tkfont.Font(font=TREE_FONT_B)

        # ── Calculate column widths ────────────────────────────────────────────
        self._actual_cols_w = 0
        col_widths = []
        for col_def in self._col_defs:
            label  = col_def[0]
            width  = col_def[1]
            anchor = col_def[2] if len(col_def) > 2 else "center"
            text_w = hfont.measure(label) + 16
            width  = max(width, text_w)
            col_widths.append((label, width, anchor))
            self._actual_cols_w += width

        # Include pad column in col_ids from the start so treeview never reconfigures
        col_ids = [f"col{i}" for i in range(len(self._col_defs))]
        all_col_ids = col_ids + ["colpad"] if self._col_defs else col_ids

        # ── Outer border frame ─────────────────────────────────────────────────
        # ── Header row frame — packed first so it appears above tree ─────────
        hdr_frame = None
        if self._col_defs:
            hdr_frame = tk.Frame(self, bg=TREE_HDR_BG, height=20)
            hdr_frame.pack(fill="x", side="top")
            hdr_frame.pack_propagate(False)

        # ── Treeview + scrollbar ───────────────────────────────────────────────
        tree_frame = tk.Frame(self, bg=bg)
        # Force tree_frame to exact width — eliminates grey gap
        total_w = TREE_WIDTH + self._actual_cols_w + TREE_SCROLL_W + TREE_PAD_R + 45
        tree_frame.pack(fill="y", side="left")
        tree_frame.pack_propagate(False)
        tree_frame.config(width=total_w)

        vsb = ttk.Scrollbar(tree_frame, orient="vertical")
        vsb.pack(side="right", fill="y")

        self._tree = ttk.Treeview(tree_frame,
                                   style=TREE_STYLE_ID,
                                   columns=all_col_ids,
                                   show="tree",
                                   selectmode="browse",
                                   yscrollcommand=vsb.set)
        vsb.config(command=self._tree.yview)

        # Column widths — ALL stretch=False, width=minwidth for rigid layout
        self._tree.column("#0", width=TREE_WIDTH, minwidth=TREE_WIDTH, stretch=False)
        for i, (label, width, anchor) in enumerate(col_widths):
            self._tree.column(f"col{i}", width=width, minwidth=width, stretch=False, anchor=anchor)
        if self._col_defs:
            self._tree.column("colpad", width=TREE_PAD_R, minwidth=TREE_PAD_R, stretch=False)

        self._tree.pack(fill="both", expand=True)

        # ── Header labels — x position = TREE_WIDTH exactly ──────────────────
        if hdr_frame:
            def _place_headers(event=None):
                x = TREE_WIDTH
                for lbl, (label, width, anchor) in zip(hdr_frame.winfo_children(), col_widths):
                    lbl.place(x=x, y=0, width=width, height=20)
                    x += width

            for label, width, anchor in col_widths:
                tk.Label(hdr_frame, text=label,
                         bg=TREE_HDR_BG, fg=TREE_HDR_FG,
                         font=TREE_FONT_B, anchor="center",
                         bd=0, highlightthickness=0)
            _place_headers()
            hdr_frame.bind("<Configure>", _place_headers)

        # Tags for colour coding
        self._tree.tag_configure("has_files", foreground=TREE_HAS_FILE,
                                  font=TREE_FONT_B)
        self._tree.tag_configure("empty",     foreground=TREE_EMPTY,
                                  font=TREE_FONT)
        self._tree.tag_configure("current",   foreground=TREE_SEL_FG,
                                  background=TREE_SEL_BG, font=TREE_FONT_B)

        # Bindings
        self._tree.bind("<<TreeviewSelect>>",
                         lambda e: self._handle_select())
        self._tree.bind("<<TreeviewOpen>>",
                         lambda e: self.after(1, self._handle_open))
        self._tree.bind("<MouseWheel>",
                         lambda e: self._tree.yview_scroll(
                             -1 if e.delta > 0 else 1, "units"))
        self._tree.bind("<Button-3>",
                         lambda e: self._handle_right_click(e))

    def _apply_style(self):
        """Apply consistent FT style to the Treeview."""
        sty = ttk.Style()
        # "vista" is Windows-only; fall back gracefully on other platforms.
        try:
            if "vista" in sty.theme_names():
                sty.theme_use("vista")
        except Exception:
            pass
        sty.configure(TREE_STYLE_ID,
                       background=TREE_BG,
                       foreground=TREE_FG,
                       fieldbackground=TREE_BG,
                       font=TREE_FONT,
                       rowheight=22)
        sty.map(TREE_STYLE_ID,
                background=[("selected", TREE_SEL_BG)],
                foreground=[("selected", TREE_SEL_FG)])
        sty.configure(f"{TREE_STYLE_ID}.Heading",
                       background=TREE_HDR_BG,
                       foreground=TREE_HDR_FG,
                       font=TREE_FONT_B,
                       relief="raised")

    # ── Public API ─────────────────────────────────────────────────────────────

    def set_root(self, path):
        """Set the root folder and populate the tree."""
        if not os.path.isdir(path):
            if self._show_root_entry and hasattr(self, '_root_count'):
                self._root_count.config(text="Folder not found or unavailable")
            if hasattr(self, '_root_var'):
                self._root_var.set(path)
            self._tree.delete(*self._tree.get_children())
            return
        self._root_path = path
        if self._show_root_entry and hasattr(self, '_root_var'):
            self._root_var.set(path)
        self._populate_root(path)

    def navigate_to(self, path):
        """Select and reveal a folder path in the tree."""
        path = os.path.normpath(path)
        if self._tree.exists(path):
            self._tree.selection_set(path)
            self._tree.see(path)
        else:
            # Try to expand ancestors until path is visible
            parts = []
            p = path
            while p and p != os.path.dirname(p):
                parts.insert(0, p)
                p = os.path.dirname(p)
            for part in parts:
                if self._tree.exists(part):
                    self._tree.item(part, open=True)
                    self._ensure_children(part)
            if self._tree.exists(path):
                self._tree.selection_set(path)
                self._tree.see(path)

    def set_col(self, node_id, col_index, value):
        """Set a data column value on a tree node."""
        if not self._tree.exists(node_id): return
        self._tree.set(node_id, f"col{col_index}", value)

    def get_selected_path(self):
        """Return the currently selected folder path or empty string."""
        sel = self._tree.selection()
        if not sel: return ""
        path = sel[0]
        if self.PLACEHOLDER in path: return ""
        return path

    def tag_node(self, node_id, *tags):
        """Apply tags to a node."""
        if not self._tree.exists(node_id): return
        self._tree.item(node_id, tags=tags)

    def refresh_node(self, node_id):
        """Re-expand a node — removes placeholder and repopulates."""
        if not self._tree.exists(node_id): return
        for child in self._tree.get_children(node_id):
            self._tree.delete(child)
        self._on_node_open(node_id)

    def tree(self):
        """Return the underlying ttk.Treeview widget."""
        return self._tree

    def actual_width(self):
        """Return the actual required panel width after heading-based column sizing."""
        cols_w = getattr(self, '_actual_cols_w', len(self._col_defs) * TREE_COL_W)
        return TREE_WIDTH + cols_w + TREE_SCROLL_W + TREE_PAD_R + 45 + 6  # +6 for border

    # ── Lazy population ────────────────────────────────────────────────────────

    def _populate_root(self, path):
        """Populate full tree from root — all levels shown, root open."""
        self._tree.delete(*self._tree.get_children())
        root_label = os.path.basename(path) or path
        self._tree.insert("", "end", iid=path, text=f"  {root_label}", open=True)
        self._populate_level(path)
        if self._show_root_entry and hasattr(self, '_root_count'):
            try:
                n = len([e for e in os.scandir(_longpath(path)) if e.is_dir()])
                self._root_count.config(
                    text=f"{n} subfolder{'s' if n!=1 else ''}")
            except Exception:
                pass

    def _populate_level(self, path):
        """Insert immediate children of path. Each gets a placeholder — lazy expansion."""
        try:
            entries = sorted(
                [e for e in os.scandir(_longpath(path)) if e.is_dir()],
                key=lambda e: e.name.lower()
            )
            for entry in entries:
                self._add_node(path, entry.path, entry.name)
        except Exception:
            pass

    def _add_node(self, parent, path, label, tags=()):
        """Insert a node with placeholder so expand arrow always appears."""
        if self._tree.exists(path): return
        self._tree.insert(parent, "end", iid=path,
                           text=f"  {label}", open=False, tags=tags)
        if _has_subdirs(path):
            self._tree.insert(path, "end",
                              iid=f"{path}{self.PLACEHOLDER}", text="")

    def _on_node_open(self, path):
        """User expanded a node — replace placeholder with real children."""
        for child in self._tree.get_children(path):
            if self.PLACEHOLDER in child:
                self._tree.delete(child)
        self._populate_level(path)

    def _ensure_children(self, path):
        children = self._tree.get_children(path)
        if len(children) == 1 and self.PLACEHOLDER in children[0]:
            self._on_node_open(path)

    def collapse_node(self, path):
        """Collapse a node — remove children, insert placeholder, close visually."""
        try:
            for child in self._tree.get_children(path):
                self._tree.delete(child)
            # Re-insert placeholder so expand arrow remains
            if _has_subdirs(path):
                self._tree.insert(path, "end",
                                  iid=f"{path}{self.PLACEHOLDER}", text="")
            self._tree.item(path, open=False)
        except Exception:
            pass

    # ── Event handlers ─────────────────────────────────────────────────────────

    def _handle_select(self):
        path = self.get_selected_path()
        if not path: return
        if self._on_select_cb:
            self._on_select_cb(path)
        self._on_select(path)

    def _handle_open(self):
        sel = self._tree.focus()
        if sel and self.PLACEHOLDER not in sel:
            self._on_node_open(sel)

    def _handle_right_click(self, event):
        # Normalise selected tree path before building menu.
        item = self._tree.identify_row(event.y)
        if not item or self.PLACEHOLDER in item: return
        self._tree.selection_set(item)
        menu = self._make_context_menu(item)
        if menu:
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()

    # ── Override hooks ─────────────────────────────────────────────────────────

    def _on_select(self, path):
        """Override to handle folder selection."""
        pass

    def _make_context_menu(self, path):
        path = _ui_path(path)
        """Right-click context menu — New Subfolders, Rename, Delete.

        FileTagger can additionally provide _remove_thumbs_for_folder(path);
        when present, show a thumbnail-cache cleanup action here.
        """
        menu = tk.Menu(self._tree, tearoff=0)

        # Run modal dialogs after the popup menu has fully closed.
        # On Windows, calling simpledialog/messagebox directly from a context
        # menu command can leave the menu grab active, making OK/Cancel appear
        # unresponsive and trapping the user behind the modal dialog.
        def _after_menu(fn, *args):
            self.after(50, lambda: fn(*args))

        menu.add_command(label="📁  New subfolders here…",
                         command=lambda p=path: _after_menu(self._dlg_new_subfolders, p))
        menu.add_command(label="✏  Rename this folder…",
                         command=lambda p=path: _after_menu(self._dlg_rename_folder, p))

        # Optional FileTagger hook: remove cached thumbnails for this folder.
        try:
            app = self.winfo_toplevel()
            handler = getattr(app, "_remove_thumbs_for_folder", None)
            # In FileTagger, the Tk toplevel is not the FileTagger instance, so also
            # allow the app object to be attached explicitly by FT.py.
            if handler is None:
                handler = getattr(getattr(self, "_ft_app", None), "_remove_thumbs_for_folder", None)
            if callable(handler):
                menu.add_separator()
                menu.add_command(label="🧹  Remove thumbnails for this folder",
                                 command=lambda p=path, h=handler: _after_menu(h, p))
        except Exception:
            pass

        menu.add_separator()
        menu.add_command(label="🗑  Delete this folder…",
                         command=lambda p=path: _after_menu(self._dlg_delete_folder, p))
        return menu

    # ── Folder operations ──────────────────────────────────────────────────────

    def _sanitise_name(self, name):
        name = name.strip()
        for ch in r'\/:*?"<>|':
            name = name.replace(ch, '_')
        return name

    def _toast(self, text, bg="#226622"):
        """Show a brief floating confirmation message."""
        root = self.winfo_toplevel()
        root.update_idletasks()
        x = root.winfo_rootx() + root.winfo_width()  // 2 - 180
        y = root.winfo_rooty() + root.winfo_height() // 2 - 40
        top = tk.Toplevel(root)
        top.overrideredirect(True)
        top.geometry(f"+{x}+{y}")
        top.configure(bg=bg)
        tk.Label(top, text=text, bg=bg, fg="white",
                 font=("Segoe UI", 10, "bold"), padx=16, pady=10).pack()
        top.after(1800, top.destroy)

    def _dlg_new_subfolders(self, parent_path):
        """Dialog to create up to 5 subfolders at once."""
        parent_path = _ui_path(parent_path)
        root = self.winfo_toplevel()

        dlg = tk.Toplevel(root)
        dlg.title("New Subfolders")
        dlg.configure(bg="#f0f0f0")
        dlg.resizable(False, False)
        dlg.transient(root)
        dlg.grab_set()

        tk.Label(
            dlg,
            text=f"Create subfolders inside:\n{parent_path}",
            bg="#f0f0f0",
            fg="#111111",
            font=("Segoe UI", 9),
            justify="left",
        ).pack(anchor="w", padx=16, pady=(14, 6))

        tk.Label(
            dlg,
            text="Enter up to 5 folder names (leave blank to skip):",
            bg="#f0f0f0",
            fg="#555555",
            font=("Segoe UI", 8),
        ).pack(anchor="w", padx=16, pady=(0, 8))

        entries = []
        first_entry = None
        for i in range(5):
            row = tk.Frame(dlg, bg="#f0f0f0")
            row.pack(fill="x", padx=16, pady=2)

            tk.Label(
                row,
                text=f"{i + 1}.",
                bg="#f0f0f0",
                fg="#333",
                font=("Segoe UI", 9),
                width=2,
            ).pack(side="left")

            var = tk.StringVar()
            ent = tk.Entry(
                row,
                textvariable=var,
                font=("Segoe UI", 9),
                width=36,
                relief="solid",
                bd=1,
            )
            ent.pack(side="left", padx=(4, 0))
            ent.bind("<Return>", lambda e: _do_create())
            entries.append(var)
            if first_entry is None:
                first_entry = ent

        btn_row = tk.Frame(dlg, bg="#f0f0f0")
        btn_row.pack(fill="x", padx=16, pady=(12, 14))

        def _refresh_parent_node():
            try:
                handler = getattr(self, "refresh_after_folder_created", None)
                if callable(handler):
                    handler(parent_path)
                elif self._tree.exists(parent_path):
                    self.refresh_node(parent_path)
            except Exception:
                pass
        def _do_create():
            names = [self._sanitise_name(v.get()) for v in entries]
            names = [n for n in names if n]

            if not names:
                messagebox.showwarning(
                    "No names",
                    "Enter at least one folder name.",
                    parent=dlg,
                )
                return

            created = 0
            errors = []

            for name in names:
                new_path = _ui_path(os.path.join(parent_path, name))
                try:
                    os.makedirs(_longpath(new_path), exist_ok=False)
                    created += 1
                except FileExistsError:
                    errors.append(f"'{name}' already exists")
                except Exception as e:
                    errors.append(f"'{name}': {e}")

            # Keep the visible shared tree in sync, then notify the owner app.
            if created:
                _refresh_parent_node()

            if self._on_folders_changed:
                try:
                    self._on_folders_changed(parent_path)
                except Exception:
                    pass

            dlg.grab_release()
            dlg.destroy()

            if errors:
                messagebox.showwarning("Some errors", "\n".join(errors), parent=root)

            if created:
                self._toast(f"✔  Created {created} folder{'s' if created != 1 else ''}")

        tk.Button(
            btn_row,
            text="Create",
            bg="#1a6b2a",
            fg="white",
            font=("Segoe UI", 9, "bold"),
            relief="flat",
            padx=14,
            pady=4,
            cursor="hand2",
            command=_do_create,
        ).pack(side="left")

        tk.Button(
            btn_row,
            text="Cancel",
            bg="#888888",
            fg="white",
            font=("Segoe UI", 9),
            relief="flat",
            padx=10,
            pady=4,
            cursor="hand2",
            command=lambda: (dlg.grab_release(), dlg.destroy()),
        ).pack(side="left", padx=(8, 0))

        dlg.protocol("WM_DELETE_WINDOW", lambda: (dlg.grab_release(), dlg.destroy()))

        # Let tkinter size the dialog to fit its contents, then centre it.
        dlg.update_idletasks()
        w = dlg.winfo_reqwidth()
        h = dlg.winfo_reqheight()
        x = root.winfo_rootx() + (root.winfo_width() - w) // 2
        y = root.winfo_rooty() + (root.winfo_height() - h) // 2
        dlg.geometry(f"+{x}+{y}")

        if first_entry is not None:
            dlg.after(100, lambda: (first_entry.focus_set(), dlg.focus_force()))

    def _dlg_rename_folder(self, folder_path):
        """Rename a folder using a non-blocking custom dialog.

        Avoid tkinter.simpledialog.askstring(), grab_set() and wait_window().
        On Windows, when launched from a Treeview right-click popup menu, the
        popup menu grab can make a modal simpledialog's OK/Cancel buttons seem
        dead. This dialog is deliberately modeless so the app can never be
        trapped behind it.
        """
        folder_path = _ui_path(folder_path)
        root = self.winfo_toplevel()
        old_name = os.path.basename(folder_path)

        try:
            old_dlg = getattr(self, "_rename_folder_dialog", None)
            if old_dlg is not None and old_dlg.winfo_exists():
                old_dlg.destroy()
        except Exception:
            pass

        dlg = tk.Toplevel(root)
        self._rename_folder_dialog = dlg
        dlg.title("Rename Folder")
        dlg.configure(bg="#f0f0f0")
        dlg.resizable(False, False)
        dlg.transient(root)

        tk.Label(
            dlg,
            text=f"Rename folder:\n{folder_path}",
            bg="#f0f0f0", fg="#111111",
            font=("Segoe UI", 9), justify="left", anchor="w"
        ).pack(fill="x", padx=16, pady=(14, 8))

        name_var = tk.StringVar(value=old_name)
        entry = tk.Entry(dlg, textvariable=name_var, font=("Segoe UI", 10),
                         width=42, relief="solid", bd=1)
        entry.pack(fill="x", padx=16, pady=(0, 12))

        btn_row = tk.Frame(dlg, bg="#f0f0f0")
        btn_row.pack(fill="x", padx=16, pady=(0, 14))

        def _close():
            try:
                dlg.destroy()
            except Exception:
                pass

        def _do_rename(event=None):
            new_name = self._sanitise_name(name_var.get())
            if not new_name:
                messagebox.showwarning("No name", "Enter a folder name.", parent=dlg)
                return "break"
            if new_name == old_name:
                _close()
                return "break"

            new_path = os.path.join(os.path.dirname(folder_path), new_name)
            try:
                os.rename(_longpath(folder_path), _longpath(new_path))

                parent = self._tree.parent(folder_path) if self._tree.exists(folder_path) else ""
                refresh_target = parent if parent else os.path.dirname(new_path)
                try:
                    if refresh_target:
                        self.refresh_node(refresh_target)
                    else:
                        self.set_root(new_path)
                except Exception:
                    pass

                try:
                    if self._tree.exists(new_path):
                        self._tree.selection_set(new_path)
                        self._tree.focus(new_path)
                        self._tree.see(new_path)
                except Exception:
                    pass

                _close()
                self._toast(f"Renamed: {old_name} -> {new_name}", bg="#224466")
                if self._on_folders_changed:
                    self._on_folders_changed(new_path)
            except Exception as e:
                messagebox.showerror("Rename failed", str(e), parent=dlg)
            return "break"

        tk.Button(btn_row, text="OK", bg="#1a6b2a", fg="white",
                  font=("Segoe UI", 9, "bold"), relief="flat",
                  padx=18, pady=4, cursor="hand2",
                  command=_do_rename).pack(side="left")
        tk.Button(btn_row, text="Cancel", bg="#888888", fg="white",
                  font=("Segoe UI", 9), relief="flat",
                  padx=12, pady=4, cursor="hand2",
                  command=_close).pack(side="left", padx=(8, 0))

        dlg.bind("<Return>", _do_rename)
        dlg.bind("<Escape>", lambda e: (_close(), "break"))
        dlg.protocol("WM_DELETE_WINDOW", _close)

        root.update_idletasks()
        w, h = 460, 170
        x = root.winfo_rootx() + (root.winfo_width() - w) // 2
        y = root.winfo_rooty() + (root.winfo_height() - h) // 2
        dlg.geometry(f"{w}x{h}+{x}+{y}")

        def _focus_entry():
            try:
                dlg.lift(root)
                entry.focus_force()
                entry.selection_range(0, "end")
            except Exception:
                pass
        dlg.after_idle(_focus_entry)

    def _dlg_delete_folder(self, folder_path):
        """Delete a folder after confirmation.

        UI shows normal paths only.  Disk operations use _longpath().
        _on_folders_changed is called compatibly for callbacks that accept
        either no arguments or the changed parent folder.
        """
        folder_path = _ui_path(folder_path)
        if not folder_path or not os.path.isdir(_longpath(folder_path)):
            return

        if not messagebox.askyesno(
            "Delete Folder",
            f"Permanently delete:\n\n{folder_path}\n\nThis cannot be undone.",
            parent=self
        ):
            return

        parent = _ui_path(os.path.dirname(folder_path))

        try:
            import shutil
            shutil.rmtree(_longpath(folder_path))
        except Exception as e:
            messagebox.showerror("Delete Folder", f"Could not delete folder:\n\n{e}", parent=self)
            return

        # Keep the visible shared tree in sync, then notify owner apps for any
        # app-specific cleanup such as current-folder state or databases.
        try:
            handler = getattr(self, "refresh_after_folder_deleted", None)
            if callable(handler):
                handler(folder_path)
            elif self._tree.exists(folder_path):
                self._tree.delete(folder_path)
                if parent and self._tree.exists(parent):
                    self.refresh_node(parent)
        except Exception:
            pass

        delete_cb = getattr(self, "_on_delete_folder", None)
        if delete_cb:
            try:
                delete_cb(folder_path)
            except TypeError:
                try:
                    delete_cb()
                except Exception:
                    pass
            except Exception:
                pass
        # Notify owner.  Some apps use callbacks with no args; some accept parent.
        cb = getattr(self, "_on_folders_changed", None)
        if cb:
            try:
                cb(parent)
            except TypeError:
                try:
                    cb()
                except Exception:
                    pass
            except Exception:
                pass

class ZoomableCanvas(tk.Canvas):
    """
    A tk.Canvas subclass with built-in zoom and pan.

    All zoom/pan state is encapsulated. The caller only needs to call
    load_pil() or load_path() — everything else is handled internally.

    Controls:
        Mouse wheel       — zoom in/out centred on cursor
        Click-drag        — pan
        Double-click      — reset to fit
    """

    def __init__(self, parent, a4_mode=False, bg="#222222", **kwargs):
        super().__init__(parent, bg=bg, highlightthickness=0,
                         cursor="hand2", **kwargs)
        self._img       = None
        self._photo     = None
        self._scale     = None
        self._offset    = [0, 0]
        self._drag      = None
        self._a4_mode   = a4_mode
        self._level_var = None

        self.bind("<Configure>",       self._on_configure)
        self.bind("<MouseWheel>",       self._on_wheel)
        self.bind("<ButtonPress-1>",    self._on_press)
        self.bind("<B1-Motion>",        self._on_drag)
        self.bind("<ButtonRelease-1>",  self._on_release)
        self.bind("<Double-Button-1>",  lambda e: self.zoom_fit())

    # ── Public API ─────────────────────────────────────────────────────────────

    def load_pil(self, img):
        """Load a PIL Image. Resets zoom to fit."""
        self._img    = img
        self._scale  = None
        self._offset = [0, 0]
        self._redraw()

    def load_path(self, path):
        """Load image from file path — auto-detects JPEG or PDF. Resets zoom."""
        ext = os.path.splitext(path)[1].lower()
        img = None
        try:
            if ext in PHOTO_EXTS and _PIL:
                ImageFile.LOAD_TRUNCATED_IMAGES = True
                img = Image.open(_longpath(path))
                try:   img = ImageOps.exif_transpose(img)
                except Exception: pass
                img.load()
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGB")
                img.thumbnail((3000, 3000), Image.LANCZOS)
                ImageFile.LOAD_TRUNCATED_IMAGES = False
            elif ext in PDF_EXTS and _FITZ:
                doc  = _fitz.open(_longpath(path))
                page = doc[0]
                mat  = _fitz.Matrix(3.0, 3.0)
                pix  = page.get_pixmap(matrix=mat, alpha=False)
                img  = Image.frombytes("RGB", [pix.width, pix.height],
                                        pix.samples)
                doc.close()
        except Exception as e:
            print(f"ZoomableCanvas.load_path error: {e}")
        self.load_pil(img)

    def clear(self):
        """Clear the canvas."""
        self._img   = None
        self._photo = None
        self.delete("all")
        self._update_level_var()

    def set_level_var(self, var):
        """Connect an external tk.StringVar to display the current zoom level."""
        self._level_var = var
        self._update_level_var()

    def set_a4_mode(self, enabled):
        """Constrain image to A4 portrait ratio (for PDF preview)."""
        self._a4_mode = enabled
        self._redraw()

    def zoom_fit(self):
        self._scale  = None
        self._offset = [0, 0]
        self._redraw()

    def zoom_in(self):
        self._scale = min((self._scale or self._fit_scale()) * 1.25, 16.0)
        self._redraw()

    def zoom_out(self):
        self._scale = max((self._scale or self._fit_scale()) * 0.8, 0.05)
        self._redraw()

    # ── Internal ───────────────────────────────────────────────────────────────

    def _fit_scale(self):
        if not self._img: return 1.0
        cw = self.winfo_width()  or 480
        ch = self.winfo_height() or 600
        iw, ih = self._img.size
        if self._a4_mode:
            PAD  = 20
            a4_w = cw - PAD * 2
            a4_h = int(a4_w * 297 / 210)
            if a4_h > ch - PAD * 2:
                a4_h = ch - PAD * 2
                a4_w = int(a4_h * 210 / 297)
            return min(a4_w / iw, a4_h / ih)
        return min(cw / iw, ch / ih)

    def _redraw(self, event=None):
        self.delete("all")
        cw = self.winfo_width()  or 480
        ch = self.winfo_height() or 600
        if not self._img:
            self.create_text(cw // 2, ch // 2,
                             text="No preview", fill="#666666",
                             font=("Segoe UI", 14))
            self._update_level_var()
            return
        scale = self._scale if self._scale else self._fit_scale()
        iw, ih = self._img.size
        dw = max(1, int(iw * scale))
        dh = max(1, int(ih * scale))
        ox = max(-(dw - 40), min(cw - 40, self._offset[0]))
        oy = max(-(dh - 40), min(ch - 40, self._offset[1]))
        self._offset = [ox, oy]
        x = (cw - dw) // 2 + ox
        y = (ch - dh) // 2 + oy
        try:
            img = self._img.resize((dw, dh), Image.LANCZOS)
            self._photo = ImageTk.PhotoImage(img)
            self.create_image(x, y, anchor="nw", image=self._photo)
        except Exception as e:
            self.create_text(cw // 2, ch // 2,
                             text=f"Cannot render image\n{e}",
                             fill="#ff6666", font=("Segoe UI", 11),
                             justify="center", width=cw - 40)
        self._update_level_var()

    def _update_level_var(self):
        if self._level_var is None: return
        if self._scale is None:     self._level_var.set("Fit")
        elif self._img:             self._level_var.set(f"{int(self._scale * 100)}%")
        else:                        self._level_var.set("")

    def _on_configure(self, event=None): self._redraw()

    def _on_wheel(self, event):
        current = self._scale or self._fit_scale()
        factor  = 1.15 if event.delta > 0 else (1 / 1.15)
        new_s   = max(0.05, min(16.0, current * factor))
        cw = self.winfo_width(); ch = self.winfo_height()
        mx = event.x - cw // 2 - self._offset[0]
        my = event.y - ch // 2 - self._offset[1]
        ratio = new_s / current
        self._offset[0] = int(self._offset[0] - mx * (ratio - 1))
        self._offset[1] = int(self._offset[1] - my * (ratio - 1))
        self._scale = new_s
        self._redraw()

    def _on_press(self, event):
        self._drag = (event.x, event.y, self._offset[0], self._offset[1])
        self.config(cursor="fleur")

    def _on_drag(self, event):
        if not self._drag: return
        sx, sy, ox, oy = self._drag
        self._offset[0] = ox + event.x - sx
        self._offset[1] = oy + event.y - sy
        self._redraw()

    def _on_release(self, event):
        self._drag = None
        self.config(cursor="hand2")


# ═══════════════════════════════════════════════════════════════════════════════
# ── FTFilerTree — FolderTreeWidget subclass for FTFiler ────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

class FTFilerTree(FolderTreeWidget):
    """
    FolderTreeWidget subclass for FTFiler.
    Colours folders blue if they contain files matching the current mode
    (photos or PDFs), grey otherwise.
    """

    PHOTO_EXTS_LOCAL = {'.jpg', '.jpeg', '.png', '.tif', '.tiff', '.bmp', '.webp'}
    PDF_EXTS_LOCAL   = {'.pdf'}

    def __init__(self, parent, mode="photos", on_select=None, **kw):
        self._file_mode = mode   # "photos" or "pdfs"
        super().__init__(parent, on_select=on_select,
                         show_root_entry=True, **kw)
        # Additional tags
        self._tree.tag_configure("has_file", foreground="#0055cc",
                                  font=TREE_FONT_B)
        self._tree.tag_configure("no_file",  foreground="#888888",
                                  font=TREE_FONT)

    def set_mode(self, mode):
        """Switch between 'photos' and 'pdfs' — refreshes tree colours."""
        self._file_mode = mode
        root = self._root_path
        if root:
            self.set_root(root)

    def _folder_has_files(self, path):
        exts = (self.PHOTO_EXTS_LOCAL if self._file_mode == "photos"
                else self.PDF_EXTS_LOCAL)
        try:
            return any(os.path.splitext(e.name)[1].lower() in exts
                       for e in os.scandir(_longpath(path)) if e.is_file())
        except Exception:
            return False

    def _add_node(self, parent, path, label, tags=()):
        """Override — insert with placeholder, colour applied lazily on expand."""
        if self._tree.exists(path): return
        self._tree.insert(parent, "end", iid=path,
                           text=f"  {label}", open=False, tags=("no_file",))
        if _has_subdirs(path):
            self._tree.insert(path, "end",
                              iid=f"{path}{self.PLACEHOLDER}", text="")

    def _on_node_open(self, path):
        """Expand node — replace placeholder, check files, populate children."""
        for child in self._tree.get_children(path):
            if self.PLACEHOLDER in child:
                self._tree.delete(child)
        # Apply colour tag now that we're actually opening this folder
        tag = "has_file" if self._folder_has_files(path) else "no_file"
        self._tree.item(path, tags=(tag,))
        self._populate_level(path)


# ═══════════════════════════════════════════════════════════════════════════════
# ── FileCountTree — FolderTreeWidget subclass with own/children file counts ────
# ═══════════════════════════════════════════════════════════════════════════════

class FileCountTree(FolderTreeWidget):
    """
    FolderTreeWidget subclass that shows two count columns:
        Own/Files — files matching extensions directly in the folder
        Tree      — files matching extensions in all descendant folders

    The direct Files count is filled synchronously because it only scans one
    folder.  Recursive Tree counts are filled asynchronously so large photo
    trees do not block the first display.

    Tree counts are shown in parentheses, e.g. (123). Zero values show as "-".
    Folders with direct files are coloured blue/bold; context-only folders are
    grey, matching the FT folder-tree convention.
    """

    def __init__(self, parent, extensions=None, col_own="Own",
                 col_child="Children", **kw):
        self._extensions = {e.lower() for e in (extensions or {'.jpg', '.jpeg'})}
        self._count_generation = 0
        self._count_threads = set()
        self._count_queue = []
        self._count_worker_active = False
        self._count_after_id = None
        kw['columns'] = [(col_own, TREE_COL_W, "e"), (col_child, TREE_TREE_COL_EXTRA_W + TREE_COL_W, "e")]
        super().__init__(parent, **kw)

    # ── Public refresh API for owner apps ──────────────────────────────────────

    @staticmethod
    def _norm_key(path):
        """Stable comparison key for paths used as tree item ids."""
        try:
            return os.path.normcase(os.path.abspath(os.path.normpath(_ui_path(path))))
        except Exception:
            return os.path.normcase(os.path.normpath(_ui_path(path or "")))

    def _visible_node_for_path(self, path):
        """Return the visible tree iid for path, allowing case/slash variants."""
        if not path:
            return ""
        path = _ui_path(path)
        try:
            if self._tree.exists(path):
                return path
        except Exception:
            pass
        wanted = self._norm_key(path)
        try:
            stack = list(self._tree.get_children(""))
            while stack:
                node = stack.pop(0)
                if self.PLACEHOLDER in node:
                    continue
                if self._norm_key(node) == wanted:
                    return node
                stack.extend(self._tree.get_children(node))
        except Exception:
            pass
        return ""

    def _visible_ancestors_for_path(self, path, include_self=True):
        """Visible node ids from path up to the displayed root."""
        nodes = []
        try:
            root = _ui_path(getattr(self, "_root_path", "") or "")
            root_key = self._norm_key(root) if root else ""
            p = _ui_path(os.path.normpath(path))
            while p:
                node = self._visible_node_for_path(p)
                if node and (include_self or self._norm_key(node) != self._norm_key(path)):
                    nodes.append(node)
                if root_key and self._norm_key(p) == root_key:
                    break
                parent = os.path.dirname(p)
                if not parent or parent == p:
                    break
                p = parent
        except Exception:
            pass
        # De-duplicate while preserving nearest-to-root queue later via caller sort.
        out = []
        seen = set()
        for n in nodes:
            k = self._norm_key(n)
            if k not in seen:
                seen.add(k)
                out.append(n)
        return out

    def refresh_after_file_ops(self, paths_changed):
        """Refresh visible Files and Tree counts after copy/move/delete files.

        Owner apps should pass the folders whose direct contents changed. This
        method does not rebuild folder structure. It only refreshes visible count
        columns, using the same private helpers normally used when a node opens.
        """
        nodes = set()
        for path in paths_changed or []:
            if not path:
                continue
            for node in self._visible_ancestors_for_path(path, include_self=True):
                nodes.add(node)
        if not nodes:
            return

        for node in sorted(nodes, key=len, reverse=True):
            try:
                self._set_own_for_node(node)
            except Exception:
                pass

        try:
            self._count_generation += 1
            self._count_queue = []
            gen = self._count_generation
            for node in sorted(nodes, key=len):
                try:
                    self._queue_child_count(node, gen)
                except Exception:
                    pass
            self._start_count_worker_later()
        except Exception:
            pass

    def refresh_after_folder_created(self, parent_path):
        """Refresh a visible parent after new subfolders are created."""
        parent_node = self._visible_node_for_path(parent_path)
        if not parent_node:
            self.refresh_after_file_ops([parent_path])
            return
        was_open = False
        try:
            was_open = bool(self._tree.item(parent_node, "open"))
        except Exception:
            pass
        try:
            self.refresh_node(parent_node)
            self._tree.item(parent_node, open=was_open)
        except Exception:
            pass
        self.refresh_after_file_ops([parent_node])

    def refresh_after_folder_deleted(self, deleted_path):
        """Remove a deleted visible folder row and refresh ancestor counts."""
        deleted_node = self._visible_node_for_path(deleted_path)
        parent_path = _ui_path(os.path.dirname(_ui_path(deleted_path))) if deleted_path else ""
        parent_node = ""
        if deleted_node:
            try:
                parent_node = self._tree.parent(deleted_node)
            except Exception:
                parent_node = ""
        if not parent_node:
            parent_node = self._visible_node_for_path(parent_path)

        try:
            if deleted_node and self._tree.exists(deleted_node):
                self._tree.delete(deleted_node)
        except Exception:
            pass

        if parent_node:
            try:
                if not _has_subdirs(parent_node) and not self._tree.get_children(parent_node):
                    self._tree.item(parent_node, open=False)
            except Exception:
                pass
            self.refresh_after_file_ops([parent_node])
        elif parent_path:
            self.refresh_after_file_ops([parent_path])

    # ── Counting helpers ───────────────────────────────────────────────────────

    def _count_own(self, path):
        """Count matching files directly in path.

        Normal folders count the active app extensions.  The generated
        _ContactSheets folder is special: its contents are PDF files even when
        the app is in Photos/JPG mode, so count PDFs there as well so the folder
        shows a useful Files count immediately.
        """
        try:
            exts = set(self._extensions)
            try:
                if os.path.basename(os.path.normpath(path)).lower() == "_contactsheets":
                    exts = set(exts) | {".pdf"}
            except Exception:
                pass
            return sum(
                1 for e in os.scandir(_longpath(path))
                if e.is_file(follow_symlinks=False)
                and os.path.splitext(e.name)[1].lower() in exts
            )
        except Exception:
            return 0

    def _count_children(self, path):
        """Count matching files in all descendant folders (not path itself)."""
        total = 0
        try:
            for entry in os.scandir(_longpath(path)):
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        total += self._count_own(entry.path)
                        total += self._count_children(entry.path)
                except OSError:
                    continue
        except Exception:
            pass
        return total

    @staticmethod
    def _fmt(n):
        return str(n) if n > 0 else "-"

    @staticmethod
    def _fmt_tree(n):
        return f"({n})" if n > 0 else "-"

    def _tag_for_own_count(self, n):
        return ("has_files",) if n > 0 else ("empty",)

    def _set_own_for_node(self, node_id):
        n = self._count_own(node_id)
        self.set_col(node_id, 0, self._fmt(n))
        self.tag_node(node_id, *self._tag_for_own_count(n))
        return n

    # ── Async Tree count fill ──────────────────────────────────────────────────

    def _queue_child_count(self, node_id, generation=None):
        """Queue one recursive Tree count.  Never recurse on the Tk thread."""
        if self.PLACEHOLDER in node_id or not self._tree.exists(node_id):
            return
        generation = self._count_generation if generation is None else generation
        self.set_col(node_id, 1, "…")
        self._count_queue.append((node_id, generation))

    # Backward-compatible name used by earlier FTView drops.
    def _start_child_count(self, node_id, generation=None):
        self._queue_child_count(node_id, generation)
        self._start_count_worker_later()

    def _start_count_worker_later(self):
        """Start queued counting after the UI has had a chance to paint."""
        if self._count_worker_active:
            return
        if self._count_after_id is not None:
            return
        try:
            self._count_after_id = self.after(350, self._start_count_worker)
        except Exception:
            self._count_after_id = None

    def _start_count_worker(self):
        self._count_after_id = None
        if self._count_worker_active or not self._count_queue:
            return
        self._count_worker_active = True

        def worker():
            while True:
                try:
                    path, gen = self._count_queue.pop(0)
                except Exception:
                    break
                if gen != self._count_generation:
                    continue
                count = self._count_children(path)

                def apply(p=path, g=gen, n=count):
                    if g != self._count_generation:
                        return
                    if self._tree.exists(p):
                        self.set_col(p, 1, self._fmt_tree(n))

                try:
                    self.after(0, apply)
                except Exception:
                    pass

            def done():
                self._count_worker_active = False
                if self._count_queue:
                    self._start_count_worker_later()

            try:
                self.after(0, done)
            except Exception:
                self._count_worker_active = False

        threading.Thread(target=worker, daemon=True).start()

    def _fill_own_of(self, path):
        """Fill only the Files column for direct children of path — fast."""
        for child in self._tree.get_children(path):
            if self.PLACEHOLDER not in child:
                self._set_own_for_node(child)
                self.set_col(child, 1, "-")

    def _fill_children_of_async(self, path):
        """Fill Files now and queue Tree counts after the UI is visible."""
        generation = self._count_generation
        for child in self._tree.get_children(path):
            if self.PLACEHOLDER not in child:
                self._set_own_for_node(child)
                self._queue_child_count(child, generation)
        self._start_count_worker_later()

    def _fill_visible_count_rows_shared(self, path, *, include_path=False):
        """Shared count filler for FileCountTree and FT-style subclasses.

        This is the single shared path used by FTView and FTmod/FTMain for
        visible folder-tree counts.  It fills direct Files counts immediately
        for the requested visible rows, then queues recursive Tree counts.

        Subclasses such as FTmod's FileTaggerTree can provide:
            _fill_node(path, skip_tree_col=True)
            _fill_tree_col_bg(nodes)
        to add extra columns such as Thumbs while still using this shared
        root/open counting strategy.
        """
        nodes = []
        try:
            if include_path and path and self.PLACEHOLDER not in path and self._tree.exists(path):
                nodes.append(path)
        except Exception:
            pass
        try:
            for child in self._tree.get_children(path):
                if self.PLACEHOLDER not in child:
                    nodes.append(child)
        except Exception:
            pass

        if not nodes:
            return

        custom_fill = getattr(self, "_fill_node", None)
        custom_tree_bg = getattr(self, "_fill_tree_col_bg", None)

        for node in nodes:
            try:
                if callable(custom_fill):
                    custom_fill(node, skip_tree_col=True)
                else:
                    self._set_own_for_node(node)
                    self.set_col(node, 1, "-")
            except TypeError:
                try:
                    custom_fill(node)
                except Exception:
                    pass
            except Exception:
                pass

        # FTmod/FileTaggerTree supplies its own Tree-column background worker
        # because its Tree column is app-specific and it also has a Thumbs column.
        if callable(custom_tree_bg):
            try:
                custom_tree_bg(nodes)
                return
            except Exception:
                pass

        try:
            generation = self._count_generation
            for node in nodes:
                self._queue_child_count(node, generation)
            self._start_count_worker_later()
        except Exception:
            pass

    # Backward-compatible name used by older FTView builds.
    def _fill_children_of(self, path):
        self._fill_visible_count_rows_shared(path, include_path=False)

    # ── Overrides ──────────────────────────────────────────────────────────────

    def set_root(self, path):
        self._count_generation += 1
        self._count_queue = []
        self._count_worker_active = False
        self._count_after_id = None
        super().set_root(path)

    def _populate_root(self, path):
        """Populate root immediately; fill root and child counts.

        The displayed root folder is a real selectable folder, so its own
        Files count must be filled immediately.  Earlier builds only filled
        the children of the root, leaving the root row as "-" even when the
        root itself contained matching JPG/PDF/DOCX files.
        """
        super()._populate_root(path)
        self._fill_visible_count_rows_shared(path, include_path=True)

    def _on_node_open(self, path):
        """Expand node, then fill visible counts without blocking the UI."""
        super()._on_node_open(path)
        self._fill_visible_count_rows_shared(path, include_path=False)


# ── SortableFileList ──────────────────────────────────────────────────────────

import queue as _queue_mod
import subprocess as _subprocess_mod
import shutil as _shutil_mod
import json as _json_mod

_SORT_COL_LABELS = {
    "name":       "Name",
    "date_taken": "Date",
    "file":       "Name",
    "size":       "Size",
}

def _sort_btn_label(column: str = "name", reverse: bool = False) -> str:
    """Return sort button label showing column and direction, e.g. 'Name ↑ ▾'."""
    col = _SORT_COL_LABELS.get(column, column.capitalize())
    arrow = "↓" if reverse else "↑"
    return f"{col} {arrow} ▾"

# Kept for backwards-compatible imports
_SORT_BTN_TEXT = _sort_btn_label("name", False)


def show_file_sort_menu(btn, columns, sort_column, sort_reverse, callback):
    """Show a Windows Explorer-style sort popup menu anchored below btn.

    columns     : list of (display_label, column_key)
                  e.g. [("Name", "name"), ("Date taken", "date_taken")]
    sort_column : currently active column key
    sort_reverse: True → Descending
    callback(column: str, reverse: bool) — called when the user changes either
                  the column or the direction.

    Menu layout:
        ○/● Name          ← radiobutton group: sort column
        ○/● Date taken
        ─────────────
        ○/● Ascending     ← radiobutton group: direction
        ○/● Descending
    """
    col_var = tk.StringVar(value=sort_column)
    rev_var = tk.StringVar(value="desc" if sort_reverse else "asc")

    def _fire():
        callback(col_var.get(), rev_var.get() == "desc")

    menu = tk.Menu(btn, tearoff=0)
    for label, key in columns:
        menu.add_radiobutton(label=label, variable=col_var, value=key, command=_fire)
    menu.add_separator()
    menu.add_radiobutton(label="Ascending",  variable=rev_var, value="asc",  command=_fire)
    menu.add_radiobutton(label="Descending", variable=rev_var, value="desc", command=_fire)

    try:
        menu.tk_popup(btn.winfo_rootx(), btn.winfo_rooty() + btn.winfo_height())
    finally:
        try:
            menu.grab_release()
        except Exception:
            pass


class SortableFileList(tk.Frame):
    """Sortable file-list Treeview with File, Duration, and Date columns.

    Columns:
        "file"   always present, stretch=True
        "dur"    present when duration_getter is provided
        "date"   always present; populated async from ft_metadata_cache / ffprobe

    Parent responsibilities:
        - Re-sort the file list and call set_files() when on_sort fires.
        - Call select_index() to sync the visual selection (e.g. from a
          thumbnail click) — this does NOT fire on_select or on_click.

    Widget responsibilities:
        - Track sort column/direction and update heading indicators (▲ ▼).
        - Fire on_sort(column, reverse) when a heading is clicked.
        - Populate the date column asynchronously.
        - Populate the duration column asynchronously via duration_getter.
        - Manage selection events and fire on_select / on_click callbacks.

    Anti-loop guard (mirrors the fix from FTVideo history item #7):
        on_select is suppressed when idx == the already-selected index.
        on_click fires unconditionally so re-clicking the same row works.
    """

    _ASC  = " ▲"
    _DESC = " ▼"

    def __init__(self, parent, *,
                 on_select=None,        # callback(idx: int) — new row selected
                 on_click=None,         # callback(idx: int) — ButtonRelease-1
                 on_sort=None,          # callback(column: str, reverse: bool)
                 duration_getter=None,  # callable(path: str) -> str, run in worker
                 sort_column="name",
                 sort_reverse=False,
                 **kwargs):
        super().__init__(parent, **kwargs)
        self._on_select       = on_select
        self._on_click        = on_click
        self._on_sort         = on_sort
        self._duration_getter = duration_getter
        self._sort_col        = sort_column
        self._sort_rev        = sort_reverse
        self._files           = []
        self._selected_idx    = None
        self._syncing         = False
        self._gen             = 0       # incremented each set_files to cancel workers

        self._show_dur  = duration_getter is not None

        # Style
        try:
            style = ttk.Style(self)
            style.configure("SFL.Treeview",         font=("Segoe UI", 9),  rowheight=22)
            style.configure("SFL.Treeview.Heading", font=("Segoe UI", 8, "bold"))
        except Exception:
            pass

        # Build columns list
        cols = ["file"]
        if self._show_dur:
            cols.append("dur")

        self._tree = ttk.Treeview(
            self, columns=cols, show="headings",
            selectmode="browse", style="SFL.Treeview",
        )

        # File column — no click handler; sorting is driven by external dropdown
        self._tree.heading("file", text="File", anchor="w")
        self._tree.column("file", anchor="w", stretch=True, minwidth=60)

        # Duration column (optional)
        if self._show_dur:
            self._tree.heading("dur", text="Dur", anchor="e")
            self._tree.column("dur", anchor="e", width=58, minwidth=45, stretch=False)

        sb = ttk.Scrollbar(self, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._tree.pack(side="left", fill="both", expand=True)

        # Ctrl-selection highlight (applied via set_ctrl_selected)
        self._tree.tag_configure("ctrl_sel", background="#c8e6ff", foreground="black")

        self._tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self._tree.bind("<ButtonRelease-1>",  self._on_tree_click)

    # ── File list ─────────────────────────────────────────────────────────────

    def set_files(self, files):
        """Load files. Cancels any in-flight async workers from the previous call."""
        self._gen += 1
        self._files = list(files)
        self._selected_idx = None

        self._tree.delete(*self._tree.get_children(""))
        for idx, p in enumerate(self._files):
            vals = ["[{}]  {}".format(
                os.path.splitext(p)[1].lstrip(".").upper(),
                os.path.basename(p)
            )]
            if self._show_dur:
                vals.append("")
            self._tree.insert("", "end", iid=str(idx), values=vals)

        self._start_async_loaders(self._gen)

    def clear(self):
        """Remove all rows and cancel async workers."""
        self._gen += 1
        self._files = []
        self._selected_idx = None
        self._tree.delete(*self._tree.get_children(""))

    # ── Async loaders ─────────────────────────────────────────────────────────

    def _start_async_loaders(self, gen: int):
        if self._show_dur:
            self._start_dur_loader(gen)

    def _start_dur_loader(self, gen: int):
        files = list(self._files)
        if not files:
            return
        q = _queue_mod.Queue()
        for i, p in enumerate(files):
            q.put((i, p))

        getter = self._duration_getter

        def _worker():
            while True:
                try:
                    i, p = q.get_nowait()
                except _queue_mod.Empty:
                    return
                if self._gen != gen:
                    return
                try:
                    val = getter(p) or ""
                except Exception:
                    val = ""
                if self._gen != gen:
                    return
                try:
                    self.after(0, lambda i=i, v=val: self._set_cell(str(i), "dur", v))
                except Exception:
                    return

        for _ in range(min(2, len(files))):
            threading.Thread(target=_worker, daemon=True).start()

    def _set_cell(self, iid: str, col: str, value: str):
        try:
            self._tree.set(iid, col, value)
        except Exception:
            pass

    # ── Selection ─────────────────────────────────────────────────────────────

    @property
    def selected_index(self):
        return self._selected_idx

    def select_index(self, idx, *, scroll=True):
        """Visually select row idx. Does NOT fire on_select or on_click."""
        if idx is None or idx < 0 or idx >= len(self._files):
            return
        self._syncing = True
        try:
            iid = str(idx)
            self._tree.selection_set(iid)
            self._tree.focus(iid)
            if scroll:
                self._tree.see(iid)
            self._selected_idx = idx
        except Exception:
            pass
        finally:
            self._syncing = False

    def set_ctrl_selected(self, indices):
        """Highlight rows that are ctrl-selected in the thumbnail panel.

        indices : iterable of integer file indices to mark with the blue tint.
        Any row not in *indices* has the tag removed.
        """
        want = set(indices)
        for iid in self._tree.get_children(""):
            try:
                idx_val = int(iid)
            except ValueError:
                continue
            tags = list(self._tree.item(iid, "tags") or ())
            has  = "ctrl_sel" in tags
            need = idx_val in want
            if need == has:
                continue
            if need:
                tags.append("ctrl_sel")
            else:
                tags.remove("ctrl_sel")
            self._tree.item(iid, tags=tags)

    def _on_tree_select(self, _event=None):
        if self._syncing:
            return
        sel = self._tree.selection()
        if not sel:
            return
        try:
            idx = int(sel[0])
        except (ValueError, IndexError):
            return
        # Anti-loop guard: Tk sometimes re-fires <<TreeviewSelect>> after
        # selection_set/focus/see.  If the index hasn't changed, ignore it.
        if idx == self._selected_idx:
            return
        self._selected_idx = idx
        if self._on_select:
            self._on_select(idx)

    def _on_tree_click(self, event):
        if self._syncing:
            return
        row = self._tree.identify_row(event.y)
        if not row:
            return
        try:
            idx = int(row)
        except ValueError:
            return
        self._selected_idx = idx
        if self._on_click:
            self._on_click(idx)
