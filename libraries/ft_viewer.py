"""ft_viewer.py — shared viewer panel for FileTagger apps.

Additive helper used first by FTView.  It does not change existing apps unless
they explicitly import it.

Responsibilities:
- display image/PDF preview in a zoomable canvas
- show a large centred message for empty folders/no file
- previous/next controls via host callback
- rotate image files (-90/+90/180) and notify host to refresh thumbnails
- open PDFs (or any current file) in the system viewer
"""

from __future__ import annotations

import os
import sys
import subprocess
import threading
import tkinter as tk
from tkinter import messagebox

from PIL import Image, ImageTk, ImageOps, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True

PHOTO_EXTS = {".jpg", ".jpeg"}
PDF_EXTS = {".pdf"}
DOCX_EXTS = {".docx"}
OFFICE_EXTS = DOCX_EXTS
VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".avi", ".mkv", ".wmv", ".mpg", ".mpeg", ".mts", ".m2ts"}

try:
    import fitz  # PyMuPDF
    HAVE_FITZ = True
except Exception:
    fitz = None
    HAVE_FITZ = False

try:
    from libraries.ft_office_preview import get_office_preview_pdf
except ImportError:
    try:
        from ft_office_preview import get_office_preview_pdf
    except Exception:
        get_office_preview_pdf = None

try:
    # Try package-qualified path first (when imported as libraries.ft_viewer from FTmod)
    from libraries.ft_movie import make_movie_thumbnail_fast as _make_video_thumb_viewer
    HAVE_FT_MOVIE_VIEWER = True
except ImportError:
    try:
        # Fallback: bare name when libraries/ is directly in sys.path
        from ft_movie import make_movie_thumbnail_fast as _make_video_thumb_viewer
        HAVE_FT_MOVIE_VIEWER = True
    except ImportError:
        _make_video_thumb_viewer = None
        HAVE_FT_MOVIE_VIEWER = False


def _longpath_default(path: str) -> str:
    if os.name == "nt":
        path = path.replace("/", "\\")
        if not path.startswith("\\\\?\\"):
            return "\\\\?\\" + os.path.abspath(path)
    return path


def _load_image_preview(path: str, longpath_func=None) -> Image.Image:
    lp = longpath_func or _longpath_default
    img = Image.open(lp(path))
    img = ImageOps.exif_transpose(img)
    return img.convert("RGB")


def _load_pdf_preview(path: str, longpath_func=None, zoom: float = 1.4) -> Image.Image:
    if not HAVE_FITZ:
        raise RuntimeError("PyMuPDF is required to preview PDFs")
    lp = longpath_func or _longpath_default
    doc = fitz.open(lp(path))
    try:
        if doc.page_count < 1:
            raise RuntimeError("PDF has no pages")
        page = doc[0]
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        return img
    finally:
        try:
            doc.close()
        except Exception:
            pass


def load_preview_image(path: str, longpath_func=None) -> Image.Image:
    ext = os.path.splitext(path)[1].lower()
    if ext in PDF_EXTS:
        return _load_pdf_preview(path, longpath_func=longpath_func)
    if ext in OFFICE_EXTS:
        if get_office_preview_pdf is None:
            raise RuntimeError("Office preview helper is not available")
        pdf_path = get_office_preview_pdf(path)
        return _load_pdf_preview(pdf_path, longpath_func=None)
    return _load_image_preview(path, longpath_func=longpath_func)


def make_preview_thumbnail(path: str, size: int, longpath_func=None):
    """Return (PIL image, ok, error) for thumbnail display.

    This is deliberately separate from ft_thumbs.py during FTView testing so
    existing apps keep their current thumbnail path untouched.
    """
    size = max(1, int(size))
    try:
        img = load_preview_image(path, longpath_func=longpath_func)
        img.thumbnail((size, size), Image.BILINEAR)
        return img, True, None
    except Exception as e:
        # Small dark placeholder, matching the broad ft_thumbs behaviour.
        return Image.new("RGB", (size, size), (50, 50, 50)), False, str(e)


def open_external(path: str) -> None:
    """Open path with the OS default viewer."""
    if not path:
        return
    if os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def rotate_image_file(path: str, degrees: int, longpath_func=None) -> None:
    """Rotate an image file in-place.  Positive degrees are clockwise."""
    ext = os.path.splitext(path)[1].lower()
    if ext not in PHOTO_EXTS:
        raise RuntimeError("Rotate is only available for image files")
    lp = longpath_func or _longpath_default
    full = lp(path)
    img = Image.open(full)
    img = ImageOps.exif_transpose(img).convert("RGB")
    # PIL positive angle is counter-clockwise. UI positive is clockwise.
    rotated = img.rotate(-int(degrees), expand=True)
    save_kwargs = {}
    if ext in {".jpg", ".jpeg"}:
        save_kwargs.update({"format": "JPEG", "quality": 95, "subsampling": 0})
    rotated.save(full, **save_kwargs)


class ZoomCanvas(tk.Canvas):
    """Zoomable/pannable canvas with a centred message state."""

    def __init__(self, parent, zbar_height=34, bg="#777777"):
        self._zbar_height = zbar_height
        super().__init__(parent, bg=bg, highlightthickness=0)
        self.img = None
        self.photo = None
        self.scale = 1.0
        self.fit_scale = 1.0
        self.offset = [0, 0]
        self.drag_start = None
        self._fit_after_id = None
        self._message = "No file selected"
        self.bind("<Configure>", self.on_configure)
        self.bind("<MouseWheel>", self.on_wheel)
        self.bind("<ButtonPress-1>", self.on_press)
        self.bind("<B1-Motion>", self.on_drag)
        self.bind("<Double-Button-1>", lambda e: self.fit())

    def set_message(self, text: str):
        self.img = None
        self.photo = None
        self._message = text or ""
        self.render()

    def set_image(self, img):
        self.img = img
        self.photo = None
        self._message = ""
        self.scale = 1.0
        self.fit_scale = 1.0
        self.offset = [0, 0]
        if self._fit_after_id is not None:
            try:
                self.after_cancel(self._fit_after_id)
            except Exception:
                pass
        self._fit_after_id = self.after(30, self.fit)

    def on_configure(self, event=None):
        if self._fit_after_id is not None:
            try:
                self.after_cancel(self._fit_after_id)
            except Exception:
                pass
        self._fit_after_id = self.after_idle(self._finish_configure)

    def _finish_configure(self):
        self._fit_after_id = None
        if self.img is None:
            self.render()
        else:
            self.fit()

    def _visible_size(self):
        """Return the actual canvas drawing area.

        The PDF/image page-turn bar is a separate fixed row underneath this
        canvas.  Do not subtract the bar height from the parent window here:
        after a sash drag Tk has already given the canvas its own reduced
        height, and subtracting again can make layout/rendering fight the
        bottom controls.
        """
        cw = max(1, int(self.winfo_width()))
        ch = max(1, int(self.winfo_height()))
        return cw, ch

    def fit(self):
        self._fit_after_id = None
        self.update_idletasks()
        cw, ch = self._visible_size()
        if cw < 20 or ch < 20:
            self.after(50, self.fit)
            return
        if self.img is None:
            self.render()
            return
        iw, ih = self.img.size
        if iw <= 0 or ih <= 0:
            return
        self.fit_scale = min(cw / iw, ch / ih)
        self.scale = self.fit_scale
        nw, nh = max(1, int(iw * self.scale)), max(1, int(ih * self.scale))
        self.offset = [int((cw - nw) / 2), int((ch - nh) / 2)]
        self.render()

    def render(self):
        self.delete("all")
        cw, ch = self._visible_size()
        if self.img is None:
            if self._message:
                self.create_text(
                    cw // 2,
                    ch // 2,
                    text=self._message,
                    fill="white",
                    font=("Segoe UI", 24, "bold"),
                    anchor="center",
                )
            return
        iw, ih = self.img.size
        nw = max(1, int(iw * self.scale))
        nh = max(1, int(ih * self.scale))
        if self.scale == self.fit_scale:
            self.offset = [int((cw - nw) / 2), int((ch - nh) / 2)]
        try:
            disp = self.img.resize((nw, nh), Image.BILINEAR)
            self.photo = ImageTk.PhotoImage(disp)
            self.create_image(self.offset[0], self.offset[1], anchor="nw", image=self.photo)
        except Exception:
            self.set_message("Could not render file")

    def on_wheel(self, event):
        if self.img is None:
            return "break"
        old = self.scale
        factor = 1.12 if event.delta > 0 else 1 / 1.12
        self.scale = max(self.fit_scale * 0.5, min(self.scale * factor, self.fit_scale * 12))
        mx, my = event.x, event.y
        ox, oy = self.offset
        self.offset[0] = int(mx - (mx - ox) * (self.scale / old))
        self.offset[1] = int(my - (my - oy) * (self.scale / old))
        self.render()
        return "break"

    def on_press(self, event):
        self.drag_start = (event.x, event.y, self.offset[0], self.offset[1])

    def on_drag(self, event):
        if not self.drag_start:
            return
        sx, sy, ox, oy = self.drag_start
        self.offset = [ox + event.x - sx, oy + event.y - sy]
        self.render()


class ViewerPanel(tk.Frame):
    """A reusable file viewer with navigation, rotate, PDF page controls and external-open controls."""

    def __init__(self, parent, *, bg="#777777", longpath_func=None,
                 on_select_index=None, on_file_changed=None):
        super().__init__(parent, bg=bg)
        self._longpath = longpath_func or _longpath_default
        self._on_select_index = on_select_index
        self._on_file_changed = on_file_changed
        self.files = []
        self.index = None
        self.current_path = None
        self._load_token = 0
        self._pdf_page_index = 0
        self._pdf_page_count = 0

        # Viewer layout is deliberately fixed:
        #   row 0 = TOP grey image/file controls: Rotate / Prev / Next / Open / Fit
        #   row 1 = image/PDF canvas, expandable
        #   row 2 = BOTTOM blue PDF page-turn bar
        # Resizing the PanedWindow sash must only resize row 1.
        self.grid_rowconfigure(0, weight=0, minsize=34)
        self.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(2, weight=0, minsize=34)
        self.grid_columnconfigure(0, weight=1)

        self.bar = tk.Frame(self, bg="#eeeeee", height=34)
        self.bar.grid(row=0, column=0, sticky="ew")
        self.bar.grid_propagate(False)
        self.bar.pack_propagate(False)

        self.btn_prev = tk.Button(self.bar, text="◀ Prev", command=self.prev_file)
        self.btn_prev.pack(side="left", padx=(4, 2), pady=3)
        self.btn_next = tk.Button(self.bar, text="Next ▶", command=self.next_file)
        self.btn_next.pack(side="left", padx=2, pady=3)

        self.btn_rot_l = tk.Button(self.bar, text="⟲ -90", command=lambda: self.rotate_current(-90))
        self.btn_rot_l.pack(side="left", padx=(12, 2), pady=3)
        self.btn_rot_r = tk.Button(self.bar, text="+90 ⟳", command=lambda: self.rotate_current(90))
        self.btn_rot_r.pack(side="left", padx=2, pady=3)
        self.btn_rot_180 = tk.Button(self.bar, text="180", command=lambda: self.rotate_current(180))
        self.btn_rot_180.pack(side="left", padx=2, pady=3)

        self.btn_open = tk.Button(self.bar, text="Open file", command=self.open_current_external)
        self.btn_open.pack(side="left", padx=(12, 2), pady=3)

        self.btn_fit = tk.Button(self.bar, text="Fit", command=lambda: self.canvas.fit())
        self.btn_fit.pack(side="right", padx=4, pady=3)
        self.lbl_info = tk.Label(self.bar, text="No file selected", bg="#eeeeee", fg="#555555", anchor="w")
        self.lbl_info.pack(side="left", fill="x", expand=True, padx=8)

        self.canvas = ZoomCanvas(self, zbar_height=0, bg=bg)
        self.canvas.grid(row=1, column=0, sticky="nsew")

        self.pdf_bar = tk.Frame(self, bg="#1a5276", height=34)
        self.pdf_bar.grid(row=2, column=0, sticky="ew")
        self.pdf_bar.grid_propagate(False)
        self.pdf_bar.pack_propagate(False)

        # One centred PDF navigation bar only:
        # First | Previous | Page xx of xx | [page entry] | Next | Last
        self.pdf_controls = tk.Frame(self.pdf_bar, bg="#1a5276")
        self.pdf_controls.place(relx=0.5, rely=0.5, anchor="center")
        self.btn_pdf_first = tk.Button(self.pdf_controls, text="First", width=7, command=self.first_pdf_page)
        self.btn_pdf_first.pack(side="left", padx=(0, 3), pady=3)
        self.btn_pdf_prev = tk.Button(self.pdf_controls, text="Previous", width=9, command=self.prev_pdf_page)
        self.btn_pdf_prev.pack(side="left", padx=3, pady=3)
        self.lbl_pdf_page = tk.Label(self.pdf_controls, text="Page 0 of 0", bg="#1a5276", fg="white", anchor="center",
                                     font=("Segoe UI", 10, "bold"), width=14)
        self.lbl_pdf_page.pack(side="left", padx=(10, 6), pady=3)
        self.pdf_page_var = tk.StringVar(value="")
        self.ent_pdf_page = tk.Entry(self.pdf_controls, textvariable=self.pdf_page_var, width=6, justify="center")
        self.ent_pdf_page.pack(side="left", padx=(0, 10), pady=4)
        self.ent_pdf_page.bind("<Return>", self.goto_pdf_page)
        self.btn_pdf_next = tk.Button(self.pdf_controls, text="Next", width=7, command=self.next_pdf_page)
        self.btn_pdf_next.pack(side="left", padx=3, pady=3)
        self.btn_pdf_last = tk.Button(self.pdf_controls, text="Last", width=7, command=self.last_pdf_page)
        self.btn_pdf_last.pack(side="left", padx=(3, 0), pady=3)

        self.bind("<Configure>", self._on_panel_configure, add="+")
        self.show_message("No file selected")

    def _on_panel_configure(self, event=None):
        """Keep the top controls and bottom PDF page bar in their fixed rows."""
        try:
            self.bar.grid(row=0, column=0, sticky="ew")
            self.canvas.grid(row=1, column=0, sticky="nsew")
            if self._is_current_pdf():
                self.pdf_bar.grid(row=2, column=0, sticky="ew")
            self.bar.lift()
            self.pdf_bar.lift()
        except Exception:
            pass

    def _is_current_pdf(self):
        return os.path.splitext(self.current_path or "")[1].lower() in (PDF_EXTS | OFFICE_EXTS)

    def _set_pdf_bar_visible(self, visible: bool):
        try:
            if visible:
                self.pdf_bar.grid(row=2, column=0, sticky="ew")
            else:
                self.pdf_bar.grid_remove()
        except Exception:
            pass

    def show_message(self, text: str):
        self.current_path = None
        self._pdf_page_index = 0
        self._pdf_page_count = 0
        self.lbl_info.configure(text=text)
        self.canvas.set_message(text)
        self._set_pdf_bar_visible(False)
        self._update_buttons()

    def set_file_list(self, files, index=None):
        self.files = list(files or [])
        if index is None:
            self.index = None
            self.show_message("No file selected" if self.files else "No files in this folder")
            return
        self.show_index(index)

    def show_index(self, index: int):
        if not self.files:
            self.index = None
            self.show_message("No files in this folder")
            return
        if index < 0 or index >= len(self.files):
            return
        self.index = index
        self.current_path = self.files[index]
        self._pdf_page_index = 0
        self._pdf_page_count = 0
        self.load_current_async()
        self._update_buttons()

    def _preview_pdf_path_for(self, path):
        """Return the real PDF path to render for PDF-like documents.

        Real PDFs are rendered directly. DOCX files are first converted to a
        cached temporary PDF by ft_office_preview.py, then rendered through the
        same PyMuPDF page pipeline as ordinary PDFs. This is what makes DOCX
        page turning behave like PDF page turning.
        """
        ext = os.path.splitext(path)[1].lower()
        if ext in OFFICE_EXTS:
            if get_office_preview_pdf is None:
                raise RuntimeError("Office preview helper is not available")
            return get_office_preview_pdf(path), False
        return path, True

    def _load_pdf_page(self, path, page_index):
        if not HAVE_FITZ:
            raise RuntimeError("PyMuPDF is required to preview PDFs")
        open_path, use_longpath = self._preview_pdf_path_for(path)
        doc = fitz.open(self._longpath(open_path) if use_longpath else open_path)
        try:
            page_count = int(doc.page_count or 0)
            if page_count < 1:
                raise RuntimeError("PDF has no pages")
            page_index = max(0, min(int(page_index), page_count - 1))
            page = doc[page_index]
            mat = fitz.Matrix(1.4, 1.4)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            return img, page_index, page_count
        finally:
            try:
                doc.close()
            except Exception:
                pass

    def load_current_async(self):
        path = self.current_path
        if not path:
            self.show_message("No file selected")
            return
        self._load_token += 1
        token = self._load_token
        ext = os.path.splitext(path)[1].lower()
        is_pdf = ext in (PDF_EXTS | OFFICE_EXTS)
        is_video = ext in VIDEO_EXTS
        page_index = self._pdf_page_index if is_pdf else 0
        self.lbl_info.configure(text=os.path.basename(path))
        self.canvas.set_message("Loading...")
        self._set_pdf_bar_visible(is_pdf)
        self._update_buttons()

        def worker():
            try:
                if is_video:
                    if not HAVE_FT_MOVIE_VIEWER:
                        raise RuntimeError("ft_movie library not available for video preview")
                    # Extract frame at full/high resolution (same position used for thumbnail)
                    img, ok, err_msg = _make_video_thumb_viewer(
                        path, 0.1, 1920, longpath_func=self._longpath)
                    if not ok or img is None:
                        raise RuntimeError(err_msg or "Could not extract video frame")
                    actual_page, page_count = 0, 0
                elif is_pdf:
                    img, actual_page, page_count = self._load_pdf_page(path, page_index)
                else:
                    img = load_preview_image(path, longpath_func=self._longpath)
                    actual_page, page_count = 0, 0
                err = None
            except Exception as e:
                img = None
                actual_page, page_count = 0, 0
                err = str(e)

            def apply():
                if token != self._load_token:
                    return
                if is_pdf:
                    self._pdf_page_index = actual_page
                    self._pdf_page_count = page_count
                    self._set_pdf_bar_visible(True)
                else:
                    self._pdf_page_index = 0
                    self._pdf_page_count = 0
                    self._set_pdf_bar_visible(False)
                if img is None:
                    self.canvas.set_message("Could not preview file")
                    if err:
                        self.lbl_info.configure(text=f"{os.path.basename(path)} — {err}")
                else:
                    self.canvas.set_image(img)
                self._update_buttons()

            self.after(0, apply)

        threading.Thread(target=worker, daemon=True).start()

    def prev_file(self):
        if self.index is None or self.index <= 0:
            return
        new_idx = self.index - 1
        if self._on_select_index:
            self._on_select_index(new_idx)
        else:
            self.show_index(new_idx)

    def next_file(self):
        if self.index is None or self.index >= len(self.files) - 1:
            return
        new_idx = self.index + 1
        if self._on_select_index:
            self._on_select_index(new_idx)
        else:
            self.show_index(new_idx)

    def first_pdf_page(self):
        if not self._is_current_pdf() or self._pdf_page_count <= 0:
            return
        if self._pdf_page_index != 0:
            self._pdf_page_index = 0
            self.load_current_async()

    def prev_pdf_page(self):
        if not self._is_current_pdf() or self._pdf_page_index <= 0:
            return
        self._pdf_page_index -= 1
        self.load_current_async()

    def next_pdf_page(self):
        if not self._is_current_pdf() or self._pdf_page_index >= self._pdf_page_count - 1:
            return
        self._pdf_page_index += 1
        self.load_current_async()

    def last_pdf_page(self):
        if not self._is_current_pdf() or self._pdf_page_count <= 0:
            return
        last = self._pdf_page_count - 1
        if self._pdf_page_index != last:
            self._pdf_page_index = last
            self.load_current_async()

    def goto_pdf_page(self, event=None):
        if not self._is_current_pdf() or self._pdf_page_count <= 0:
            return "break"
        raw = (self.pdf_page_var.get() or "").strip()
        try:
            page = int(raw)
        except Exception:
            self.pdf_page_var.set(str(self._pdf_page_index + 1))
            return "break"
        page = max(1, min(page, self._pdf_page_count))
        new_index = page - 1
        self.pdf_page_var.set(str(page))
        if new_index != self._pdf_page_index:
            self._pdf_page_index = new_index
            self.load_current_async()
        return "break"

    def rotate_current(self, degrees: int):
        path = self.current_path
        if not path:
            return
        ext = os.path.splitext(path)[1].lower()
        if ext not in PHOTO_EXTS:
            messagebox.showinfo("Rotate", "Rotate is only available for image files.", parent=self)
            return
        self.lbl_info.configure(text=f"Rotating {os.path.basename(path)}...")
        self.canvas.set_message("Rotating...")

        def worker():
            err = None
            try:
                rotate_image_file(path, degrees, longpath_func=self._longpath)
            except Exception as e:
                err = str(e)

            def apply():
                if err:
                    messagebox.showerror("Rotate failed", err, parent=self)
                if self._on_file_changed:
                    self._on_file_changed(path)
                self.load_current_async()

            self.after(0, apply)

        threading.Thread(target=worker, daemon=True).start()

    def open_current_external(self):
        if not self.current_path:
            return
        try:
            open_external(self.current_path)
        except Exception as e:
            messagebox.showerror("Open", str(e), parent=self)

    def _update_buttons(self):
        has = self.index is not None and bool(self.files)
        ext = os.path.splitext(self.current_path or "")[1].lower()
        can_rotate = has and ext in PHOTO_EXTS
        can_prev = has and self.index > 0
        can_next = has and self.index < len(self.files) - 1
        is_pdf = has and ext in (PDF_EXTS | OFFICE_EXTS)
        can_pdf_prev = is_pdf and self._pdf_page_index > 0
        can_pdf_next = is_pdf and self._pdf_page_count > 0 and self._pdf_page_index < self._pdf_page_count - 1

        for btn, enabled in (
            (self.btn_prev, can_prev),
            (self.btn_next, can_next),
            (self.btn_rot_l, can_rotate),
            (self.btn_rot_r, can_rotate),
            (self.btn_rot_180, can_rotate),
            (self.btn_open, has),
            (self.btn_fit, has),
            (self.btn_pdf_first, can_pdf_prev),
            (self.btn_pdf_prev, can_pdf_prev),
            (self.btn_pdf_next, can_pdf_next),
            (self.btn_pdf_last, can_pdf_next),
            (self.ent_pdf_page, is_pdf and self._pdf_page_count > 0),
        ):
            try:
                btn.configure(state=("normal" if enabled else "disabled"))
            except Exception:
                pass
        self.btn_open.configure(text="Open file" if is_pdf else "Open viewer")
        if is_pdf:
            total = self._pdf_page_count or 0
            if total:
                page_text = str(self._pdf_page_index + 1)
                self.lbl_pdf_page.configure(text=f"Page {page_text} of {total}")
                try:
                    if self.pdf_page_var.get() != page_text:
                        self.pdf_page_var.set(page_text)
                except Exception:
                    pass
            else:
                self.lbl_pdf_page.configure(text="Page 0 of 0")
                try:
                    self.pdf_page_var.set("")
                except Exception:
                    pass
            self._set_pdf_bar_visible(True)
        else:
            self.lbl_pdf_page.configure(text="")
            try:
                self.pdf_page_var.set("")
            except Exception:
                pass
            self._set_pdf_bar_visible(False)
