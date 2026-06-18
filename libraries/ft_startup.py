"""
ft_startup.py — startup/build helpers for FileTagger.

Extracted from FT.py as a low-risk refactor step.
Owns:
- build id / build timestamp display helpers
- startup splash window
- library presence check and warning dialog

This module deliberately does not know about FileTagger application state.
FT.py sets the build source file with set_build_source(__file__) so build
timestamps remain based on FT.py, not this helper file.
"""

from __future__ import annotations

import os
import sys
import time
import importlib.util
import tkinter as tk

_BUILD_SOURCE_FILE = __file__


def set_build_source(path: str) -> None:
    """Set the file whose modified time drives the displayed build timestamp."""
    global _BUILD_SOURCE_FILE
    if path:
        _BUILD_SOURCE_FILE = os.path.abspath(path)


def _build_id_file() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(_BUILD_SOURCE_FILE)), ".ft_build_id")


def get_build_id() -> int:
    """Persistent build id. Increments when the build source file timestamp changes."""
    try:
        path = os.path.abspath(_BUILD_SOURCE_FILE)
        mtime = int(os.path.getmtime(path))
        last_mtime = None
        build_id = 0
        build_id_file = _build_id_file()

        if os.path.exists(build_id_file):
            try:
                with open(build_id_file, "r", encoding="utf-8") as f:
                    parts = f.read().strip().split(",")
                if len(parts) == 2:
                    last_mtime = int(parts[0])
                    build_id = int(parts[1])
            except Exception:
                pass

        if last_mtime != mtime:
            build_id += 1
            try:
                with open(build_id_file, "w", encoding="utf-8") as f:
                    f.write(f"{mtime},{build_id}")
            except Exception:
                pass

        return build_id
    except Exception:
        return 0


def get_build_timestamp() -> str:
    """Return build source file's last-modified local time."""
    try:
        path = os.path.abspath(_BUILD_SOURCE_FILE)
        ts = os.path.getmtime(path)
        dt = time.localtime(ts)
        tz_index = dt.tm_isdst if dt.tm_isdst in (0, 1) else 0
        tz = time.tzname[tz_index]
        return time.strftime("%d %b %Y  %H:%M", dt) + (f" {tz}" if tz else "")
    except Exception:
        return "Unknown build time"


def get_build_string() -> str:
    return f"Build {get_build_id()}  {get_build_timestamp()}"


def show_startup_splash(root):
    """Splash shown before the main window appears — light theme only."""
    build = get_build_string()

    bg = "#e8e8e8"
    bd = "#888888"
    fg = "#111111"
    div = "#aaaaaa"

    splash = tk.Toplevel(root)
    splash.overrideredirect(True)
    sw = root.winfo_screenwidth(); sh = root.winfo_screenheight()
    w, h = 680, 280
    splash.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")
    splash.configure(bg=bg)
    border = tk.Frame(splash, bg=bd, padx=2, pady=2)
    border.pack(fill="both", expand=True)
    inner = tk.Frame(border, bg=bg)
    inner.pack(fill="both", expand=True)

    tk.Label(inner, text="FileTagger with SQLite",
             bg=bg, fg=fg,
             font=("Segoe UI", 36, "bold")).pack(pady=(24, 0))
    tk.Label(inner, text="📷  Photos    +    📄  PDFs    —    Tag  ·  Browse  ·  Export  ·  SQLite",
             bg=bg, fg=fg,
             font=("Segoe UI", 11)).pack(pady=(6, 0))
    tk.Frame(inner, bg=div, height=1).pack(fill="x", padx=40, pady=12)
    tk.Label(inner, text=f"Build  {build}",
             bg=bg, fg=fg,
             font=("Segoe UI", 11)).pack()
    tk.Label(inner, text="Loading...",
             bg=bg, fg=fg,
             font=("Segoe UI", 11, "italic")).pack(pady=(8, 0))
    splash.update()
    return splash


def check_libraries():
    """Check required and optional FileTagger libraries."""
    required = [
        ("PIL",   "Pillow",        "pip install Pillow",        "Image loading and thumbnailing"),
        ("numpy", "numpy",         "pip install numpy",         "Similarity scan, FFT filter, transforms"),
    ]
    optional = [
        ("fitz",           "PyMuPDF",       "pip install pymupdf",       "PDF thumbnail rendering"),
        ("fpdf",           "fpdf2",         "pip install fpdf2",         "Contact sheet generation"),
        ("tkintermapview", "tkintermapview","pip install tkintermapview","GPS map window"),
    ]
    missing_req = []
    missing_opt = []
    for mod, pkg, cmd, purpose in required:
        if importlib.util.find_spec(mod) is None:
            missing_req.append((pkg, cmd, purpose))
    for mod, pkg, cmd, purpose in optional:
        if importlib.util.find_spec(mod) is None:
            missing_opt.append((pkg, cmd, purpose))
    return len(missing_req) == 0, missing_req, missing_opt


def show_library_warning(root, missing_req, missing_opt):
    """Show a dialog listing missing libraries. Blocks if required libs missing."""
    dlg = tk.Toplevel(root)
    dlg.title("FileTagger — Library Check")
    dlg.configure(bg="#f0f4f8")
    dlg.resizable(False, False)
    dlg.transient(root)
    dlg.grab_set()

    dlg.update_idletasks()
    w, h = 560, 420
    sw = dlg.winfo_screenwidth(); sh = dlg.winfo_screenheight()
    dlg.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")

    if missing_req:
        hdr_col = "#cc2222"; hdr_text = "⛔  Missing Required Libraries"
        sub_text = "FileTagger cannot start until these are installed."
    else:
        hdr_col = "#cc7700"; hdr_text = "⚠  Missing Optional Libraries"
        sub_text = "Some features will be unavailable. FileTagger will start normally."

    tk.Label(dlg, text=hdr_text, bg="#f0f4f8", fg=hdr_col,
             font=("Segoe UI", 13, "bold")).pack(pady=(20, 4))
    tk.Label(dlg, text=sub_text, bg="#f0f4f8", fg="#444444",
             font=("Segoe UI", 9)).pack(pady=(0, 12))

    frame = tk.Frame(dlg, bg="#f0f4f8"); frame.pack(fill="x", padx=24)

    def section(title, items, title_col):
        if not items:
            return
        tk.Label(frame, text=title, bg="#f0f4f8", fg=title_col,
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(fill="x", pady=(8,2))
        for pkg, cmd, purpose in items:
            row = tk.Frame(frame, bg="#e8eef4", highlightbackground="#ccd8e8",
                           highlightthickness=1)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=f"  {pkg}", bg="#e8eef4", fg="#1a3a6a",
                     font=("Segoe UI", 9, "bold"), width=16, anchor="w").pack(side="left", padx=(4,0), pady=4)
            tk.Label(row, text=purpose, bg="#e8eef4", fg="#444444",
                     font=("Segoe UI", 8), anchor="w").pack(side="left", padx=8)
            cmd_fr = tk.Frame(row, bg="#1a2a4a", padx=6, pady=2)
            cmd_fr.pack(side="right", padx=6, pady=4)
            tk.Label(cmd_fr, text=cmd, bg="#1a2a4a", fg="#88ddff",
                     font=("Courier New", 8)).pack()

    section("Required — must install to run:", missing_req, "#cc2222")
    section("Optional — install to enable features:", missing_opt, "#cc7700")

    tk.Frame(dlg, bg="#cccccc", height=1).pack(fill="x", padx=20, pady=(16, 0))

    bf = tk.Frame(dlg, bg="#f0f4f8"); bf.pack(pady=12)
    can_continue = not missing_req

    if can_continue:
        tk.Button(bf, text="  Continue  ", bg="#226633", fg="white",
                  font=("Segoe UI", 10, "bold"), relief="flat", padx=12, pady=4,
                  cursor="hand2", command=dlg.destroy).pack(side="left", padx=6)
    tk.Button(bf, text="  Exit  ", bg="#662222", fg="white",
              font=("Segoe UI", 10, "bold"), relief="flat", padx=12, pady=4,
              cursor="hand2", command=lambda: (root.destroy(), sys.exit(1))).pack(side="left", padx=6)

    if not can_continue:
        dlg.protocol("WM_DELETE_WINDOW", lambda: (root.destroy(), sys.exit(1)))
        root.wait_window(dlg)
    else:
        dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)
