"""
FTCompare.py  —  FileTagger Two-Panel Photo Compare

Layout:
  [Left folder tree] | [Left thumbs] | [Right folder tree] | [Right thumbs]
  [Action strip along bottom]

Standalone:
    pythonw.exe FTCompare.py
    pythonw.exe FTCompare.py "S:\\Left" "S:\\Right"

Embedded:
    pythonw.exe FTCompare.py --embedded
"""

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import os, sys, threading, shutil

# Must be before tk.Tk() — lets tkinter see real physical pixels
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

import tkinter as tk
from tkinter import messagebox

try:
    from PIL import Image, ImageTk, ImageOps, ImageFile
except ImportError:
    messagebox.showerror("Missing library", "Pillow is required.\n\npip install Pillow")
    sys.exit(1)

from libraries.ft_thumbs import get_thumbnail, open_image_rgb
from libraries.ft_widgets import FolderTreeWidget, TREE_COL_W

try:
    from libraries.ft_project_roots import read_project_roots
except Exception:
    def read_project_roots(base_file=None):
        return {"photos": "", "pdfs": "", "project": ""}

# ── Version ───────────────────────────────────────────────────────────────────
VERSION = "FTCompare  v3.5  2026-04-29  FT-style zoom + selection alignment"

# ── Constants ─────────────────────────────────────────────────────────────────
PHOTO_EXTS = {'.jpg', '.jpeg', '.png', '.tif', '.tiff', '.bmp', '.webp'}
THUMB_SZ   = 150
CELL_PAD   = 4
FNAME_H    = 18
ZOOM_H     = 18
CELL_W     = THUMB_SZ + CELL_PAD * 2
CELL_H     = THUMB_SZ + FNAME_H + ZOOM_H + CELL_PAD * 2
TREE_W     = 250

BG       = "#f0f0f0"
BG2      = "#e8e8e8"
BG3      = "#d8d8d8"
HDR_BG   = "#1a3a5c"
HDR_FG   = "white"
ACCENT   = "#4f8ef7"
SEL_BD   = "#4f8ef7"
TEXT     = "#111111"
TEXT_DIM = "#777777"
GREEN    = "#1a6b2a"
BLUE     = "#1a4a6b"
RED      = "#8b1a1a"

# ── Helpers ───────────────────────────────────────────────────────────────────

def _longpath(p):
    if sys.platform == "win32" and not p.startswith("\\\\?\\"):
        return "\\\\?\\" + os.path.abspath(p)
    return p

def _ui_path(p):
    """Return a normal display/UI path; keep the Windows long-path prefix internal only."""
    if not p:
        return p
    p = str(p)
    if p.startswith("\\\\?\\"):
        p = p[4:]
    return os.path.normpath(p)

def _script_dir():
    try:    return os.path.dirname(os.path.abspath(__file__))
    except: return os.path.dirname(os.path.abspath(sys.argv[0]))

def _ipc_dir():
    import configparser as _cp
    ini = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "FileTagger.ini")
    if os.path.exists(ini):
        cfg = _cp.ConfigParser(strict=False)
        cfg.read(ini)
        p = cfg.get("FileTagger", "ipc_folder", fallback="").strip()
        if p:
            os.makedirs(p, exist_ok=True)
            return p
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "FT_IPC")
    os.makedirs(p, exist_ok=True)
    return p

def _scan_folder(folder):
    folder = _ui_path(folder)
    try:
        return sorted(
            [os.path.join(folder, f) for f in os.listdir(folder)
             if os.path.splitext(f)[1].lower() in PHOTO_EXTS],
            key=lambda p: os.path.basename(p).lower()
        )
    except Exception:
        return []

def _make_thumb(path):
    img, ok, err = get_thumbnail(path, THUMB_SZ, longpath_func=_longpath)
    return img if ok else None


# ── Folder Tree wrapper ──────────────────────────────────────────────────────


class CompareFileTree(FolderTreeWidget):
    """FTCompare folder tree using the current shared ft_widgets FolderTreeWidget.

    Replaces the old FileOnlyTree class, which no longer exists in ft_widgets.
    Shows one count column for matching image files directly inside each folder.
    """

    def __init__(self, parent, extensions=None, **kw):
        self._extensions = {e.lower() for e in (extensions or PHOTO_EXTS)}
        kw["columns"] = [("Files", TREE_COL_W, "e")]
        super().__init__(parent, **kw)

    @staticmethod
    def _fmt(n):
        return str(n) if n > 0 else "-"

    def _count_own(self, path):
        try:
            return sum(
                1 for e in os.scandir(_longpath(path))
                if e.is_file() and os.path.splitext(e.name)[1].lower() in self._extensions
            )
        except Exception:
            return 0

    def _apply_node_state(self, path):
        try:
            n = self._count_own(path)
            self.set_col(path, 0, self._fmt(n))
            self.tag_node(path, "has_files" if n > 0 else "empty")
        except Exception:
            pass

    def _fill_children_of(self, path):
        try:
            for child in self.tree().get_children(path):
                if self.PLACEHOLDER not in child:
                    self._apply_node_state(child)
        except Exception:
            pass

    def _populate_root(self, path):
        super()._populate_root(path)
        self._apply_node_state(path)
        self._fill_children_of(path)

    def _on_node_open(self, path):
        super()._on_node_open(path)
        self._apply_node_state(path)
        self._fill_children_of(path)

    def set_folder(self, folder):
        folder = _ui_path(folder)
        self.set_root(folder)
        try:
            self.tree().selection_set(folder)
            self.tree().focus(folder)
            self.tree().see(folder)
        except Exception:
            pass

    def get_root(self):
        return _ui_path(getattr(self, "_root_path", ""))


class CompareFolderTree(tk.Frame):
    """FTCompare folder-tree panel using the shared ft_widgets tree."""

    def __init__(self, parent, label, on_select, on_root_change=None):
        super().__init__(parent, bg=BG2)
        self._on_root_change = on_root_change
        self._last_root_notified = ""
        self._root_notify_after = None
        self.configure(width=TREE_W)
        self.pack_propagate(False)
        self.grid_propagate(False)

        hdr = tk.Frame(self, bg=HDR_BG)
        hdr.pack(fill="x")
        tk.Label(hdr, text=label, bg=HDR_BG, fg=HDR_FG,
                 font=("Segoe UI", 9, "bold"), padx=8, pady=5).pack(anchor="w")

        self._tree = CompareFileTree(
            self,
            extensions=PHOTO_EXTS,
            on_select=on_select,
            show_root_entry=True,
            bg=BG2,
        )
        self._tree.pack(fill="both", expand=True)

        # Watch the shared tree root so left-root defaulting copies the ROOT,
        # not whichever child folder the user selects later.
        try:
            rv = getattr(self._tree, "_root_var", None)
            if rv is not None:
                rv.trace_add("write", lambda *_: self._schedule_root_notify())
        except Exception:
            pass

        # Match the thumbnail panel footer height so the tree visual area does
        # not run lower than the matching thumbnail panel.
        tk.Frame(self, bg=BG3, height=20).pack(fill="x")
        try:
            self.configure(width=self._tree.actual_width())
        except Exception:
            pass

    def _schedule_root_notify(self):
        if not self._on_root_change:
            return
        if self._root_notify_after is not None:
            try:
                self.after_cancel(self._root_notify_after)
            except Exception:
                pass
        self._root_notify_after = self.after(80, self._notify_root_change)

    def _notify_root_change(self):
        self._root_notify_after = None
        if not self._on_root_change:
            return
        root = self.get_root()
        if not root or root == self._last_root_notified:
            return
        if not os.path.isdir(root):
            return
        self._last_root_notified = root
        self._on_root_change(root)

    def set_folder(self, folder):
        """Set the tree root using the current ft_widgets API and select/load it."""
        folder = _ui_path(folder)
        if not folder or not os.path.isdir(folder):
            return

        # Current shared FolderTreeWidget API is set_root(), not the old FileOnlyTree set_folder().
        if hasattr(self._tree, "set_root"):
            self._tree.set_root(folder)
        elif hasattr(self._tree, "set_folder"):
            self._tree.set_folder(folder)

        # Explicitly select/focus/root-load. Shared set_root() builds the tree but
        # may not emit a TreeviewSelect event, so FTCompare must load the root.
        try:
            tv = self._tree.tree()
            if tv.exists(folder):
                tv.selection_set(folder)
                tv.focus(folder)
                tv.see(folder)
        except Exception:
            pass

        try:
            self._tree._on_select(folder)
        except Exception:
            pass

        self._notify_root_change()

    def get_root(self):
        try:
            if hasattr(self._tree, "get_root"):
                return _ui_path(self._tree.get_root())
            return _ui_path(getattr(self._tree, "_root_path", ""))
        except Exception:
            return ""

    def is_empty(self):
        return not bool((self.get_root() or "").strip())


# ── Thumb Panel ───────────────────────────────────────────────────────────────

class ThumbPanel:
    """Scrollable thumbnail grid for one folder."""

    def __init__(self, parent, on_focus, on_select_change=None, on_zoom=None):
        self._frame            = tk.Frame(parent, bg=BG)
        self._on_focus         = on_focus          # called when panel clicked
        self._on_select_change = on_select_change
        self._on_zoom          = on_zoom
        self._folder           = ""
        self._files            = []
        self._selected         = set()
        self._thumb_cache      = {}
        self._photo_refs       = []
        self._cell_widgets     = {}
        self._cols             = 1
        self._gen              = 0
        self._last_clicked_idx = None
        self._build()

    def pack(self, **kw): self._frame.pack(**kw)
    def grid(self, **kw): self._frame.grid(**kw)

    def _build(self):
        # Active indicator bar (yellow when this panel is focused)
        self._active_bar = tk.Frame(self._frame, bg=BG, height=4)
        self._active_bar.pack(fill="x")

        # Canvas + scrollbar
        canvas_frame = tk.Frame(self._frame, bg=BG)
        canvas_frame.pack(fill="both", expand=True)
        vsb = tk.Scrollbar(canvas_frame, orient="vertical", bg=BG2)
        vsb.pack(side="right", fill="y")
        self._canvas = tk.Canvas(canvas_frame, bg=BG,
                                 highlightthickness=0,
                                 yscrollcommand=vsb.set)
        self._canvas.pack(side="left", fill="both", expand=True)
        vsb.config(command=self._canvas.yview)
        self._canvas.bind("<Configure>",     self._on_configure)
        self._canvas.bind("<MouseWheel>",    self._on_wheel)
        self._canvas.bind("<ButtonPress-1>", self._on_click)

        # Status bar
        self._status_var = tk.StringVar(value="No folder selected")
        tk.Label(self._frame, textvariable=self._status_var,
                 bg=BG3, fg=TEXT_DIM, font=("Segoe UI", 8),
                 anchor="w", padx=6).pack(fill="x")

    def load_folder(self, folder):
        folder = _ui_path(folder)
        self._folder = folder
        self._selected.clear()
        self._thumb_cache.clear()
        self._photo_refs.clear()
        self._files = _scan_folder(folder)
        n = len(self._files)
        self._status_var.set(
            f"{n} photo{'s' if n!=1 else ''}  —  {os.path.basename(folder)}")
        self._redraw()
        if self._on_select_change:
            self._on_select_change()

    def reload(self):
        if self._folder:
            old_sel = {p for p in self._selected if os.path.isfile(p)}
            self.load_folder(self._folder)
            self._selected = old_sel

    def _on_configure(self, event=None):
        if hasattr(self, "_resize_after"):
            self._canvas.after_cancel(self._resize_after)
        self._resize_after = self._canvas.after(150, self._do_resize)

    def _do_resize(self):
        new_cols = max(1, self._canvas.winfo_width() // CELL_W)
        if new_cols != self._cols:
            self._cols = new_cols
            self._redraw()

    def _redraw(self):
        self._canvas.update_idletasks()
        self._gen += 1
        self._canvas.delete("all")
        self._cell_widgets.clear()
        if not self._files:
            cw = self._canvas.winfo_width()  or 400
            ch = self._canvas.winfo_height() or 400
            self._canvas.create_text(cw//2, ch//2,
                text="No photos\n\nSelect a folder in the tree",
                fill=TEXT_DIM, font=("Segoe UI", 12), justify="center")
            return
        cols = max(1, self._canvas.winfo_width() // CELL_W)
        rows = (len(self._files) + cols - 1) // cols
        self._canvas.configure(
            scrollregion=(0, 0, cols * CELL_W, rows * CELL_H + CELL_PAD))
        for i, path in enumerate(self._files):
            col = i % cols
            row = i // cols
            self._draw_placeholder(path, col * CELL_W + CELL_PAD, row * CELL_H + CELL_PAD)
        self._cols = cols
        self._start_thumb_thread()

    def _draw_placeholder(self, path, x, y):
        is_sel = path in self._selected
        bd_col = SEL_BD if is_sel else "#444466"
        self._canvas.create_rectangle(
            x, y, x + CELL_W, y + CELL_H,
            outline=bd_col, fill="white", width=2 if is_sel else 1,
            tags=("cell", f"cell_{id(path)}"))
        self._canvas.create_rectangle(
            x + CELL_PAD, y + CELL_PAD,
            x + CELL_PAD + THUMB_SZ, y + CELL_PAD + THUMB_SZ,
            fill="#cccccc", outline="",
            tags=("placeholder", f"ph_{id(path)}"))
        fname = os.path.basename(path)
        if len(fname) > 22: fname = fname[:19] + "…"
        self._canvas.create_text(
            x + CELL_W // 2, y + CELL_PAD + THUMB_SZ + FNAME_H // 2,
            text=fname, fill=TEXT, font=("Segoe UI", 7), anchor="center",
            tags=("fname", f"fname_{id(path)}"))

        # FT-style per-thumbnail Zoom link. This is deliberately not a toolbar button.
        zy0 = y + CELL_PAD + THUMB_SZ + FNAME_H
        zy1 = zy0 + ZOOM_H
        self._canvas.create_rectangle(
            x + CELL_PAD, zy0, x + CELL_W - CELL_PAD, zy1,
            fill="#eef5ff", outline="#c8d8ee",
            tags=("zoom_bg", f"zoom_bg_{id(path)}"))
        self._canvas.create_text(
            x + CELL_W // 2, zy0 + ZOOM_H // 2,
            text="Zoom", fill="#004c99", font=("Segoe UI", 8, "bold"),
            anchor="center", tags=("zoom_text", f"zoom_text_{id(path)}"))

        self._cell_widgets[path] = (x, y)
        self._update_cell_selection_overlay(path)

    def _update_cell_thumb(self, path, photo):
        if path not in self._cell_widgets: return
        x, y = self._cell_widgets[path]
        self._canvas.delete(f"ph_{id(path)}")
        iw, ih = photo.width(), photo.height()
        tx = x + CELL_PAD + (THUMB_SZ - iw) // 2
        ty = y + CELL_PAD + (THUMB_SZ - ih) // 2
        self._canvas.create_image(tx, ty, anchor="nw", image=photo,
                                   tags=("thumb", f"thumb_{id(path)}"))
        self._canvas.tag_raise("fname")
        self._canvas.tag_raise("zoom_bg")
        self._canvas.tag_raise("zoom_text")
        self._canvas.tag_raise("selected_overlay")

    def _update_cell_border(self, path):
        if path not in self._cell_widgets: return
        x, y = self._cell_widgets[path]
        self._canvas.delete(f"cell_{id(path)}")
        is_sel = path in self._selected
        bd_col = "#e03030" if is_sel else "#444466"
        self._canvas.create_rectangle(
            x, y, x + CELL_W, y + CELL_H,
            outline=bd_col, fill="white", width=3 if is_sel else 1,
            tags=("cell", f"cell_{id(path)}"))
        self._canvas.tag_lower(f"cell_{id(path)}")
        self._update_cell_selection_overlay(path)
        self._canvas.tag_raise("fname")
        self._canvas.tag_raise("zoom_bg")
        self._canvas.tag_raise("zoom_text")
        self._canvas.tag_raise("selected_overlay")

    def _update_cell_selection_overlay(self, path):
        """FT-style Selected text overlay: white text only, no box."""
        self._canvas.delete(f"sel_{id(path)}")
        if path not in self._selected or path not in self._cell_widgets:
            return
        x, y = self._cell_widgets[path]
        cx = x + CELL_W // 2
        cy = y + CELL_PAD + THUMB_SZ // 2

        # Match FT: simple white Selected text over the image.
        # No boxed background, no filled badge.
        self._canvas.create_text(
            cx + 1, cy + 1, text="Selected", fill="#000000",
            font=("Segoe UI", 12, "bold"), anchor="center",
            tags=("selected_overlay", f"sel_{id(path)}"))
        self._canvas.create_text(
            cx, cy, text="Selected", fill="#ffffff",
            font=("Segoe UI", 12, "bold"), anchor="center",
            tags=("selected_overlay", f"sel_{id(path)}"))

    def _start_thumb_thread(self):
        gen = self._gen
        threading.Thread(target=self._gen_thumbs_bg,
                         args=(list(self._files), gen), daemon=True).start()

    def _gen_thumbs_bg(self, files, gen):
        for path in files:
            if self._gen != gen: return
            if path in self._thumb_cache:
                cached = self._thumb_cache[path]
                self._canvas.after(0, lambda p=path, ph=cached, g=gen:
                                   self._on_thumb_ready(p, None, g, ph))
                continue
            img = _make_thumb(path)
            if self._gen != gen: return
            if img:
                self._canvas.after(0, lambda p=path, i=img, g=gen:
                                   self._on_thumb_ready(p, i, g))

    def _on_thumb_ready(self, path, img, gen, photo=None):
        if self._gen != gen: return
        if path not in self._cell_widgets: return
        if photo is None:
            photo = ImageTk.PhotoImage(img)
            self._thumb_cache[path] = photo
            self._photo_refs.append(photo)
            if len(self._photo_refs) > 500:
                self._photo_refs = self._photo_refs[-400:]
        self._update_cell_thumb(path, photo)

    def _on_click(self, event):
        self._on_focus()   # tell app which panel is active
        path = self._path_at_event(event)
        if not path:
            return

        if self._event_is_on_zoom_link(event):
            if self._on_zoom:
                self._on_zoom(self, path)
            return "break"

        try:
            idx = self._files.index(path)
        except ValueError:
            return

        shift = (event.state & 0x0001) != 0

        # FT-style selection/unselection:
        #   Click       toggles this thumbnail selected/unselected.
        #   Shift-click adds a range from the last clicked thumbnail.
        if shift and self._last_clicked_idx is not None:
            a = self._last_clicked_idx
            b = idx
            for i in range(min(a, b), max(a, b) + 1):
                self._selected.add(self._files[i])
        else:
            if path in self._selected:
                self._selected.discard(path)
            else:
                self._selected.add(path)
            self._last_clicked_idx = idx

        self._refresh_selection_display()
        return "break"

    def _path_at_event(self, event):
        x = self._canvas.canvasx(event.x)
        y = self._canvas.canvasy(event.y)
        cols = max(1, self._canvas.winfo_width() // CELL_W)
        col = int(x // CELL_W)
        row = int(y // CELL_H)
        idx = row * cols + col
        if 0 <= idx < len(self._files):
            return self._files[idx]
        return None

    def _event_is_on_zoom_link(self, event):
        y = self._canvas.canvasy(event.y)
        row = int(y // CELL_H)
        local_y = y - row * CELL_H
        return CELL_PAD + THUMB_SZ + FNAME_H <= local_y <= CELL_PAD + THUMB_SZ + FNAME_H + ZOOM_H

    def _refresh_selection_display(self):
        for p in self._files:
            self._update_cell_border(p)
        self._update_status()
        if self._on_select_change:
            self._on_select_change()

    def get_files(self):
        return list(self._files)

    def _update_status(self):
        n     = len(self._files)
        n_sel = len(self._selected)
        base  = f"{n} photo{'s' if n!=1 else ''}  —  {os.path.basename(self._folder)}"
        if n_sel: base += f"  |  {n_sel} selected"
        self._status_var.set(base)

    def _on_wheel(self, event):
        self._canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

    def get_selected(self):
        """Return selected files once only, in display order."""
        selected = set(self._selected)
        return [p for p in self._files if p in selected]

    def get_folder(self):    return self._folder

    def select_all(self):
        self._selected = set(self._files)
        self._refresh_selection_display()

    def select_none(self):
        self._selected.clear()
        self._refresh_selection_display()

    def invert_selection(self):
        self._selected = set(self._files) - set(self._selected)
        self._refresh_selection_display()

    def set_active(self, active):
        """Show yellow bar when this is the active panel."""
        self._active_bar.configure(bg="#ffcc00" if active else BG)


# ── Zoom Window ───────────────────────────────────────────────────────────────

class CompareZoomWindow(tk.Toplevel):
    """FT-style zoom/pan viewer for FTCompare thumbnails.

    Important family rules:
      - zoom navigation does NOT alter thumbnail selection
      - navigation arrows sit midway down the image sides, like FT
      - the top bar is informational only, not the navigation control surface
    """

    def __init__(self, parent, panel, start_path):
        super().__init__(parent)
        self.panel = panel
        self.files = list(panel.get_files())
        self.idx = self.files.index(start_path) if start_path in self.files else 0
        self.img = None
        self.photo = None
        self.scale = 1.0
        self.fit_scale = 1.0
        self.offset = [0, 0]
        self.drag_start = None

        self.title("FTCompare Zoom")
        self.configure(bg="#111111")
        self.geometry("1100x850")
        self.minsize(500, 350)

        bar = tk.Frame(self, bg=HDR_BG, height=32)
        bar.pack(fill="x")
        bar.pack_propagate(False)

        self.lbl = tk.Label(bar, text="", bg=HDR_BG, fg=HDR_FG,
                            font=("Segoe UI", 9, "bold"), anchor="w")
        self.lbl.pack(side="left", fill="x", expand=True, padx=8)

        tk.Button(bar, text="Fit", command=self.fit, bg="#444444", fg="white",
                  relief="flat", padx=8).pack(side="right", padx=4)
        tk.Button(bar, text="Close", command=self.destroy, bg=RED, fg="white",
                  relief="flat", padx=8).pack(side="right", padx=4)

        self.canvas = tk.Canvas(self, bg="#222222", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        # FT-style side navigation buttons overlaid on the image canvas.
        self._btn_prev = tk.Button(
            self.canvas, text="❮", bg="#1a2a4a", fg="white",
            font=("Segoe UI", 16, "bold"), relief="flat",
            padx=6, pady=4, cursor="hand2", bd=0,
            activebackground="#334466", activeforeground="white",
            command=lambda: self.step(-1)
        )
        self._btn_next = tk.Button(
            self.canvas, text="❯", bg="#1a2a4a", fg="white",
            font=("Segoe UI", 16, "bold"), relief="flat",
            padx=6, pady=4, cursor="hand2", bd=0,
            activebackground="#334466", activeforeground="white",
            command=lambda: self.step(1)
        )

        self.canvas.bind("<Configure>", lambda e: (self.fit(), self._place_nav()))
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<Double-Button-1>", lambda e: self.fit())
        self.bind("<Left>", lambda e: self.step(-1))
        self.bind("<Right>", lambda e: self.step(1))
        self.bind("<Escape>", lambda e: self.destroy())
        self.focus_set()

        self.load_current()
        self.after(120, self._place_nav)

    def load_current(self):
        if not self.files:
            return
        path = self.files[self.idx]
        self.title(f"FTCompare Zoom - {os.path.basename(path)}")
        self.lbl.config(text=f"{self.idx + 1} of {len(self.files)}   {path}")
        try:
            self.img = open_image_rgb(path, longpath_func=_longpath)
        except Exception as e:
            self.img = None
            self.canvas.delete("all")
            self.canvas.create_text(
                max(20, self.canvas.winfo_width() // 2),
                max(20, self.canvas.winfo_height() // 2),
                text=f"Could not open image\n{e}",
                fill="white", font=("Segoe UI", 13), justify="center")
            self._place_nav()
            return

        # Deliberately DO NOT modify selection here.
        # Zoom navigation is only viewing/navigation, not selecting.
        self.fit()

    def step(self, delta):
        if not self.files:
            return
        new_idx = self.idx + delta
        if new_idx < 0 or new_idx >= len(self.files):
            return
        self.idx = new_idx
        self.load_current()

    def fit(self):
        if self.img is None:
            return
        self.canvas.update_idletasks()
        cw = max(1, self.canvas.winfo_width())
        ch = max(1, self.canvas.winfo_height())
        iw, ih = self.img.size
        if iw <= 0 or ih <= 0:
            return
        self.fit_scale = min(cw / iw, ch / ih)
        self.scale = self.fit_scale
        nw = max(1, int(iw * self.scale))
        nh = max(1, int(ih * self.scale))
        self.offset = [(cw - nw) // 2, (ch - nh) // 2]
        self.render()

    def render(self):
        self.canvas.delete("all")
        if self.img is None:
            self._place_nav()
            return
        iw, ih = self.img.size
        nw = max(1, int(iw * self.scale))
        nh = max(1, int(ih * self.scale))
        try:
            disp = self.img.resize((nw, nh), Image.BILINEAR)
            self.photo = ImageTk.PhotoImage(disp)
            self.canvas.create_image(self.offset[0], self.offset[1], anchor="nw", image=self.photo)
        except Exception as e:
            self.canvas.create_text(
                self.canvas.winfo_width() // 2,
                self.canvas.winfo_height() // 2,
                text=f"Could not render image\n{e}", fill="white")
        self._place_nav()

    def _place_nav(self):
        """Place nav chevrons halfway down the image canvas, matching FT."""
        try:
            self.canvas.update_idletasks()
            cw = max(1, self.canvas.winfo_width())
            ch = max(1, self.canvas.winfo_height())

            bp = self._btn_prev
            bn = self._btn_next
            bw = bp.winfo_reqwidth() or 34
            bh = bp.winfo_reqheight() or 42
            by = max(0, (ch - bh) // 2)

            bp.place(x=0, y=by, anchor="nw")
            bn.place(x=max(0, cw - bw), y=by, anchor="nw")
            bp.lift()
            bn.lift()
        except Exception:
            pass

    def _on_wheel(self, event):
        if self.img is None:
            return "break"
        old = self.scale
        factor = 1.12 if event.delta > 0 else 1 / 1.12
        self.scale = max(self.fit_scale * 0.25, min(self.scale * factor, self.fit_scale * 16))
        mx, my = event.x, event.y
        ox, oy = self.offset
        self.offset[0] = int(mx - (mx - ox) * (self.scale / old))
        self.offset[1] = int(my - (my - oy) * (self.scale / old))
        self.render()
        return "break"

    def _on_press(self, event):
        self.drag_start = (event.x, event.y, self.offset[0], self.offset[1])

    def _on_drag(self, event):
        if not self.drag_start:
            return
        sx, sy, ox, oy = self.drag_start
        self.offset = [ox + event.x - sx, oy + event.y - sy]
        self.render()


# ── Main app ──────────────────────────────────────────────────────────────────

class FTCompare(tk.Tk):

    def __init__(self, left_folder="", right_folder="", embedded=False):
        super().__init__()
        self.title(VERSION)
        self.configure(bg=BG)
        self.resizable(True, True)

        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        w  = int(sw * 0.90)
        h  = int(sh * 0.85)
        x  = (sw - w) // 2
        y  = int((sh - h) * 0.2)   # sit higher on screen
        self.geometry(f"{w}x{h}+{x}+{y}")

        self._embedded   = embedded
        self._ipc_seq    = -1
        self._operations = []
        self._active     = "left"  # which panel was last clicked

        self._build_ui()

        if embedded:
            self.after(200, self._poll_ipc)
        else:
            if left_folder and os.path.isdir(left_folder):
                left_folder = _ui_path(left_folder)
                self._left_tree.set_folder(left_folder)
                self._left.load_folder(left_folder)
                if not right_folder:
                    right_folder = left_folder
            if right_folder and os.path.isdir(right_folder):
                right_folder = _ui_path(right_folder)
                self._right_tree.set_folder(right_folder)
                self._right.load_folder(right_folder)

    def _build_ui(self):
        # Header
        hdr = tk.Frame(self, bg=HDR_BG)
        hdr.pack(fill="x")
        tk.Label(hdr, text=VERSION, bg=HDR_BG, fg=HDR_FG,
                 font=("Segoe UI", 11, "bold"), padx=12, pady=6).pack(side="left")
        tk.Label(hdr, text="Click thumbnails to select/unselect; Shift-click selects a range; click Zoom under a thumbnail",
                 bg=HDR_BG, fg="#aaccff", font=("Segoe UI", 9), padx=8).pack(side="left")

        # Main row: left tree | left thumbs | right tree | right thumbs
        main = tk.Frame(self, bg=BG)
        main.pack(side="top", fill="both", expand=True)
        main.columnconfigure(1, weight=1)
        main.columnconfigure(3, weight=1)
        main.rowconfigure(0, weight=1)

        # Left folder tree
        self._left_tree = CompareFolderTree(main, "LEFT FOLDER",
                                            on_select=self._on_left_tree_select,
                                            on_root_change=self._on_left_root_change)
        self._left_tree.grid(row=0, column=0, sticky="nsew")

        # Left thumb panel
        self._left = ThumbPanel(main,
                                on_focus=self._on_left_focus,
                                on_select_change=self._update_buttons,
                                on_zoom=self._open_zoom)
        self._left.grid(row=0, column=1, sticky="nsew")

        # Right folder tree — immediately left of the right thumb panel
        self._right_tree = CompareFolderTree(main, "RIGHT FOLDER",
                                             on_select=self._on_right_tree_select)
        self._right_tree.grid(row=0, column=2, sticky="nsew", padx=(2, 0))

        # Right thumb panel
        self._right = ThumbPanel(main,
                                 on_focus=self._on_right_focus,
                                 on_select_change=self._update_buttons,
                                 on_zoom=self._open_zoom)
        self._right.grid(row=0, column=3, sticky="nsew")

        # Bottom bar
        bot = tk.Frame(self, bg=BG3)
        bot.pack(side="bottom", fill="x")
        bot.columnconfigure(0, weight=1)
        bot.columnconfigure(1, weight=0)
        bot.columnconfigure(2, weight=1)

        self._ops_var = tk.StringVar(value="Ready")
        tk.Label(bot, textvariable=self._ops_var,
                 bg=BG3, fg=TEXT_DIM, font=("Segoe UI", 8),
                 anchor="w", padx=8).grid(row=0, column=0, sticky="w", pady=3)

        ops = tk.Frame(bot, bg=BG3)
        ops.grid(row=0, column=1, pady=2)
        self._build_ops_strip(ops)

        if self._embedded:
            tk.Button(bot, text="✔  Send to FTDB", bg=GREEN, fg="white",
                      disabledforeground="#cccccc",
                      font=("Segoe UI", 9, "bold"), relief="flat",
                      padx=12, pady=4, cursor="hand2",
                      command=self._write_result).grid(row=0, column=2, sticky="e", padx=8, pady=3)

        self._update_buttons()

    def _build_ops_strip(self, ops):
        # Single direction arrow: left of buttons when moving left → right,
        # right of buttons when moving right → left.  No surrounding button box.
        self._arrow_left = tk.Label(ops, text="➜", bg=BG3, fg=SEL_BD,
                                    font=("Segoe UI", 22, "bold"))
        self._arrow_right = tk.Label(ops, text="⬅", bg=BG3, fg=SEL_BD,
                                     font=("Segoe UI", 22, "bold"))

        self._btn_frame = tk.Frame(ops, bg=BG3)

        def _btn(text, color, cmd):
            b = tk.Button(self._btn_frame, text=text, bg=color, fg="white",
                          disabledforeground="#cccccc",
                          font=("Segoe UI", 9, "bold"), relief="flat",
                          padx=10, pady=3, cursor="hand2",
                          activebackground="#555555", activeforeground="white",
                          command=cmd)
            b.pack(side="left", padx=2)
            return b

        self._btn_move = _btn("Move",    GREEN, self._do_move)
        self._btn_copy = _btn("Copy",    BLUE,  self._do_copy)
        self._btn_del  = _btn("Delete",  RED,   self._do_delete)
        self._btn_sel_all  = _btn("Select All",  "#335577", self._sel_all)
        self._btn_sel_none = _btn("Deselect", "#445566", self._sel_none)
        self._btn_invert = _btn("Invert", "#555555", self._invert_sel)

        self._show_direction_arrow("left")

    def _show_direction_arrow(self, active_side):
        """Show one direction arrow on the source side of the action buttons."""
        for widget in (self._arrow_left, self._btn_frame, self._arrow_right):
            try:
                widget.pack_forget()
            except Exception:
                pass

        if active_side == "left":
            self._arrow_left.pack(side="left", padx=(0, 10))
            self._btn_frame.pack(side="left")
        else:
            self._btn_frame.pack(side="left")
            self._arrow_right.pack(side="left", padx=(10, 0))

    # ── Focus / arrow ─────────────────────────────────────────────────────────

    def _on_left_focus(self):
        self._active = "left"
        self._show_direction_arrow("left")
        self._left.set_active(True)
        self._right.set_active(False)
        self._update_buttons()

    def _on_right_focus(self):
        self._active = "right"
        self._show_direction_arrow("right")
        self._right.set_active(True)
        self._left.set_active(False)
        self._update_buttons()

    # ── Tree callbacks ────────────────────────────────────────────────────────

    def _on_left_root_change(self, root):
        """Default the right side from the left ROOT while right is still empty."""
        root = _ui_path(root)
        if root and os.path.isdir(root) and self._right_tree.is_empty():
            self._right_tree.set_folder(root)
            self._right.load_folder(root)

    def _on_left_tree_select(self, folder):
        folder = _ui_path(folder)
        self._left.load_folder(folder)
        if self._right_tree.is_empty():
            root = self._left_tree.get_root() or folder
            self._on_left_root_change(root)
        self._on_left_focus()

    def _on_right_tree_select(self, folder):
        self._right.load_folder(_ui_path(folder))
        self._on_right_focus()

    # ── Button state ──────────────────────────────────────────────────────────

    def _update_buttons(self):
        src = self._left  if self._active == "left"  else self._right
        dst = self._right if self._active == "left"  else self._left
        has_sel = bool(src.get_selected())
        has_dst = bool(dst.get_folder())

        def s(ok): return "normal" if ok else "disabled"
        self._btn_move.config(state=s(has_sel and has_dst))
        self._btn_copy.config(state=s(has_sel and has_dst))
        self._btn_del.config(state=s(has_sel))
        self._btn_sel_all.config(state=s(bool(src.get_folder())))
        self._btn_sel_none.config(state=s(has_sel))
        self._btn_invert.config(state=s(bool(src.get_folder())))

    # ── Selection ─────────────────────────────────────────────────────────────

    def _src_panel(self):
        return self._left if self._active == "left" else self._right

    def _dst_panel(self):
        return self._right if self._active == "left" else self._left

    def _sel_all(self):
        self._src_panel().select_all()

    def _sel_none(self):
        self._src_panel().select_none()

    def _invert_sel(self):
        self._src_panel().invert_selection()

    def _open_zoom(self, panel, path):
        try:
            CompareZoomWindow(self, panel, path)
        except Exception as e:
            messagebox.showerror("Zoom failed", str(e), parent=self)

    # ── Operations ────────────────────────────────────────────────────────────

    def _do_move(self):  self._do_op("MOVE")
    def _do_copy(self):  self._do_op("COPY")

    def _do_op(self, op):
        src = self._src_panel()
        dst = self._dst_panel()
        files = src.get_selected()
        _seen = set()
        files = [p for p in files if not (os.path.normcase(os.path.normpath(p)) in _seen or _seen.add(os.path.normcase(os.path.normpath(p))))]
        if not files: return
        dst_folder = dst.get_folder()
        if not dst_folder or not os.path.isdir(dst_folder):
            messagebox.showwarning("No destination",
                "The destination folder is not set.", parent=self)
            return
        verb = "Move" if op == "MOVE" else "Copy"
        n = len(files)
        if not messagebox.askyesno(f"{verb} files",
                f"{verb} {n} file{'s' if n!=1 else ''} to:\n{dst_folder}?",
                parent=self): return
        errors = []
        for path in files:
            dst_path = os.path.join(dst_folder, os.path.basename(path))
            if os.path.exists(_longpath(dst_path)):
                stem, ext = os.path.splitext(os.path.basename(path))
                i = 1
                while os.path.exists(_longpath(os.path.join(dst_folder, f"{stem}_{i}{ext}"))):
                    i += 1
                dst_path = os.path.join(dst_folder, f"{stem}_{i}{ext}")
            try:
                if op == "MOVE": shutil.move(_longpath(path), _longpath(dst_path))
                else:            shutil.copy2(_longpath(path), _longpath(dst_path))
                self._operations.append((op, path, dst_path))
            except Exception as e:
                errors.append(f"{os.path.basename(path)}: {e}")
        src.reload()
        dst.reload()
        self._ops_var.set(f"{'Moved' if op=='MOVE' else 'Copied'} {n-len(errors)} file{'s' if n!=1 else ''}")
        if errors:
            messagebox.showerror("Errors", "\n".join(errors), parent=self)

    def _do_delete(self):
        panel = self._src_panel()
        files = panel.get_selected()
        _seen = set()
        files = [p for p in files if not (os.path.normcase(os.path.normpath(p)) in _seen or _seen.add(os.path.normcase(os.path.normpath(p))))]
        if not files: return
        n = len(files)
        if not messagebox.askyesno("Delete files",
                f"Permanently delete {n} file{'s' if n!=1 else ''}?\n\nThis cannot be undone.",
                parent=self): return
        errors = []
        for path in files:
            try:
                os.remove(_longpath(path))
                self._operations.append(("DELETE", path, ""))
            except Exception as e:
                errors.append(f"{os.path.basename(path)}: {e}")
        panel.reload()
        self._ops_var.set(f"Deleted {n-len(errors)} file{'s' if n!=1 else ''}")
        if errors:
            messagebox.showerror("Errors", "\n".join(errors), parent=self)

    # ── IPC ───────────────────────────────────────────────────────────────────

    def _poll_ipc(self):
        try:
            req_path = os.path.join(_ipc_dir(), "FTCompare_request.csv")
            if os.path.exists(req_path):
                with open(req_path, encoding="utf-8") as f:
                    lines = [l.strip() for l in f if l.strip()]
                seq = int(lines[0].split(",",1)[1]) if lines and lines[0].startswith("SEQ,") else 0
                if seq != self._ipc_seq:
                    self._ipc_seq = seq
                    os.remove(req_path)
                    left = right = ""
                    for line in lines[1:]:
                        if line.startswith("LEFT,"):  left  = _ui_path(line.split(",",1)[1])
                        if line.startswith("RIGHT,"): right = _ui_path(line.split(",",1)[1])
                    self._operations.clear()
                    if left and os.path.isdir(left):
                        self._left_tree.set_folder(left)
                        self._left.load_folder(left)
                        if not right:
                            right = left
                    if right and os.path.isdir(right):
                        self._right_tree.set_folder(right)
                        self._right.load_folder(right)
                    self.deiconify(); self.lift()
                    self.attributes("-topmost", True)
                    self.after(100, lambda: self.attributes("-topmost", False))
                    self.focus_force()
        except Exception as e:
            print(f"FTCompare poll error: {e}")
        self.after(500, self._poll_ipc)

    def _write_result(self):
        if not self._operations:
            messagebox.showinfo("Nothing to send",
                "No operations performed yet.", parent=self)
            return
        try:
            res_path = os.path.join(_ipc_dir(), "FTCompare_result.csv")
            with open(res_path, "w", encoding="utf-8") as f:
                f.write(f"SEQ,{self._ipc_seq}\n")
                for op, src, dst in self._operations:
                    f.write(f"{op},{src},{dst}\n" if dst else f"{op},{src}\n")
            n = len(self._operations)
            messagebox.showinfo("Sent to FTDB",
                f"{n} operation{'s' if n!=1 else ''} sent to FileTagger.", parent=self)
        except Exception as e:
            messagebox.showerror("Send failed", str(e), parent=self)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    args     = sys.argv[1:]
    embedded = "--embedded" in args
    paths    = [a for a in args if not a.startswith("--")]
    left     = _ui_path(paths[0]) if len(paths) > 0 else ""
    right    = _ui_path(paths[1]) if len(paths) > 1 else ""

    # Standalone default: if Projects.ini exists, open the active project Photos root.
    # If Projects.ini/root is absent, leave the root boxes empty for manual entry.
    if not embedded and not left and not right:
        roots = read_project_roots(__file__)
        photos_root = roots.get("photos", "")
        if photos_root and os.path.isdir(photos_root):
            left = photos_root
            right = photos_root

    FTCompare(left_folder=left, right_folder=right, embedded=embedded).mainloop()

if __name__ == "__main__":
    main()
