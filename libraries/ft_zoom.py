"""ft_zoom.py — zoom-window mixin for FileTagger.

FT42 extraction step.

This module owns the FileTagger zoom-window controller code that previously
lived inside FT.py. It deliberately stays as a mixin so existing FileTagger
state and callbacks continue to work without changing app behaviour.
"""

from __future__ import annotations

import os
import sys
import tkinter as tk
from tkinter import messagebox


class FTZoomMixin:
    """Mixin containing FileTagger's zoom-window behaviour."""

    def _ftmod(self):
        """Return the module that defines the concrete FileTagger class."""
        return sys.modules[self.__class__.__module__]

    # ── Zoom ──────────────────────────────────────────────────────────────────

    # ── _ZoomState — shared mutable state for the zoom window ─────────────────
    class _ZoomState:
        """Holds all shared mutable state for a zoom window session."""
        __slots__ = [
            'cur_path', 'z_scale', 'z_offset', 'z_full',
            'render_id', 'zoom_ref', 'z_lbl_var',
            'rename_var', 'rename_entry', 'lbl_path', 'lbl_info',
            'struct_dlg_update', 'renaming', 'focusout_id', 'mode',
            '_canvas', '_btn_prev', '_btn_next', '_btn_map', '_drag', 'loaded_path',
        ]
        def __init__(self, path, mode):
            self.cur_path        = [path]
            self.z_scale         = [None]
            self.z_offset        = [0, 0]
            self.z_full          = [None]
            self.render_id       = [None]
            self.zoom_ref        = [None]
            self.z_lbl_var       = None
            self.rename_var      = None
            self.rename_entry    = None
            self.lbl_path        = None
            self.lbl_info        = None
            self.struct_dlg_update = [None]
            self.renaming        = [False]
            self.focusout_id     = [None]
            self.mode            = mode
            self._canvas         = None
            self._btn_prev       = None
            self._btn_next       = None
            self._btn_map        = None
            self._drag           = None
            self.loaded_path      = None

    def _zoom(self, orig_path):
        """Open zoom window. Remembers position between images."""
        existing_x = existing_y = None
        if self._zoom_win:
            try:
                if self._zoom_win.winfo_exists():
                    geom  = self._zoom_win.geometry()
                    parts = geom.replace('-','+').split('+')
                    if len(parts) >= 3:
                        existing_x = int(parts[1]); existing_y = int(parts[2])
                    self._zoom_win.destroy()
            except: pass

        try:    self._zoom_index = self._all_files.index(orig_path)
        except: self._zoom_index = 0

        win_w, win_h, x, y = self._zoom_calc_geometry(orig_path, existing_x, existing_y)

        zw = tk.Toplevel(self.win)
        zw.title("Zoom")
        zw.geometry(f"{win_w}x{win_h}+{x}+{y}")
        zw.configure(bg="#000")
        zw.resizable(True, True)
        zw.minsize(200, 150)
        zw.transient(self.win)
        self._zoom_win = zw

        st = self._ZoomState(orig_path, self.mode)

        # Top bar packed first, then bottom bar, then canvas fills remaining space
        self._zoom_build_top_bar(zw, st)
        canvas, bot = self._zoom_build_frame(zw, st)
        self._zoom_build_bottom_bar(zw, bot, st)
        self._zoom_build_canvas_bindings(canvas, st)
        self._zoom_build_nav(zw, canvas, st)
        self._zoom_build_controls(zw, bot, canvas, st)

        canvas.bind("<Configure>", lambda e: (self._zoom_render(e, canvas, st),
                                               self._zoom_place_nav(canvas, st)))
        zw.after(120, lambda: self._zoom_place_nav(canvas, st))

        def _on_zoom_close():
            path = st.cur_path[0]
            self._zoom_win = None
            zw.destroy()
            self._scroll_grid_to_file(path)
        zw.protocol("WM_DELETE_WINDOW", _on_zoom_close)

        zw.bind("<Left>",  lambda e: self._zoom_nav(zw, canvas, st, -1))
        zw.bind("<Right>", lambda e: self._zoom_nav(zw, canvas, st,  1))
        zw.bind("<Up>",    lambda e: self._zoom_nav(zw, canvas, st, -1))
        zw.bind("<Down>",  lambda e: self._zoom_nav(zw, canvas, st,  1))
        zw.bind("<space>", lambda e: self._zoom_toggle_tag(st))
        zw.focus_set()

        self._zoom_update_info(st)
        zw.after(150, lambda: self._zoom_do_render(canvas, st))

    def _zoom_calc_geometry(self, path, existing_x, existing_y):
        """Calculate zoom window size and position from image dimensions."""
        self.win.update_idletasks()
        try:
            cx = self.canvas.winfo_rootx(); cy = self.canvas.winfo_rooty()
            cw = self.canvas.winfo_width(); ch = self.canvas.winfo_height()
        except:
            cx = self.win.winfo_rootx(); cy = self.win.winfo_rooty()
            cw = self.win.winfo_width(); ch = self.win.winfo_height()
        BAR_H  = 80; margin = 10
        sh     = self.win.winfo_screenheight()
        max_h  = max(200, int(sh * 930 / 1080))
        avail_h = max_h - BAR_H

        def _dims(path):
            if self.mode == "photos":
                try:
                    with self._ftmod().Image.open(self._ftmod()._longpath(path)) as im:
                        from PIL import ImageOps as _IOS
                        return _IOS.exif_transpose(im).size
                except: return (800, 600)
            else:
                if self._ftmod().HAVE_FITZ:
                    try:
                        doc = self._ftmod().fitz.open(self._ftmod()._longpath(path)); pg = doc[0]
                        w,h = int(pg.rect.width), int(pg.rect.height); doc.close()
                        return (w, h)
                    except: pass
                return (595, 842)

        iw, ih = _dims(path)
        ratio  = iw / ih
        win_w  = min(max(200, int(avail_h * ratio)), cw)
        win_h  = max_h
        if existing_x is not None:
            x, y = existing_x, existing_y
        else:
            x = cx + margin
            y = max(0, cy + max(0, (ch - win_h) // 2) - 150)
        return win_w, win_h, x, y

    def _zoom_build_canvas(self, zw, st):
        """Create the shared zoom canvas.

        Import ft_zoom_canvas lazily so a zoom-widget problem cannot prevent the
        main FileTagger layout, folder tree, collections, or thumbnails from
        initialising. The zoom window still uses the shared ZoomableCanvas when
        opened.
        """
        _img_bg = "#fff" if st.mode == "pdfs" else "#000"
        canvas_frame = tk.Frame(zw, bg="#000")
        canvas_frame.pack(fill="both", expand=True)
        try:
            from ft_zoom_canvas import ZoomableCanvas as _FTZoomableCanvas
            canvas = _FTZoomableCanvas(canvas_frame, bg=_img_bg)
        except Exception as _e:
            # Do not let the optional shared zoom widget break the rest of FT.
            # The error is shown inside the zoom window itself.
            canvas = tk.Canvas(canvas_frame, bg=_img_bg, highlightthickness=0)
            canvas._ftzoom_import_error = _e
        canvas.pack(fill="both", expand=True)
        try:
            if st.z_lbl_var is not None and hasattr(canvas, 'set_level_var'):
                canvas.set_level_var(st.z_lbl_var)
        except Exception:
            pass
        st._canvas = canvas   # store so bottom bar buttons can reach it
        return canvas

    def _zoom_build_frame(self, zw, st):
        """Create bottom bar then canvas — correct pack order."""
        bot = tk.Frame(zw, bg="#111")
        bot.pack(fill="x", side="bottom")
        canvas = self._zoom_build_canvas(zw, st)
        return canvas, bot

    def _zoom_build_top_bar(self, zw, st):
        """Build the two-row top bar: filename entry + Edit button + folder label."""
        top = tk.Frame(zw, bg="#e8e8e8"); top.pack(fill="x")
        top_row1 = tk.Frame(top, bg="#e8e8e8"); top_row1.pack(fill="x")
        tk.Label(top_row1, text="Name:", bg="#e8e8e8", fg="#555555",
                 font=("Segoe UI", 8)).pack(side="left", padx=(8,2), pady=(4,0))
        st.rename_var = tk.StringVar()
        st.rename_entry = tk.Entry(top_row1, textvariable=st.rename_var,
                                   bg="white", fg="black", insertbackground="black",
                                   font=("Segoe UI", 9), relief="solid", bd=1, width=36)
        st.rename_entry.pack(side="left", fill="x", expand=True, padx=(0,6), pady=(4,2))
        tk.Button(top_row1, text="Edit", bg="#00aa33", fg="white",
                  font=("Segoe UI", 9, "bold"), relief="flat", padx=10, pady=3,
                  cursor="hand2", activebackground="#008828", activeforeground="white",
                  bd=0, command=lambda: self._zoom_open_structured_rename(zw, st)).pack(
                      side="left", padx=(0,8), pady=(4,2))
        top_row2 = tk.Frame(top, bg="#e8e8e8"); top_row2.pack(fill="x")
        st.lbl_path = tk.Label(top_row2, text=os.path.dirname(st.cur_path[0]),
                               bg="#e8e8e8", fg="#555555", font=("Segoe UI", 8), anchor="w")
        st.lbl_path.pack(side="left", padx=(8,8), pady=(0,4))
        st.rename_var.set(os.path.splitext(os.path.basename(st.cur_path[0]))[0])
        st.rename_entry.bind("<Return>",   lambda e: (self._zoom_do_rename(zw, st), zw.focus_set()))
        st.rename_entry.bind("<Escape>",   lambda e: (self._zoom_revert_entry(st), zw.focus_set()))
        st.rename_entry.bind("<FocusOut>", lambda e: self._zoom_on_focusout(zw, st))

    def _zoom_build_bottom_bar(self, zw, bot, st):
        """Build bottom bar: info label, zoom buttons, PDF viewer button."""
        st.lbl_info = tk.Label(bot, text="", bg="#111", fg="#cccccc", font=("Segoe UI",9))
        st.lbl_info.pack(side="left", padx=8, pady=4)
        st.z_lbl_var = tk.StringVar(value="Fit")
        tk.Label(bot, textvariable=st.z_lbl_var, bg="#111", fg="#6699cc",
                 font=("Segoe UI", 8), padx=4).pack(side="right", pady=4)

        def _zbtn(text, tip, action):
            b = tk.Button(bot, text=text, bg="#223344", fg="white",
                          font=("Segoe UI", 9, "bold"), relief="flat",
                          padx=8, pady=2, cursor="hand2",
                          activebackground="#334455", activeforeground="white",
                          command=action)
            b.pack(side="right", padx=2, pady=3)
            tw=[None]
            def _sh(e):
                tw[0]=tk.Toplevel(b); tw[0].overrideredirect(True); tw[0].configure(bg="#ffffe0")
                tk.Label(tw[0],text=tip,bg="#ffffe0",fg="#111",font=("Segoe UI",8),
                         padx=6,pady=3,relief="solid",bd=1).pack()
                tw[0].geometry(f"+{e.x_root+12}+{e.y_root-30}")
            def _hi(e):
                if tw[0]:
                    try: tw[0].destroy()
                    except: pass
                    tw[0]=None
            b.bind("<Enter>",_sh); b.bind("<Leave>",_hi)

        _zbtn("Fit", "Reset to fit  (double-click)", lambda: self._zoom_fit(st._canvas, st))
        _zbtn("−",   "Zoom out  (mouse wheel down)",  lambda: self._zoom_step(st._canvas, -1, st))
        _zbtn("+",   "Zoom in  (mouse wheel up)",     lambda: self._zoom_step(st._canvas,  1, st))

        if st.mode == "pdfs":
            def _open_viewer():
                try:
                    if os.name == "nt": os.startfile(self._ftmod()._longpath(st.cur_path[0]))
                    else:
                        import subprocess as _sp
                        _sp.Popen(["open" if sys.platform=="darwin" else "xdg-open", st.cur_path[0]])
                except Exception as e:
                    messagebox.showerror("Cannot open PDF", str(e), parent=zw)
            tk.Button(bot, text="📄  Open in PDF Viewer", bg="#335577", fg="white",
                      font=("Segoe UI",9,"bold"), relief="flat", padx=8, pady=2,
                      cursor="hand2", command=_open_viewer).pack(
                          side="left", padx=(8,0), pady=3)

    def _zoom_build_canvas_bindings(self, canvas, st):
        """Zoom/pan is handled by ft_zoom_canvas.ZoomableCanvas.

        This hook is retained so the surrounding zoom-window build code stays
        unchanged, but the shared canvas owns mouse wheel, drag, and double-click.
        """
        try:
            if st.z_lbl_var is not None:
                canvas.set_level_var(st.z_lbl_var)
        except Exception:
            pass

    def _zoom_build_nav(self, zw, canvas, st):
        """Build prev/next chevron buttons overlaid on canvas."""
        btn_prev = tk.Button(canvas, text="❮", bg="#1a2a4a", fg="white",
                             font=("Segoe UI",16,"bold"), relief="flat",
                             padx=6, pady=4, cursor="hand2", bd=0,
                             activebackground="#334466", activeforeground="white",
                             command=lambda: self._zoom_nav(zw, canvas, st, -1))
        btn_next = tk.Button(canvas, text="❯", bg="#1a2a4a", fg="white",
                             font=("Segoe UI",16,"bold"), relief="flat",
                             padx=6, pady=4, cursor="hand2", bd=0,
                             activebackground="#334466", activeforeground="white",
                             command=lambda: self._zoom_nav(zw, canvas, st, 1))
        st._btn_prev = btn_prev
        st._btn_next = btn_next

        if st.mode == "photos":
            st._btn_map = tk.Button(canvas, text="📍 Map", bg="#cc0000", fg="white",
                                    font=("Segoe UI",10,"bold"), relief="flat",
                                    padx=8, pady=4, cursor="hand2", bd=0,
                                    activebackground="#aa0000", activeforeground="white",
                                    command=lambda: self._launch_ftmapimg_from_selection(
                                        center_path=st.cur_path[0]))
        else:
            st._btn_map = None

    def _zoom_build_controls(self, zw, bot, canvas, st):
        """Build rotate/flip/edit buttons for photos mode."""
        if st.mode != "photos": return

        def _do_rotate(degrees):
            path = st.cur_path[0]
            direction = "clockwise" if degrees == -90 else ("180°" if degrees == 180 else "counter-clockwise")
            if not messagebox.askyesno("Rotate Image",
                f"Permanently rotate this image {direction}?\n\n{os.path.basename(path)}\n\n"
                "This modifies the original file and cannot be undone.", parent=zw): return
            try:
                from PIL import ImageOps as _IOS
                img = self._ftmod().Image.open(self._ftmod()._longpath(path))
                img = _IOS.exif_transpose(img)
                img = img.rotate(degrees, expand=True)
                try:
                    from PIL import ExifTags
                    exif = img.getexif()
                    ot = next((k for k,v in ExifTags.TAGS.items() if v=="Orientation"), None)
                    if ot and ot in exif: del exif[ot]
                    img.save(self._ftmod()._longpath(path), quality=95, optimize=True, exif=exif.tobytes())
                except:
                    img.save(self._ftmod()._longpath(path), quality=95, optimize=True)
                self._make_thumb(path)
                st.z_full[0] = None
                st.loaded_path = None
                self._zoom_do_render(canvas, st)
            except Exception as e:
                messagebox.showerror("Rotate failed", str(e), parent=zw)

        def _do_flip(axis):
            path = st.cur_path[0]
            direction = "horizontally" if axis == "h" else "vertically"
            if not messagebox.askyesno("Flip Image",
                f"Permanently flip this image {direction}?\n\n{os.path.basename(path)}\n\n"
                "This modifies the original file and cannot be undone.", parent=zw): return
            try:
                from PIL import ImageOps as _IOS
                img = self._ftmod().Image.open(self._ftmod()._longpath(path))
                img = _IOS.exif_transpose(img)
                img = _IOS.mirror(img) if axis == "h" else _IOS.flip(img)
                try:
                    from PIL import ExifTags
                    exif = img.getexif()
                    ot = next((k for k,v in ExifTags.TAGS.items() if v=="Orientation"), None)
                    if ot and ot in exif: del exif[ot]
                    img.save(self._ftmod()._longpath(path), quality=95, optimize=True, exif=exif.tobytes())
                except:
                    img.save(self._ftmod()._longpath(path), quality=95, optimize=True)
                self._make_thumb(path)
                st.z_full[0] = None
                st.loaded_path = None
                self._zoom_do_render(canvas, st)
            except Exception as e:
                messagebox.showerror("Flip failed", str(e), parent=zw)

        tk.Frame(bot, bg="#333", width=1).pack(side="right", fill="y", padx=4, pady=4)
        for text, cmd in [("↕ Flip V", lambda: _do_flip("v")),
                          ("↔ Flip H", lambda: _do_flip("h"))]:
            tk.Button(bot, text=text, bg="#446644", fg="white",
                      font=("Segoe UI",9,"bold"), relief="flat", padx=6,
                      cursor="hand2", command=cmd).pack(side="right", padx=(0,3), pady=2)
        tk.Label(bot, text="Flip:", bg="#111", fg="#666",
                 font=("Segoe UI",8)).pack(side="right", padx=(6,2), pady=3)
        tk.Frame(bot, bg="#333", width=1).pack(side="right", fill="y", padx=4, pady=4)
        for text, cmd in [("↻ CW",    lambda: _do_rotate(-90)),
                          ("↺ CCW",   lambda: _do_rotate(90)),
                          ("↕↔ 180°", lambda: _do_rotate(180))]:
            tk.Button(bot, text=text, bg="#334466", fg="white",
                      font=("Segoe UI",9,"bold"), relief="flat", padx=6,
                      cursor="hand2", command=cmd).pack(side="right", padx=(0,3), pady=2)
        tk.Label(bot, text="Rotate:", bg="#111", fg="#666",
                 font=("Segoe UI",8)).pack(side="right", padx=(6,2), pady=3)
        tk.Frame(bot, bg="#333", width=1).pack(side="right", fill="y", padx=4, pady=4)
        tk.Button(bot, text="✂ Edit", bg="#334466", fg="white",
                  font=("Segoe UI",9,"bold"), relief="flat", padx=10,
                  cursor="hand2",
                  command=lambda: self._launch_ftediti(st.cur_path[0])).pack(
                      side="right", padx=(0,3), pady=2)

    # ── Zoom window — canvas rendering ────────────────────────────────────────

    def _zoom_fit_scale(self, canvas, st):
        """Compatibility wrapper for older zoom code; ft_zoom_canvas owns fit scale."""
        try:
            return canvas._fit_scale()
        except Exception:
            return 1.0

    def _zoom_update_zlbl(self, canvas, st):
        if st.z_lbl_var is None: return
        try:
            if getattr(canvas, '_scale', None) is None:
                st.z_lbl_var.set("Fit")
            else:
                st.z_lbl_var.set(f"{int(canvas._scale * 100)}%")
        except Exception:
            st.z_lbl_var.set("Fit")

    def _zoom_fit(self, canvas, st):
        try:
            if hasattr(canvas, 'zoom_fit'):
                canvas.zoom_fit()
        except Exception:
            pass
        st.z_scale[0] = None; st.z_offset[0] = 0; st.z_offset[1] = 0
        self._zoom_update_zlbl(canvas, st)
        self._zoom_place_nav(canvas, st)

    def _zoom_step(self, canvas, direction, st):
        try:
            if hasattr(canvas, 'zoom_in') and hasattr(canvas, 'zoom_out'):
                if direction > 0: canvas.zoom_in()
                else:             canvas.zoom_out()
        except Exception:
            pass
        try:
            st.z_scale[0] = canvas._scale
            st.z_offset[0], st.z_offset[1] = canvas._offset
        except Exception:
            pass
        self._zoom_update_zlbl(canvas, st)
        self._zoom_place_nav(canvas, st)

    def _zoom_on_wheel(self, canvas, event, st):
        # Retained for compatibility; ft_zoom_canvas handles the bound event.
        try:
            return canvas._on_wheel(event)
        except Exception:
            return "break"

    def _zoom_on_press(self, canvas, event, st):
        try: canvas._on_press(event)
        except Exception: pass

    def _zoom_on_drag(self, canvas, event, st):
        try: canvas._on_drag(event)
        except Exception: pass

    def _zoom_on_release(self, canvas, st):
        try: canvas._on_release(None)
        except Exception: pass

    def _zoom_load_full(self, path, canvas, st):
        """Load current file through the shared ZoomableCanvas engine."""
        try:
            if hasattr(canvas, '_ftzoom_import_error'):
                raise canvas._ftzoom_import_error
            if not hasattr(canvas, 'load_path'):
                raise RuntimeError('ft_zoom_canvas.ZoomableCanvas is not available')
            canvas.load_path(path)
            st.z_full[0] = getattr(canvas, '_img', None)
            st.loaded_path = path
        except Exception as _e:
            print(f"_zoom_load_full error: {_e}")
            st.z_full[0] = None
            st.loaded_path = None
        st.z_scale[0] = None; st.z_offset[0] = 0; st.z_offset[1] = 0
        self._zoom_update_zlbl(canvas, st)

    def _zoom_render(self, event, canvas, st):
        """Debounced redraw triggered by Configure."""
        if st.render_id[0]:
            try: canvas.winfo_toplevel().after_cancel(st.render_id[0])
            except: pass
        st.render_id[0] = canvas.winfo_toplevel().after(80, lambda: self._zoom_do_render(canvas, st))

    def _zoom_do_render(self, canvas, st):
        """Render current file using ft_zoom_canvas, preserving FT's zoom-window controls."""
        st.render_id[0] = None
        try:
            canvas.winfo_toplevel().update_idletasks()
        except Exception:
            pass
        cw = canvas.winfo_width(); ch = canvas.winfo_height()
        if cw < 2 or ch < 2:
            try:
                st.render_id[0] = canvas.winfo_toplevel().after(80, lambda: self._zoom_do_render(canvas, st))
            except Exception:
                pass
            return
        path = st.cur_path[0]
        try:
            if getattr(st, 'loaded_path', None) != path or getattr(canvas, '_img', None) is None:
                self._zoom_load_full(path, canvas, st)
            else:
                canvas._redraw()
                st.z_full[0] = getattr(canvas, '_img', None)
            if st.z_full[0] is None:
                canvas.delete("all")
                canvas.create_text(cw//2, ch//2, anchor="center",
                                   text=f"Cannot display:\n{os.path.basename(path)}",
                                   fill="#ff6666", font=("Segoe UI",10), justify="center", width=cw-40)
                return
            try:
                st.z_scale[0] = canvas._scale
                st.z_offset[0], st.z_offset[1] = canvas._offset
            except Exception:
                pass
            self._zoom_update_zlbl(canvas, st)
            canvas.winfo_toplevel().after(10, lambda: self._zoom_place_nav(canvas, st))
        except Exception as _e:
            canvas.delete("all")
            canvas.create_text(cw//2, ch//2, anchor="center",
                               text=f"Cannot display:\n{os.path.basename(path)}\n{_e}",
                               fill="#ff6666", font=("Segoe UI",10), justify="center", width=cw-40)

    def _zoom_place_nav(self, canvas, st):
        """Place prev/next chevrons and map/edit buttons on canvas."""
        cw = canvas.winfo_width(); ch = canvas.winfo_height()
        if cw < 2 or ch < 2: return
        bp = st._btn_prev; bn = st._btn_next
        bw = bp.winfo_reqwidth(); bh = bp.winfo_reqheight()
        by = (ch - bh) // 2
        canvas.delete("nav_bg")
        for rx1, rx2 in [(0, bw), (cw-bw, cw)]:
            canvas.create_rectangle(rx1, by, rx2, by+bh,
                                    fill="#000000", stipple="gray50",
                                    outline="", tags="nav_bg")
        bp.place(x=0, y=by, anchor="nw"); bn.place(x=cw-bw, y=by, anchor="nw")
        bp.lift(); bn.lift()
        if hasattr(st, "_btn_map") and st._btn_map:
            has_gps = self._ftmod()._get_gps_coords(st.cur_path[0]) is not None
            if has_gps:
                st._btn_map.update_idletasks()
                bw2 = st._btn_map.winfo_reqwidth() or 80
                bh2 = st._btn_map.winfo_reqheight() or 30
                canvas.delete("map_bg")
                canvas.create_rectangle(0, ch-bh2-4, bw2+4, ch,
                                         fill="#000000", stipple="gray50",
                                         outline="", tags="map_bg")
                st._btn_map.place(x=2, y=ch-bh2-2, anchor="nw"); st._btn_map.lift()
            else:
                st._btn_map.place_forget(); canvas.delete("map_bg")

    # ── Zoom window — navigation ───────────────────────────────────────────────

    def _zoom_nav(self, zw, canvas, st, direction):
        """Navigate prev/next through _all_files."""
        idx = self._zoom_index + direction
        if idx < 0 or idx >= len(self._all_files): return
        self._zoom_cancel_focusout(zw, st)
        if st.rename_var.get().strip() != os.path.splitext(os.path.basename(st.cur_path[0]))[0]:
            self._zoom_do_rename(zw, st)
            try: idx = self._all_files.index(st.cur_path[0]) + direction
            except: pass
            if idx < 0 or idx >= len(self._all_files): return
        self._zoom_index = idx
        path = self._all_files[idx]
        self._focused_orig = path
        st.cur_path[0] = path
        st.z_full[0] = None
        st.loaded_path = None
        self._zoom_update_info(st)
        self._zoom_do_render(canvas, st)
        if st.struct_dlg_update[0]:
            try: st.struct_dlg_update[0](path)
            except: st.struct_dlg_update[0] = None
        if st.mode == "photos":
            self._send_to_ftediti(path)

    def _zoom_toggle_tag(self, st):
        """Toggle tag on current file via spacebar."""
        path = st.cur_path[0]
        for item in self.thumb_widgets:
            if item[2] == path:
                self._toggle_tag_canvas(path, item[0]); return
        if path in self.tagged:
            self.tagged.discard(path)
            if path in self.tagged_order: self.tagged_order.remove(path)
        else:
            self.tagged.add(path)
            if path not in self.tagged_order: self.tagged_order.append(path)
        self._save_current_collection()

    def _zoom_update_info(self, st):
        """Update info label and path label for current file."""
        path = st.cur_path[0]
        if st.lbl_path: st.lbl_path.config(text=os.path.dirname(path))
        if st.lbl_info:
            sz = os.path.getsize(path)//1024 if os.path.exists(path) else 0
            info = f"{sz:,} KB"
            if st.mode == "pdfs" and self._ftmod().HAVE_FITZ:
                pages, _ = self._ftmod()._get_pdf_info(path)
                if pages: info = f"{pages} pages   {info}"
            st.lbl_info.config(text=info)
        stem = os.path.splitext(os.path.basename(path))[0]
        if st.rename_var: st.rename_var.set(stem)

    # ── Zoom window — rename ───────────────────────────────────────────────────

    def _zoom_revert_entry(self, st):
        if st.rename_var:
            st.rename_var.set(os.path.splitext(os.path.basename(st.cur_path[0]))[0])

    def _zoom_cancel_focusout(self, zw, st):
        if st.focusout_id[0]:
            try: zw.after_cancel(st.focusout_id[0])
            except: pass
            st.focusout_id[0] = None

    def _zoom_on_focusout(self, zw, st):
        if st.renaming[0]: return
        self._zoom_cancel_focusout(zw, st)
        def _check():
            st.focusout_id[0] = None
            stem = os.path.splitext(os.path.basename(st.cur_path[0]))[0]
            if not st.renaming[0] and st.rename_var.get().strip() != stem:
                self._zoom_do_rename(zw, st)
        st.focusout_id[0] = zw.after(150, _check)

    def _zoom_open_structured_rename(self, zw, st):
        """Open the structured rename dialog."""
        self._zoom_cancel_focusout(zw, st)
        path = st.cur_path[0]
        ext  = os.path.splitext(path)[1]
        stem = os.path.splitext(os.path.basename(path))[0]
        def _on_apply(new_stem):
            if new_stem == os.path.splitext(os.path.basename(st.cur_path[0]))[0]: return
            st.rename_var.set(new_stem)
            self._zoom_do_rename(zw, st)
        update_fn = self._ftmod()._show_structured_rename_dialog(self.win, stem, ext, _on_apply, zoom_win=zw)
        st.struct_dlg_update[0] = update_fn

    def _zoom_do_rename(self, zw, st):
        """Perform the rename. Returns True if renamed, False otherwise."""
        if st.renaming[0]: return False
        new_stem = st.rename_var.get().strip()
        path = st.cur_path[0]
        ext  = os.path.splitext(os.path.basename(path))[1]
        if new_stem.lower().endswith(ext.lower()): new_stem = new_stem[:-len(ext)]
        stem = os.path.splitext(os.path.basename(path))[0]
        if not new_stem or new_stem == stem:
            self._zoom_revert_entry(st); return False
        st.renaming[0] = True
        try:
            if not messagebox.askyesno("Confirm rename",
                    f"Rename to:\n{new_stem}{ext}?", parent=zw):
                self._zoom_revert_entry(st); return False
            new_path = os.path.join(os.path.dirname(path), new_stem + ext)
            if os.path.exists(self._ftmod()._longpath(new_path)):
                messagebox.showerror("Name conflict", f"{new_stem+ext} already exists.", parent=zw)
                self._zoom_revert_entry(st); return False
            os.rename(self._ftmod()._longpath(path), self._ftmod()._longpath(new_path))
            self._handle_rename(path, new_path)
            # Update canvas cell filename label
            icon = "📷 " if self.mode == "photos" else "📄 "
            new_fname = os.path.basename(new_path)
            for i, item in enumerate(self.thumb_widgets):
                if item[2] == path:
                    try:
                        cv = item[0]
                        cv.itemconfigure("fname_text",
                                         text=self._ftmod()._fit_text(icon+new_fname, cv.winfo_width()-28))
                        lst = list(item); lst[2] = new_path
                        self.thumb_widgets[i] = tuple(lst)
                    except: pass
                    break
            st.cur_path[0] = new_path
            try: self._zoom_index = self._all_files.index(new_path)
            except: pass
            st.rename_var.set(os.path.splitext(new_fname)[0])
            self._save_current_collection()
            self._schedule_tree_refresh()
            return True
        except Exception as e:
            messagebox.showerror("Rename failed", str(e), parent=zw)
            self._zoom_revert_entry(st); return False
        finally:
            st.renaming[0] = False


