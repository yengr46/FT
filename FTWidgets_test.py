"""
FTWidgets_test.py — Test harness for FolderTreeWidget

Enter a root folder path and press Enter to populate the tree.
Enter number of extra columns (0-5) to dynamically resize the panel.
Columns are filled with random values. Right-click any folder to test menu.
No actual file operations are performed.
"""

import os
import random
import tkinter as tk
from tkinter import ttk
from FTWidgets import FolderTreeWidget, TREE_WIDTH, TREE_COL_W, TREE_SCROLL_W


class TestApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("FTWidgets Test Harness")
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        w, h = min(900, sw - 40), int(sh * 0.7)
        self.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")
        self.configure(bg="#f0f0f0")
        self._tree_widget = None
        self._build()

    def _build(self):
        # ── Top control bar ────────────────────────────────────────────────
        bar = tk.Frame(self, bg="#e0e0e0", padx=8, pady=6)
        bar.pack(fill="x", side="top")

        tk.Label(bar, text="Extra columns (0–5):", bg="#e0e0e0",
                 font=("Segoe UI", 9)).pack(side="left")

        self._ncols_var = tk.StringVar(value="0")
        col_entry = tk.Entry(bar, textvariable=self._ncols_var, width=4,
                             font=("Segoe UI", 9), relief="solid", bd=1)
        col_entry.pack(side="left", padx=(4, 8))
        col_entry.bind("<Return>", lambda e: self._rebuild_tree())

        tk.Button(bar, text="Apply", bg="#1a3a5c", fg="white",
                  font=("Segoe UI", 9, "bold"), relief="flat",
                  padx=8, cursor="hand2",
                  command=self._rebuild_tree).pack(side="left")

        # ── Status bar ─────────────────────────────────────────────────────
        self._status = tk.StringVar(value="Enter a root folder path and press Enter")
        tk.Label(self, textvariable=self._status, bg="#dddddd",
                 font=("Segoe UI", 9), anchor="w", padx=8).pack(
                     fill="x", side="bottom", pady=(2, 0))

        # ── Tree container (so we can destroy/rebuild) ─────────────────────
        self._tree_container = tk.Frame(self, bg="#f0f0f0")
        self._tree_container.pack(fill="both", expand=True)

        self._rebuild_tree()

    def _rebuild_tree(self):
        # Parse column count
        try:
            ncols = max(0, min(5, int(self._ncols_var.get())))
        except ValueError:
            ncols = 0
        self._ncols_var.set(str(ncols))

        # Remember root path before destroying
        old_root = ""
        if self._tree_widget:
            old_root = self._tree_widget._root_path
            self._tree_widget.destroy()

        # Build column definitions
        col_labels = ["Col A", "Col B", "Col C", "Col D", "Col E"]
        columns = [(col_labels[i], TREE_COL_W, "e") for i in range(ncols)]

        # Total width: tree + (ncols * col width) + scrollbar
        total_w = TREE_WIDTH + ncols * TREE_COL_W + TREE_SCROLL_W

        self._tree_widget = FolderTreeWidget(
            self._tree_container,
            columns=columns,
            on_select=self._on_select,
            on_delete_folder=self._on_delete,
            on_folders_changed=self._on_changed,
            show_root_entry=True,
            bg="#f0f0f0"
        )
        self._tree_widget.pack(fill="y", side="left")
        self._tree_widget.pack_propagate(False)
        self._tree_widget.configure(width=total_w)

        self._tree_widget._make_context_menu = self._make_test_menu

        # Hook open/set_root to fill random column data
        orig_open = self._tree_widget._on_node_open
        def _open_with_data(path):
            orig_open(path)
            self._fill_columns_for(path)
        self._tree_widget._on_node_open = _open_with_data

        orig_set = self._tree_widget.set_root
        def _set_with_data(path):
            orig_set(path)
            self._fill_all_columns()
        self._tree_widget.set_root = _set_with_data

        # Restore root if we had one
        if old_root and os.path.isdir(old_root):
            self._tree_widget.set_root(old_root)

        self._status.set(
            f"{ncols} extra column{'s' if ncols != 1 else ''} — "
            f"panel width: {total_w} internal px"
        )

    def _fill_columns_for(self, path):
        tree = self._tree_widget.tree()
        ncols = len(self._tree_widget._col_defs)
        for child in tree.get_children(path):
            if self._tree_widget.PLACEHOLDER not in child:
                for i in range(ncols):
                    tree.set(child, f"col{i}", str(random.randint(0, 999)))

    def _fill_all_columns(self):
        tree = self._tree_widget.tree()
        ncols = len(self._tree_widget._col_defs)
        def _fill(node):
            for child in tree.get_children(node):
                if self._tree_widget.PLACEHOLDER not in child:
                    for i in range(ncols):
                        tree.set(child, f"col{i}", str(random.randint(0, 999)))
                    _fill(child)
        _fill("")

    def _make_test_menu(self, path):
        menu = tk.Menu(self._tree_widget.tree(), tearoff=0)
        menu.add_command(label="📁  New subfolders here…",
                         command=lambda: self._tree_widget._dlg_new_subfolders(path))
        menu.add_command(label="✏  Rename this folder…",
                         command=lambda: self._tree_widget._dlg_rename_folder(path))
        menu.add_separator()
        menu.add_command(label="🗑  Delete this folder…",
                         command=lambda: self._tree_widget._dlg_delete_folder(path))
        return menu

    def _on_select(self, path):
        self._status.set(f"Selected: {path}")

    def _on_delete(self, path):
        self._status.set(f"Deleted: {path}")

    def _on_changed(self, path):
        self._fill_all_columns()
        self._status.set(f"Changed: {path}")


if __name__ == "__main__":
    app = TestApp()
    app.mainloop()
