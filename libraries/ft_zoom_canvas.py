"""
ft_zoom_canvas.py — Reusable zoomable/pannable canvas widget for FileTagger suite.

Drop-in replacement for tk.Canvas with built-in zoom and pan.
Used by FTFiler, FTMap, FTImgedit for their embedded preview panels.

Usage:
    from ft_zoom_canvas import ZoomableCanvas

    canvas = ZoomableCanvas(parent, bg="#222222")
    canvas.pack(fill="both", expand=True)

    # Load a PIL Image
    canvas.load_pil(img)

    # Or load from file path (auto PIL/fitz)
    canvas.load_path(path)

    # Programmatic zoom control
    canvas.zoom_fit()
    canvas.zoom_in()
    canvas.zoom_out()

    # Optional: connect an external StringVar to display zoom level
    canvas.set_level_var(tk_stringvar)

    # Optional: A4 mode constrains PDF display to 210:297 ratio
    canvas.set_a4_mode(True)

Controls (built-in):
    Mouse wheel       — zoom in/out centred on cursor
    Click-drag        — pan
    Double-click      — reset to fit
"""

import tkinter as tk

try:
    from PIL import Image, ImageTk, ImageOps
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


def _longpath(p):
    import sys, os
    if sys.platform == "win32" and not p.startswith("\\\\?\\"):
        return "\\\\?\\" + os.path.abspath(p)
    return p


class ZoomableCanvas(tk.Canvas):
    """
    A tk.Canvas subclass with built-in zoom and pan.

    All zoom/pan state is encapsulated. The caller only needs to call
    load_pil() or load_path() — everything else is handled internally.
    """

    def __init__(self, parent, a4_mode=False, bg="#222222", **kwargs):
        super().__init__(parent, bg=bg, highlightthickness=0, cursor="hand2", **kwargs)

        # Image state
        self._img       = None   # full-resolution PIL Image
        self._photo     = None   # current ImageTk.PhotoImage (kept alive)

        # Zoom/pan state
        self._scale     = None   # None = fit mode
        self._offset    = [0, 0] # pan offset in pixels
        self._drag      = None   # drag start state

        # Options
        self._a4_mode   = a4_mode   # constrain to A4 portrait ratio for PDFs
        self._level_var = None      # optional tk.StringVar for zoom label

        # Bindings
        self.bind("<Configure>",        self._on_configure)
        self.bind("<MouseWheel>",        self._on_wheel)
        self.bind("<ButtonPress-1>",     self._on_press)
        self.bind("<B1-Motion>",         self._on_drag)
        self.bind("<ButtonRelease-1>",   self._on_release)
        self.bind("<Double-Button-1>",   lambda e: self.zoom_fit())

    # ── Public API ────────────────────────────────────────────────────────────

    def load_pil(self, img):
        """Load a PIL Image. Resets zoom to fit."""
        self._img    = img
        self._scale  = None
        self._offset = [0, 0]
        self._redraw()

    def load_path(self, path):
        """Load image from file path — auto-detects JPEG or PDF. Resets zoom."""
        import os
        ext = os.path.splitext(path)[1].lower()
        img = None
        try:
            if ext in PHOTO_EXTS and _PIL:
                img = Image.open(_longpath(path))
                try:   img = ImageOps.exif_transpose(img)
                except Exception: pass
                img.thumbnail((3000, 3000), Image.LANCZOS)
            elif ext in PDF_EXTS and _FITZ:
                doc  = _fitz.open(_longpath(path))
                page = doc[0]
                mat  = _fitz.Matrix(3.0, 3.0)
                pix  = page.get_pixmap(matrix=mat, alpha=False)
                img  = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                doc.close()
        except Exception as e:
            print(f"ZoomableCanvas.load_path error: {e}")
        self.load_pil(img)

    def clear(self):
        """Clear the canvas — no image."""
        self._img   = None
        self._photo = None
        self.delete("all")
        self._update_level_var()

    def set_level_var(self, var):
        """Connect an external tk.StringVar to display the current zoom level."""
        self._level_var = var
        self._update_level_var()

    def set_a4_mode(self, enabled):
        """When True, constrain image to A4 portrait ratio (for PDF preview)."""
        self._a4_mode = enabled
        self._redraw()

    def zoom_fit(self):
        """Reset zoom to fit image in canvas."""
        self._scale  = None
        self._offset = [0, 0]
        self._redraw()

    def zoom_in(self):
        """Zoom in by 25%."""
        self._scale = min((self._scale or self._fit_scale()) * 1.25, 16.0)
        self._redraw()

    def zoom_out(self):
        """Zoom out by 20%."""
        self._scale = max((self._scale or self._fit_scale()) * 0.8, 0.05)
        self._redraw()

    # ── Internal ──────────────────────────────────────────────────────────────

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
        # Clamp pan
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
        if self._scale is None:
            self._level_var.set("Fit")
        elif self._img:
            self._level_var.set(f"{int(self._scale * 100)}%")
        else:
            self._level_var.set("")

    def _on_configure(self, event=None):
        self._redraw()

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
