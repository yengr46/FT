"""
ft_combine_strip.py - Multi-clip timeline scrub bar for FTVideo.

A horizontal timeline panel showing video clips as filmstrip segments, with:
  - Time ruler across the top with adaptive tick marks
  - Zoom slider (control bar above canvas) to expand/contract the timeline
  - Filmstrip thumbnails tiled within each clip's segment
  - Yellow playhead that advances during playback
  - Horizontal scroll when zoomed in (mouse-wheel or drag ruler)
  - Drag-to-reorder clips; right-click context menu

Public API
----------
add_clip(path, fps, total_frames, duration_s, thumb_data=None) -> ClipEntry
remove_clip(index)
clear()
get_clips() -> list[ClipEntry]
get_active_index() -> int | None
get_active_entry() -> ClipEntry | None
set_active_index(index)
set_playback_position(clip_index, frame)   -- move yellow position line
save_edit_list(index, edit_list)           -- sync MarkerBar state back
get_entry_edit_list(index) -> EditList | None
export_all(output_folder)                  -- run via thread
set_drop_highlight(on)                     -- highlight during external drag

Callbacks
---------
on_clip_selected(index, ClipEntry)
on_seek(clip_index, frame)
on_drop_path(path)
on_play_all()
"""

from __future__ import annotations

__version__ = "1.06"
import io
import os
import shutil
import threading
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox

try:
    from PIL import Image, ImageTk
    _PIL = True
except ImportError:
    _PIL = False

# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------
_BG        = "#0d2535"
_RULER_BG  = "#091d2b"
_CTRL_BG   = "#091d2b"
_SEG_NORM  = "#1a3a5c"
_SEG_ACT   = "#2e6da4"
_SEG_OUT_N = "#2e5070"
_SEG_OUT_A = "#ffffff"
_CUT_COL   = "#c0392b"
_DROP_BG   = "#091d2b"
_DROP_HL   = "#1e4d7a"
_TEXT_COL  = "#c0d8f0"
_DIM_COL   = "#6699bb"
_POS_COL   = "#ffff00"
_TICK_COL  = "#2e5070"
_BTN_BG    = "#122840"
_BTN_FG    = "#88bbdd"
_GRIP_COL  = "#3a6a9a"

# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------
_CTRL_H    = 24     # control bar height (zoom slider row)
_RULER_H   = 18     # time ruler height inside canvas
_CANVAS_H  = 92     # canvas height (ruler + clip area)
_STRIP_H   = _CTRL_H + _CANVAS_H  # total frame height = 116

_LEFT_W    = 36     # ▶ All button column width
_DROP_W    = 40     # drop-zone width at right
_MIN_SEG_W = 24     # minimum segment width in pixels
_DRAG_PX   = 5      # drag threshold
_LABEL_H   = 14     # filename label strip at bottom of each clip
_THUMB_DIV = 1      # 1px divider between tiled thumbnails

# Clip area vertical bounds (relative to canvas)
_CLIP_Y1   = _RULER_H
_CLIP_Y2   = _CANVAS_H          # bottom of canvas
_CLIP_H    = _CLIP_Y2 - _CLIP_Y1   # = 74px for filmstrip

# Zoom limits
_ZOOM_MIN  = 1.0
_ZOOM_MAX  = 20.0


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ClipEntry:
    """EDL segment: path + in/out frame references into the source file.
    duration_s is computed from in_point/out_point — never stored."""
    path:         str
    fps:          float
    total_frames: int
    in_point:     int           # first source frame (inclusive)
    out_point:    int           # last source frame (exclusive)
    edit_list:    object
    thumb_data:   Optional[bytes] = field(default=None, repr=False)
    _photos:      list            = field(default_factory=list, repr=False)

    @property
    def duration_s(self) -> float:
        return max(0.001, (self.out_point - self.in_point) / max(0.001, self.fps))

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def dur_str(self) -> str:
        s = max(0, int(self.duration_s))
        return f"{s // 60}:{s % 60:02d}"

    @property
    def cut_count(self) -> int:
        try:
            return len(self.edit_list.cuts)
        except Exception:
            return 0


# ---------------------------------------------------------------------------
# CombineStrip
# ---------------------------------------------------------------------------

class CombineStrip(tk.Frame):
    """Timeline scrub bar: filmstrip clips laid out like a video editor."""

    def __init__(self, parent, *,
                 on_clip_selected=None,
                 on_seek=None,
                 on_drop_path=None,
                 on_play_all=None,
                 on_strip_activate=None):
        super().__init__(parent, bg=_CTRL_BG,
                         height=_STRIP_H)
        self.configure(height=_STRIP_H)
        self.pack_propagate(False)
        self.grid_propagate(False)

        self.on_clip_selected  = on_clip_selected
        self.on_seek           = on_seek
        self.on_drop_path      = on_drop_path
        self.on_play_all       = on_play_all
        self.on_strip_activate = on_strip_activate

        # Data
        self._clips      = []            # list[ClipEntry]
        self._clip_t0    = []            # start time (seconds) of each clip
        self._overlay_markers    = []    # marker frames from MarkerBar
        self._overlay_marker_clip = None # index of the clip markers belong to
        self._on_right_click_extra = None  # callable(menu) injected by MoviePlayerPanel
        self._total_s    = 0.0           # sum of all durations
        self._active     = None
        self._playhead_s = 0.0           # playhead position in seconds

        # View state
        self._zoom       = 1.0           # zoom multiplier (1 = fit all)
        self._scroll_x   = 0             # canvas x-offset in pixels

        # Interaction
        self._drag_idx    = None         # clip being drag-reordered
        self._drag_insert = None
        self._press_x     = 0
        self._press_mode  = None         # "clip" | "playhead" | "scroll" | None
        self._is_dragging = False

        self._drop_hl     = False
        self._seg_ranges  = []

        # Undo/redo — each entry is a snapshot of _clips (no Tk photo refs)
        self._history    : list = []
        self._redo_stack : list = []           # (px_start, px_end) per clip in canvas coords

        self._build_ui()

    # -----------------------------------------------------------------------
    # Construction
    # -----------------------------------------------------------------------

    def _build_ui(self):
        # ── Control bar (zoom) ───────────────────────────────────────────
        ctrl = tk.Frame(self, bg=_CTRL_BG, height=_CTRL_H)
        ctrl.pack(side="top", fill="x")
        ctrl.pack_propagate(False)

        # ▶ All button
        tk.Button(
            ctrl, text="▶ All", command=self._do_play_all,
            bg=_BTN_BG, fg=_BTN_FG, relief="flat", bd=0,
            font=("Segoe UI", 7, "bold"), cursor="hand2",
            activebackground=_SEG_ACT, activeforeground="white",
            padx=4, pady=0,
        ).pack(side="left", padx=(2, 8), pady=2)

        # Export & Clear
        tk.Button(
            ctrl, text="Export…", command=self._do_export,
            bg=_BTN_BG, fg=_BTN_FG, relief="flat", bd=0,
            font=("Segoe UI", 7), cursor="hand2",
            padx=4, pady=0,
        ).pack(side="left", padx=(0, 4), pady=2)
        tk.Button(
            ctrl, text="Clear", command=self.clear,
            bg=_BTN_BG, fg=_BTN_FG, relief="flat", bd=0,
            font=("Segoe UI", 7), cursor="hand2",
            padx=4, pady=0,
        ).pack(side="left", padx=(0, 4), pady=2)
        tk.Button(
            ctrl, text="✂ Split", command=self.split_at_playhead,
            bg=_BTN_BG, fg=_BTN_FG, relief="flat", bd=0,
            font=("Segoe UI", 7), cursor="hand2",
            padx=4, pady=0,
        ).pack(side="left", padx=(0, 12), pady=2)

        # Zoom controls on the right
        tk.Label(ctrl, text="Zoom", bg=_CTRL_BG, fg=_DIM_COL,
                 font=("Segoe UI", 7)).pack(side="right", padx=(0, 2))
        self._zoom_var = tk.DoubleVar(value=1.0)
        self._zoom_slider = tk.Scale(
            ctrl, from_=_ZOOM_MIN, to=_ZOOM_MAX,
            orient="horizontal", variable=self._zoom_var,
            resolution=0.1, showvalue=False,
            bg=_CTRL_BG, fg=_TEXT_COL, troughcolor="#1a3a5c",
            highlightthickness=0, sliderlength=14, bd=0, width=8,
            length=160, command=self._on_zoom_slider,
        )
        self._zoom_slider.pack(side="right", pady=2)
        self._zoom_lbl = tk.Label(ctrl, text="1.0×", width=4,
                                   bg=_CTRL_BG, fg=_DIM_COL,
                                   font=("Segoe UI", 7))
        self._zoom_lbl.pack(side="right", padx=(0, 2))

        # ── Timeline canvas ───────────────────────────────────────────────
        self._cv = tk.Canvas(self, bg=_BG, highlightthickness=0,
                             height=_CANVAS_H)
        self._cv.pack(side="top", fill="x", expand=True)

        self._cv.bind("<Configure>",       lambda e: self.after_idle(self._redraw))
        self._cv.bind("<ButtonPress-1>",   self._on_press)
        self._cv.bind("<B1-Motion>",       self._on_motion)
        self._cv.bind("<ButtonRelease-1>", self._on_release)
        self._cv.bind("<Button-3>",        self._on_right_click)
        self._cv.bind("<MouseWheel>",      self._on_wheel)
        self._cv.bind("<Button-4>",        self._on_wheel)  # Linux
        self._cv.bind("<Button-5>",        self._on_wheel)  # Linux
        self._cv.bind("<Delete>",          self.delete_active_clip)
        self._cv.bind("<Control-z>",       self.undo)
        self._cv.bind("<Control-Z>",       self.undo)
        self._cv.bind("<Control-y>",       self.redo)
        self._cv.bind("<Control-Y>",       self.redo)

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def add_clip(self, path, fps, total_frames, duration_s=None, thumb_data=None,
                 *, in_point=0, out_point=None):
        """Add a clip segment to the timeline.

        in_point / out_point reference frames in the *source* file.
        duration_s is accepted for backward compatibility but ignored —
        it is always computed from in_point and out_point.
        """
        # NOTE: same path may appear more than once (B-roll reuse)
        try:
            from libraries.ft_movie_edit import EditList
        except ImportError:
            try:
                from ft_movie_edit import EditList
            except ImportError:
                EditList = _FallbackEditList

        _fps   = float(fps) or 25.0
        _total = max(1, int(total_frames))
        _in    = max(0, int(in_point or 0))
        _out   = max(_in + 1, min(int(out_point if out_point is not None else _total), _total))

        entry = ClipEntry(
            path=path,
            fps=_fps,
            total_frames=_total,
            in_point=_in,
            out_point=_out,
            edit_list=EditList(),
            thumb_data=thumb_data,
        )
        self._clips.append(entry)
        self._recompute_layout()

        if self._active is None:
            self._active = 0
            if self.on_clip_selected:
                self.on_clip_selected(0, entry)

        self.after_idle(self._redraw)
        self.after(100, self._redraw)  # fallback: ensure canvas width is known
        return entry

    def remove_clip(self, index):
        if not (0 <= index < len(self._clips)):
            return
        self._clips.pop(index)
        if not self._clips:
            self._active = None
        elif self._active is not None:
            self._active = min(self._active, len(self._clips) - 1)
        self._recompute_layout()
        self.after_idle(self._redraw)

    def clear(self):
        self._clips.clear()
        self._active      = None
        self._playhead_s  = 0.0
        self._scroll_x    = 0
        self._recompute_layout()
        self.after_idle(self._redraw)

    def get_clips(self):
        return list(self._clips)

    def get_active_index(self):
        return self._active

    def get_active_entry(self):
        if self._active is not None and 0 <= self._active < len(self._clips):
            return self._clips[self._active]
        return None

    def set_active_index(self, index):
        if 0 <= index < len(self._clips):
            changed = (index != self._active)
            self._active    = index
            if changed:
                self._playhead_s = self._clip_t0[index] if index < len(self._clip_t0) else 0.0
            self.after_idle(self._redraw)

    def set_playback_position(self, clip_index, frame):
        """Update yellow playhead.  Called each playback tick.
        frame is an absolute source-file frame number."""
        if clip_index != self._active:
            self._active = clip_index
        entry = self._clips[clip_index] if 0 <= clip_index < len(self._clips) else None
        if entry and entry.fps > 0:
            t0 = self._clip_t0[clip_index] if clip_index < len(self._clip_t0) else 0.0
            local_frame = frame - entry.in_point
            self._playhead_s = t0 + max(0.0, local_frame / entry.fps)
        self._cv.delete("posline")
        self._draw_pos_line()

    def get_entry_edit_list(self, index):
        if 0 <= index < len(self._clips):
            return self._clips[index].edit_list
        return None

    def save_edit_list(self, index, edit_list):
        if 0 <= index < len(self._clips):
            self._clips[index].edit_list = edit_list
            self.after_idle(self._redraw)

    def set_drop_highlight(self, on):
        if on != self._drop_hl:
            self._drop_hl = on
            self.after_idle(self._redraw)

    def export_all(self, output_folder):
        try:
            from libraries.ft_movie_edit import commit_edits
        except ImportError:
            from ft_movie_edit import commit_edits

        clips  = list(self._clips)
        parent = self.winfo_toplevel()

        def _worker():
            ok, errs = [], []
            for entry in clips:
                try:
                    if entry.edit_list.has_cuts:
                        segs = entry.edit_list.kept_segments(entry.total_frames)
                        out  = commit_edits(
                            entry.path, segs, entry.fps, entry.total_frames,
                            overwrite=False, precise=False,
                            output_folder=output_folder,
                        )
                    else:
                        dst = os.path.join(output_folder,
                                           os.path.basename(entry.path))
                        if os.path.abspath(entry.path) != os.path.abspath(dst):
                            shutil.copy2(entry.path, dst)
                        out = dst
                    ok.append(out)
                except Exception as ex:
                    errs.append(f"{entry.name}: {ex}")

            def _done():
                if errs:
                    messagebox.showerror(
                        "Export errors",
                        f"{len(ok)} succeeded, {len(errs)} failed:\n\n"
                        + "\n".join(errs), parent=parent)
                else:
                    if messagebox.askyesno(
                        "Export complete",
                        f"{len(ok)} clip{'s' if len(ok) != 1 else ''} written.\n"
                        f"Output: {output_folder}\n\nOpen folder?",
                        parent=parent):
                        try:
                            import subprocess
                            subprocess.Popen(
                                ["explorer", os.path.normpath(output_folder)])
                        except Exception:
                            pass
            try:
                parent.after(0, _done)
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()


    # ------------------------------------------------------------------
    # History / undo-redo
    # ------------------------------------------------------------------

    def _snapshot_clips(self):
        """Shallow-copy _clips, clearing Tk photo refs (not deepcopy-safe)."""
        import copy
        snapped = []
        for clip in self._clips:
            c = copy.copy(clip)
            c._photos   = []
            c.edit_list = copy.deepcopy(clip.edit_list)
            snapped.append(c)
        return snapped

    def _push_history(self):
        self._history.append(self._snapshot_clips())
        self._redo_stack.clear()
        if len(self._history) > 50:
            self._history.pop(0)

    def undo(self, _event=None):
        if not self._history:
            return
        self._redo_stack.append(self._snapshot_clips())
        self._clips = self._history.pop()
        self._active = min(self._active or 0, len(self._clips) - 1) if self._clips else None
        self._recompute_layout()
        self.after_idle(self._redraw)
        if self.on_clip_selected and self._active is not None and self._clips:
            self.on_clip_selected(self._active, self._clips[self._active])

    def redo(self, _event=None):
        if not self._redo_stack:
            return
        self._history.append(self._snapshot_clips())
        self._clips = self._redo_stack.pop()
        self._active = min(self._active or 0, len(self._clips) - 1) if self._clips else None
        self._recompute_layout()
        self.after_idle(self._redraw)
        if self.on_clip_selected and self._active is not None and self._clips:
            self.on_clip_selected(self._active, self._clips[self._active])

    # ------------------------------------------------------------------
    # Split / delete
    # ------------------------------------------------------------------

    def split_at_playhead(self, _event=None):
        """Split the clip currently under the yellow playhead."""
        for i, (t0, clip) in enumerate(zip(self._clip_t0, self._clips)):
            t1 = t0 + clip.duration_s
            if t0 <= self._playhead_s < t1:
                local_t   = self._playhead_s - t0
                src_frame = clip.in_point + int(local_t * clip.fps)
                self.split_clip(i, src_frame)
                return

    def split_clip(self, idx: int, source_frame: int):
        """Replace clip[idx] with two clips split at source_frame."""
        import copy
        if not (0 <= idx < len(self._clips)):
            return
        entry = self._clips[idx]
        split = max(entry.in_point + 1, min(source_frame, entry.out_point - 1))
        if split <= entry.in_point or split >= entry.out_point:
            return  # degenerate — nothing to split
        self._push_history()
        e1 = copy.copy(entry)
        e1.out_point = split
        e1._photos   = []
        e1.edit_list = copy.deepcopy(entry.edit_list)
        e2 = copy.copy(entry)
        e2.in_point  = split
        e2._photos   = []
        e2.edit_list = copy.deepcopy(entry.edit_list)
        self._clips[idx:idx + 1] = [e1, e2]
        self._active = idx
        self._recompute_layout()
        self.after_idle(self._redraw)

    def delete_active_clip(self, _event=None):
        """Remove the currently active clip from the timeline."""
        if self._active is None or not self._clips:
            return
        self._push_history()
        self._clips.pop(self._active)
        if not self._clips:
            self._active = None
        else:
            self._active = min(self._active, len(self._clips) - 1)
        self._recompute_layout()
        self.after_idle(self._redraw)
        if self.on_clip_selected and self._active is not None:
            self.on_clip_selected(self._active, self._clips[self._active])

    # -----------------------------------------------------------------------
    # Layout / coordinate helpers
    # -----------------------------------------------------------------------

    def _recompute_layout(self):
        """Recompute _clip_t0 and _total_s from current clip list."""
        t = 0.0
        self._clip_t0 = []
        for c in self._clips:
            self._clip_t0.append(t)
            t += c.duration_s
        self._total_s = t

    def _pps(self) -> float:
        """Current pixels-per-second, based on zoom and canvas width."""
        w = max(1, self._cv.winfo_width()) - _LEFT_W - _DROP_W
        total = max(0.01, self._total_s)
        base  = w / total
        return base * self._zoom

    def _time_to_cx(self, t: float) -> int:
        """Convert timeline time (s) to canvas x pixel."""
        return _LEFT_W + int((t * self._pps()) - self._scroll_x)

    def _cx_to_time(self, cx: int) -> float:
        """Convert canvas x pixel to timeline time (s)."""
        pps = self._pps()
        if pps <= 0:
            return 0.0
        return (cx - _LEFT_W + self._scroll_x) / pps

    def _seg_ranges_for(self, canvas_w: int) -> List[Tuple[int, int]]:
        """Compute (cx1, cx2) for each clip in current view.
        A 2-px gap is reserved on the right of each segment so clip
        boundaries are clearly visible even when zoomed out.
        """
        pps = self._pps()
        _GAP = 2  # px gap between clips
        ranges = []
        for i, clip in enumerate(self._clips):
            t0 = self._clip_t0[i]
            cx1 = _LEFT_W + int(t0 * pps) - self._scroll_x
            cx2 = _LEFT_W + int((t0 + clip.duration_s) * pps) - self._scroll_x
            cx2 = max(cx1 + _MIN_SEG_W, cx2)
            # Leave a gap on the right edge of every segment except the last
            if i < len(self._clips) - 1:
                cx2 -= _GAP
            ranges.append((cx1, cx2))
        return ranges

    def _clip_at(self, cx: int) -> Optional[int]:
        for i, (x1, x2) in enumerate(self._seg_ranges):
            if x1 <= cx < x2:
                return i
        return None

    def _insert_pos_at(self, cx: int) -> int:
        for i, (x1, x2) in enumerate(self._seg_ranges):
            if cx < (x1 + x2) // 2:
                return i
        return len(self._clips)

    def _max_scroll(self, canvas_w: int) -> int:
        """Maximum scroll_x value (0 when all clips fit)."""
        if not self._clips:
            return 0
        pps = self._pps()
        total_px = int(self._total_s * pps)
        avail    = canvas_w - _LEFT_W - _DROP_W
        return max(0, total_px - avail)

    def _clamp_scroll(self, canvas_w: int):
        self._scroll_x = max(0, min(self._scroll_x, self._max_scroll(canvas_w)))

    def _nice_tick_interval(self, pps: float) -> float:
        """Return a nice time interval (seconds) for ruler ticks at current pps."""
        # Target: one tick every ~80px
        target_s = 80.0 / max(0.001, pps)
        candidates = [0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60,
                      120, 300, 600, 1800, 3600]
        for c in candidates:
            if c >= target_s:
                return c
        return 3600.0

    def _fmt_time(self, t: float) -> str:
        t = max(0.0, t)
        m = int(t) // 60
        s = int(t) % 60
        f = int((t - int(t)) * 10)
        if m == 0:
            return f"{s}.{f}s"
        return f"{m}:{s:02d}"

    # -----------------------------------------------------------------------
    # Rendering
    # -----------------------------------------------------------------------

    def set_overlay_markers(self, markers, active_clip_idx):
        """Update marker overlay and refresh the canvas.
        Call after any marker change in the active clip."""
        self._overlay_markers    = list(markers)
        self._overlay_marker_clip = active_clip_idx
        self._draw_marker_lines()
        self._draw_pos_line()  # keep playhead on top

    def _draw_marker_lines(self):
        """Draw red marker lines on the filmstrip canvas.
        Uses tag='markerline' so they can be cleared without full redraw."""
        c = self._cv
        c.delete("markerline")
        if not self._overlay_markers or not self._clips:
            return
        idx = self._overlay_marker_clip
        if idx is None or not (0 <= idx < len(self._clips)):
            return
        clip = self._clips[idx]
        t0   = self._clip_t0[idx] if idx < len(self._clip_t0) else 0.0
        h    = max(1, c.winfo_height())
        fps  = clip.fps if clip.fps > 0 else 25.0
        for m in self._overlay_markers:
            local_t = (m - clip.in_point) / fps
            x = self._time_to_cx(t0 + local_t)
            c.create_line(x, _RULER_H, x, h,
                          fill="#ff3333", width=1, tags="markerline")

    def _redraw(self, _event=None):
        c = self._cv
        c.delete("all")
        w = max(1, c.winfo_width())
        h = max(1, c.winfo_height())
        if w < 8 or h < 4:
            return

        self._clamp_scroll(w)
        self._seg_ranges = self._seg_ranges_for(w)

        for clip in self._clips:
            clip._photos.clear()

        # ▶ All / left column background
        c.create_rectangle(0, 0, _LEFT_W - 1, h,
                           fill=_BTN_BG, outline="")
        c.create_text(_LEFT_W // 2, h // 2 - 4,
                      text="▶", fill=_BTN_FG,
                      font=("Segoe UI", 9, "bold"), anchor="center")
        c.create_text(_LEFT_W // 2, h // 2 + 8,
                      text="All", fill=_DIM_COL,
                      font=("Segoe UI", 6), anchor="center")

        # Time ruler background
        c.create_rectangle(_LEFT_W, 0, w, _RULER_H,
                           fill=_RULER_BG, outline="")
        # Clip area background
        c.create_rectangle(_LEFT_W, _RULER_H, w, h,
                           fill=_BG, outline="")

        # Time ruler ticks + labels
        pps = self._pps()
        interval = self._nice_tick_interval(pps)
        if interval > 0:
            t_start = self._cx_to_time(_LEFT_W)
            t_end   = self._cx_to_time(w)
            import math
            t = math.ceil(t_start / interval) * interval
            while t <= t_end + interval:
                px = self._time_to_cx(t)
                if _LEFT_W <= px <= w:
                    c.create_line(px, _RULER_H - 6, px, _RULER_H,
                                  fill=_TICK_COL, width=1)
                    label = self._fmt_time(t)
                    c.create_text(px + 2, 3,
                                  text=label, fill=_DIM_COL,
                                  font=("Segoe UI", 7), anchor="nw")
                t += interval

        # Clips
        if not self._clips:
            c.create_text(
                (_LEFT_W + w) // 2, (h + _RULER_H) // 2,
                text="Drag video clips here from the thumbnail grid",
                fill=_DIM_COL, font=("Segoe UI", 9), anchor="center",
            )
        else:
            for i, clip in enumerate(self._clips):
                if i >= len(self._seg_ranges):
                    break
                x1, x2 = self._seg_ranges[i]
                # Only draw if at least partially visible
                if x2 < _LEFT_W or x1 > w:
                    continue
                x1v = max(_LEFT_W, x1)
                x2v = min(w - _DROP_W, x2)
                if x2v > x1v:
                    try:
                        self._draw_segment(c, clip, i, x1, x2, x1v, x2v, h)
                    except Exception as _e:
                        print(f"CombineStrip _draw_segment[{i}] error: {_e}")

        # Drop zone
        self._draw_drop_zone(c, w, h)

        # Drag-reorder insertion indicator
        if self._is_dragging and self._drag_insert is not None:
            ins = self._drag_insert
            if ins < len(self._seg_ranges):
                lx = self._seg_ranges[ins][0] - 2
            elif self._seg_ranges:
                lx = self._seg_ranges[-1][1] + 2
            else:
                lx = _LEFT_W
            c.create_line(lx, _RULER_H, lx, h,
                          fill=_POS_COL, width=3, tags="dragline")

        # Ruler border
        c.create_line(_LEFT_W, _RULER_H, w, _RULER_H,
                      fill=_SEG_OUT_N, width=1)

        self._draw_marker_lines()
        self._draw_pos_line()

    def _draw_segment(self, c, clip, idx,
                      x1: int, x2: int,
                      x1v: int, x2v: int, h: int):
        """Draw one clip segment.  x1/x2 are full bounds; x1v/x2v are visible bounds."""
        active  = (idx == self._active)
        fill    = _SEG_ACT   if active else _SEG_NORM
        outline = _SEG_OUT_A if active else _SEG_OUT_N
        bw      = 2          if active else 1

        y1 = _CLIP_Y1
        y2 = _CLIP_Y2
        c.create_rectangle(x1v, y1, x2v, y2,
                           fill=fill, outline=outline, width=bw)

        # Filmstrip thumbnails tiled across visible area
        lbl_y = y2 - _LABEL_H
        if clip.thumb_data and _PIL and (x2v - x1v) > 8 and (lbl_y - y1) > 8:
            self._draw_filmstrip(c, clip, x1v, x2v, y1 + bw, lbl_y - 1)

        # Label strip at bottom
        c.create_rectangle(x1v, lbl_y, x2v, y2 - bw,
                           fill="#0a1e2e", outline="")
        seg_w = max(1, x2v - x1v)
        name  = clip.name
        if len(name) > 24:
            name = name[:22] + "…"
        n_cuts = clip.cut_count
        label  = name + (f"  ✂{n_cuts}" if n_cuts else "")
        c.create_text(x1v + seg_w // 2, lbl_y + _LABEL_H // 2,
                      text=label, fill=_TEXT_COL if active else _DIM_COL,
                      font=("Segoe UI", 7), anchor="center",
                      width=max(1, seg_w - 6))

        # Cut bands (3px at bottom edge)
        total = max(1, clip.total_frames)
        seg_full_w = max(1, x2 - x1)
        for cut in clip.edit_list.cuts:
            bx1 = x1 + int(cut.start / total * seg_full_w)
            bx2 = x1 + max(bx1 + 2,
                            int((cut.end + 1) / total * seg_full_w))
            bx1v = max(x1v, bx1)
            bx2v = min(x2v, bx2)
            if bx2v > bx1v:
                c.create_rectangle(bx1v, y2 - 4, bx2v, y2 - bw,
                                   fill=_CUT_COL, outline="")

    def _draw_filmstrip(self, c, clip, x1: int, x2: int,
                        y1: int, y2: int):
        """Tile the clip's thumbnail across the visible horizontal range."""
        if not (_PIL and clip.thumb_data):
            return
        th = max(1, y2 - y1)
        tw = max(1, int(th * 16 / 9))   # assume 16:9 aspect
        if tw < 4 or th < 4:
            return
        try:
            src = Image.open(io.BytesIO(clip.thumb_data)).convert("RGB")
            src = src.resize((tw, th), Image.LANCZOS)
        except Exception:
            return

        slot = 0
        x = x1
        while x < x2 and slot < 60:
            visible_w = min(tw, x2 - x)
            if visible_w <= 0:
                break
            try:
                frame = src.crop((0, 0, visible_w, th))
                photo = ImageTk.PhotoImage(frame)
                clip._photos.append(photo)
                c.create_image(x, y1, image=photo, anchor="nw")
                if visible_w == tw and x + tw + _THUMB_DIV < x2:
                    # Draw 1px dark divider between tiles
                    c.create_line(x + tw, y1, x + tw, y2,
                                  fill="#000000", width=_THUMB_DIV)
            except Exception:
                pass
            x += tw + _THUMB_DIV
            slot += 1

    def _draw_drop_zone(self, c, canvas_w: int, h: int):
        # Drop zone: last _DROP_W pixels at right
        dz   = canvas_w - _DROP_W
        fill = _DROP_HL if self._drop_hl else _DROP_BG
        fg   = "#88ccff" if self._drop_hl else "#3366aa"
        c.create_rectangle(dz, _RULER_H, canvas_w - 1, h,
                           fill=fill, outline="#2e5070", dash=(4, 3))
        label = "+" if self._clips else "+\ndrop"
        c.create_text((dz + canvas_w) // 2, (h + _RULER_H) // 2,
                      text=label, fill=fg,
                      font=("Segoe UI", 8, "bold"), justify="center")

    def _draw_pos_line(self):
        c  = self._cv
        c.delete("posline")
        if not self._clips:
            return
        px = self._time_to_cx(self._playhead_s)
        h  = max(1, c.winfo_height())
        c.create_line(px, 0, px, h, fill=_POS_COL, width=2, tags="posline")
        # Small triangle handle on ruler
        c.create_polygon(px - 5, 0, px + 5, 0, px, 8,
                         fill=_POS_COL, outline="", tags="posline")

    # -----------------------------------------------------------------------
    # Mouse events
    # -----------------------------------------------------------------------

    def _on_press(self, event):
        self._press_x     = event.x
        self._is_dragging = False
        self._drag_insert = None
        # Notify player of any strip interaction so it can switch to timeline mode
        if self.on_strip_activate:
            self.on_strip_activate()

        # ▶ All button column
        if event.x < _LEFT_W:
            self._press_mode = "btn"
            self._drag_idx   = None
            return

        # Playhead handle (click on ruler)
        if event.y < _RULER_H:
            self._press_mode = "playhead"
            self._drag_idx   = None
            t = self._cx_to_time(event.x)
            self._playhead_s = max(0.0, min(t, self._total_s))
            self._seek_to_playhead()
            return

        # Clip area
        idx = self._clip_at(event.x)
        self._drag_idx   = idx
        self._press_mode = "clip" if idx is not None else "scroll"

    def _on_motion(self, event):
        dx = event.x - self._press_x

        if self._press_mode == "playhead":
            t = self._cx_to_time(event.x)
            self._playhead_s = max(0.0, min(t, self._total_s))
            self._seek_to_playhead()
            self._cv.delete("posline")
            self._draw_pos_line()
            return

        if self._press_mode == "scroll":
            # Drag on empty area = pan timeline
            w = max(1, self._cv.winfo_width())
            self._scroll_x -= dx
            self._press_x   = event.x
            self._clamp_scroll(w)
            self.after_idle(self._redraw)
            return

        if self._press_mode == "clip" and self._drag_idx is not None:
            if abs(dx) >= _DRAG_PX:
                self._is_dragging = True
                self._drag_insert = self._insert_pos_at(event.x)
                self.after_idle(self._redraw)

    def _on_release(self, event):
        press_mode  = self._press_mode
        drag_idx    = self._drag_idx
        drag_insert = self._drag_insert
        dragging    = self._is_dragging

        self._press_mode  = None
        self._drag_idx    = None
        self._drag_insert = None
        self._is_dragging = False

        # ▶ All button
        if press_mode == "btn" and event.x < _LEFT_W:
            self._do_play_all()
            return

        if press_mode == "playhead":
            return

        if dragging and drag_idx is not None and drag_insert is not None:
            # Reorder
            src = drag_idx
            dst = drag_insert
            if dst != src and dst != src + 1:
                clip = self._clips.pop(src)
                if dst > src:
                    dst -= 1
                self._clips.insert(dst, clip)
                if self._active == src:
                    self._active = dst
                self._recompute_layout()
            self.after_idle(self._redraw)

        elif press_mode == "clip" and drag_idx is not None:
            # Click selects (highlights) the clip — no cursor movement
            if drag_idx != self._active:
                self._activate(drag_idx)
            # Same-clip click: just ensure it's visually selected (no seek, no cursor move)

    def _on_wheel(self, event):
        # Ctrl+wheel = zoom; plain wheel = horizontal scroll
        ctrl = bool(event.state & 0x4)
        if hasattr(event, "delta") and event.delta:
            ticks = event.delta // 120
        elif event.num == 4:
            ticks = 1
        elif event.num == 5:
            ticks = -1
        else:
            ticks = 0

        if ctrl:
            new_zoom = max(_ZOOM_MIN, min(_ZOOM_MAX,
                                          self._zoom + ticks * 0.5))
            self._zoom = new_zoom
            self._zoom_var.set(new_zoom)
            self._zoom_lbl.configure(text=f"{new_zoom:.1f}\xd7")
        else:
            w = max(1, self._cv.winfo_width())
            self._scroll_x -= ticks * 50
            self._clamp_scroll(w)
        self.after_idle(self._redraw)

    def _on_zoom_slider(self, val):
        try:
            self._zoom = float(val)
            self._zoom_lbl.configure(text=f"{self._zoom:.1f}\xd7")
            w = max(1, self._cv.winfo_width())
            self._clamp_scroll(w)
            self.after_idle(self._redraw)
        except Exception:
            pass

    def _on_right_click(self, event):
        idx  = self._clip_at(event.x)
        menu = tk.Menu(self, tearoff=0)
        if idx is not None:
            self._active = idx
            self.after_idle(self._redraw)
            menu.add_command(
                label=f"Remove  {self._clips[idx].name}",
                command=lambda i=idx: self.remove_clip(i))
            menu.add_command(label="✂  Split at playhead",
                             command=self.split_at_playhead)
            menu.add_command(label="🗑  Delete this clip",
                             command=self.delete_active_clip)
            menu.add_separator()
        if self._clips:
            menu.add_command(label="▶  Play all",    command=self._do_play_all)
            menu.add_command(label="⬆  Export all…", command=self._do_export)
            menu.add_separator()
        menu.add_command(label="↩  Undo", command=self.undo,
                         state="normal" if self._history else "disabled")
        menu.add_command(label="↪  Redo", command=self.redo,
                         state="normal" if self._redo_stack else "disabled")
        menu.add_separator()
        menu.add_command(label="✕  Clear all", command=self.clear)
        if self._on_right_click_extra:
            try:
                self._on_right_click_extra(menu, event.x)
            except Exception as _e:
                print(f"right_click_extra error: {_e}")
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _activate(self, idx: int):
        self._active    = idx
        # Do NOT move _playhead_s here — cursor only moves via ruler/triangle
        self.after_idle(self._redraw)
        if self.on_clip_selected:
            self.on_clip_selected(idx, self._clips[idx])

    def _seek_to_playhead(self):
        """Find which clip the playhead is in and fire on_seek."""
        for i, (t0, clip) in enumerate(zip(self._clip_t0, self._clips)):
            t1 = t0 + clip.duration_s
            if t0 <= self._playhead_s <= t1:
                if i != self._active:
                    self._active = i
                    if self.on_clip_selected:
                        self.on_clip_selected(i, clip)
                local_t = self._playhead_s - t0
                frame = clip.in_point + int(local_t * clip.fps)
                if self.on_seek:
                    self.on_seek(i, frame)
                return

    def _do_play_all(self):
        if not self._clips:
            return
        if self.on_play_all:
            self.on_play_all()

    def _do_export(self):
        """Export the whole timeline as one combined file."""
        if not self._clips:
            return
        try:
            from libraries.ft_movie_edit import commit_sequence
        except ImportError:
            from ft_movie_edit import commit_sequence

        first = self._clips[0]
        ext   = os.path.splitext(first.path)[1] or ".mp4"
        stem  = os.path.splitext(os.path.basename(first.path))[0]
        if len(self._clips) == 1:
            init_name = f"{stem}_export{ext}"
        else:
            init_name = f"sequence_export{ext}"

        out_path = filedialog.asksaveasfilename(
            title="Export timeline",
            initialdir=os.path.dirname(os.path.abspath(first.path)),
            initialfile=init_name,
            defaultextension=ext,
            filetypes=[(f"Video (*{ext})", f"*{ext}"),
                       ("All video files",
                        "*.mp4 *.mov *.avi *.mkv *.mts *.m2ts"),
                       ("All files", "*.*")],
            parent=self)
        if not out_path:
            return

        clips  = list(self._clips)
        parent = self.winfo_toplevel()

        def _worker():
            try:
                commit_sequence(clips, out_path)
                def _done():
                    msg = (f"Export complete.\n\nSaved to:\n{out_path}\n\n"
                           f"Open folder?")
                    if messagebox.askyesno("Export complete", msg,
                                           parent=parent):
                        try:
                            import subprocess as _sp
                            _sp.Popen(["explorer",
                                       os.path.dirname(
                                           os.path.abspath(out_path))])
                        except Exception:
                            pass
                parent.after(0, _done)
            except Exception as ex:
                def _err(e=str(ex)):
                    messagebox.showerror("Export failed", e, parent=parent)
                parent.after(0, _err)

        import threading as _th
        _th.Thread(target=_worker, daemon=True).start()


# ---------------------------------------------------------------------------
# Fallback stub
# ---------------------------------------------------------------------------

class _FallbackEditList:
    @property
    def cuts(self):
        return []

    @property
    def has_cuts(self):
        return False

    def kept_segments(self, total):
        return [(0, total - 1)]
