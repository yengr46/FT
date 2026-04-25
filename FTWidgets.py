"""
FTWidgets.py  —  Shared widget library
Version: 23:44 26-Apr-2026
Version: 22:00 26-Apr-2026
Version: 17:54 26-Apr-2026 for the FileTagger suite.

Provides:
    FolderTreeWidget   — standardised folder tree with optional data columns
    ZoomableCanvas     — embedded canvas with built-in zoom and pan

Usage:
    from FTWidgets import FolderTreeWidget, ZoomableCanvas

Both widgets have a consistent appearance across all FT apps.
FolderTreeWidget can be subclassed to add columns, right-click menus
and custom colour coding (as in FTDBXnew.py).
"""

import os
import tkinter as tk
from tkinter import ttk

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
TREE_EMPTY    = "#888888"   # folder is empty
TREE_STYLE_ID = "FT.Treeview"
TREE_WIDTH    = 200          # 250 screen px / 1.25 DPI scale
TREE_COL_W    = 44           # 55 screen px / 1.25 DPI scale
TREE_COLS_W   = TREE_COL_W * 3  # FTDB currently uses 3 columns (JPGs, Tagged, Thumbs)
TREE_SCROLL_W = 18           # 22 screen px / 1.25 DPI scale
TREE_PAD_R    = 10           # right padding between last column and scrollbar
TREE_LEFT_W   = 312  # 390 screen px / 1.25 DPI scale — sash position for FileTagger


def _longpath(p):
    import sys
    if sys.platform == "win32" and not p.startswith("\\\\?\\"):
        return "\\\\?\\" + os.path.abspath(p)
    return p


# ═══════════════════════════════════════════════════════════════════════════════
# ── FolderTreeWidget ───────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

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
        self._root_entry.pack(side="left", fill="x", expand=True)
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
        total_w = TREE_WIDTH + self._actual_cols_w + TREE_SCROLL_W
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
        sty.theme_use("vista")
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
        return TREE_WIDTH + cols_w + TREE_SCROLL_W + TREE_PAD_R + 6  # +6 for border

    # ── Lazy population ────────────────────────────────────────────────────────

    def _populate_root(self, path):
        """Populate full tree from root — all levels shown, root open."""
        import time as _time
        t0 = _time.time()
        self._tree.delete(*self._tree.get_children())
        root_label = os.path.basename(path) or path
        self._tree.insert("", "end", iid=path, text=f"  {root_label}", open=True)
        self._populate_level(path)
        print(f"DEBUG _populate_root took {_time.time()-t0:.2f}s for {path}")
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
            try:
                has_subdirs = any(e.is_dir() for e in os.scandir(_longpath(path)))
            except Exception:
                has_subdirs = False
            if has_subdirs:
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
        """Right-click context menu — New Subfolders, Rename, Delete."""
        menu = tk.Menu(self._tree, tearoff=0)
        menu.add_command(label="📁  New subfolders here…",
                         command=lambda: self._dlg_new_subfolders(path))
        menu.add_command(label="✏  Rename this folder…",
                         command=lambda: self._dlg_rename_folder(path))
        menu.add_separator()
        menu.add_command(label="🗑  Delete this folder…",
                         command=lambda: self._dlg_delete_folder(path))
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
        root = self.winfo_toplevel()
        dlg = tk.Toplevel(root)
        dlg.title("New Subfolders")
        dlg.configure(bg="#f0f0f0")
        dlg.resizable(False, False)
        dlg.transient(root)
        dlg.grab_set()

        tk.Label(dlg, text=f"Create subfolders inside:\n{parent_path}",
                 bg="#f0f0f0", fg="#111111",
                 font=("Segoe UI", 9), justify="left").pack(
                     anchor="w", padx=16, pady=(14, 6))
        tk.Label(dlg, text="Enter up to 5 folder names (leave blank to skip):",
                 bg="#f0f0f0", fg="#555555",
                 font=("Segoe UI", 8)).pack(anchor="w", padx=16, pady=(0, 8))

        entries = []
        for i in range(5):
            row = tk.Frame(dlg, bg="#f0f0f0")
            row.pack(fill="x", padx=16, pady=2)
            tk.Label(row, text=f"{i+1}.", bg="#f0f0f0", fg="#333",
                     font=("Segoe UI", 9), width=2).pack(side="left")
            var = tk.StringVar()
            e = tk.Entry(row, textvariable=var, font=("Segoe UI", 9),
                         width=36, relief="solid", bd=1)
            e.pack(side="left", padx=(4, 0))
            entries.append(var)

        entries[0].set("")
        dlg.after(100, lambda: dlg.focus_force())

        btn_row = tk.Frame(dlg, bg="#f0f0f0")
        btn_row.pack(fill="x", padx=16, pady=(12, 14))

        created = [0]

        def _do_create():
            names = [self._sanitise_name(v.get()) for v in entries]
            names = [n for n in names if n]
            if not names:
                messagebox.showwarning("No names", "Enter at least one folder name.",
                                       parent=dlg)
                return
            errors = []
            for name in names:
                new_path = os.path.join(parent_path, name)
                try:
                    os.makedirs(_longpath(new_path), exist_ok=False)
                    # Add to tree
                    phs = [c for c in self._tree.get_children(parent_path)
                           if self.PLACEHOLDER in c]
                    for ph in phs: self._tree.delete(ph)
                    if not self._tree.exists(new_path):
                        self._add_node(parent_path, new_path, name)
                    created[0] += 1
                except FileExistsError:
                    errors.append(f"'{name}' already exists")
                except Exception as e:
                    errors.append(f"'{name}': {e}")
            self._tree.item(parent_path, open=True)
            self._tree.selection_set(parent_path)
            self._tree.see(parent_path)
            dlg.destroy()
            if errors:
                messagebox.showwarning("Some errors",
                    "\n".join(errors), parent=root)
            if created[0]:
                self._toast(f"✔  Created {created[0]} folder{'s' if created[0]!=1 else ''}")
            if self._on_folders_changed:
                self._on_folders_changed(parent_path)

        tk.Button(btn_row, text="Create", bg="#1a6b2a", fg="white",
                  font=("Segoe UI", 9, "bold"), relief="flat",
                  padx=14, pady=4, cursor="hand2",
                  command=_do_create).pack(side="left")
        tk.Button(btn_row, text="Cancel", bg="#888888", fg="white",
                  font=("Segoe UI", 9), relief="flat",
                  padx=10, pady=4, cursor="hand2",
                  command=dlg.destroy).pack(side="left", padx=(8, 0))

        # Centre dialog
        root.update_idletasks()
        w, h = 420, 280
        x = root.winfo_rootx() + (root.winfo_width()  - w) // 2
        y = root.winfo_rooty() + (root.winfo_height() - h) // 2
        dlg.geometry(f"{w}x{h}+{x}+{y}")

    def _dlg_rename_folder(self, folder_path):
        """Rename a folder."""
        import tkinter.simpledialog as _sd
        root  = self.winfo_toplevel()
        old_name = os.path.basename(folder_path)
        new_name = _sd.askstring("Rename Folder",
                                  f"Rename '{old_name}' to:",
                                  initialvalue=old_name, parent=root)
        if not new_name or not new_name.strip(): return
        new_name = self._sanitise_name(new_name)
        if new_name == old_name: return
        new_path = os.path.join(os.path.dirname(folder_path), new_name)
        try:
            os.rename(_longpath(folder_path), _longpath(new_path))
            # Update tree node
            if self._tree.exists(folder_path):
                self._tree.item(folder_path, text=f"  {new_name}")
                # ttk iid can't be changed — rebuild parent's children
                parent = self._tree.parent(folder_path)
                self.refresh_node(parent if parent else folder_path)
            self._toast(f"✔  Renamed: {old_name} → {new_name}", bg="#224466")
            if self._on_folders_changed:
                self._on_folders_changed(new_path)
        except Exception as e:
            messagebox.showerror("Rename failed", str(e), parent=root)

    def _dlg_delete_folder(self, folder_path):
        """Delete a folder after confirmation. Fires on_delete callback."""
        import shutil
        root = self.winfo_toplevel()
        try:
            has_children = any(e.is_dir()
                               for e in os.scandir(_longpath(folder_path)))
        except Exception:
            has_children = False
        if has_children:
            messagebox.showwarning("Cannot Delete",
                "This folder has subfolders and cannot be deleted.\n\n"
                "Remove all subfolders first.", parent=root)
            return
        folder_name = os.path.basename(folder_path)
        if not messagebox.askyesno("Delete Folder",
                f"Permanently delete:\n\n{folder_path}\n\n"
                "This cannot be undone.", icon="warning", parent=root):
            return
        try:
            shutil.rmtree(_longpath(folder_path))
        except Exception as e:
            messagebox.showerror("Delete failed", str(e), parent=root)
            return
        # Remove from tree
        if self._tree.exists(folder_path):
            self._tree.delete(folder_path)
        self._toast(f"🗑  Deleted: {folder_name}", bg="#662222")
        if self._on_delete_folder:
            self._on_delete_folder(folder_path)
        if self._on_folders_changed:
            self._on_folders_changed(os.path.dirname(folder_path))


# ═══════════════════════════════════════════════════════════════════════════════
# ── ZoomableCanvas ────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

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
        Own      — files matching extensions directly in the folder
        Children — files matching extensions in all descendant folders

    Values show as a number or '-' if zero.

    Parameters
    ----------
    extensions : set of str
        File extensions to count, e.g. {'.jpg', '.jpeg'} or {'.pdf'}
    col_own    : str   — heading for the own-files column (default "Own")
    col_child  : str   — heading for the children column (default "Children")

    All other FolderTreeWidget parameters are passed through.

    Usage
    -----
        tree = FileCountTree(
            parent,
            extensions={'.jpg', '.jpeg'},
            col_own="JPGs",
            col_child="In sub",
            on_select=my_callback,
            show_root_entry=True,
        )
        tree.pack(fill="y", side="left")
        tree.pack_propagate(False)
        tree.configure(width=tree.actual_width())
    """

    def __init__(self, parent, extensions=None, col_own="Own",
                 col_child="Children", **kw):
        self._extensions = {e.lower() for e in (extensions or {'.jpg', '.jpeg'})}
        # Inject the two fixed columns — caller must not pass columns=
        kw['columns'] = [(col_own, TREE_COL_W, "e"), (col_child, TREE_COL_W, "e")]
        super().__init__(parent, **kw)

    # ── Counting helpers ───────────────────────────────────────────────────────

    def _count_own(self, path):
        """Count matching files directly in path."""
        try:
            return sum(
                1 for e in os.scandir(_longpath(path))
                if e.is_file()
                and os.path.splitext(e.name)[1].lower() in self._extensions
            )
        except Exception:
            return 0

    def _count_children(self, path):
        """Count matching files in all descendant folders (not path itself)."""
        total = 0
        try:
            for entry in os.scandir(_longpath(path)):
                if entry.is_dir():
                    total += self._count_own(entry.path)
                    total += self._count_children(entry.path)
        except Exception:
            pass
        return total

    @staticmethod
    def _fmt(n):
        return str(n) if n > 0 else "-"

    # ── Fill columns ───────────────────────────────────────────────────────────

    def _fill_own_of(self, path):
        """Fill only the Own column for direct children of path — fast, no recursion."""
        for child in self._tree.get_children(path):
            if self.PLACEHOLDER not in child:
                self.set_col(child, 0, self._fmt(self._count_own(child)))

    def _fill_children_of(self, path):
        """Fill both columns for direct children of path."""
        for child in self._tree.get_children(path):
            if self.PLACEHOLDER not in child:
                self.set_col(child, 0, self._fmt(self._count_own(child)))
                self.set_col(child, 1, self._fmt(self._count_children(child)))

    # ── Overrides ──────────────────────────────────────────────────────────────

    def _populate_root(self, path):
        """Populate root then fill columns — Own only for all, Children only for root."""
        super()._populate_root(path)
        self._fill_own_of(path)
        # Root is always open so fill Children for its immediate children too
        for child in self._tree.get_children(path):
            if self.PLACEHOLDER not in child:
                self.set_col(child, 1, self._fmt(self._count_children(child)))

    def _on_node_open(self, path):
        """Expand node then fill both columns for its children."""
        super()._on_node_open(path)
        self._fill_children_of(path)
