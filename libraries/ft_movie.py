"""ft_movie.py — Movie thumbnail and scrub-player panel for FTView.

Provides:
  - get_video_info(path)                            -> dict
  - extract_movie_frame(path, frame_index)          -> PIL Image
  - make_movie_thumbnail(path, position, size)      -> (PIL Image, ok, err)
  - MoviePlayerPanel — replaces ViewerPanel when FTView is in Movies mode

Interaction model:
    Click+drag scrub bar  : pauses immediately, seeks on drag (preview every
                            100 ms), stays paused on release
    Space bar             : play / pause toggle
    Left / Right          : step ±1 frame when paused
    Shift+Left/Right      : step ±10 frames when paused
    Grab & Save Frame btn : save current frame as JPG to chosen output folder

Playback uses a dedicated background thread with its own cv2.VideoCapture so
frames are read sequentially without seeking.  Frames are delivered to the Tk
thread via after(0, ...) with explicit argument binding to avoid late-binding
closure bugs.

Dependencies: opencv-python (cv2). Pillow assumed present.
"""

from __future__ import annotations

__version__ = "1.25"
import hashlib
import os
import sys
import json
import shutil
import subprocess
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox
from typing import Callable, Optional

from PIL import Image, ImageTk

try:
    from libraries.ft_movie_edit import MarkerBar
    HAVE_EDIT = True
except ImportError:
    try:
        from ft_movie_edit import MarkerBar  # fallback for direct-run from libraries/
        HAVE_EDIT = True
    except ImportError:
        HAVE_EDIT = False

try:
    import vlc as _vlc_module
    HAVE_VLC = True
except ImportError:
    _vlc_module = None
    HAVE_VLC = False

try:
    from libraries.ft_combine_strip import CombineStrip
    HAVE_COMBINE = True
except ImportError:
    try:
        from ft_combine_strip import CombineStrip  # type: ignore[import]
        HAVE_COMBINE = True
    except ImportError:
        HAVE_COMBINE = False

try:
    import cv2
    HAVE_CV2 = True
except ImportError:
    cv2 = None
    HAVE_CV2 = False

# pygame is used only in a subprocess (see _AudioPlayer.play) so no
# module-level import or mixer init is needed here.

def _find_audio_python() -> str:
    """Return a Python executable that should have pygame.

    If we're running under 32-bit Python, look for a 64-bit sibling in the
    same Programs/Python directory (e.g. Python311-32 -> Python311).
    Falls back to sys.executable if no sibling is found.
    """
    import struct
    if struct.calcsize("P") * 8 == 64:
        return sys.executable          # already 64-bit
    exe      = os.path.abspath(sys.executable)
    exe_dir  = os.path.dirname(exe)
    parent   = os.path.dirname(exe_dir)
    folder   = os.path.basename(exe_dir)   # e.g. "Python311-32"
    if folder.endswith("-32"):
        sibling_dir = os.path.join(parent, folder[:-3])   # "Python311"
        sibling_exe = os.path.join(sibling_dir, os.path.basename(exe))
        if os.path.exists(sibling_exe):
            return sibling_exe
    return exe

_AUDIO_PYTHON = _find_audio_python()

# ---------------------------------------------------------------------------
# Palette (matches FTView)
# ---------------------------------------------------------------------------
BG          = "#dddddd"
BG2         = "#eeeeee"
ACCENT      = "#1a5276"
DIM         = "#555555"
TEXT        = "#111111"
CANVAS_BG   = "#555555"
SCRUB_BG    = "#1a3a52"
PLAY_GREEN  = "#1a6b35"
PAUSE_AMBER = "#8a5e00"


# ---------------------------------------------------------------------------
# Low-level video helpers
# ---------------------------------------------------------------------------

def _open_cap(path: str):
    """Open a cv2.VideoCapture on the plain path (never the \\?\\ prefix)."""
    if not HAVE_CV2:
        raise RuntimeError(
            "opencv-python is required.\n\nRun:  pip install opencv-python"
        )
    cap = cv2.VideoCapture(path)
    if cap.isOpened():
        return cap
    cap.release()
    cap = cv2.VideoCapture(os.path.abspath(path))
    if cap.isOpened():
        return cap
    cap.release()
    raise RuntimeError(f"Could not open video: {os.path.basename(path)}")


try:
    from libraries.ft_metadata_cache import put_creation_time as _put_creation_time
except ImportError:
    try:
        from ft_metadata_cache import put_creation_time as _put_creation_time
    except ImportError:
        _put_creation_time = None  # type: ignore[assignment]


def _ffprobe_info(path: str) -> dict:
    """Get video metadata via ffprobe including rotation, SAR, and creation_time.

    creation_time is also written to the app-wide metadata cache so that
    sort-by-date is available without a separate ffprobe pass.
    """
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {}
    try:
        # -show_format added to get format-level tags (creation_time lives there)
        cmd = [ffprobe, "-v", "quiet", "-print_format", "json",
               "-show_streams", "-select_streams", "v:0", "-show_format", path]
        _cf = 0x08000000 if os.name == "nt" else 0
        result = subprocess.run(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=15,
                                creationflags=_cf)
        data = json.loads(result.stdout)
        stream = data.get("streams", [{}])[0]
        fps = 0.0
        try:
            n, d = stream.get("r_frame_rate", "0/1").split("/")
            fps = float(n) / float(d) if float(d) else 0.0
        except Exception:
            pass
        nb = int(stream.get("nb_frames", 0) or 0)
        dur = float(stream.get("duration", 0) or 0)
        if nb == 0 and dur > 0 and fps > 0:
            nb = int(dur * fps)

        # Rotation: check tags first, then side_data_list as fallback only.
        # tags["rotate"] = N means "rotate N° CW to display correctly" (positive = CW).
        # side_data["rotation"] uses the display-matrix convention where the sign is
        # OPPOSITE: -90 means the same correction as tags rotate=90.  Only use
        # side_data if tags has no "rotate" key; never let it overwrite tags.
        rotation = 0
        tags = stream.get("tags", {})
        if "rotate" in tags:
            try:
                rotation = int(tags["rotate"])
            except Exception:
                pass
        else:
            for sd in stream.get("side_data_list", []):
                if "rotation" in sd:
                    try:
                        # Negate: display-matrix sign convention is opposite to tags
                        rotation = -int(sd["rotation"])
                    except Exception:
                        pass

        # SAR (sample aspect ratio) for anamorphic footage
        sar_n, sar_d = 1, 1
        sar_str = stream.get("sample_aspect_ratio", "1:1") or "1:1"
        if ":" in sar_str:
            try:
                sn, sd2 = sar_str.split(":")
                sar_n, sar_d = int(sn), int(sd2)
                if sar_d == 0:
                    sar_n, sar_d = 1, 1
            except Exception:
                pass

        # Interlacing: field_order is "progressive" or absent for progressive;
        # anything else (tt, bb, tb, bt) means interlaced.
        field_order = stream.get("field_order", "progressive") or "progressive"
        interlaced = field_order.lower() not in ("progressive", "unknown", "")

        # creation_time from format-level tags (cameras write capture time here)
        fmt_tags = data.get("format", {}).get("tags", {})
        creation_time = fmt_tags.get("creation_time") or None  # None if absent/empty

        # Write to app-wide metadata cache so sort-by-date needs no extra ffprobe pass
        if _put_creation_time is not None:
            try:
                _put_creation_time(path, creation_time)
            except Exception:
                pass

        return {
            "frame_count": nb, "fps": fps, "duration_s": dur,
            "width":    int(stream.get("width",  0) or 0),
            "height":   int(stream.get("height", 0) or 0),
            "rotation": rotation,
            "sar_n": sar_n, "sar_d": sar_d,
            "interlaced": interlaced,
            "creation_time": creation_time,
        }
    except Exception:
        return {}


# Module-level cache: path -> (rotation, sar_n, sar_d, interlaced, width, height)
_meta_cache: dict = {}

def _get_meta(path: str) -> tuple:
    """Return (rotation, sar_n, sar_d, interlaced, width, height) cached per path."""
    if path not in _meta_cache:
        m = _ffprobe_info(path)
        _meta_cache[path] = (
            m.get("rotation", 0),
            m.get("sar_n", 1),
            m.get("sar_d", 1),
            m.get("interlaced", False),
            m.get("width", 0),
            m.get("height", 0),
        )
    return _meta_cache[path]


def _correct_bgr(bgr, rotation: int, sar_n: int, sar_d: int):
    """Apply rotation and SAR correction to a raw BGR numpy array (from cv2).

    Works entirely in numpy/cv2 — much faster than PIL per-frame ops.
    Returns corrected BGR array.
    """
    import cv2 as _cv2
    import numpy as _np

    # SAR correction — stretch width for non-square pixels
    if sar_n != sar_d and sar_d > 0 and sar_n > 0:
        h, w = bgr.shape[:2]
        new_w = max(1, int(w * sar_n / sar_d))
        bgr = _cv2.resize(bgr, (new_w, h), interpolation=_cv2.INTER_LINEAR)

    # Rotation correction using cv2.rotate (GPU-friendly, in-place friendly)
    rot = rotation % 360
    if rot == 90:
        bgr = _cv2.rotate(bgr, _cv2.ROTATE_90_CLOCKWISE)
    elif rot == 180:
        bgr = _cv2.rotate(bgr, _cv2.ROTATE_180)
    elif rot == 270:
        bgr = _cv2.rotate(bgr, _cv2.ROTATE_90_COUNTERCLOCKWISE)
    return bgr


def _correct_frame(img: Image.Image, rotation: int,
                   sar_n: int, sar_d: int) -> Image.Image:
    """Apply rotation and SAR correction to a PIL image (used for thumbnails only)."""
    import numpy as _np
    import cv2 as _cv2
    bgr = _cv2.cvtColor(_np.array(img), _cv2.COLOR_RGB2BGR)
    bgr = _correct_bgr(bgr, rotation, sar_n, sar_d)
    return Image.fromarray(_cv2.cvtColor(bgr, _cv2.COLOR_BGR2RGB))


def _ffmpeg_frame(path: str, frame_index: int, fps: float,
                  vf: str = "") -> Image.Image:
    """Extract one frame via ffmpeg subprocess (fallback when cv2 can't decode).

    vf: optional -vf filter string for SAR/rotation correction.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH")
    ts = (frame_index / fps) if fps > 0 else 0.0
    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    tmp.close()
    try:
        cf = 0x08000000 if os.name == "nt" else 0
        cmd = [ffmpeg, "-y", "-ss", f"{ts:.6f}", "-i", path,
               "-frames:v", "1", "-q:v", "2"]
        if vf:
            cmd += ["-vf", vf]
        cmd.append(tmp.name)
        r = subprocess.run(cmd, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=30,
                           creationflags=cf)
        if r.returncode != 0 or not os.path.getsize(tmp.name):
            raise RuntimeError(r.stderr.decode(errors="replace")[-300:])
        return Image.open(tmp.name).convert("RGB")
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


def get_video_info(path: str, longpath_func=None) -> dict:
    """Return frame_count, fps, duration_s, width, height (all 0 on error).

    Uses ffprobe first (fast, no COM/DirectShow) and falls back to cv2 only
    if ffprobe returns incomplete data.  This prevents Windows DirectShow COM
    initialisation from blocking the UI thread via cross-apartment marshalling.
    """
    info = {"frame_count": 0, "fps": 0.0, "duration_s": 0.0,
            "width": 0, "height": 0}
    # ── ffprobe primary ───────────────────────────────────────────────────
    fb = _ffprobe_info(path)
    for k in info:
        if fb.get(k):
            info[k] = fb[k]
    if info["fps"] and info["frame_count"]:
        return info   # ffprobe gave us everything we need — skip cv2
    # ── cv2 fallback (only if ffprobe failed or returned zeros) ───────────
    try:
        cap = _open_cap(path)
        if not info["frame_count"]:
            info["frame_count"] = max(0, int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0))
        if not info["fps"]:
            info["fps"]    = float(cap.get(cv2.CAP_PROP_FPS) or 0)
        if not info["width"]:
            info["width"]  = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        if not info["height"]:
            info["height"] = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        if info["fps"] > 0 and info["frame_count"] > 0 and not info["duration_s"]:
            info["duration_s"] = info["frame_count"] / info["fps"]
        cap.release()
    except Exception:
        pass
    return info


def extract_movie_frame(path: str, frame_index: int,
                        longpath_func=None) -> Image.Image:
    """Extract frame by index; try cv2 first, fall back to ffmpeg."""
    fps_fallback = 25.0
    # Use cached metadata (rotation, sar, interlace, stored dimensions)
    rotation, sar_n, sar_d, _interlaced, meta_w, meta_h = _get_meta(path)

    try:
        cap = _open_cap(path)
        try:
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            fps_fallback = float(cap.get(cv2.CAP_PROP_FPS) or 25.0)
            if total > 0:
                frame_index = max(0, min(frame_index, total - 1))
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
            ok, bgr = cap.read()
            if not ok or bgr is None:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, bgr = cap.read()
            if ok and bgr is not None:
                if rotation % 360 != 0 or sar_n != sar_d:
                    bgr = _correct_bgr(bgr, rotation, sar_n, sar_d)
                return Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
        finally:
            cap.release()
    except Exception:
        pass
    # ffmpeg fallback — build SAR/rotation vf so the output has correct geometry
    vf_parts = []
    if sar_n != sar_d and sar_d and meta_w and meta_h:
        disp_w = int(round(meta_w * sar_n / sar_d))
        if disp_w % 2 != 0:
            disp_w += 1
        vf_parts.append(f"scale={disp_w}:{meta_h}")
    rot = rotation % 360
    if rot == 90:
        vf_parts.append("transpose=1")
    elif rot == 180:
        vf_parts.append("transpose=1,transpose=1")
    elif rot == 270:
        vf_parts.append("transpose=2")
    return _ffmpeg_frame(path, frame_index, fps_fallback,
                         vf=",".join(vf_parts) if vf_parts else "")


def make_movie_thumbnail(path: str, position: float, size: int,
                         longpath_func=None):
    """Return (PIL Image, ok, err).  position is 0.0–1.0 fraction into video."""
    size = max(1, int(size))
    try:
        info  = get_video_info(path)
        total = info.get("frame_count", 0)
        idx   = max(0, int(total * max(0.0, min(1.0, position)))) if total > 0 else 0
        img   = extract_movie_frame(path, idx)
        img.thumbnail((size, size), Image.BILINEAR)
        return img, True, None
    except Exception as e:
        err = str(e)
        print(f"ft_movie thumb [{os.path.basename(path)}]: {err}", file=sys.stderr)
        return Image.new("RGB", (size, size), (40, 40, 60)), False, err


# ---------------------------------------------------------------------------
# Fast thumbnail with disk cache
# ---------------------------------------------------------------------------

def _thumb_cache_file(path: str, position: float, size: int) -> str:
    """Return the disk-cache path for a video thumbnail JPEG."""
    try:
        mtime = int(os.path.getmtime(path))
    except Exception:
        mtime = 0
    key = hashlib.md5(
        f"{os.path.normcase(os.path.abspath(path))}|{mtime}|{position:.4f}|{size}|v7".encode()
    ).hexdigest()
    cache_dir = os.path.join(tempfile.gettempdir(), "ft_thumb_cache")
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, f"{key}.jpg")


def make_movie_thumbnail_fast(path: str, position: float, size: int,
                               longpath_func=None):
    """Return (PIL Image, ok, err) — fast version with disk cache.

    First call for a given (path, mtime, size):
        Uses ffmpeg with keyframe-level fast seeking (-ss before -i) if
        available, otherwise falls back to the cv2 path.  Saves the result
        to a per-session JPEG cache in the system temp folder.

    Subsequent calls (same path / mtime / size):
        Reads the cached JPEG — typically < 5 ms vs 1–5 s for cv2 seeking.
    """
    size = max(1, int(size))
    cache_file = _thumb_cache_file(path, position, size)

    # ── Cache hit ────────────────────────────────────────────────────────────
    if os.path.exists(cache_file):
        try:
            img = Image.open(cache_file)
            img.load()
            return img.convert("RGB"), True, None
        except Exception:
            try:
                os.unlink(cache_file)
            except Exception:
                pass

    # ── Build vf filter chain (outside try so silent fallback can't poison cache) ─
    # Always apply yadif with deint=interlaced: passes progressive frames through
    # unchanged; deinterlaces interlaced frames via bitstream flags.
    # Apply SAR correction so anamorphic footage (e.g. 1440×1080 SAR 4:3 →
    # display 1920×1080) gets the right aspect ratio.
    # Apply rotation via transpose so the thumbnail is always correctly oriented
    # regardless of ffmpeg version autorotate behaviour (-noautorotate is set on
    # the input, so rotation is entirely our responsibility here).
    _vf_thumb = ["yadif=mode=0:deint=interlaced"]
    _thumb_rotation = 0
    try:
        _tmeta = _ffprobe_info(path)
        _sar_n = _tmeta.get("sar_n", 1)
        _sar_d = _tmeta.get("sar_d", 1) or 1
        _thumb_rotation = _tmeta.get("rotation", 0)
        if _sar_n != _sar_d:
            _tw = _tmeta.get("width", 0)
            _th = _tmeta.get("height", 0)
            if _tw and _th:
                _dw = int(round(_tw * _sar_n / _sar_d))
                if _dw % 2 != 0:
                    _dw += 1
                _vf_thumb.append(f"scale={_dw}:{_th}")
    except Exception:
        pass   # SAR/rotation unknown — proceed without correction
    _trot = _thumb_rotation % 360
    if _trot == 90:
        _vf_thumb.append("transpose=1")
    elif _trot == 180:
        _vf_thumb.append("transpose=1,transpose=1")
    elif _trot == 270:
        _vf_thumb.append("transpose=2")
    _vf_thumb.append(f"scale={size}:-2")

    # ── ffmpeg fast-seek extraction ──────────────────────────────────────────
    ffmpeg_exe = shutil.which("ffmpeg")
    if ffmpeg_exe:
        try:
            # Get duration via ffprobe only (reads container headers — no
            # cv2, no full decode).  If ffprobe is unavailable or times out,
            # default to 10 s which lands inside most clips.
            ts = 10.0
            ffprobe_exe = shutil.which("ffprobe")
            if ffprobe_exe:
                try:
                    _cf = 0x08000000 if os.name == "nt" else 0
                    r = subprocess.run(
                        [ffprobe_exe, "-v", "error",
                         "-show_entries", "format=duration",
                         "-of", "default=noprint_wrappers=1:nokey=1", path],
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        timeout=5, creationflags=_cf,
                    )
                    duration_s = float(r.stdout.decode().strip())
                    if duration_s > 0:
                        ts = duration_s * position
                except Exception:
                    pass

            # -ss BEFORE -i → keyframe seek (no frame-by-frame decode needed).
            # -noautorotate: we handle rotation explicitly in _vf_thumb above.
            cmd = [
                ffmpeg_exe, "-y",
                "-ss", f"{ts:.3f}",
                "-noautorotate",
                "-i", path,
                "-vframes", "1",
                "-vf", ",".join(_vf_thumb),
                "-q:v", "5",
                cache_file,
            ]
            _cf = 0x08000000 if os.name == "nt" else 0
            r = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=15, creationflags=_cf,
            )
            if (r.returncode == 0
                    and os.path.exists(cache_file)
                    and os.path.getsize(cache_file) > 0):
                img = Image.open(cache_file)
                img.load()
                img = img.convert("RGB")
                img.thumbnail((size, size), Image.BILINEAR)
                return img, True, None
        except Exception:
            pass

    # ── cv2 fallback (slower) ────────────────────────────────────────────────
    img, ok, err = make_movie_thumbnail(path, position, size, longpath_func)
    if ok:
        try:
            img.save(cache_file, "JPEG", quality=85)
        except Exception:
            pass
    return img, ok, err


def _fmt_timecode(frame: int, fps: float) -> str:
    if fps <= 0:
        return f"f{frame}"
    t  = frame / fps
    h  = int(t // 3600)
    m  = int((t % 3600) // 60)
    s  = int(t % 60)
    ff = round((t - int(t)) * fps)
    return f"{h:02d}:{m:02d}:{s:02d}:{ff:02d}"


def _fmt_duration(seconds: float) -> str:
    if seconds <= 0:
        return "0:00"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ---------------------------------------------------------------------------
# Zoomable / pannable canvas
# ---------------------------------------------------------------------------

class _ZoomCanvas(tk.Canvas):
    def __init__(self, parent, bg=CANVAS_BG):
        super().__init__(parent, bg=bg, highlightthickness=0)
        self.img = self.photo = None
        self.scale = self.fit_scale = 1.0
        self.offset = [0, 0]
        self._drag = None
        self._fit_id = None
        self._msg = ""
        self._play_image_id = None   # persistent canvas item for playback frames
        self.bind("<Configure>",       lambda e: self._schedule_fit())
        self.bind("<MouseWheel>",      self._on_wheel)
        self.bind("<ButtonPress-1>",   lambda e: setattr(self, "_drag",
                                       (e.x, e.y, self.offset[0], self.offset[1])))
        self.bind("<B1-Motion>",       self._on_drag)
        self.bind("<Double-Button-1>", lambda e: self.fit())

    def set_image(self, img: Image.Image):
        self._msg = ""
        self.img = img
        self.scale = self.fit_scale = 1.0
        self.offset = [0, 0]
        self._play_image_id = None   # force full redraw on next playback
        self._schedule_fit(30)

    def show_frame(self, img: Image.Image, pre_resized: bool = False):
        """Fast render for continuous playback — reuses a persistent canvas item.

        If pre_resized is True the image is already scaled to fit the canvas,
        so we skip the PIL resize and go straight to PhotoImage + itemconfig.
        """
        self._msg = ""
        self.img = img
        cw, ch = max(1, self.winfo_width()), max(1, self.winfo_height())
        if pre_resized:
            # Frame already sized by the worker thread — just convert and display
            try:
                self.photo = ImageTk.PhotoImage(img)
                iw, ih = img.size
                ox = max(0, (cw - iw) // 2)
                oy = max(0, (ch - ih) // 2)
                if self._play_image_id is None:
                    self.delete("all")
                    self._play_image_id = self.create_image(ox, oy, anchor="nw",
                                                            image=self.photo)
                else:
                    self.itemconfig(self._play_image_id, image=self.photo)
                    self.coords(self._play_image_id, ox, oy)
            except Exception:
                pass
            return
        # Fallback: resize here (used when worker did not pre-scale)
        if cw >= 20 and ch >= 20:
            iw, ih = img.size
            if iw > 0 and ih > 0:
                self.fit_scale = min(cw / iw, ch / ih)
                self.scale = self.fit_scale
        self._render_playback()

    def _render_playback(self):
        """Resize-then-display path for show_frame without pre_resized."""
        if self.img is None:
            return
        cw, ch = max(1, self.winfo_width()), max(1, self.winfo_height())
        iw, ih = self.img.size
        nw = max(1, int(iw * self.scale))
        nh = max(1, int(ih * self.scale))
        ox = max(0, (cw - nw) // 2)
        oy = max(0, (ch - nh) // 2)
        try:
            disp = self.img.resize((nw, nh), Image.BILINEAR)
            self.photo = ImageTk.PhotoImage(disp)
            if self._play_image_id is None:
                self.delete("all")
                self._play_image_id = self.create_image(ox, oy, anchor="nw",
                                                        image=self.photo)
            else:
                self.itemconfig(self._play_image_id, image=self.photo)
                self.coords(self._play_image_id, ox, oy)
        except Exception:
            pass

    def set_message(self, text: str):
        self._msg = text or ""
        self.img = self.photo = None
        self._play_image_id = None   # force recreate on next playback
        self._render()

    def _schedule_fit(self, ms=10):
        if self._fit_id:
            try:
                self.after_cancel(self._fit_id)
            except Exception:
                pass
        self._fit_id = self.after(ms, self.fit)

    def fit(self):
        self._fit_id = None
        self.update_idletasks()
        cw, ch = max(1, self.winfo_width()), max(1, self.winfo_height())
        if cw < 20 or ch < 20:
            self.after(50, self.fit)
            return
        if self.img is None:
            self._render()
            return
        iw, ih = self.img.size
        if iw <= 0 or ih <= 0:
            return
        self.fit_scale = min(cw / iw, ch / ih)
        self.scale = self.fit_scale
        nw, nh = max(1, int(iw * self.scale)), max(1, int(ih * self.scale))
        self.offset = [int((cw - nw) / 2), int((ch - nh) / 2)]
        self._render()

    def _render(self):
        self.delete("all")
        cw, ch = max(1, self.winfo_width()), max(1, self.winfo_height())
        if self.img is None:
            if self._msg:
                self.create_text(cw // 2, ch // 2, text=self._msg,
                                 fill="white", font=("Segoe UI", 18, "bold"),
                                 anchor="center")
            return
        iw, ih = self.img.size
        nw, nh = max(1, int(iw * self.scale)), max(1, int(ih * self.scale))
        if self.scale == self.fit_scale:
            self.offset = [int((cw - nw) / 2), int((ch - nh) / 2)]
        try:
            disp = self.img.resize((nw, nh), Image.BILINEAR)
            self.photo = ImageTk.PhotoImage(disp)
            self.create_image(self.offset[0], self.offset[1],
                              anchor="nw", image=self.photo)
        except Exception:
            pass

    def _on_wheel(self, event):
        if self.img is None:
            return "break"
        old = self.scale
        factor = 1.12 if event.delta > 0 else 1 / 1.12
        self.scale = max(self.fit_scale * 0.5,
                         min(self.scale * factor, self.fit_scale * 12))
        mx, my = event.x, event.y
        ox, oy = self.offset
        self.offset[0] = int(mx - (mx - ox) * (self.scale / old))
        self.offset[1] = int(my - (my - oy) * (self.scale / old))
        self._render()
        return "break"

    def _on_drag(self, event):
        if not self._drag:
            return
        sx, sy, ox, oy = self._drag
        self.offset = [ox + event.x - sx, oy + event.y - sy]
        self._render()


# ---------------------------------------------------------------------------
# One-shot frame loader (seek / scrub)
# ---------------------------------------------------------------------------

class _FrameLoader:
    """Persistent single-thread frame loader -- keeps VideoCapture open between
    requests for the same file to eliminate open/close overhead on every scrub.
    Latest request wins; results from superseded requests are discarded."""

    def __init__(self, widget: tk.Widget, on_frame: Callable):
        self._widget   = widget
        self._callback = on_frame   # (PIL Image|None, frame_index, err|None)
        self._lock     = threading.Lock()
        self._pending  = None       # (path, frame_index) or None
        self._event    = threading.Event()
        self._cap      = None       # persistent VideoCapture
        self._cap_path = None       # path the cap is open on
        threading.Thread(target=self._worker, daemon=True,
                         name="FrameLoader").start()

    def request(self, path: str, frame_index: int):
        with self._lock:
            self._pending = (path, frame_index)
        self._event.set()

    def cancel(self):
        with self._lock:
            self._pending = None

    def _worker(self):
        while True:
            self._event.wait()
            self._event.clear()
            with self._lock:
                req = self._pending
                self._pending = None
            if req is None:
                continue
            path, frame_index = req
            try:
                img = self._read_frame(path, frame_index)
                err = None
            except Exception as e:
                img, err = None, str(e)
            # Discard result if a newer request arrived while we were reading
            with self._lock:
                if self._pending is not None:
                    continue
            def _apply(i=img, f=frame_index, e=err):
                with self._lock:
                    stale = self._pending is not None
                if stale:
                    return
                self._callback(i, f, e)
            try:
                self._widget.after(0, _apply)
            except Exception:
                pass

    def _read_frame(self, path: str, frame_index: int):
        """Read one frame, reusing the open cap when path is unchanged."""
        rotation, sar_n, sar_d, _, _, _ = _get_meta(path)
        # Reopen cap if file changed or cap was never opened
        if self._cap_path != path or self._cap is None:
            if self._cap is not None:
                try:
                    self._cap.release()
                except Exception:
                    pass
            self._cap = _open_cap(path)
            self._cap_path = path
        cap = self._cap
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total > 0:
            frame_index = max(0, min(frame_index, total - 1))
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        ok, bgr = cap.read()
        if not ok or bgr is None:
            # Cap may be stale -- reopen and retry once
            try:
                cap.release()
            except Exception:
                pass
            self._cap = _open_cap(path)
            self._cap_path = path
            cap = self._cap
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
            ok, bgr = cap.read()
        if ok and bgr is not None:
            if rotation % 360 != 0 or sar_n != sar_d:
                bgr = _correct_bgr(bgr, rotation, sar_n, sar_d)
            return Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
        # Last resort: ffmpeg fallback (opens its own cap)
        return extract_movie_frame(path, frame_index)


# ---------------------------------------------------------------------------
# Playback engine — dedicated thread, own VideoCapture, stop via Event
# ---------------------------------------------------------------------------

class _PlaybackEngine:
    """Master-clock playback engine (Gemini/perf_counter approach).

    Single worker thread owns the VideoCapture and drives its own timing
    using time.perf_counter(). If ahead: sleep the difference. If more than
    100ms behind: skip the frame. Display delivered via after(1) which allows
    Windows DWM to swap buffers between frames.
    """

    def __init__(self, widget, on_frame):
        self._widget      = widget
        self._callback    = on_frame
        self._lock        = threading.Lock()
        self._generation  = 0

    def start(self, path, start_frame, fps, total, edit_list=None,
              end_frame=None, canvas_size=None):
        with self._lock:
            self._generation += 1
            gen = self._generation
        threading.Thread(
            target=self._worker,
            args=(gen, path, start_frame, fps, total, edit_list, end_frame,
                  canvas_size),
            daemon=True,
        ).start()

    def stop(self):
        with self._lock:
            self._generation += 1

    def _is_current(self, gen):
        with self._lock:
            return gen == self._generation

    def _worker(self, gen, path, start_frame, fps, total, edit_list, end_frame=None,
               canvas_size=None):
        try:
            import cv2 as _cv2
        except ImportError:
            self._worker_ffmpeg(gen, path, start_frame, fps, total, edit_list)
            return
        rotation, sar_n, sar_d, _interlaced, _mw, _mh = _get_meta(path)
        needs_correction = (rotation % 360 != 0) or (sar_n != sar_d)

        cap = _cv2.VideoCapture(path)
        if not cap.isOpened():
            cap = _cv2.VideoCapture(os.path.abspath(path))
        if not cap.isOpened():
            self._deliver(gen, None, -1)
            return

        fps = fps if fps > 0 else cap.get(_cv2.CAP_PROP_FPS) or 25.0
        frame_duration = 1.0 / fps
        # start_time is initialised lazily after the first frame is decoded so
        # that seek overhead (which can be 100s of ms for mid-video positions in
        # compressed formats) is not charged against the master clock.  Without
        # this, every frame appears "behind" the clock on the first read and the
        # worker skips the entire video without ever delivering a single frame.
        start_time = None
        frames_delivered = 0

        try:
            cap.set(_cv2.CAP_PROP_POS_FRAMES, start_frame)
            idx = start_frame

            while self._is_current(gen):
                # Skip cut ranges
                if edit_list and edit_list.is_cut(idx):
                    next_idx = edit_list.next_kept_frame(idx, total)
                    if next_idx >= total:
                        break
                    cap.set(_cv2.CAP_PROP_POS_FRAMES, next_idx)
                    idx = next_idx

                ok, bgr = cap.read()
                if not ok or bgr is None:
                    break

                idx += 1

                now = time.perf_counter()
                # Start the master clock on the first successfully decoded frame.
                if start_time is None:
                    start_time = now

                # Master clock sync
                target_time = start_time + (frames_delivered * frame_duration)

                if now - target_time > 0.1:
                    # More than 100ms behind — advance clock and skip to catch up
                    frames_delivered += 1
                    continue

                if target_time > now:
                    # Ahead of schedule — sleep the difference
                    time.sleep(target_time - now)

                if not self._is_current(gen):
                    break

                # Apply correction and convert
                if needs_correction:
                    bgr = _correct_bgr(bgr, rotation, sar_n, sar_d)
                img = Image.fromarray(_cv2.cvtColor(bgr, _cv2.COLOR_BGR2RGB))

                # Pre-resize to canvas dimensions in the worker thread so the
                # main thread only needs ImageTk.PhotoImage() + itemconfig().
                pre_resized = False
                if canvas_size:
                    cw, ch = canvas_size
                    iw, ih = img.size
                    if cw > 10 and ch > 10 and iw > 0 and ih > 0:
                        scale = min(cw / iw, ch / ih)
                        nw = max(1, int(iw * scale))
                        nh = max(1, int(ih * scale))
                        if nw != iw or nh != ih:
                            img = img.resize((nw, nh), Image.BILINEAR)
                        pre_resized = True

                self._deliver(gen, img, idx - 1, pre_resized)
                frames_delivered += 1

                stop_at = end_frame if end_frame is not None else total
                if stop_at > 0 and idx >= stop_at:
                    break

        finally:
            cap.release()

        if self._is_current(gen):
            self._deliver(gen, None, -1)

    def _worker_ffmpeg(self, gen, path, start_frame, fps, total, edit_list):
        """Playback via ffmpeg rawvideo pipe — used when cv2 is not installed."""
        ffmpeg_exe = shutil.which("ffmpeg")
        if not ffmpeg_exe:
            self._deliver(gen, None, -1)
            return

        meta = _ffprobe_info(path)
        width  = meta.get("width", 0)
        height = meta.get("height", 0)
        if not width or not height:
            self._deliver(gen, None, -1)
            return

        if fps <= 0:
            fps = meta.get("fps", 25.0) or 25.0
        frame_duration = 1.0 / fps
        start_ts       = start_frame / fps

        # Build SAR/deinterlace filter chain.
        # Rotation is handled by ffmpeg's built-in autorotate (enabled by default).
        # We do NOT add -noautorotate or an explicit transpose filter here — doing
        # both caused double-rotation (autorotate + transpose stacked, net 180° wrong).
        # IMPORTANT: update width/height to match ffmpeg's actual output dimensions
        # so frame_size (calculated below) is correct.
        rotation   = meta.get("rotation", 0)
        sar_n      = meta.get("sar_n", 1)
        sar_d      = meta.get("sar_d", 1) or 1
        vf_parts = []
        # Always apply yadif with deint=interlaced: passes progressive frames
        # through unchanged; deinterlaces interlaced frames via bitstream flags.
        vf_parts.append("yadif=mode=0:deint=interlaced")
        if sar_n != sar_d and sar_d:
            # Compute the display width explicitly (must be even for rawvideo).
            display_w = int(round(width * sar_n / sar_d))
            if display_w % 2 != 0:
                display_w += 1
            vf_parts.append(f"scale={display_w}:{height}")
            width = display_w   # ← update so frame_size matches actual output
        # Account for the dimension swap that ffmpeg's autorotate will apply.
        # frame_size must reflect the post-rotation output size, not the stored size.
        rot = rotation % 360
        if rot in (90, 270):
            width, height = height, width

        frame_size = width * height * 3  # RGB24 — calculated AFTER all dimension changes

        cmd = [
            ffmpeg_exe, "-y",
            "-ss", f"{start_ts:.3f}",
            "-i", path,
            "-f", "rawvideo", "-pix_fmt", "rgb24",
            "-an",
        ]
        if vf_parts:
            cmd += ["-vf", ",".join(vf_parts)]
        cmd.append("pipe:1")

        cf = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=cf,
            )
        except Exception:
            self._deliver(gen, None, -1)
            return

        clock_start     = None
        frames_delivered = 0
        idx             = start_frame

        try:
            while self._is_current(gen):
                # Skip cut ranges by discarding frames
                if edit_list and edit_list.is_cut(idx):
                    next_idx = edit_list.next_kept_frame(idx, total)
                    if next_idx >= total:
                        break
                    for _ in range(next_idx - idx):
                        proc.stdout.read(frame_size)
                    idx = next_idx

                data = proc.stdout.read(frame_size)
                if len(data) < frame_size:
                    break
                idx += 1

                now = time.perf_counter()
                if clock_start is None:
                    clock_start = now

                target_time = clock_start + (frames_delivered * frame_duration)
                if now - target_time > 0.1:
                    frames_delivered += 1
                    continue
                if target_time > now:
                    time.sleep(target_time - now)

                if not self._is_current(gen):
                    break

                # autorotate may change dimensions; let PIL figure it out
                try:
                    img = Image.frombytes("RGB", (width, height), data)
                except Exception:
                    frames_delivered += 1
                    continue

                self._deliver(gen, img, idx - 1)
                frames_delivered += 1

                if total > 0 and idx >= total:
                    break
        finally:
            try:
                proc.kill()
                proc.stdout.close()
                proc.wait(timeout=2)
            except Exception:
                pass

        if self._is_current(gen):
            self._deliver(gen, None, -1)

    def _deliver(self, gen, img, frame_idx, pre_resized=False):
        def _cb(i=img, f=frame_idx, g=gen, pr=pre_resized):
            if not self._is_current(g):
                return
            self._callback(i, f, None, pr)
        try:
            # after(1) not after(0) — lets Windows DWM swap buffers between frames
            self._widget.after(1, _cb)
        except Exception:
            pass


def _edited_audio_offset(start_frame: int, segments, fps: float) -> float:
    """Return the position in seconds within a cut-aware WAV that corresponds
    to start_frame in the original video.

    segments is the list of (start, end) kept-frame ranges from EditList.kept_segments().
    """
    elapsed = 0.0
    for seg_start, seg_end in segments:
        if start_frame <= seg_end:
            elapsed += max(0.0, (start_frame - seg_start) / fps)
            return elapsed
        elapsed += (seg_end - seg_start + 1) / fps
    return elapsed


class _AudioPlayer:
    """Audio extractor + persistent-subprocess playback.

    A single long-lived pygame subprocess is started once (using _AUDIO_PYTHON
    which has pygame installed) and kept alive for the session.  play/stop
    commands are sent via its stdin as JSON lines — no per-play startup
    overhead, so audio starts in sync with video.

    Usage:
        audio = _AudioPlayer()
        audio.load(path, on_status=lambda t: label.config(text=t))
        audio.play(offset_seconds)   # call from _start_playback
        audio.stop()                 # call from _stop_playback

    For cut-preview playback:
        audio.prepare_edited(widget, path, segments, fps, on_ready)
        # on_ready(wav_path_or_None) called on UI thread when WAV is ready.
        # If wav_path is not None, _ready and _wav_path are already set;
        # caller just computes offset and calls audio.play(offset).
    """

    _cache: dict = {}           # source_path → full wav_path
    _edited_cache: dict = {}    # (source_path, segments_key) → cut-aware wav_path

    # ── Class-level persistent audio subprocess (shared across all instances)
    _server_proc: "Optional[subprocess.Popen]" = None
    _server_lock: threading.Lock = threading.Lock()

    # Inline script run by the persistent subprocess.  Reads JSON lines from
    # stdin and handles "play" / "stop" commands until stdin closes.
    _SERVER_SCRIPT = (
        "import pygame, sys, json\n"
        "pygame.mixer.init(frequency=44100,size=-16,channels=2,buffer=1024)\n"
        "sys.stdout.write('ready\\n'); sys.stdout.flush()\n"
        "for line in sys.stdin:\n"
        "  line=line.strip()\n"
        "  if not line: continue\n"
        "  try:\n"
        "    msg=json.loads(line)\n"
        "  except Exception: continue\n"
        "  cmd=msg.get('cmd')\n"
        "  if cmd=='play':\n"
        "    try:\n"
        "      pygame.mixer.music.load(msg['wav'])\n"
        "      pygame.mixer.music.play(start=float(msg.get('offset',0)))\n"
        "    except Exception as e: sys.stderr.write(str(e)+'\\n')\n"
        "  elif cmd=='stop':\n"
        "    try: pygame.mixer.music.stop()\n"
        "    except Exception: pass\n"
    )

    @classmethod
    def _get_server(cls) -> "Optional[subprocess.Popen]":
        """Return the running audio server, starting it if necessary."""
        with cls._server_lock:
            if cls._server_proc is not None and cls._server_proc.poll() is None:
                return cls._server_proc
            # Start a fresh persistent subprocess
            cf = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW
            try:
                proc = subprocess.Popen(
                    [_AUDIO_PYTHON, "-c", cls._SERVER_SCRIPT],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    creationflags=cf,
                )
                # Wait for "ready" line (pygame init done) — timeout 5 s
                import select as _sel, time as _t
                deadline = _t.time() + 5.0
                ready = False
                while _t.time() < deadline:
                    if proc.poll() is not None:
                        break   # subprocess died
                    # Non-blocking readline on all platforms via a reader thread
                    line = proc.stdout.readline()
                    if line.strip() == b"ready":
                        ready = True
                        break
                if not ready:
                    try: proc.terminate()
                    except Exception: pass
                    cls._server_proc = None
                    return None
                cls._server_proc = proc
                return proc
            except Exception as e:
                print(f"ft_movie AudioPlayer server: {e}")
                cls._server_proc = None
                return None

    @classmethod
    def _send(cls, msg: dict):
        """Send a JSON command to the persistent audio server."""
        import json as _json
        proc = cls._get_server()
        if proc is None or proc.stdin is None:
            return
        try:
            proc.stdin.write((_json.dumps(msg) + "\n").encode())
            proc.stdin.flush()
        except Exception:
            # Pipe broken — mark dead so next call restarts it
            with cls._server_lock:
                cls._server_proc = None

    def __init__(self):
        self._source_path: Optional[str] = None
        self._wav_path:    Optional[str] = None
        self._ready        = False
        self._extracting   = False
        self._on_status_cb: Optional[Callable] = None
        # Pre-warm the audio server so first play() has minimal latency
        threading.Thread(target=self._get_server, daemon=True,
                         name="AudioServerWarm").start()

    # ── Public API ────────────────────────────────────────────────────

    def load(self, path: str, on_status: Optional[Callable] = None):
        """Begin audio extraction for path (no-op if already cached)."""
        self._on_status_cb = on_status
        if path == self._source_path and (self._ready or self._extracting):
            return
        self.stop()
        self._source_path = path
        self._ready       = False
        self._extracting  = True
        # Reuse previously extracted WAV if it still exists
        if path in _AudioPlayer._cache:
            wav = _AudioPlayer._cache[path]
            if os.path.exists(wav):
                self._wav_path   = wav
                self._ready      = True
                self._extracting = False
                if on_status:
                    on_status("♪ ready")
                return
        if on_status:
            on_status("♪ extracting audio…")
        threading.Thread(target=self._extract, args=(path,), daemon=True).start()

    def play(self, offset_seconds: float):
        """Send a play command to the persistent audio server."""
        self.stop()
        if not self._ready or not self._wav_path:
            return
        self._send({"cmd": "play", "wav": self._wav_path,
                    "offset": max(0.0, offset_seconds)})

    def stop(self):
        """Send a stop command to the persistent audio server."""
        self._send({"cmd": "stop"})

    @property
    def ready(self) -> bool:
        return self._ready

    def prepare_edited(self, widget, path: str, segments: list, fps: float,
                       on_ready):
        """Build a cut-aware WAV containing only the kept segments, then call
        on_ready(wav_path_or_None) on the UI thread.

        Uses the cached full WAV as source when available (fast byte-copy
        operations on uncompressed PCM) — falls back to extracting directly
        from the video file otherwise.

        If on_ready receives a non-None path, self._ready and self._wav_path
        are already set; the caller should compute the audio start offset via
        _edited_audio_offset() and call self.play(offset).
        """
        import hashlib
        self.stop()
        key = tuple(segments)
        cache_key = (path, key)

        # ── Cache hit ────────────────────────────────────────────────────
        if cache_key in _AudioPlayer._edited_cache:
            cached = _AudioPlayer._edited_cache[cache_key]
            if os.path.exists(cached):
                self._wav_path = cached
                self._ready    = True
                widget.after(0, lambda: on_ready(cached))
                return

        sig     = hashlib.md5(str(cache_key).encode()).hexdigest()[:16]
        out_wav = os.path.join(tempfile.gettempdir(), f"ftview_edited_{sig}.wav")
        cf      = 0x08000000 if os.name == "nt" else 0

        if self._on_status_cb:
            self._on_status_cb("♪ preparing cut audio…")

        def _work():
            try:
                ffmpeg_exe = shutil.which("ffmpeg")
                if not ffmpeg_exe:
                    widget.after(0, lambda: on_ready(None))
                    return

                # Prefer full WAV source — uncompressed, so per-segment
                # extraction is near-instant.  Fall back to video source.
                full_wav = _AudioPlayer._cache.get(path)
                source   = (full_wav
                            if full_wav and os.path.exists(full_wav)
                            else path)

                tmp_dir  = tempfile.gettempdir()
                seg_wavs = []
                ok       = True

                try:
                    for i, (s, e) in enumerate(segments):
                        seg_wav = os.path.join(tmp_dir,
                                               f"ftview_audseg_{sig}_{i}.wav")
                        dur = (e + 1 - s) / max(fps, 1.0)
                        cmd = [ffmpeg_exe, "-y",
                               "-ss", f"{s / fps:.3f}",
                               "-i", source,
                               "-t",  f"{dur:.3f}",
                               "-vn", "-acodec", "pcm_s16le",
                               "-ar", "44100", "-ac", "2",
                               seg_wav]
                        r = subprocess.run(cmd,
                                           stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE,
                                           timeout=300, creationflags=cf)
                        if r.returncode != 0 or not os.path.exists(seg_wav):
                            ok = False
                            break
                        seg_wavs.append(seg_wav)

                    if ok and len(seg_wavs) == 1:
                        # Single kept segment — rename directly
                        import shutil as _sh
                        _sh.move(seg_wavs[0], out_wav)
                        seg_wavs = []
                    elif ok:
                        # Multiple segments — concat with ffmpeg concat demuxer
                        lst = os.path.join(tmp_dir, f"ftview_audlist_{sig}.txt")
                        with open(lst, "w") as fh:
                            for sw in seg_wavs:
                                fh.write(f"file '{sw.replace(chr(92), chr(47))}'\n")
                        r = subprocess.run(
                            [ffmpeg_exe, "-y", "-f", "concat", "-safe", "0",
                             "-i", lst, "-c", "copy", out_wav],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=120, creationflags=cf)
                        ok = r.returncode == 0 and os.path.exists(out_wav)
                        try: os.unlink(lst)
                        except Exception: pass
                finally:
                    for sw in seg_wavs:
                        try: os.unlink(sw)
                        except Exception: pass

                if ok:
                    _AudioPlayer._edited_cache[cache_key] = out_wav
                    self._wav_path = out_wav
                    self._ready    = True
                    if self._on_status_cb:
                        self._on_status_cb("♪ ready")
                    widget.after(0, lambda: on_ready(out_wav))
                else:
                    widget.after(0, lambda: on_ready(None))

            except Exception:
                widget.after(0, lambda: on_ready(None))

        threading.Thread(target=_work, daemon=True).start()

    # ── Extraction thread ─────────────────────────────────────────────

    def _extract(self, path: str):
        import hashlib
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self._extracting = False
            if self._on_status_cb:
                self._on_status_cb("♪ ffmpeg not found")
            return
        key      = hashlib.md5(path.encode("utf-8", errors="replace")).hexdigest()[:16]
        wav_path = os.path.join(tempfile.gettempdir(), f"ftview_audio_{key}.wav")
        if not os.path.exists(wav_path):
            cmd = [ffmpeg, "-y", "-i", path, "-vn",
                   "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2", wav_path]
            try:
                r = subprocess.run(cmd, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, timeout=600)
                if r.returncode != 0:
                    self._extracting = False
                    if self._on_status_cb:
                        self._on_status_cb("♪ extraction failed")
                    return
            except Exception as e:
                self._extracting = False
                if self._on_status_cb:
                    self._on_status_cb(f"♪ error: {e}")
                return
        _AudioPlayer._cache[path] = wav_path
        self._wav_path   = wav_path
        self._ready      = True
        self._extracting = False
        if self._on_status_cb:
            self._on_status_cb("♪ ready")




class _VLCEngine:
    """GPU-accelerated playback via python-vlc.

    VLC renders directly into a native window handle (HWND on Windows) so
    video is decoded on the GPU and audio is kept in perfect sync.  Position
    updates arrive via a ~100 ms polling timer rather than per-frame callbacks.

    Callbacks:
        on_position(frame_index: int)  — ~10 Hz during playback
        on_eos()                       — once when stream ends
    """

    def __init__(self, widget: tk.Widget):
        self._widget   = widget
        self._instance = None
        self._player   = None
        self._gen      = 0
        self._poll_id  = None
        self._fps      = 25.0
        self._cut_tmp  = None
        self._on_pos   = None
        self._on_eos   = None
        self._try_init()

    def _try_init(self):
        try:
            self._instance = _vlc_module.Instance([
                '--no-video-title-show', '--no-snapshot-preview',
            ])
            self._player = self._instance.media_player_new()
        except Exception as e:
            print(f"[VLC] init error: {e}")

    @property
    def ok(self) -> bool:
        return self._player is not None

    # ── Public ──────────────────────────────────────────────────────────────

    def attach(self, hwnd: int):
        """Attach VLC renderer to a native window handle (call before start)."""
        if self._player:
            try:
                self._player.set_hwnd(int(hwnd))
            except Exception as e:
                print(f"[VLC] attach error: {e}")

    def start(self, path: str, start_frame: int, fps: float, total: int,
              edit_list=None, end_frame=None,
              on_position=None, on_eos=None):
        if not self._player:
            return
        self._gen  += 1
        gen         = self._gen
        self._fps   = max(1.0, float(fps))
        self._on_pos = on_position
        self._on_eos = on_eos

        if self._poll_id:
            try:
                self._widget.after_cancel(self._poll_id)
            except Exception:
                pass
            self._poll_id = None
        self._cleanup_cut_tmp()

        # Pre-generate cut file if the edit list has cuts
        media_path = path
        if edit_list and edit_list.has_cuts and self._fps > 0:
            tmp = self._make_cut_file(path, edit_list, total, self._fps)
            if tmp:
                self._cut_tmp = tmp
                media_path    = tmp
                start_frame   = 0
                end_frame     = None

        media = self._instance.media_new(media_path)
        if self._fps > 0:
            start_s = max(0.0, start_frame / self._fps)
            if start_s > 0.05:
                media.add_option(f':start-time={start_s:.3f}')
            if end_frame is not None and end_frame < total and not self._cut_tmp:
                media.add_option(f':stop-time={end_frame / self._fps:.3f}')

        self._player.set_media(media)
        try:
            em = self._player.event_manager()
            em.event_attach(
                _vlc_module.EventType.MediaPlayerEndReached,
                lambda e, g=gen: self._widget.after(0, self._fire_eos, g),
            )
        except Exception:
            pass
        self._player.play()
        self._poll(gen)

    def stop(self):
        self._gen += 1
        if self._poll_id:
            try:
                self._widget.after_cancel(self._poll_id)
            except Exception:
                pass
            self._poll_id = None
        if self._player:
            try:
                self._player.stop()
            except Exception:
                pass
        self._cleanup_cut_tmp()

    def get_time_ms(self) -> int:
        if self._player:
            try:
                return self._player.get_time()
            except Exception:
                pass
        return -1

    def set_pause(self, paused: bool):
        if self._player:
            try:
                self._player.set_pause(1 if paused else 0)
            except Exception:
                pass

    # ── Internal ─────────────────────────────────────────────────────────────

    def _poll(self, gen: int):
        if self._gen != gen:
            return
        ms = self.get_time_ms()
        if ms >= 0 and self._on_pos and self._fps > 0:
            try:
                self._on_pos(int(ms / 1000.0 * self._fps))
            except Exception:
                pass
        self._poll_id = self._widget.after(100, lambda: self._poll(gen))

    def _fire_eos(self, gen: int):
        if self._gen != gen:
            return
        if self._poll_id:
            try:
                self._widget.after_cancel(self._poll_id)
            except Exception:
                pass
            self._poll_id = None
        if self._on_eos:
            try:
                self._on_eos()
            except Exception:
                pass

    def _make_cut_file(self, path: str, edit_list, total: int, fps: float):
        """Pre-generate an edited temp file via ffmpeg concat (stream copy, lossless)."""
        try:
            ffmpeg = shutil.which('ffmpeg')
            if not ffmpeg:
                return None
            segments = edit_list.kept_segments(total)
            if not segments:
                return None
            pid       = os.getpid()
            tmp_dir   = tempfile.gettempdir()
            list_path = os.path.join(tmp_dir, f'ft_vlc_concat_{pid}.txt')
            out_path  = os.path.join(tmp_dir, f'ft_vlc_cut_{pid}.mp4')
            safe_path = path.replace("'", "\'")
            with open(list_path, 'w', encoding='utf-8') as lf:
                for in_f, out_f in segments:
                    lf.write(f"file '{safe_path}'\n")
                    lf.write(f"inpoint {in_f / fps:.3f}\n")
                    lf.write(f"outpoint {out_f / fps:.3f}\n")
            result = subprocess.run(
                [ffmpeg, '-y', '-f', 'concat', '-safe', '0',
                 '-i', list_path, '-c', 'copy', out_path],
                capture_output=True, timeout=30,
            )
            try:
                os.unlink(list_path)
            except Exception:
                pass
            if result.returncode == 0 and os.path.exists(out_path):
                return out_path
        except Exception as e:
            print(f"[VLC] cut file error: {e}")
        return None

    def _cleanup_cut_tmp(self):
        if self._cut_tmp:
            try:
                os.unlink(self._cut_tmp)
            except Exception:
                pass
            self._cut_tmp = None

    def prime(self, hwnd: int):
        """Play a blank internal source for ~150 ms to warm up DirectDraw/codec.

        Called once at app startup so the first real video plays without stutter.
        """
        if not self._player:
            return
        try:
            self.attach(hwnd)
            dummy = self._instance.media_new('vlc://pause:0.1')
            self._player.set_media(dummy)
            self._player.play()
        except Exception as e:
            print(f"[VLC] prime error: {e}")


class MoviePlayerPanel(tk.Frame):
    """Scrub player for FTView Movies mode.

    Public API mirrors ViewerPanel:
        set_file_list(files, index)
        show_index(index)
        show_message(text)
        set_output_folder(folder)
    """

    THUMB_POSITION = 0.10   # 10% into video for thumbnail

    def __init__(self, parent, *, bg=CANVAS_BG, longpath_func=None,
                 on_select_index: Optional[Callable] = None,
                 output_folder=None,
                 on_edit_done: Optional[Callable] = None):
        super().__init__(parent, bg=bg)
        self._longpath        = longpath_func
        self._on_select_index = on_select_index
        # output_folder may be a plain string or a callable(video_path) -> str
        self._output_folder   = output_folder or ""
        self._on_edit_done_cb = on_edit_done

        # File list
        self.files: list[str]     = []
        self.index: Optional[int] = None
        self.current_path: Optional[str] = None

        # Video metadata
        self._total_frames = 0
        self._fps          = 0.0
        self._duration_s   = 0.0

        # Position
        self._frame_index  = 0

        # State
        self._playing        = False
        self._scrub_dragging  = False
        self._scrub_after_id  = None
        self._clip_end_frame  = None
        self._last_ui_tick    = 0.0    # perf_counter timestamp of last scrub/TC update
        self._player_mode     = "clip"  # "clip" = single preview, "timeline" = strip sequence
        self._auto_play_timer = None   # pending after() id from _load_strip_clip auto_play
        self._preview_loading = False  # True while background thumbnail load is in-flight

        # Play-all (combine strip sequential playback)
        self._play_all_mode  = False
        self._play_all_idx   = 0

        # Workers
        self._loader = _FrameLoader(self, self._on_frame_loaded)
        if HAVE_VLC:
            self._vlc    = _VLCEngine(self)
            self._engine = None
            self._audio  = None
        else:
            self._vlc    = None
            self._engine = _PlaybackEngine(self, self._on_playback_frame)
            self._audio  = _AudioPlayer()
        self._vlc_active = False   # VLC has media loaded (playing or paused)
        self._vlc_paused = False   # VLC is currently paused (not stopped)

        self._build_ui()
        self.show_message("No video selected")
        if self._vlc and self._vlc.ok:
            # Warm up VLC DirectDraw/codec on startup to eliminate first-play stutter
            self.after(400, self._vlc_prime)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _btn(self, parent, text, cmd, bg=BG2, fg=TEXT, width=None, bold=False):
        kw = dict(text=text, command=cmd, bg=bg, fg=fg,
                  activebackground=bg, activeforeground=fg,
                  font=("Segoe UI", 9, "bold") if bold else ("Segoe UI", 9),
                  relief="raised", bd=1, padx=5, pady=1)
        if width:
            kw["width"] = width
        return tk.Button(parent, **kw)

    def _build_ui(self):
        self.grid_rowconfigure(0, weight=0, minsize=34)
        self.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(2, weight=0, minsize=52)
        self.grid_rowconfigure(3, weight=0)
        self.grid_rowconfigure(4, weight=0, minsize=116)
        self.grid_columnconfigure(0, weight=1)

        # ── Top bar ──────────────────────────────────────────────────
        bar = tk.Frame(self, bg=BG2, height=34)
        bar.grid(row=0, column=0, sticky="ew")
        bar.grid_propagate(False)
        bar.pack_propagate(False)

        self.btn_prev = self._btn(bar, "◀ Prev", self.prev_file)
        self.btn_prev.pack(side="left", padx=(4, 2), pady=3)
        self.btn_next = self._btn(bar, "Next ▶", self.next_file)
        self.btn_next.pack(side="left", padx=2, pady=3)

        self._btn(bar, "Output folder…", self._choose_output_folder).pack(
            side="left", padx=(14, 2), pady=3)
        self._btn(bar, "Open in player", self._open_external).pack(
            side="left", padx=(10, 2), pady=3)
        self._btn(bar, "Fit", lambda: self.canvas.fit()).pack(
            side="right", padx=4, pady=3)

        self.lbl_info = tk.Label(bar, text="No video selected",
                                 bg=BG2, fg=DIM, anchor="w", font=("Segoe UI", 9))
        self.lbl_info.pack(side="left", fill="x", expand=True, padx=6)

        # ── Canvas ───────────────────────────────────────────────────
        self.canvas = _ZoomCanvas(self, bg=CANVAS_BG)
        self.canvas.grid(row=1, column=0, sticky="nsew")

        # bind_all so arrow keys work regardless of which widget has focus
        self.bind_all("<Left>",       lambda e: self._step(-1))
        self.bind_all("<Right>",      lambda e: self._step(+1))
        self.bind_all("<Shift-Left>", lambda e: self._step(-10))
        self.bind_all("<Shift-Right>",lambda e: self._step(+10))
        self.canvas.bind("<ButtonPress-1>",
                         lambda e: self.canvas.focus_set(), add="+")
        # Space via bind_all covers all focus targets (panel, canvas, buttons)
        # Do NOT add direct <space> bindings here — bind_all already fires for
        # self and canvas, and adding both causes double-toggle.
        self.bind_all("<space>", self._on_space)

        # ── Scrub bar ────────────────────────────────────────────────
        scrub_bar = tk.Frame(self, bg=SCRUB_BG, height=50)
        scrub_bar.grid(row=2, column=0, sticky="ew")
        scrub_bar.grid_propagate(False)
        scrub_bar.pack_propagate(False)

        row1 = tk.Frame(scrub_bar, bg=SCRUB_BG)
        row1.pack(fill="x", padx=8, pady=(5, 0))

        def _play_click():
            self._toggle_play()
        self.btn_play = self._btn(row1, "▶", _play_click,
                                  bg=PLAY_GREEN, fg="white", width=10, bold=True)
        self.btn_play.pack(side="left", padx=(0, 6))

        self.btn_grab = self._btn(row1, "Save Frame", self._grab_frame,
                                  bg=ACCENT, fg="white", bold=True)
        self.btn_grab.pack(side="left", padx=(0, 10))

        self.lbl_timecode = tk.Label(row1, text="00:00:00:00", width=13,
                                     bg=SCRUB_BG, fg="white",
                                     font=("Courier New", 10, "bold"))
        self.lbl_timecode.pack(side="left")

        self.lbl_duration = tk.Label(row1, text="/ 0:00", width=8,
                                     bg=SCRUB_BG, fg="#aaccee",
                                     font=("Segoe UI", 9))
        self.lbl_duration.pack(side="left", padx=(4, 0))

        self.lbl_out = tk.Label(row1, text="Output: (none set)",
                                bg=SCRUB_BG, fg="#aaccee",
                                font=("Segoe UI", 8), anchor="w")
        self.lbl_out.pack(side="left", padx=(16, 0), fill="x", expand=True)

        self.lbl_audio = tk.Label(row1, text="♪ …",
                                  bg=SCRUB_BG, fg="#aaccee",
                                  font=("Segoe UI", 8), anchor="e")
        self.lbl_audio.pack(side="right", padx=(0, 8))

        # scrub_var kept as a stub so other methods that call .set() don't crash
        self.scrub_var = tk.IntVar(value=0)

        # ── Scrub position track ──────────────────────────────────────
        self._scrub_canvas = tk.Canvas(scrub_bar, bg="#0a1a2a", height=15,
                                       highlightthickness=0,
                                       cursor="sb_h_double_arrow")
        self._scrub_canvas.pack(fill="x", padx=8, pady=(2, 2))
        self._scrub_canvas.bind("<ButtonPress-1>",   self._scrub_press)
        self._scrub_canvas.bind("<B1-Motion>",       self._scrub_drag)
        self._scrub_canvas.bind("<ButtonRelease-1>", self._scrub_release)
        self._scrub_canvas.bind("<Configure>",       self._scrub_redraw)
        self._scrub_canvas.bind("<Button-3>",        self._scrub_right_click)

        self._update_out_label()

        # ── Marker / edit bar ────────────────────────────────────────
        if HAVE_EDIT:
            self.marker_bar = MarkerBar(
                self,
                get_frame = lambda: self._frame_index,
                get_total = lambda: self._total_frames,
                get_fps   = lambda: self._fps,
                get_path  = lambda: self.current_path,
                seek_to             = self._seek_to_frame,
                on_commit           = self._on_edit_done,
                on_markers_changed  = self._on_markers_changed,
                on_split_delete     = self._on_pending_split_delete,
                bg=SCRUB_BG,
            )
            self.marker_bar.grid(row=3, column=0, sticky="ew")
            for w in (self, self.canvas):
                w.bind("<m>", lambda e: self.marker_bar.set_marker(), add="+")
                w.bind("<M>", lambda e: self.marker_bar.set_marker(), add="+")
        else:
            self.marker_bar = None

        # ── Combine strip (multi-clip timeline) ──────────────────────
        if HAVE_COMBINE:
            self.combine_strip = CombineStrip(
                self,
                on_clip_selected  = self._on_strip_clip_selected,
                on_seek           = self._on_strip_seek,
                on_drop_path      = self._on_strip_drop,
                on_play_all       = self._on_strip_play_all,
                on_strip_activate = self._on_strip_activate,
            )
            self.combine_strip.grid(row=4, column=0, sticky="ew")
            self.combine_strip._on_right_click_extra = self._combine_marker_menu
        else:
            self.combine_strip = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_output_folder(self, folder: str):
        self._output_folder = folder or ""
        self._update_out_label()

    def set_file_list(self, files, index=None):
        new_files = list(files or [])
        # Only reset file list if it actually changed (avoids stop() on every click)
        if new_files != self.files:
            self.files = new_files
            self._stop_playback()
        if index is None or not self.files:
            self.index = None
            self.show_message("No video selected" if not self.files
                              else "No file selected")
            return
        self.show_index(index)

    def show_index(self, index: int):
        if not self.files or index < 0 or index >= len(self.files):
            return
        new_path = self.files[index]
        # Don't reload if already showing this file
        if new_path == self.current_path and self.index == index:
            return
        self._stop_playback()
        self.index           = index
        self.current_path    = new_path
        self._frame_index    = 0     # single-clip preview always starts at frame 0
        self._clip_end_frame = None  # play to EOF for individual clip preview
        self._player_mode    = "clip"
        self._load_info()

    def _on_edit_done(self, path: str):
        """Called after a successful commit — notify FTView to refresh."""
        if self._on_edit_done_cb:
            self.after(0, lambda p=path: self._on_edit_done_cb(p))

    # ------------------------------------------------------------------
    # Combine strip — public entry point
    # ------------------------------------------------------------------
    def _on_pending_split_delete(self, start_f: int, end_f: int):
        """Called by MarkerBar._do_commit for each pending cut (in reverse order).
        start_f / end_f are frame indices relative to the active clip (0-based).
        Performs CombineStrip split+delete without writing any file.
        """
        if not (self.combine_strip and self.marker_bar):
            return
        cs  = self.combine_strip
        act = cs.get_active_index()
        if act is None:
            return
        clips = cs.get_clips()
        if not clips or act >= len(clips):
            return
        ip        = clips[act].in_point
        src_start = ip + start_f
        src_end   = ip + end_f
        # Split at deletion-zone start → head at act, rest at act+1
        cs.split_clip(act, src_start)
        # Split rest at deletion-zone end → delete-zone at act+1, tail at act+2
        cs.split_clip(act + 1, src_end)
        # Remove the deletion zone
        cs.remove_clip(act + 1)
        # Pin active back to head so next cut (in descending order) hits it
        cs.set_active_index(act)


    def add_clip_to_strip(self, path: str, thumb_data=None):
        """Add a single clip. Delegates to add_clips_to_strip for ordering safety."""
        self.add_clips_to_strip([(path, thumb_data)])

    def add_clips_to_strip(self, path_thumb_pairs):
        """Add multiple clips in order, using a single thread so they land in sequence.

        path_thumb_pairs: iterable of (path, thumb_data) — thumb_data may be None.
        """
        if not self.combine_strip:
            return
        pairs = list(path_thumb_pairs)

        def _work():
            resolved = []
            for path, thumb_data in pairs:
                try:
                    info  = get_video_info(path, self._longpath)
                    fps   = float(info.get("fps", 0.0) or 25.0)
                    total = max(1, int(info.get("frame_count", 0)))
                    dur   = float(info.get("duration_s", 0.0) or 0.0)
                except Exception:
                    fps, total, dur = 25.0, 1000, 30.0

                td = thumb_data
                if td is None:
                    try:
                        from libraries import ft_thumb_cache as _tc
                        n = os.path.normpath(path.lstrip("\\\\?\\"))
                        td = _tc.get_thumb(n)
                    except Exception:
                        pass
                resolved.append((path, fps, total, dur, td))

            def _add_all(items=resolved):
                if self.combine_strip:
                    for p, f, t, d, tb in items:
                        self.combine_strip.add_clip(p, f, t, d, tb)
            self.after(0, _add_all)

        threading.Thread(target=_work, daemon=True).start()

    # ------------------------------------------------------------------
    # Combine strip — callbacks
    # ------------------------------------------------------------------

    def _on_strip_clip_selected(self, idx: int, entry):
        """User clicked a clip in the strip — switch to timeline mode and load the clip."""
        self._player_mode = "timeline"
        self._save_strip_edit_state()
        self._stop_playback()
        self._play_all_mode = False
        self.current_path    = entry.path
        self.index           = None
        self._frame_index    = getattr(entry, "in_point",  0)
        self._clip_end_frame = getattr(entry, "out_point", None)
        self._load_info()
        self.after(250, lambda i=idx: self._load_strip_edit_state(i))

    def _on_strip_seek(self, clip_idx: int, frame: int):
        """Strip canvas click — switch to timeline mode and seek."""
        self._player_mode = "timeline"
        if not self.combine_strip:
            return
        clips = self.combine_strip.get_clips()
        if not (0 <= clip_idx < len(clips)):
            return
        entry = clips[clip_idx]
        if self.current_path != entry.path:
            self._on_strip_clip_selected(clip_idx, entry)
            return
        self._stop_playback()
        self._frame_index = frame
        self._update_timecode()
        if self.current_path:
            self._loader.request(self.current_path, frame)

    def _on_strip_drop(self, path: str):
        self.add_clip_to_strip(path)

    def _on_strip_activate(self):
        """Any press on the strip canvas — switch to timeline mode.
        If switching FROM clip mode, load the active strip clip so that
        in/out points and clip_end_frame are applied correctly."""
        if self._player_mode == "timeline":
            return  # already in timeline mode
        self._player_mode = "timeline"
        if self.combine_strip:
            active = self.combine_strip.get_active_index()
            if active is not None:
                clips = self.combine_strip.get_clips()
                if 0 <= active < len(clips):
                    self._on_strip_clip_selected(active, clips[active])

    def _on_strip_play_all(self):
        """Play/pause toggle for the full clip sequence."""
        self._player_mode = "timeline"
        self._toggle_play()

    def _combine_marker_menu(self, menu, event_x):
        """Inject marker-cut items into the CombineStrip right-click menu.
        event_x is the canvas x of the right-click — we convert it to the
        active clip's frame number so markers around the MOUSE position are used,
        not the playhead position.
        """
        if not self.marker_bar or not self.combine_strip:
            return
        markers = self.marker_bar.get_markers()
        cuts    = self.marker_bar.edit_list.cuts
        if not markers and not cuts:
            return
        # Convert mouse x → timeline seconds → active-clip frame
        try:
            t_click = self.combine_strip._cx_to_time(event_x)
            active  = self.combine_strip.get_active_index()
            clips   = self.combine_strip.get_clips()
            t0_list = self.combine_strip._clip_t0
            frame   = self._frame_index  # fallback
            if active is not None and 0 <= active < len(clips) and active < len(t0_list):
                clip    = clips[active]
                fps     = (clip.fps if clip.fps > 0 else self._fps) or 25.0
                local_t = t_click - t0_list[active]
                frame   = clip.in_point + int(local_t * fps)
        except Exception as _e:
            print(f"_combine_marker_menu frame calc error: {_e}")
            frame = self._frame_index
        menu.add_separator()
        fps = self._fps or 25.0
        # Between two markers — offer to cut
        left_m  = max((m for m in markers if m <= frame), default=None)
        right_m = min((m for m in markers if m  > frame), default=None)
        if left_m is not None and right_m is not None:
            dur = (right_m - left_m) / fps
            menu.add_command(
                label=f"\u2702  Cut {left_m}\u2013{right_m} ({dur:.1f}s)",
                command=lambda l=left_m, r=right_m: self.marker_bar._cut_between(l, r))
        # Inside a cut — offer to remove it
        for cut in cuts:
            if cut.contains(frame):
                menu.add_command(
                    label=f"Remove cut ({cut.start}\u2013{cut.end}, {cut.duration(fps):.1f}s)",
                    command=lambda c=cut: self.marker_bar._remove_cut(c))
                break
        if markers:
            menu.add_command(label="Clear all markers",
                             command=self.marker_bar._clear_markers)
        menu.add_command(label="\u21a9  Undo last cut",
                         command=self.marker_bar._do_undo,
                         state="normal" if cuts else "disabled")

    def _on_markers_changed(self, markers):
        """Relay marker changes to CombineStrip and scrub canvas."""
        if self.combine_strip:
            active = self.combine_strip.get_active_index()
            self.combine_strip.set_overlay_markers(markers, active)
        self._scrub_redraw()

    def _seek_to_frame(self, frame: int):
        """Seek to frame, using VLC set_time() when active (instant GPU display),
        or PIL loader otherwise.  Called by MarkerBar seek_to."""
        self._frame_index = max(0, min(frame, max(0, self._total_frames - 1)))
        if self._vlc and self._vlc.ok and self._vlc_active and self._fps > 0:
            try:
                ms = int(self._frame_index / self._fps * 1000)
                self._vlc._player.set_time(max(0, ms))
            except Exception:
                pass
        elif self.current_path:
            self._loader.request(self.current_path, self._frame_index)
        self._update_timecode()

    def _load_strip_clip(self, idx: int, auto_play: bool = False):
        if not self.combine_strip:
            return
        clips = self.combine_strip.get_clips()
        if not (0 <= idx < len(clips)):
            return
        entry = clips[idx]
        self._save_strip_edit_state()
        self._stop_playback()
        self.current_path    = entry.path
        self.index           = None
        self._frame_index    = entry.in_point
        self._clip_end_frame = entry.out_point

        # Fast path: ClipEntry already carries all metadata -- use it directly
        # to avoid the async _load_info() "Loading..." pause and 400 ms timer.
        if auto_play and entry.fps > 0 and entry.total_frames > 0:
            self._total_frames = entry.total_frames
            self._fps          = entry.fps
            self._duration_s   = entry.duration_s
            self._loader.cancel()
            self._update_scrub_range()
            self._update_timecode()
            dur  = _fmt_duration(self._duration_s)
            self.lbl_info.configure(
                text=f"{entry.name}   {dur}  {self._fps:.2f} fps")
            self.lbl_duration.configure(text=f"/ {dur}")
            self._update_buttons()
            # Load edit-list markers immediately from ClipEntry
            self._load_strip_edit_state(idx)
            # Load audio in background (non-blocking)
            if self._audio:
                self._audio.load(entry.path)
            # Start playback immediately -- no timer needed
            self._start_playback()
            self._update_buttons()
            return

        # Slow path: fall back to async metadata load (first manual clip select)
        self._load_info()
        self.after(250, lambda i=idx: self._load_strip_edit_state(i))
        if auto_play:
            if self._auto_play_timer:
                try:
                    self.after_cancel(self._auto_play_timer)
                except Exception:
                    pass
            self._auto_play_timer = self.after(400, self._auto_play_after_load)

    def _auto_play_after_load(self):
        self._auto_play_timer = None
        if self.current_path and self._total_frames > 0 and not self._playing:
            self._start_playback()
            self._update_buttons()

    def _save_strip_edit_state(self):
        if not (self.combine_strip and self.marker_bar):
            return
        idx = self.combine_strip.get_active_index()
        if idx is not None:
            self.combine_strip.save_edit_list(idx, self.marker_bar.get_edit_list())

    def _load_strip_edit_state(self, clip_idx: int):
        if not (self.combine_strip and self.marker_bar):
            return
        # Always reset markers for the incoming clip — old clip state was already
        # saved by _save_strip_edit_state before this is called.
        self.marker_bar.reset_for_new_file()
        el = self.combine_strip.get_entry_edit_list(clip_idx)
        if el is not None:
            self.marker_bar.load_edit_list(el)

    @property
    def is_playing(self) -> bool:
        """True while video playback is running."""
        return bool(self._playing)

    def show_message(self, text: str):
        self._stop_playback()
        # Fully release VLC when clearing display (file switch / no selection)
        if self._vlc:
            try:
                self._vlc.stop()
            except Exception:
                pass
            self._vlc_active = False
            self._vlc_paused = False
        self.current_path  = None
        self._total_frames = 0
        self._fps          = 0.0
        self._duration_s   = 0.0
        self._frame_index  = 0
        self._clip_end_frame = None
        self.lbl_info.configure(text=text)
        self.canvas.set_message(text)
        self._update_scrub_range()
        self._update_timecode()
        self._update_buttons()

    # ------------------------------------------------------------------
    # Load metadata + first frame
    # ------------------------------------------------------------------

    def _load_info(self):
        path = self.current_path
        if not path:
            return
        self._loader.cancel()
        self.canvas.set_message("Loading…")
        self.lbl_info.configure(text=f"Loading  {os.path.basename(path)}…")
        # Reset edit list for new file
        if self.marker_bar is not None:
            self.marker_bar.reset_for_new_file()

        def _work(p=path):
            info = get_video_info(p)
            def _apply(info=info, p=p):
                if self.current_path != p:
                    return
                self._total_frames = max(1, info.get("frame_count", 1))
                self._fps          = info.get("fps", 0.0) or 25.0  # default 25fps if unknown
                self._duration_s   = info.get("duration_s", 0.0)
                w, h = info.get("width", 0), info.get("height", 0)
                dur  = _fmt_duration(self._duration_s)
                fps_s = f"  {self._fps:.2f} fps" if self._fps > 0 else ""
                dim_s = f"  {w}×{h}" if w and h else ""
                self.lbl_info.configure(
                    text=f"{os.path.basename(p)}   {dur}{dim_s}{fps_s}")
                self.lbl_duration.configure(text=f"/ {dur}")
                self._update_scrub_range()
                self._update_buttons()
                # Reset scrub to 0 so bar shows start position
                try:
                    self.scrub_var.set(0)
                except Exception:
                    pass
                # Show the thumbnail frame (10% in) as preview — avoids black opening frames
                preview_frame = max(0, int(self._total_frames * self.THUMB_POSITION))
                self._preview_loading = True
                self._loader.request(p, preview_frame)
                # Begin audio extraction in background
                def _audio_status(text, _p=p):
                    if self.current_path == _p:
                        try:
                            self.lbl_audio.configure(text=text)
                        except Exception:
                            pass
                self._audio.load(p, on_status=_audio_status)
            self.after(0, _apply)

        threading.Thread(target=_work, daemon=True).start()

    # ------------------------------------------------------------------
    # Frame callbacks
    # ------------------------------------------------------------------

    def _on_frame_loaded(self, img: Optional[Image.Image],
                         frame_index: int, err: Optional[str]):
        """Callback for _FrameLoader (seek / scrub)."""
        if img is None:
            msg = f"Could not read frame: {err}" if err else "Could not read frame"
            self.canvas.set_message(msg)
            self.lbl_info.configure(text=msg)
            return
        # Use show_frame (not set_image) so zoom is preserved and the
        # persistent canvas item is reused rather than deleted+recreated.
        self.canvas.show_frame(img)
        self._preview_loading = False
        # _frame_index is authoritative from whoever called _loader.request():
        #   • _step()       already updated it before requesting
        #   • _scrub_jump() already updated it before requesting
        #   • preview load  keeps _frame_index at in_point intentionally
        # Only sync it here when scrub-dragging so the timecode stays aligned
        # with what the user actually dragged to (callbacks may arrive late).
        if self._scrub_dragging:
            self._frame_index = frame_index
        # Always update timecode so user can see the current frame position
        self._update_timecode()

    def _on_playback_frame(self, img: Optional[Image.Image],
                           frame_index: int, err: Optional[str],
                           pre_resized: bool = False):
        """Callback for _PlaybackEngine (playback)."""
        if frame_index == -1:
            # End-of-stream — reset position to clip start so next ▶ restarts cleanly
            in_pt = 0
            if self.combine_strip:
                _active = self.combine_strip.get_active_index()
                if _active is not None:
                    _clips = self.combine_strip.get_clips()
                    if 0 <= _active < len(_clips):
                        in_pt = _clips[_active].in_point
            self._frame_index = in_pt
            self._playing = False
            self._audio.stop()   # always stop audio at EOS
            self._update_buttons()
            # Play-all: advance to next clip
            if self._play_all_mode and self.combine_strip:
                self._play_all_idx += 1
                clips = self.combine_strip.get_clips()
                if self._play_all_idx < len(clips):
                    self.combine_strip.set_active_index(self._play_all_idx)
                    self._load_strip_clip(self._play_all_idx, auto_play=True)
                else:
                    self._play_all_mode = False
            return
        if img is None:
            return

        self.canvas.show_frame(img, pre_resized=pre_resized)
        self._frame_index = frame_index

        # Rate-limit scrub bar, timecode and strip cursor to ~10 Hz — these
        # are expensive UI operations that don't need per-frame precision.
        now = time.perf_counter()
        if now - self._last_ui_tick >= 0.1:
            self._last_ui_tick = now
            try:
                self.scrub_var.set(frame_index)
            except Exception:
                pass
            self._update_timecode()
            if self._player_mode == "timeline" and self.combine_strip:
                active = self.combine_strip.get_active_index()
                if active is not None:
                    self.combine_strip.set_playback_position(active, frame_index)

    def _activate_vlc_for_scrub(self, frame: int):
        """Load current media into VLC and start playing so set_time() works.

        After ~100 ms we seek to the current scrub position and pause, giving
        instant GPU-decoded frame updates during drag.
        """
        if not self.current_path or not self._vlc or not self._vlc.ok:
            return
        fps = max(1.0, self._fps)
        try:
            self.canvas.update_idletasks()
            self._vlc.attach(self.canvas.winfo_id())
            media   = self._vlc._instance.media_new(self.current_path)
            start_s = frame / fps
            if start_s > 0.05:
                media.add_option(f':start-time={start_s:.3f}')
            self._vlc._player.set_media(media)
            self._vlc._player.play()
            self._vlc_active = True
            self._vlc_paused = False
            self.after(100, self._vlc_scrub_seek)
        except Exception as e:
            print(f"[VLC] activate scrub error: {e}")

    def _vlc_scrub_seek(self):
        """Called ~100 ms after VLC starts: seek to current _frame_index and pause."""
        if self._playing or not self._vlc or not self._vlc_active:
            return
        try:
            ms = int(self._frame_index / max(1.0, self._fps) * 1000)
            self._vlc._player.set_time(max(0, ms))
            self._vlc._player.set_pause(True)
            self._vlc_paused = True
        except Exception as e:
            print(f"[VLC] scrub seek error: {e}")

    def _vlc_prime(self):
        """Warm up VLC by playing a blank internal source for ~150 ms."""
        if not self._vlc or not self._vlc.ok:
            return
        try:
            self.canvas.update_idletasks()
            self._vlc.prime(self.canvas.winfo_id())
            self.after(200, self._vlc_prime_stop)
        except Exception as e:
            print(f"[VLC] prime trigger error: {e}")

    def _vlc_prime_stop(self):
        try:
            if self._vlc and not self._playing:
                self._vlc._player.stop()
        except Exception:
            pass

    def _on_vlc_position(self, frame: int):
        """Called by VLC poll ~10 Hz during playback."""
        self._frame_index = frame
        try:
            self.scrub_var.set(frame)
        except Exception:
            pass
        self._update_timecode()
        if self._player_mode == "timeline" and self.combine_strip:
            active = self.combine_strip.get_active_index()
            if active is not None:
                self.combine_strip.set_playback_position(active, frame)
        # Enforce out_point boundary (VLC :stop-time can be imprecise)
        if self._clip_end_frame is not None and frame >= self._clip_end_frame:
            self.after(0, self._on_vlc_eos)

    def _on_vlc_eos(self):
        """Called when VLC reaches end of stream."""
        if not self._playing:
            return  # guard against double-fire
        in_pt = 0
        if self.combine_strip:
            _active = self.combine_strip.get_active_index()
            if _active is not None:
                _clips = self.combine_strip.get_clips()
                if 0 <= _active < len(_clips):
                    in_pt = _clips[_active].in_point
        self._frame_index = in_pt
        self._playing     = False
        # Fully stop VLC at EOS so PIL can draw the in_point frame cleanly
        if self._vlc:
            try:
                self._vlc.stop()
            except Exception:
                pass
        self._vlc_active = False
        self._vlc_paused = False
        self._update_buttons()
        # Show in_point frame via PIL
        if self.current_path and self._total_frames > 0:
            self._loader.request(self.current_path, in_pt)
        # Play-all: advance to next clip
        if self._play_all_mode and self.combine_strip:
            self._play_all_idx += 1
            clips = self.combine_strip.get_clips()
            if self._play_all_idx < len(clips):
                self.combine_strip.set_active_index(self._play_all_idx)
                self._load_strip_clip(self._play_all_idx, auto_play=True)
            else:
                self._play_all_mode = False

    # ------------------------------------------------------------------
    # Playback
    # ------------------------------------------------------------------

    def _on_space(self, event=None):
        try:
            fw = self.focus_get()
            if fw and fw.winfo_class() in ("Entry", "Text"):
                return
        except Exception:
            pass
        self._toggle_play()
        return "break"

    def _toggle_play(self):
        if not self.current_path or self._total_frames <= 0:
                return
        if self._playing:
            self._stop_playback()
        else:
            self._start_playback()
        self._update_buttons()

    def _start_playback(self):
        if not self.current_path or self._total_frames <= 0:
            return
        # In timeline mode, play all strip clips sequentially.
        # In clip mode, play only the current single file.
        if self._player_mode == "timeline" and self.combine_strip and self.combine_strip.get_clips():
            active = self.combine_strip.get_active_index()
            if active is None:
                active = 0
            self._play_all_mode = True
            self._play_all_idx  = active
        else:
            self._play_all_mode = False

        self._playing = True
        self._loader.cancel()
        edit_list = self.marker_bar.edit_list if self.marker_bar else None

        if self._vlc and self._vlc.ok:
            # ── VLC path (GPU-accelerated, audio in sync) ──────────────────
            has_cuts = bool(edit_list and edit_list.has_cuts)
            if self._vlc_active and self._vlc_paused and not has_cuts:
                # VLC is already paused at the right position — just unpause
                self._vlc_paused  = False
                self._vlc._gen   += 1
                gen               = self._vlc._gen
                self._vlc._fps    = max(1.0, self._fps)
                self._vlc._on_pos = self._on_vlc_position
                self._vlc._on_eos = self._on_vlc_eos
                try:
                    em = self._vlc._player.event_manager()
                    em.event_attach(
                        _vlc_module.EventType.MediaPlayerEndReached,
                        lambda e, g=gen: self.after(0, self._vlc._fire_eos, g),
                    )
                except Exception:
                    pass
                try:
                    self._vlc._player.set_pause(False)
                except Exception:
                    pass
                self._vlc._poll(gen)
            else:
                self.canvas.update_idletasks()
                self._vlc.attach(self.canvas.winfo_id())
                self._vlc.start(
                    path        = self.current_path,
                    start_frame = self._frame_index,
                    fps         = self._fps,
                    total       = self._total_frames,
                    edit_list   = edit_list,
                    end_frame   = self._clip_end_frame,
                    on_position = self._on_vlc_position,
                    on_eos      = self._on_vlc_eos,
                )
            self._vlc_active = True
            self._vlc_paused = False
        else:
            # ── cv2 fallback ────────────────────────────────────────────────
            cw = max(1, self.canvas.winfo_width())
            ch = max(1, self.canvas.winfo_height())
            canvas_size = (cw, ch) if cw > 20 and ch > 20 else None
            self._engine.start(self.current_path, self._frame_index,
                               self._fps, self._total_frames, edit_list,
                               end_frame=self._clip_end_frame,
                               canvas_size=canvas_size)
            if self._fps <= 0:
                return
            if edit_list and edit_list.has_cuts and self._total_frames > 0:
                segments  = edit_list.kept_segments(self._total_frames)
                snap_path = self.current_path
                snap_fps  = self._fps

                def _on_audio_ready(wav_path):
                    if not self._playing or self.current_path != snap_path:
                        return
                    if wav_path:
                        offset = _edited_audio_offset(self._frame_index, segments, snap_fps)
                        self._audio.play(offset)

                self._audio.prepare_edited(self, self.current_path, segments,
                                           self._fps, _on_audio_ready)
            else:
                self._audio.play(self._frame_index / self._fps)

    def _stop_playback(self):
        self._playing = False
        if self._vlc and self._vlc.ok:
            # Pause rather than stop — keeps VLC surface active showing current frame
            self._vlc._gen += 1
            if self._vlc._poll_id:
                try:
                    self.after_cancel(self._vlc._poll_id)
                except Exception:
                    pass
                self._vlc._poll_id = None
            if self._vlc_active:
                try:
                    self._vlc._player.set_pause(True)
                except Exception:
                    pass
                self._vlc_paused = True
            else:
                self._vlc_paused = False
        else:
            if self._engine:
                self._engine.stop()
            if self._audio:
                self._audio.stop()
        # Cancel any pending auto-play timer so a manual pause stays paused
        if self._auto_play_timer:
            try:
                self.after_cancel(self._auto_play_timer)
            except Exception:
                pass
            self._auto_play_timer = None

    def destroy(self):
        """Stop playback and release resources when the panel is destroyed."""
        if self._vlc:
            try:
                self._vlc.stop()
            except Exception:
                pass
        if self._audio:
            try:
                self._audio.stop()
            except Exception:
                pass
        super().destroy()

    # ------------------------------------------------------------------
    # Scrub
    # ------------------------------------------------------------------

    def _scrub_jump(self, x: int):
        """Compute frame from pixel x (scrub Scale removed; method kept for compat)."""
        if self._total_frames <= 0:
            return
        sc = getattr(self, '_scrub_canvas', None)
        usable = max(sc.winfo_width() if sc else 400, 2)
        x = max(0, min(x, usable - 1))
        frame = int(round(x / (usable - 1) * (self._total_frames - 1)))
        self.scrub_var.set(frame)
        self._frame_index = frame
        self._update_timecode()
        if self._vlc and self._vlc.ok and self._vlc_active and self._fps > 0:
            # VLC scrub — instant GPU seek, no PIL timer needed
            try:
                self._vlc._player.set_time(max(0, int(frame / self._fps * 1000)))
            except Exception:
                pass
            if self._scrub_after_id:
                try:
                    self.after_cancel(self._scrub_after_id)
                except Exception:
                    pass
                self._scrub_after_id = None
        else:
            # PIL fallback — 100 ms throttled
            if self._scrub_after_id:
                try:
                    self.after_cancel(self._scrub_after_id)
                except Exception:
                    pass
            self._scrub_after_id = self.after(
                100, lambda f=frame: self._scrub_load(f))

    def _scrub_redraw(self, event=None):
        """Repaint the thin scrub position track below the play controls."""
        c = getattr(self, '_scrub_canvas', None)
        if not c:
            return
        w = max(2, c.winfo_width())
        h = max(2, c.winfo_height())
        c.delete("all")
        c.create_rectangle(0, 0, w, h, fill="#0a1a2a", outline="")
        total_f = max(1, self._total_frames - 1)
        if self._total_frames > 0:
            pct = max(0.0, min(1.0, self._frame_index / total_f))
            x = int(pct * (w - 1))
            if x > 0:
                c.create_rectangle(0, 1, x, h - 1, fill="#1a5a8a", outline="")
            c.create_line(x, 0, x, h, fill="white", width=2)
        if self.marker_bar:
            # Draw pending-deletion sections as red masks (drawn before markers
            # so marker lines are visible on top)
            for start_f, end_f in self.marker_bar.get_pending_cuts():
                x1 = int(start_f / total_f * (w - 1))
                x2 = int(end_f   / total_f * (w - 1))
                c.create_rectangle(x1, 0, x2, h, fill="#cc0000", outline="", stipple="gray50")
                c.create_rectangle(x1, 0, x2, h, fill="", outline="#ff4444")
            # Draw marker lines (red) on top
            for mf in self.marker_bar.get_markers():
                mx = int(mf / total_f * (w - 1))
                c.create_line(mx, 0, mx, h, fill="#ff3333", width=1)

    def _scrub_right_click(self, event):
        """Right-click on scrub bar — toggle pending-deletion mark between markers."""
        if not self.marker_bar:
            return
        sc = self._scrub_canvas
        w = max(2, sc.winfo_width())
        total = max(1, self._total_frames - 1)
        frame = int(max(0, min(event.x, w - 1)) / (w - 1) * total)
        self.marker_bar.toggle_pending_cut_at(frame)
        self._scrub_redraw()

    def _scrub_press(self, event=None):
        if self._playing:
            if self._vlc and self._vlc.ok:
                # Pause VLC in-place — surface stays active, set_time() keeps working
                self._vlc._gen += 1
                if self._vlc._poll_id:
                    try:
                        self.after_cancel(self._vlc._poll_id)
                    except Exception:
                        pass
                    self._vlc._poll_id = None
                try:
                    self._vlc._player.set_pause(True)
                except Exception:
                    pass
                self._playing    = False
                self._vlc_paused = True
            else:
                self._stop_playback()
        elif self._vlc and self._vlc.ok and not self._vlc_active:
            # VLC not yet loaded — start it up so scrub drag can use set_time()
            self._activate_vlc_for_scrub(self._frame_index)
        self._scrub_dragging = True
        if event:
            self._scrub_jump(event.x)
        self._update_buttons()
        return "break"  # suppress Tk's default trough-click step behaviour

    def _scrub_drag(self, event=None):
        """Handle B1-Motion on the scrub bar — gives smooth live scrubbing."""
        if not self._scrub_dragging or not event:
            return "break"
        self._scrub_jump(event.x)
        return "break"

    def _scrub_release(self, event=None):
        self._scrub_dragging = False
        if self._scrub_after_id:
            try:
                self.after_cancel(self._scrub_after_id)
            except Exception:
                pass
            self._scrub_after_id = None
        if self._vlc and self._vlc.ok and self._vlc_active:
            # VLC already shows the correct frame — sync _frame_index from VLC
            ms = self._vlc.get_time_ms()
            if ms >= 0 and self._fps > 0:
                self._frame_index = int(ms / 1000.0 * self._fps)
            try:
                self.scrub_var.set(self._frame_index)
            except Exception:
                pass
            self._update_timecode()
        elif self.current_path and self._total_frames > 0:
            target = max(0, min(int(self.scrub_var.get()),
                                self._total_frames - 1))
            self._loader.request(self.current_path, target)
        self._update_buttons()

    def _on_scrub_moved(self, value=None):
        if not self._scrub_dragging:
            return
        try:
            frame = max(0, min(int(self.scrub_var.get()),
                               self._total_frames - 1))
        except Exception:
            return
        self._frame_index = frame
        self._update_timecode()
        # Throttle preview loads to ~10 fps
        if self._scrub_after_id:
            try:
                self.after_cancel(self._scrub_after_id)
            except Exception:
                pass
        self._scrub_after_id = self.after(
            100, lambda f=frame: self._scrub_load(f))

    def _scrub_load(self, frame: int):
        self._scrub_after_id = None
        if self._scrub_dragging and self.current_path:
            self._loader.request(self.current_path, frame)

    # ------------------------------------------------------------------
    # Keyboard step
    # ------------------------------------------------------------------

    def _step(self, delta: int):
        """Step delta frames when paused.  Crosses clip boundaries in timeline mode.
        Updates _frame_index immediately so held-key auto-repeat accumulates correctly."""
        try:
            fw = self.focus_get()
            if fw and fw.winfo_class() in ("Entry", "Text"):
                return
        except Exception:
            pass
        if self._playing or not self.current_path or self._total_frames <= 0:
            return
        target = self._frame_index + delta

        if self._player_mode == "timeline" and self.combine_strip:
            clips  = self.combine_strip.get_clips()
            active = self.combine_strip.get_active_index()
            if clips and active is not None:
                entry  = clips[active]
                in_pt  = entry.in_point
                out_pt = entry.out_point  # exclusive

                if target >= out_pt and active < len(clips) - 1:
                    # ── Cross forward into next clip ──────────────────────
                    nxt = clips[active + 1]
                    self.combine_strip.set_active_index(active + 1)
                    self.current_path    = nxt.path
                    self._total_frames   = nxt.total_frames
                    self._fps            = nxt.fps
                    self._clip_end_frame = nxt.out_point
                    self._frame_index    = nxt.in_point
                    self._loader.request(self.current_path, self._frame_index)
                    self.combine_strip.set_playback_position(active + 1, self._frame_index)
                    return

                if target < in_pt and active > 0:
                    # ── Cross backward into previous clip ─────────────────
                    prv = clips[active - 1]
                    self.combine_strip.set_active_index(active - 1)
                    self.current_path    = prv.path
                    self._total_frames   = prv.total_frames
                    self._fps            = prv.fps
                    self._clip_end_frame = prv.out_point
                    self._frame_index    = prv.out_point - 1
                    self._loader.request(self.current_path, self._frame_index)
                    self.combine_strip.set_playback_position(active - 1, self._frame_index)
                    return

                # ── Stay within current clip ──────────────────────────────
                target = max(in_pt, min(target, out_pt - 1))
                if target != self._frame_index:
                    self._frame_index = target
                    self._loader.request(self.current_path, target)
                return

        # Clip mode (or no strip) — clamp within the file
        target = max(0, min(target, self._total_frames - 1))
        if target != self._frame_index:
            self._frame_index = target
            self._loader.request(self.current_path, target)

    # ------------------------------------------------------------------
    # Grab frame
    # ------------------------------------------------------------------

    def _grab_frame(self):
        if not self.current_path:
            messagebox.showinfo("Grab Frame", "No video loaded.", parent=self)
            return

        # Resolve output folder — callable receives the video path so FTView
        # can return  <video_folder>/FrameGrabs  dynamically.
        if callable(self._output_folder):
            out_dir = self._output_folder(self.current_path)
        else:
            out_dir = self._output_folder

        # If still nothing (no callable and no stored folder), build it here
        # as a fallback so grabbing always works without manual setup.
        if not out_dir:
            out_dir = os.path.join(
                os.path.dirname(os.path.abspath(self.current_path)),
                "FrameGrabs",
            )

        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception:
            pass

        frame_idx = self._frame_index
        path      = self.current_path

        def _work(p=path, f=frame_idx, d=out_dir):
            try:
                import datetime
                img  = extract_movie_frame(p, f)
                stem = os.path.splitext(os.path.basename(p))[0]

                # Prepend creation date (file mtime) as yyyy-mm-dd_
                try:
                    mtime = os.path.getmtime(p)
                    date_prefix = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d_")
                except Exception:
                    date_prefix = ""

                fname = f"{date_prefix}{stem}_frame{f:06d}.jpg"
                out   = os.path.join(d, fname)
                n = 1
                while os.path.exists(out):
                    fname = f"{date_prefix}{stem}_frame{f:06d}_{n}.jpg"
                    out   = os.path.join(d, fname)
                    n += 1
                img.save(out, "JPEG", quality=95, subsampling=0)

                # ── Embed EXIF: date/time + GPS from source video ──────
                try:
                    import piexif, datetime

                    exif_dict = {"0th": {}, "Exif": {}, "GPS": {}}

                    # Date/time: prefer video file's modification time
                    try:
                        mtime = os.path.getmtime(p)
                        dt = datetime.datetime.fromtimestamp(mtime)
                        dt_str = dt.strftime("%Y:%m:%d %H:%M:%S").encode()
                        exif_dict["0th"][piexif.ImageIFD.DateTime]         = dt_str
                        exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = dt_str
                        exif_dict["Exif"][piexif.ExifIFD.DateTimeDigitized] = dt_str
                    except Exception:
                        pass

                    # GPS: read from video container tags via ffprobe
                    try:
                        import json as _json, shutil as _shutil, subprocess as _sub
                        ffprobe = _shutil.which("ffprobe")
                        if ffprobe:
                            r = _sub.run(
                                [ffprobe, "-v", "quiet", "-print_format", "json",
                                 "-show_format", p],
                                stdout=_sub.PIPE, stderr=_sub.PIPE, timeout=10,
                            )
                            fmt = _json.loads(r.stdout).get("format", {})
                            tags = fmt.get("tags", {})
                            # Common GPS tag names written by cameras/phones
                            loc = (tags.get("location") or
                                   tags.get("com.apple.quicktime.location.ISO6709") or
                                   tags.get("location-eng") or "")
                            # ISO 6709 format: ±DD.DDDD±DDD.DDDD/  or  ±DDMM.MM±DDDMM.MM/
                            if loc:
                                import re as _re
                                m = _re.match(
                                    r"([+-]\d+\.?\d*)([+-]\d+\.?\d*)([+-]\d+\.?\d*)?",
                                    loc.strip().rstrip("/"),
                                )
                                if m:
                                    lat = float(m.group(1))
                                    lon = float(m.group(2))
                                    alt = float(m.group(3)) if m.group(3) else None

                                    def _to_dms(deg):
                                        deg = abs(deg)
                                        d = int(deg)
                                        mn = int((deg - d) * 60)
                                        sc = round(((deg - d) * 60 - mn) * 60 * 100)
                                        return ((d, 1), (mn, 1), (sc, 100))

                                    exif_dict["GPS"][piexif.GPSIFD.GPSLatitudeRef]  = (
                                        b"N" if lat >= 0 else b"S")
                                    exif_dict["GPS"][piexif.GPSIFD.GPSLatitude]     = (
                                        _to_dms(lat))
                                    exif_dict["GPS"][piexif.GPSIFD.GPSLongitudeRef] = (
                                        b"E" if lon >= 0 else b"W")
                                    exif_dict["GPS"][piexif.GPSIFD.GPSLongitude]    = (
                                        _to_dms(lon))
                                    if alt is not None:
                                        exif_dict["GPS"][piexif.GPSIFD.GPSAltitudeRef] = (
                                            0 if alt >= 0 else 1)
                                        exif_dict["GPS"][piexif.GPSIFD.GPSAltitude] = (
                                            (abs(int(alt * 100)), 100))
                    except Exception:
                        pass

                    exif_bytes = piexif.dump(exif_dict)
                    piexif.insert(exif_bytes, out)
                except ImportError:
                    pass   # piexif not installed — skip EXIF silently
                except Exception:
                    pass   # EXIF embedding failed — JPEG already saved, keep it
                # ──────────────────────────────────────────────────────

                def _notify(name=fname, dest=out):
                    messagebox.showinfo(
                        "Frame Saved",
                        f"Frame grab saved:\n\n{name}\n\nIn folder:\n{os.path.dirname(dest)}",
                        parent=self,
                    )
                    self.lbl_info.configure(text=f"Saved  {name}")
                    self.after(3000, self._restore_info)
                self.after(0, _notify)
            except Exception as e:
                self.after(0, lambda err=str(e): messagebox.showerror(
                    "Grab Frame", f"Could not save:\n{err}", parent=self))

        threading.Thread(target=_work, daemon=True).start()

    def _restore_info(self):
        if not self.current_path:
            return
        dur  = _fmt_duration(self._duration_s)
        fps_s = f"  {self._fps:.2f} fps" if self._fps > 0 else ""
        self.lbl_info.configure(
            text=f"{os.path.basename(self.current_path)}   {dur}{fps_s}")

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def prev_file(self):
        if self.index is None or self.index <= 0:
            return
        self._stop_playback()
        new = self.index - 1
        if self._on_select_index:
            self._on_select_index(new)
        else:
            self.show_index(new)

    def next_file(self):
        if self.index is None or self.index >= len(self.files) - 1:
            return
        self._stop_playback()
        new = self.index + 1
        if self._on_select_index:
            self._on_select_index(new)
        else:
            self.show_index(new)

    # ------------------------------------------------------------------
    # Output folder
    # ------------------------------------------------------------------

    def _choose_output_folder(self):
        if callable(self._output_folder):
            initial = self._output_folder(self.current_path) if self.current_path else os.path.expanduser("~")
        else:
            initial = self._output_folder or os.path.expanduser("~")
        folder = filedialog.askdirectory(
            parent=self, title="Choose output folder",
            initialdir=initial)
        if folder:
            self._output_folder = folder
            self._update_out_label()

    def _update_out_label(self):
        if callable(self._output_folder):
            # Resolve using current video path if available
            d = self._output_folder(self.current_path) if self.current_path else None
            if d:
                if len(d) > 55:
                    d = "…" + d[-52:]
                self.lbl_out.configure(text=f"Output: {d}")
            else:
                self.lbl_out.configure(text="Output: FrameGrabs/ (next to video)")
        elif self._output_folder:
            d = self._output_folder
            if len(d) > 55:
                d = "…" + d[-52:]
            self.lbl_out.configure(text=f"Output: {d}")
        else:
            self.lbl_out.configure(text="Output: (none set)")

    # ------------------------------------------------------------------
    # External player
    # ------------------------------------------------------------------

    def _open_external(self):
        """Open current_path in the OS default video player."""
        if not self.current_path or not os.path.isfile(self.current_path):
            messagebox.showinfo("Open in player",
                                "No video file is loaded.", parent=self)
            return
        self._stop_playback()
        try:
            os.startfile(self.current_path)
        except Exception as e:
            messagebox.showerror("Open in player",
                                 f"Could not open file:\n{e}", parent=self)

    # ------------------------------------------------------------------
    # Button state
    # ------------------------------------------------------------------

    def _update_buttons(self):
        """Refresh play-button label and enable/disable controls."""
        has_video = bool(self.current_path and self._total_frames > 0)

        # Play / Pause icon
        if self._playing:
            self.btn_play.configure(text="⏸")
        else:
            self.btn_play.configure(text="▶")

        # Grab-frame only makes sense when a video is loaded
        state_grab = "normal" if has_video else "disabled"
        self.btn_grab.configure(state=state_grab)

        # Prev / Next navigation
        can_prev = (self.index is not None and self.index > 0)
        can_next = (self.index is not None and self.index < len(self.files) - 1)
        self.btn_prev.configure(state="normal" if can_prev else "disabled")
        self.btn_next.configure(state="normal" if can_next else "disabled")

    # ------------------------------------------------------------------
    # Scrub range + timecode
    # ------------------------------------------------------------------

    def _update_scrub_range(self):
        """Clamp scrub_var to valid frame range (Scale widget removed; var kept as stub)."""
        total = max(1, self._total_frames)
        clamped = max(0, min(self._frame_index, total - 1))
        try:
            self.scrub_var.set(clamped)
        except Exception:
            pass

    def _update_timecode(self):
        """Update lbl_timecode with current frame position."""
        if self._total_frames <= 0 or self._fps <= 0:
            try:
                self.lbl_timecode.configure(text="00:00:00:00")
                self.lbl_duration.configure(text="/ 0:00")
            except Exception:
                pass
            return
        try:
            tc = _fmt_timecode(self._frame_index, self._fps)
            self.lbl_timecode.configure(text=tc)
        except Exception:
            pass
        self._scrub_redraw()
        if self.marker_bar:
            self.marker_bar.update_cursor()
