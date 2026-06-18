"""ft_movie_edit.py — Non-destructive edit list for FTView Movies mode.

Design:
  - Video always plays from the original file unchanged.
  - A list of CutRange(start, end) is maintained in memory.
  - During playback, the engine skips cut ranges automatically.
  - The scrub bar renders cut ranges as collapsed grey bands.
  - Undo pops the last cut from the stack.
  - Commit is the only time ffmpeg runs — it concatenates the kept
    segments into a new file in one pass.

Keyboard:
  M              — set marker at current frame
  Shift-click    — select range between two markers then cut

UI:
  Marker bar canvas — yellow ticks for markers, red bands for cuts,
                      blue highlight for current selection
  Set Marker / Prev / Next / Clear Markers
  Cut Selection  — adds selected range to cut list
  Undo           — removes last cut
  Commit         — runs ffmpeg, saves result
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import tkinter as tk
from tkinter import messagebox
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------
SCRUB_BG   = "#1a3a52"
MARKER_COL = "#ff3333"    # red
CUT_COL    = "#8a2020"    # dark red band = cut section
CUT_EDGE   = "#cc4040"
SEL_COL    = "#3a6abf"    # blue selection
SEL_EDGE   = "#60aaff"
BTN_MARK   = "#1a3a6b"
BTN_CUT    = "#8a2020"
BTN_UNDO   = "#555555"
BTN_COMMIT = "#1a6b35"
BTN_CLEAR  = "#444444"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class CutRange:
    start: int   # first frame to cut (inclusive)
    end:   int   # last frame to cut (inclusive)

    def contains(self, frame: int) -> bool:
        return self.start <= frame <= self.end

    def duration(self, fps: float) -> float:
        return max(0, self.end - self.start) / max(fps, 1.0)


class EditList:
    """Non-destructive list of cut ranges with undo support."""

    def __init__(self):
        self._cuts: List[CutRange] = []
        self._undo_stack: List[CutRange] = []

    @property
    def cuts(self) -> List[CutRange]:
        return list(self._cuts)

    def add_cut(self, start: int, end: int):
        if end <= start:
            return
        cut = CutRange(min(start, end), max(start, end))
        # Merge overlapping/adjacent ranges
        merged = []
        inserted = False
        for c in self._cuts:
            if c.end < cut.start - 1:
                merged.append(c)
            elif c.start > cut.end + 1:
                if not inserted:
                    merged.append(cut)
                    inserted = True
                merged.append(c)
            else:
                # Overlapping — expand cut to cover both
                cut = CutRange(min(cut.start, c.start), max(cut.end, c.end))
        if not inserted:
            merged.append(cut)
        self._cuts = merged
        self._undo_stack.append(cut)

    def undo(self) -> bool:
        """Remove last added cut. Returns True if something was undone."""
        if not self._undo_stack:
            return False
        last = self._undo_stack.pop()
        # Remove the exact cut (after merge, the stored cut may differ —
        # find the range that contains last.start)
        self._cuts = [c for c in self._cuts
                      if not (c.start <= last.start and c.end >= last.end)]
        return True

    def clear(self):
        self._cuts.clear()
        self._undo_stack.clear()

    def is_cut(self, frame: int) -> bool:
        return any(c.contains(frame) for c in self._cuts)

    def next_kept_frame(self, frame: int, total: int) -> int:
        """Given a frame index, return the next frame that isn't cut."""
        f = frame
        while f < total and self.is_cut(f):
            # Jump to end of the cut range + 1
            for c in self._cuts:
                if c.contains(f):
                    f = c.end + 1
                    break
        return min(f, total - 1)

    def kept_segments(self, total: int) -> List[Tuple[int, int]]:
        """Return list of (start, end) frame ranges that are NOT cut."""
        if not self._cuts:
            return [(0, total - 1)]
        segments = []
        pos = 0
        for cut in sorted(self._cuts, key=lambda c: c.start):
            if pos < cut.start:
                segments.append((pos, cut.start - 1))
            pos = cut.end + 1
        if pos < total:
            segments.append((pos, total - 1))
        return segments

    @property
    def has_cuts(self) -> bool:
        return bool(self._cuts)


# ---------------------------------------------------------------------------
# ffmpeg helpers
# ---------------------------------------------------------------------------

def _ffmpeg() -> str:
    ff = shutil.which("ffmpeg")
    if not ff:
        raise RuntimeError(
            "ffmpeg not found on PATH.\nDownload from https://ffmpeg.org")
    return ff


def _output_path(input_path: str, overwrite: bool) -> str:
    input_path = os.path.abspath(input_path)
    if overwrite:
        return input_path
    folder = os.path.dirname(input_path) or "."
    stem, ext = os.path.splitext(os.path.basename(input_path))
    stem = re.sub(r'_ed\d*$', '', stem)
    candidate = os.path.join(folder, f"{stem}_ed{ext}")
    n = 2
    while os.path.exists(candidate):
        candidate = os.path.join(folder, f"{stem}_ed{n}{ext}")
        n += 1
    return candidate


def _ts(frame: int, fps: float) -> str:
    t = frame / max(fps, 1.0)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def commit_edits(input_path: str, segments: List[Tuple[int, int]],
                 fps: float, total: int,
                 overwrite: bool = False, precise: bool = False,
                 output_folder: Optional[str] = None) -> str:
    """Run ffmpeg to produce the edited file from kept segments.

    If *output_folder* is provided the output file is placed there;
    otherwise it lands next to the source (original behaviour).
    """
    # Normalise early so dirname/join work correctly regardless of cwd
    input_path = os.path.abspath(input_path)
    ff  = _ffmpeg()
    if output_folder:
        stem, ext = os.path.splitext(os.path.basename(input_path))
        stem = re.sub(r'_ed\d*$', '', stem)
        out  = os.path.join(output_folder, f"{stem}_ed{ext}")
        n = 2
        while os.path.exists(out):
            out = os.path.join(output_folder, f"{stem}_ed{n}{ext}")
            n += 1
        folder = output_folder
    else:
        out    = _output_path(input_path, overwrite)
        folder = os.path.dirname(input_path) or "."
    ext = os.path.splitext(input_path)[1]

    if not segments:
        raise RuntimeError("No segments to keep — all frames are cut.")

    codec = (["-c:v", "libx264", "-preset", "fast", "-crf", "18", "-c:a", "aac"]
             if precise else ["-c", "copy"])

    if len(segments) == 1:
        s, e = segments[0]
        tmp = out + ".tmp" + ext
        cmd = [ff, "-y", "-i", input_path,
               "-ss", _ts(s, fps), "-to", _ts(e + 1, fps)] + codec + [tmp]
        r = subprocess.run(cmd, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=600)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode(errors="replace")[-500:])
        os.replace(tmp, out)
        return out

    # Multiple segments — extract each, then concatenate
    seg_files = []
    lst_path  = os.path.join(folder, "_ftedit_list.txt")
    try:
        for i, (s, e) in enumerate(segments):
            seg = os.path.join(folder, f"_ftedit_seg{i}{ext}")
            cmd = [ff, "-y", "-i", input_path,
                   "-ss", _ts(s, fps), "-to", _ts(e + 1, fps)] + codec + [seg]
            r = subprocess.run(cmd, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=600)
            if r.returncode != 0:
                raise RuntimeError(r.stderr.decode(errors="replace")[-300:])
            seg_files.append(seg)

        with open(lst_path, "w") as f:
            for sf in seg_files:
                f.write(f"file '{sf}'\n")

        tmp = out + ".tmp" + ext
        r = subprocess.run(
            [ff, "-y", "-f", "concat", "-safe", "0",
             "-i", lst_path, "-c", "copy", tmp],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode(errors="replace")[-300:])
        os.replace(tmp, out)
        return out
    finally:
        for sf in seg_files:
            try: os.unlink(sf)
            except Exception: pass
        try: os.unlink(lst_path)
        except Exception: pass


# ---------------------------------------------------------------------------
# MarkerBar widget
# ---------------------------------------------------------------------------

class MarkerBar(tk.Frame):
    """Non-destructive marker/cut editor bar.

    Callbacks expected from MoviePlayerPanel:
        get_frame()    -> int   current frame
        get_total()    -> int   total frames
        get_fps()      -> float fps
        get_path()     -> str   current video path
        seek_to(frame) -> None  seek player to frame
        on_commit(path)-> None  called after successful commit
        is_cut_frame   -> Callable(frame)->bool  used by playback to skip cuts
    """

    MAX_MARKERS = 10

    def __init__(self, parent, *,
                 get_frame:  Callable,
                 get_total:  Callable,
                 get_fps:    Callable,
                 get_path:   Callable,
                 seek_to:    Callable,
                 on_commit:  Callable,
                 on_markers_changed: Callable = None,
                 on_split_delete:    Callable = None,
                 bg=SCRUB_BG):
        super().__init__(parent, bg=bg)
        self._get_frame = get_frame
        self._get_total = get_total
        self._get_fps   = get_fps
        self._get_path  = get_path
        self._seek_to   = seek_to
        self._on_commit = on_commit
        self._on_markers_changed = on_markers_changed
        self._on_split_delete = on_split_delete

        self.edit_list   = EditList()
        self._markers:   List[int]            = []
        self._sel:       Optional[Tuple[int,int]] = None
        self._sel_anchor: Optional[int]       = None
        self._pending_cuts: List[Tuple[int,int]] = []

        self._build_ui()

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def _btn(self, parent, text, cmd, bg=BTN_MARK, fg="white", width=None):
        kw = dict(text=text, command=cmd, bg=bg, fg=fg,
                  activebackground=bg, activeforeground=fg,
                  font=("Segoe UI", 9, "bold"), relief="raised", bd=1,
                  padx=5, pady=1)
        if width:
            kw["width"] = width
        return tk.Button(parent, **kw)

    def _build_ui(self):
        # ── Row 1: marker controls ────────────────────────────────────
        r1 = tk.Frame(self, bg=SCRUB_BG)
        r1.pack(fill="x", padx=8, pady=(4, 0))

        self._btn(r1, "Set Marker [M]", self.set_marker,  bg=BTN_MARK).pack(side="left", padx=(0,4))
        self._btn(r1, "◀ Prev",         self.prev_marker, bg=BTN_MARK).pack(side="left", padx=(0,2))
        self._btn(r1, "Next ▶",         self.next_marker, bg=BTN_MARK).pack(side="left", padx=(0,8))
        self._btn(r1, "Clear Markers",  self._clear_markers, bg=BTN_CLEAR).pack(side="left", padx=(0,12))
        self._btn(r1, "↩  Undo",         self._do_undo,  bg=BTN_UNDO).pack(side="left", padx=(0,6))
        self._btn(r1, "✔  Commit",        self._do_commit,bg=BTN_COMMIT).pack(side="left", padx=(0,0))

        self.lbl_status = tk.Label(r1, text="No markers — press M to set",
                                   bg=SCRUB_BG, fg="#aaccee",
                                   font=("Segoe UI", 8), anchor="w")
        self.lbl_status.pack(side="left", padx=(12,0), fill="x", expand=True)

        # ── Row 2: marker/cut canvas ──────────────────────────────────
        self._canvas = tk.Canvas(self, bg="#0d2535", height=22,
                                 highlightthickness=0)
        # _canvas kept but not packed — scrub display is in MoviePlayerPanel._scrub_canvas
        self._canvas.bind("<Configure>",     self.redraw)
        self._canvas.bind("<ButtonPress-1>", self._on_click)
        self._canvas.bind("<B1-Motion>",     self._on_drag_scrub)
        self._canvas.bind("<Button-3>",      self._on_right_click)

        # ── Row 3: commit options ─────────────────────────────────────
        r3 = tk.Frame(self, bg=SCRUB_BG)
        r3.pack(fill="x", padx=8, pady=(3,5))

        tk.Label(r3, text="Save as:", bg=SCRUB_BG, fg="#aaccee",
                 font=("Segoe UI", 8)).pack(side="left", padx=(0,4))
        self._save_mode = tk.StringVar(value="new")
        tk.Radiobutton(r3, text="New file (_ed)", variable=self._save_mode,
                       value="new", bg=SCRUB_BG, fg="white",
                       selectcolor=SCRUB_BG, activebackground=SCRUB_BG,
                       font=("Segoe UI", 8)).pack(side="left")
        tk.Radiobutton(r3, text="Overwrite original", variable=self._save_mode,
                       value="overwrite", bg=SCRUB_BG, fg="white",
                       selectcolor=SCRUB_BG, activebackground=SCRUB_BG,
                       font=("Segoe UI", 8)).pack(side="left", padx=(8,0))

        tk.Label(r3, text="    Cut mode:", bg=SCRUB_BG, fg="#aaccee",
                 font=("Segoe UI", 8)).pack(side="left", padx=(0,4))
        self._cut_mode = tk.StringVar(value="precise")
        tk.Radiobutton(r3, text="Fast (stream copy)", variable=self._cut_mode,
                       value="fast", bg=SCRUB_BG, fg="white",
                       selectcolor=SCRUB_BG, activebackground=SCRUB_BG,
                       font=("Segoe UI", 8)).pack(side="left")
        tk.Radiobutton(r3, text="Precise (re-encode)", variable=self._cut_mode,
                       value="precise", bg=SCRUB_BG, fg="white",
                       selectcolor=SCRUB_BG, activebackground=SCRUB_BG,
                       font=("Segoe UI", 8)).pack(side="left", padx=(8,0))

        self.lbl_cuts = tk.Label(r3, text="",
                                 bg=SCRUB_BG, fg="#aaccee",
                                 font=("Segoe UI", 8), anchor="w")
        self.lbl_cuts.pack(side="left", padx=(16,0), fill="x", expand=True)

    # ------------------------------------------------------------------
    # Marker management
    # ------------------------------------------------------------------

    def set_marker(self, frame: int = None):
        if frame is None:
            frame = self._get_frame()
        if frame in self._markers:
            return
        if len(self._markers) >= self.MAX_MARKERS:
            messagebox.showinfo("Markers", f"Maximum {self.MAX_MARKERS} markers.", parent=self)
            return
        self._markers.append(frame)
        self._markers.sort()
        self._sel = None
        self._sel_anchor = None
        self._update_status()
        self.redraw()
        if self._on_markers_changed:
            self._on_markers_changed(list(self._markers))

    def _clear_markers(self):
        self._markers.clear()
        self._sel = None
        self._sel_anchor = None
        self._update_status()
        self.redraw()
        if self._on_markers_changed:
            self._on_markers_changed(list(self._markers))

    def prev_marker(self):
        cur = self._get_frame()
        prev = [m for m in self._markers if m < cur]
        if prev:
            self._seek_to(prev[-1])

    def next_marker(self):
        cur = self._get_frame()
        nxt = [m for m in self._markers if m > cur]
        if nxt:
            self._seek_to(nxt[0])

    def reset_for_new_file(self):
        """Call when a new video is loaded."""
        self._markers.clear()
        self._sel = None
        self._sel_anchor = None
        self._pending_cuts = []
        self.edit_list.clear()
        self._update_status()
        self.redraw()

    def get_edit_list(self) -> "EditList":
        """Return the current EditList (for saving to a CombineStrip ClipEntry)."""
        return self.edit_list

    def get_markers(self) -> list:
        """Return a copy of the current marker frame list."""
        return list(self._markers)

    def get_pending_cuts(self) -> list:
        """Return list of (start_frame, end_frame) pending-deletion regions."""
        return list(self._pending_cuts)

    def toggle_pending_cut_at(self, frame: int):
        """Right-click handler: mark/unmark the section between the two markers
        surrounding *frame* as a pending deletion."""
        # Check if inside an existing pending cut — toggle it off
        for i, (s, e) in enumerate(self._pending_cuts):
            if s <= frame <= e:
                self._pending_cuts.pop(i)
                self._update_status()
                if self._on_markers_changed:
                    self._on_markers_changed(list(self._markers))
                return
        # Find surrounding markers
        left  = max((m for m in self._markers if m <= frame), default=None)
        right = min((m for m in self._markers if m >  frame), default=None)
        if left is None or right is None:
            return  # no markers bracketing this position
        self._pending_cuts.append((left, right))
        self._pending_cuts.sort()
        self._update_status()
        if self._on_markers_changed:
            self._on_markers_changed(list(self._markers))

    def load_edit_list(self, edit_list: "EditList"):
        """Load a saved EditList into this MarkerBar (e.g. when switching strip clips)."""
        self._markers.clear()
        self._sel         = None
        self._sel_anchor  = None
        self.edit_list    = edit_list
        self._update_status()
        self.redraw()

    # ------------------------------------------------------------------
    # Canvas drawing
    # ------------------------------------------------------------------

    def _frame_to_x(self, frame: int) -> float:
        total = max(1, self._get_total())
        w = max(1, self._canvas.winfo_width())
        return frame / total * w

    def _x_to_frame(self, x: float) -> int:
        total = max(1, self._get_total())
        w = max(1, self._canvas.winfo_width())
        return max(0, min(total - 1, int(x / w * total)))

    def redraw(self, event=None):
        c = self._canvas
        c.delete("all")
        w = max(1, c.winfo_width())
        h = max(1, c.winfo_height())

        # Cut ranges — red bands (collapsed sections)
        for cut in self.edit_list.cuts:
            x1 = self._frame_to_x(cut.start)
            x2 = max(x1 + 3, self._frame_to_x(cut.end))  # min 3px wide
            c.create_rectangle(x1, 2, x2, h-2, fill=CUT_COL, outline=CUT_EDGE)
            # Diagonal lines to signal "collapsed"
            for dx in range(0, int(x2-x1), 5):
                c.create_line(x1+dx, 2, x1+dx-4, h-2, fill=CUT_EDGE, width=1)

        # Current selection — blue band
        if self._sel:
            s, e = self._sel
            x1, x2 = self._frame_to_x(s), self._frame_to_x(e)
            c.create_rectangle(x1, 0, x2, h, fill=SEL_COL, outline="", stipple="gray50")
            c.create_line(x1, 0, x1, h, fill=SEL_EDGE, width=2)
            c.create_line(x2, 0, x2, h, fill=SEL_EDGE, width=2)

        # Marker ticks — red vertical lines
        for m in self._markers:
            x = self._frame_to_x(m)
            c.create_line(x, 0, x, h, fill=MARKER_COL, width=1)

        # Current-position cursor — drawn last so it's on top of markers
        total = self._get_total()
        if total > 0:
            cx = self._frame_to_x(self._get_frame())
            c.create_line(cx, 0, cx, h, fill="#ffdd44", width=2, tags="cursor")

    def update_cursor(self):
        """Lightweight cursor update — no full redraw, just repositions the cursor line."""
        c = self._canvas
        c.delete("cursor")
        total = self._get_total()
        if total <= 0:
            return
        h = max(1, c.winfo_height())
        x = self._frame_to_x(self._get_frame())
        c.create_line(x, 0, x, h, fill="#ffdd44", width=2, tags="cursor")

    # ------------------------------------------------------------------
    # Mouse
    # ------------------------------------------------------------------

    def _nearest_marker(self, x: float, threshold: int = 10):
        """Return index of nearest marker within threshold pixels, or None."""
        best_i, best_d = None, threshold
        for i, m in enumerate(self._markers):
            d = abs(self._frame_to_x(m) - x)
            if d < best_d:
                best_d, best_i = d, i
        return best_i

    def _markers_around(self, x: float):
        """Return (left_marker, right_marker) bracketing x.

        Edge cases:
          - Click before first marker: returns (0, first_marker)
          - Click after last marker:   returns (last_marker, total_frames-1)
          - No markers at all:         returns (None, None)
        """
        if not self._markers:
            return None, None
        frame = self._x_to_frame(x)
        left  = [m for m in self._markers if m <= frame]
        right = [m for m in self._markers if m >  frame]
        # Before first marker — use frame 0 as implicit left boundary
        if not left:
            return 0, self._markers[0]
        # After last marker — use last frame as implicit right boundary
        if not right:
            total = max(1, self._get_total())
            return self._markers[-1], total - 1
        return left[-1], right[0]

    def _on_click(self, event):
        """Left click: if on a marker tick jump to it, otherwise seek to position."""
        i = self._nearest_marker(event.x)
        if i is not None:
            self._seek_to(self._markers[i])
        else:
            self._seek_to(self._x_to_frame(event.x))

    def _on_drag_scrub(self, event):
        """B1-Motion on the marker canvas — live scrub as user drags."""
        self._seek_to(self._x_to_frame(event.x))

    def show_context_menu_at_frame(self, frame: int, x_root: int, y_root: int):
        """Public: show the right-click edit context menu for a given frame.
        Called from MoviePlayerPanel when the user right-clicks the scrub canvas.
        """
        total = max(1, self._get_total())
        # Threshold for "on a marker": ±1% of duration, min 5 frames
        thr = max(5, total // 100)

        marker_i = None
        for i, m in enumerate(self._markers):
            if abs(m - frame) <= thr:
                marker_i = i
                break

        left_m  = max((m for m in self._markers if m <= frame), default=None)
        right_m = min((m for m in self._markers if m  > frame), default=None)

        menu = tk.Menu(self, tearoff=0)

        # Inside a cut range — offer to remove it
        for cut in self.edit_list.cuts:
            if cut.contains(frame):
                fps = self._get_fps() or 25.0
                menu.add_command(
                    label=f"Remove cut ({cut.start}\u2013{cut.end}, {cut.duration(fps):.1f}s)",
                    command=lambda c=cut: self._remove_cut(c))
                menu.add_separator()
                break

        # Between two markers — offer to cut that section
        if left_m is not None and right_m is not None:
            fps = self._get_fps() or 25.0
            dur = (right_m - left_m) / fps
            menu.add_command(
                label=f"\u2702  Cut section ({left_m}\u2013{right_m}, {dur:.1f}s)",
                command=lambda l=left_m, r=right_m: self._cut_between(l, r))
            menu.add_separator()

        # On a marker — offer to delete it
        if marker_i is not None:
            mf = self._markers[marker_i]
            menu.add_command(label=f"Delete marker at frame {mf}",
                             command=lambda f=mf: self._delete_marker(f))
            menu.add_separator()

        menu.add_command(label=f"Set marker here (frame {frame})",
                         command=lambda f=frame: self.set_marker(f))
        menu.add_command(label="Undo last cut", command=self._do_undo)

        try:
            menu.tk_popup(x_root, y_root)
        finally:
            menu.grab_release()

    def _on_right_click(self, event):
        """Right click: show context menu based on what's at click position."""
        frame = self._x_to_frame(event.x)
        marker_i = self._nearest_marker(event.x, threshold=12)
        left_m, right_m = self._markers_around(event.x)

        menu = tk.Menu(self, tearoff=0)

        # If inside a cut range — offer to undo that cut
        for cut in self.edit_list.cuts:
            if cut.contains(frame):
                fps = self._get_fps() or 25.0
                menu.add_command(
                    label=f"Remove cut ({cut.start}–{cut.end}, {cut.duration(fps):.1f}s)",
                    command=lambda c=cut: self._remove_cut(c))
                menu.add_separator()
                break

        # If between two markers — offer to cut that section
        if left_m is not None and right_m is not None:
            fps = self._get_fps() or 25.0
            dur = (right_m - left_m) / fps
            menu.add_command(
                label=f"✂  Cut section ({left_m}–{right_m}, {dur:.1f}s)",
                command=lambda l=left_m, r=right_m: self._cut_between(l, r))
            menu.add_separator()

        # If on a marker — offer to delete it
        if marker_i is not None:
            mf = self._markers[marker_i]
            menu.add_command(label=f"Delete marker at frame {mf}",
                             command=lambda f=mf: self._delete_marker(f))
            menu.add_separator()

        menu.add_command(label=f"Set marker here (frame {frame})",
                         command=lambda f=frame: self.set_marker(f))
        menu.add_command(label="Undo last cut", command=self._do_undo)

        if menu.index("end") is not None:
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()

    def _cut_between(self, left_m: int, right_m: int):
        """Cut the section between two markers and remove those markers."""
        self.edit_list.add_cut(left_m, right_m)
        for f in (left_m, right_m):
            if f in self._markers:
                self._markers.remove(f)
        self._sel = None
        self._sel_anchor = None
        self._update_status()
        self.redraw()

    def _remove_cut(self, cut):
        """Remove a specific cut from the edit list."""
        self.edit_list._cuts = [c for c in self.edit_list._cuts
                                if not (c.start == cut.start and c.end == cut.end)]
        self._update_status()
        self.redraw()

    def _delete_marker(self, frame: int):
        if frame in self._markers:
            self._markers.remove(frame)
        if self._sel and frame in self._sel:
            self._sel = None
        if self._sel_anchor == frame:
            self._sel_anchor = None
        self._update_status()
        self.redraw()

    # ------------------------------------------------------------------
    # Edit operations
    # ------------------------------------------------------------------

    def _do_cut(self):
        if not self._sel:
            messagebox.showinfo("Cut",
                "No selection.\nClick a marker then Shift-click another.",
                parent=self)
            return
        s, e = self._sel
        fps = self._get_fps() or 25.0
        self.edit_list.add_cut(s, e)
        # Clear selection and markers used for this cut
        self._sel = None
        self._sel_anchor = None
        # Remove the two markers that defined the cut
        for f in (s, e):
            if f in self._markers:
                self._markers.remove(f)
        self._update_status()
        self.redraw()

    def _do_undo(self):
        if not self.edit_list.undo():
            messagebox.showinfo("Undo", "Nothing to undo.", parent=self)
            return
        self._update_status()
        self.redraw()

    def _do_commit(self):
        """Apply pending-deletion sections to the timeline via split+delete.

        Each pending cut calls on_split_delete(start_frame, end_frame) which
        wires to CombineStrip split+delete.  No file is written — the original
        file is unchanged until the user clicks Save/Export.
        """
        if not self._pending_cuts:
            messagebox.showinfo("Commit",
                "No sections marked for deletion.\n\n"
                "Right-click the scrub bar between two markers to mark a section.",
                parent=self)
            return

        fps = self._get_fps() or 25.0
        cut_summary = "\n".join(
            f"  frames {s}\u2013{e}  ({(e-s)/fps:.1f}s)"
            for s, e in self._pending_cuts
        )
        if not messagebox.askyesno(
            "Commit deletions",
            f"{len(self._pending_cuts)} section(s) to remove from timeline:\n\n"
            f"{cut_summary}\n\n"
            "The original file is NOT modified.\n"
            "Use Save/Export to write the result to disk.",
            parent=self):
            return

        if self._on_split_delete:
            for start_f, end_f in sorted(self._pending_cuts, reverse=True):
                self._on_split_delete(start_f, end_f)

        self._pending_cuts = []
        self._update_status()
        self.redraw()
        if self._on_markers_changed:
            self._on_markers_changed(list(self._markers))

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def _update_status(self):
        n = len(self._markers)
        nc = len(self.edit_list.cuts)
        fps = self._get_fps() or 25.0

        if n == 0:
            self.lbl_status.configure(text="No markers — press M or right-click bar to set")
        elif n == 1:
            self.lbl_status.configure(text="1 marker set — add another then right-click between them to cut")
        else:
            self.lbl_status.configure(
                text=f"{n} markers — right-click between two markers to cut that section")

        np = len(self._pending_cuts)
        if np > 0:
            total_del = sum((e - s) for s, e in self._pending_cuts) / fps
            self.lbl_cuts.configure(
                text=f"{np} section(s) marked for deletion ({total_del:.1f}s) — click ✔ Commit to apply")
        elif nc > 0:
            total_cut = sum(c.duration(fps) for c in self.edit_list.cuts)
            self.lbl_cuts.configure(
                text=f"{nc} cut{'s' if nc!=1 else ''} in edit list ({total_cut:.1f}s removed)")
        else:
            self.lbl_cuts.configure(text="")


# ---------------------------------------------------------------------------
# Timeline export helpers
# ---------------------------------------------------------------------------

def effective_segments(entry) -> list:
    """Frame ranges for a ClipEntry that survive both the edit list and
    the clip's in_point / out_point boundaries.

    Args:
        entry: ClipEntry with .edit_list, .in_point, .out_point, .total_frames

    Returns:
        List of (start_frame, end_frame) tuples, inclusive on both ends.
    """
    raw = entry.edit_list.kept_segments(entry.total_frames)
    result = []
    lo = entry.in_point
    hi = entry.out_point - 1          # out_point is exclusive → convert
    for s, e in raw:
        s2 = max(s, lo)
        e2 = min(e, hi)
        if s2 <= e2:
            result.append((s2, e2))
    # Fallback: at minimum keep in→out even if edit list says nothing
    return result if result else [(lo, hi)]


def commit_sequence(clips, output_path: str, *, on_progress=None) -> str:
    """Render all ClipEntry objects in timeline order into a single file.

    Uses fast stream-copy (-c copy).  Each clip's in/out points and edit
    list cuts are respected via effective_segments().

    Args:
        clips:        Iterable of ClipEntry objects.
        output_path:  Destination file path (extension determines container).
        on_progress:  Optional callable(done, total) — called from worker thread.

    Returns:
        output_path on success; raises RuntimeError on failure.
    """
    ff  = _ffmpeg()
    ext = os.path.splitext(output_path)[1] or ".mp4"
    folder = os.path.dirname(os.path.abspath(output_path)) or "."

    # Build flat list of (source_path, start_frame, end_frame, fps)
    all_segs = []
    for entry in clips:
        for s, e in effective_segments(entry):
            all_segs.append((entry.path, s, e, entry.fps))

    if not all_segs:
        raise RuntimeError("Nothing to export — timeline is empty.")

    total = len(all_segs)
    seg_files = []
    lst_path  = os.path.join(folder, "_ftseq_list.txt")

    try:
        for i, (path, s, e, fps) in enumerate(all_segs):
            seg = os.path.join(folder, f"_ftseq_seg{i}{ext}")
            cmd = [ff, "-y",
                   "-ss", _ts(s, fps), "-to", _ts(e + 1, fps),
                   "-i",  path,
                   "-c",  "copy", seg]
            r = subprocess.run(cmd, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=600)
            if r.returncode != 0:
                raise RuntimeError(
                    f"Segment {i} failed:\n"
                    + r.stderr.decode(errors="replace")[-400:])
            seg_files.append(seg)
            if on_progress:
                on_progress(i + 1, total)

        if len(seg_files) == 1:
            # Single segment — just move it
            if os.path.exists(output_path):
                os.remove(output_path)
            os.replace(seg_files[0], output_path)
            seg_files = []          # prevent cleanup deletion
        else:
            with open(lst_path, "w", encoding="utf-8") as f:
                for sf in seg_files:
                    f.write(f"file '{sf}'\n")
            r = subprocess.run(
                [ff, "-y", "-f", "concat", "-safe", "0",
                 "-i", lst_path, "-c", "copy", output_path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=1800)
            if r.returncode != 0:
                raise RuntimeError(
                    "Concat failed:\n"
                    + r.stderr.decode(errors="replace")[-400:])

    finally:
        for sf in seg_files:
            try:
                os.unlink(sf)
            except Exception:
                pass
        try:
            os.unlink(lst_path)
        except Exception:
            pass

    return output_path
