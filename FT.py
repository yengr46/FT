"""
FileTagger.py  —  v1.0
-
Version: 16:57 26-Apr-2026----------------------
Combined photo and PDF tagging utility.
Toggle between Photos and PDFs with the 📷/📄 button.

Architecture:
  Photos root / PDFs root  — originals, NEVER modified
  <script_dir>/Database/FileTagger.db  — SQLite: thumbnails, collections, cull list, settings
  <script_dir>/ContactSheets/          — generated contact sheet PDFs
  <script_dir>/Reports/                — CSV exports and other reports
  Root/_tags_*.txt         — named tag collections per mode

Required libraries (install with pip):
  pip install Pillow          # image loading, thumbnailing, transforms
  pip install numpy           # pHash similarity, FFT filter, perspective transform
  pip install pymupdf         # PDF thumbnail rendering (optional — Photos mode works without it)
  pip install fpdf2           # contact sheet PDF generation
  pip install tkintermapview  # GPS map window (optional — used only when images have GPS EXIF)

Usage:  python FileTagger.py
Config: FileTagger.ini alongside script
"""

# ── DPI awareness — must be set before tkinter initialises ─────────────────────
import ctypes
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

import os, sys, threading, configparser, math

# ── GPS / EXIF helpers (extracted) ─────────────────────────────────────────────
from ft_gps import _get_gps_coords, _scan_folder_for_gps

from tkinter import simpledialog
import tkinter as tk
from tkinter import ttk, messagebox
from FTWidgets import TREE_LEFT_W, FileCountTree, FolderTreeWidget, TREE_COL_W, TREE_WIDTH, TREE_SCROLL_W, TREE_PAD_R

# ── Tooltip (extracted) ───────────────────────────────────────────────────────
from ft_tooltip import _Tooltip, _tip

try:
    from PIL import Image, ImageTk, ImageFont, ImageDraw, ImageFile
except ImportError:
    print("ERROR: Pillow required.  pip install Pillow"); sys.exit(1)

try:
    import fitz
    HAVE_FITZ = True
except ImportError:
    HAVE_FITZ = False

try:
    import numpy
    HAVE_NUMPY = True
except ImportError:
    HAVE_NUMPY = False

try:
    import fpdf
    HAVE_FPDF = True
except ImportError:
    HAVE_FPDF = False

try:
    import tkintermapview
    HAVE_MAPVIEW = True
except ImportError:
    HAVE_MAPVIEW = False

# ── Default config ─────────────────────────────────────────────────────────────
PHOTOS_ROOT  = ""
PDFS_ROOT    = ""
# Multiple roots — list of (path, name) tuples. Populated by _load_config.
PHOTOS_ROOTS: list = []
PDFS_ROOTS:   list = []
THUMB_SIZE   = 250          # default display size
THUMB_PAD    = 6
THUMB_IMG_H  = 170        # image box height at THUMB_SIZE=250 → 190px at sz=280
THUMB_STORE_SIZE = 250    # fixed storage size in DB — independent of display size
DEFAULT_COLS = 10           # default column count — override in ini [display] cols=N
GRID_TOP_PAD = 0  # grid_frame starts at y=0 in canvas

BLOB_IN_MEM_LIMIT = 8_000_000  # cache blobs up to 8MB in memory
COLL_PREFIX      = "_tags_"
FT_SYSTEM_DIR    = "_FileTagger"           # top-level system folder under each root
COLL_SUBDIR      = "_Collections"          # inside FT_SYSTEM_DIR
CULL_FILENAME    = "_for_deletion.txt"
EDIT_FILENAME    = "_edited_.txt"
DELETED_SUBDIR   = "_Deleted_Files"        # inside FT_SYSTEM_DIR
SHEETS_SUBDIR    = "_Collection_PDFs"      # inside FT_SYSTEM_DIR

def _ft_dir(root):
    """Return <root>/_FileTagger/ — the top-level FileTagger system folder."""
    d = os.path.join(root, FT_SYSTEM_DIR)
    os.makedirs(d, exist_ok=True)
    return d

def _collections_dir(root):
    d = os.path.join(_ft_dir(root), COLL_SUBDIR)
    os.makedirs(d, exist_ok=True)
    return d

def _deleted_dir(root):
    d = os.path.join(_ft_dir(root), DELETED_SUBDIR)
    os.makedirs(d, exist_ok=True)
    return d

def _sheets_dir(root=None):
    """Central contact sheets folder — uses active project if available."""
    if _ACTIVE_PROJECT.get('path'):
        return _project_sheets_dir(_ACTIVE_PROJECT['path'])
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ContactSheets')
    os.makedirs(d, exist_ok=True)
    return d

def _edit_path(root):
    return os.path.join(_collections_dir(root), EDIT_FILENAME)

def _read_edited(folder):
    """Return set of normalised paths marked as edited in this folder."""
    result = set()
    p = _edit_path(folder)
    if not os.path.exists(p): return result
    try:
        with open(p, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    result.add(os.path.normpath(line))
    except: pass
    return result

def _write_edited(folder, edited_set):
    """Persist the edited set for this folder."""
    p = _edit_path(folder)
    try:
        with open(p, 'w', encoding='utf-8') as f:
            f.write("# FileTagger edited files\n")
            for path in sorted(edited_set):
                f.write(path + "\n")
    except: pass

PHOTO_EXTS = {'.jpg', '.jpeg'}
PDF_EXTS   = {'.pdf'}

# ── Theme ──────────────────────────────────────────────────────────────────────
THEME = "light"   # overridden by ini [display] theme = dark

# ── Fixed light-theme colour constants ───────────────────────────────────────
THEME       = "light"
BG          = "#dddddd";  BG2 = "#dddddd"; BG3 = "#dddddd"
TAGGED_BG   = "#dddddd";  TAGGED_BD   = "#e03030"
UNTAGGED_BG = "#dddddd";  UNTAGGED_BD = "#bbbbbb"
CULLED_BG   = "#dddddd";  CULLED_BD   = "#cc7700"
HOVER_BD    = "#888888";  TEXT_DIM = "#444444"; TEXT_BRIGHT = "#111111"
TREE_BG     = "#dddddd";  TREE_FG  = "#111111"; TREE_SEL_BG = "#a0c0e0"

def _apply_theme(theme):
    pass   # retained for compatibility — light theme only

SELECT_BD   = "#ffdd00"
ACCENT      = "#4f8ef7";  GREEN = "#27ae60"; AMBER = "#f5a623"

DISP_SIZES  = [150, 200, 250, 300, 350, 400]

# Processing config
_HERE = os.path.dirname(os.path.abspath(__file__)) if '__file__' in dir() else os.getcwd()
PROC_TEMP_FOLDER    = os.path.join(_HERE, "FileTaggerData")
# FT_FileOps.msf lives in FT_IPC\ (part of FT suite)
# StringHelpers.msf and other general macros live in MGEN\SystemMacros\
# derived automatically from mgen_exe location — not user-configurable
PROC_MGEN_EXE_DEFAULT = os.path.join(os.path.dirname(_HERE), "MGEN", "mgen.py")
PROC_MGEN_EXE       = PROC_MGEN_EXE_DEFAULT
# StringHelpers.msf = <mgen_exe_dir>\SystemMacros\StringHelpers.msf  (auto-derived from ini mgen_exe)

def _parse_roots(ini_path, section):
    """Read all 'root = path : name' lines from a section, handling duplicates.
    Returns list of (path, name) tuples. Path may contain spaces and colons
    (e.g. drive letters), so we split on the LAST colon that is followed by
    non-path text (i.e. the separator between path and display name).
    Format:  root = C:/Some Path With Spaces : Display Name
    """
    roots = []
    if not os.path.exists(ini_path):
        return roots
    in_section = False
    with open(ini_path, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            stripped = line.strip()
            if stripped.startswith('['):
                in_section = (stripped.lower() == f'[{section}]')
                continue
            if not in_section: continue
            if stripped.startswith('#') or stripped.startswith(';'): continue
            if '=' not in stripped: continue
            key, _, rest = stripped.partition('=')
            if key.strip().lower() != 'root': continue
            rest = rest.strip()
            # Split path:name — find the colon that isn't part of a Windows drive letter
            # Strategy: last colon in rest that has text on both sides and isn't at pos 1
            # (drive letter colons are always at index 1, e.g. C:)
            name = ""
            path = rest
            # Find last colon at position > 1
            idx = rest.rfind(':')
            while idx > 1:
                candidate_path = rest[:idx].strip()
                candidate_name = rest[idx+1:].strip()
                # Valid if name is non-empty and path looks like a path
                if candidate_name and (os.sep in candidate_path or
                                       candidate_path.endswith(':') or
                                       len(candidate_path) > 2):
                    path = candidate_path
                    name = candidate_name[:30]  # max 30 chars
                    break
                idx = rest.rfind(':', 0, idx)
            if not name:
                # No name separator found — use last folder component as name
                name = os.path.basename(path.rstrip('/\\')) or path
            roots.append((path, name))
    return roots

def _parse_roots_from_text(text):
    """Parse a single root line: 'path : name' → [(path, name)]."""
    text = text.strip()
    if not text: return []
    idx = len(text) - 1
    while idx > 2:
        if text[idx] == ':' and idx > 2 and not text[idx-1] == ':':
            candidate_path = text[:idx].strip()
            candidate_name = text[idx+1:].strip()
            if candidate_path and candidate_name and len(candidate_path) > 2:
                return [(candidate_path, candidate_name)]
        idx -= 1
    return [(text, os.path.basename(text.rstrip('/\\')) or text)]
_INI_PATH: str = ""   # set by _load_config; used by db_open() to write back

# ── Project system ─────────────────────────────────────────────────────────────

_ACTIVE_PROJECT: dict = {}   # {name, path, photos_roots, pdfs_roots}

def _projects_ini_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "Projects.ini")

def _load_ft_categories():
    """Load FTCategories.json from script directory. Returns dict or empty structure."""
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "FTCategories.json")
    empty = {"who": [], "categories": {}}
    if not os.path.exists(p):
        return empty
    try:
        import json as _json
        with open(p, 'r', encoding='utf-8') as f:
            return _json.load(f)
    except Exception as e:
        print(f"_load_ft_categories error: {e}")
        return empty

def _load_projects():
    """Parse Projects.ini. Returns dict: {name -> {path, photos_roots, pdfs_roots}}."""
    projects = {}
    p = _projects_ini_path()
    if not os.path.exists(p):
        return projects
    cfg = configparser.ConfigParser(strict=False)
    cfg.read(p, encoding='utf-8')
    for section in cfg.sections():
        proj_path = cfg.get(section, 'path', fallback='').strip()
        if not proj_path:
            continue
        photos = _parse_roots(p, section) if False else []  # parse inline below
        pdfs   = []
        # Parse multi-value photos/pdfs lines manually
        try:
            import io as _io2
            with open(p, 'r', encoding='utf-8') as _f:
                lines = _f.readlines()
            in_section = False
            for line in lines:
                ls = line.strip()
                if ls.lower() == f'[{section.lower()}]':
                    in_section = True; continue
                if ls.startswith('[') and in_section:
                    break
                if not in_section or not ls or ls.startswith('#'):
                    continue
                if '=' in ls:
                    key, _, val = ls.partition('=')
                    key = key.strip().lower(); val = val.strip()
                    if key == 'photos' and val:
                        parsed = _parse_roots_from_text(val)
                        photos.extend(parsed)
                    elif key == 'pdfs' and val:
                        parsed = _parse_roots_from_text(val)
                        pdfs.extend(parsed)
        except Exception as _e:
            print(f"_load_projects parse error: {_e}")
        projects[section] = {
            'path':         proj_path,
            'photos_roots': photos,
            'pdfs_roots':   pdfs,
        }
    return projects

def _save_projects(projects):
    """Write projects dict to Projects.ini."""
    p = _projects_ini_path()
    lines = []
    for name, proj in projects.items():
        lines.append(f"[{name}]")
        lines.append(f"path    = {proj['path']}")
        for path, label in proj.get('photos_roots', []):
            lines.append(f"photos  = {path} : {label}")
        for path, label in proj.get('pdfs_roots', []):
            lines.append(f"pdfs    = {path} : {label}")
        lines.append("")
    try:
        with open(p, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
    except Exception as e:
        print(f"_save_projects error: {e}")

def _project_db_path(proj_path):
    return os.path.join(proj_path, 'FileTagger.db')

def _project_sheets_dir(proj_path):
    d = os.path.join(proj_path, 'ContactSheets')
    os.makedirs(d, exist_ok=True)
    return d

def _project_reports_dir(proj_path):
    d = os.path.join(proj_path, 'Reports')
    os.makedirs(d, exist_ok=True)
    return d

def _activate_project(name, proj):
    """Set _ACTIVE_PROJECT and update globals for roots and DB path."""
    global _ACTIVE_PROJECT, _DB_PATH, PHOTOS_ROOT, PDFS_ROOT, PHOTOS_ROOTS, PDFS_ROOTS
    _ACTIVE_PROJECT = dict(proj, name=name)
    proj_path = proj['path']
    os.makedirs(proj_path, exist_ok=True)
    _project_sheets_dir(proj_path)
    _project_reports_dir(proj_path)
    _DB_PATH = _project_db_path(proj_path)
    PHOTOS_ROOTS = proj.get('photos_roots', [])
    PDFS_ROOTS   = proj.get('pdfs_roots', [])
    PHOTOS_ROOT  = PHOTOS_ROOTS[0][0] if PHOTOS_ROOTS else ""
    PDFS_ROOT    = PDFS_ROOTS[0][0]   if PDFS_ROOTS   else ""

def _discover_ftproj_folders():
    """Scan drives and common locations for FTProj_ folders."""
    found = []
    # Script dir and its parent
    script_dir = os.path.dirname(os.path.abspath(__file__))
    search_roots = [script_dir, os.path.dirname(script_dir)]
    # All drive letters on Windows
    if os.name == 'nt':
        import string
        for letter in string.ascii_uppercase:
            d = f"{letter}:\\"
            if os.path.exists(d):
                search_roots.append(d)
    for search_root in search_roots:
        try:
            for entry in os.scandir(search_root):
                if entry.is_dir() and entry.name.startswith('FTProj_'):
                    db = os.path.join(entry.path, 'FileTagger.db')
                    found.append({
                        'name': entry.name[7:],   # strip FTProj_ prefix
                        'path': entry.path,
                        'has_db': os.path.exists(db),
                    })
        except PermissionError:
            pass
    return found

def _create_project(name, proj_path, photos_roots=None, pdfs_roots=None):
    """Create a new FTProj_ folder structure and register in Projects.ini."""
    folder_name = f"FTProj_{name}"
    full_path = os.path.join(proj_path, folder_name)
    os.makedirs(full_path, exist_ok=True)
    os.makedirs(os.path.join(full_path, 'ContactSheets'), exist_ok=True)
    os.makedirs(os.path.join(full_path, 'Reports'), exist_ok=True)
    proj = {
        'path':         full_path,
        'photos_roots': photos_roots or [],
        'pdfs_roots':   pdfs_roots or [],
    }
    projects = _load_projects()
    projects[name] = proj
    _save_projects(projects)
    return proj

def _get_active_project_name():
    """Return active project name from FileTagger.ini."""
    ini = os.path.join(os.path.dirname(os.path.abspath(__file__)), "FileTagger.ini")
    if not os.path.exists(ini): return ""
    cfg = configparser.ConfigParser(strict=False)
    cfg.read(ini)
    return cfg.get("FileTagger", "active_project", fallback="")

def _set_active_project_name(name):
    """Write active_project to FileTagger.ini."""
    ini = os.path.join(os.path.dirname(os.path.abspath(__file__)), "FileTagger.ini")
    cfg = configparser.ConfigParser(strict=False)
    if os.path.exists(ini):
        cfg.read(ini)
    if not cfg.has_section("FileTagger"):
        cfg.add_section("FileTagger")
    cfg.set("FileTagger", "active_project", name)
    try:
        with open(ini, 'w', encoding='utf-8') as f:
            cfg.write(f)
    except Exception as e:
        print(f"_set_active_project_name error: {e}")

def _load_config():
    global PHOTOS_ROOT, PDFS_ROOT, THUMB_SIZE, PHOTOS_ROOTS, PDFS_ROOTS
    global PROC_TEMP_FOLDER, _DB_PATH, _INI_PATH
    ini = os.path.join(os.path.dirname(os.path.abspath(__file__)), "FileTagger.ini")
    _INI_PATH = ini
    print(f"Loading ini: {ini}  (exists: {os.path.exists(ini)})")
    if not os.path.exists(ini): return
    cfg = configparser.ConfigParser(strict=False)
    cfg.read(ini)
    THUMB_SIZE           = cfg.getint("display", "thumb_size",     fallback=THUMB_SIZE)
    theme                = cfg.get("display",    "theme",          fallback="light").strip().lower()
    _apply_theme(theme if theme in ("dark", "light") else "dark")
    if cfg.has_option("display", "cols"):
        cfg.remove_option("display", "cols")
        try:
            with open(ini, "w") as f: cfg.write(f)
        except: pass
    PROC_TEMP_FOLDER     = cfg.get("processing", "temp_folder",    fallback=PROC_TEMP_FOLDER)
    global PROC_MGEN_EXE
    PROC_MGEN_EXE        = cfg.get("processing", "mgen_exe",       fallback=PROC_MGEN_EXE)

    # Load active project from Projects.ini if available
    active_name = cfg.get("FileTagger", "active_project", fallback="")
    if active_name:
        projects = _load_projects()
        if active_name in projects:
            _activate_project(active_name, projects[active_name])
            print(f"Active project: {active_name}  DB: {_DB_PATH}")
            print(f"Photos roots: {PHOTOS_ROOTS}")
            print(f"PDFs roots:   {PDFS_ROOTS}")
            return

    # Fallback: load roots from FileTagger.ini directly (legacy / no project system)
    PHOTOS_ROOTS = _parse_roots(ini, "photos")
    PDFS_ROOTS   = _parse_roots(ini, "pdfs")
    if PHOTOS_ROOTS:
        PHOTOS_ROOT = PHOTOS_ROOTS[0][0]
    else:
        PHOTOS_ROOT = cfg.get("photos", "root", fallback=PHOTOS_ROOT)
        PHOTOS_ROOTS = [(PHOTOS_ROOT, os.path.basename(PHOTOS_ROOT.rstrip('/\\')) or "Photos")]
    if PDFS_ROOTS:
        PDFS_ROOT = PDFS_ROOTS[0][0]
    else:
        PDFS_ROOT = cfg.get("pdfs", "root", fallback=PDFS_ROOT)
        PDFS_ROOTS = [(PDFS_ROOT, os.path.basename(PDFS_ROOT.rstrip('/\\')) or "PDFs")]
    print(f"Photos roots: {PHOTOS_ROOTS}")
    print(f"PDFs roots:   {PDFS_ROOTS}")
    _DB_PATH = cfg.get("FileTagger", "database", fallback="")

_load_config()

def _path_accessible(p):
    """Check if path is accessible without hanging on offline network drives."""
    try:
        if os.name == 'nt':
            drive = os.path.splitdrive(p)[0]
            if drive and not os.path.exists(drive + '\\'):
                return False
        return os.path.isdir(p)
    except:
        return False

# ── Long path (Windows) ────────────────────────────────────────────────────────
def _longpath(p):
    if os.name == 'nt':
        p = p.replace('/', '\\')
        if not p.startswith('\\\\?\\'):
            return '\\\\?\\' + os.path.abspath(p)
    return p

# ── Mode configuration ─────────────────────────────────────────────────────────
def _mode_cfg(mode):
    if mode == "photos":
        return dict(root=PHOTOS_ROOT,
                    exts=PHOTO_EXTS, label="Photos", icon="📷",
                    file_word="image", col_head="JPGs")
    else:
        return dict(root=PDFS_ROOT,
                    exts=PDF_EXTS, label="PDFs", icon="📄",
                    file_word="PDF", col_head="PDFs")

_FileTagger_instance = None  # set in __init__

# ── SQLite thumbnail cache ─────────────────────────────────────────────────────
import sqlite3 as _sqlite3, io as _io

_db_conn: '_sqlite3.Connection | None' = None   # single session connection

_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS thumbnails (
    path      TEXT PRIMARY KEY,
    jpeg      BLOB NOT NULL,
    mtime     REAL,
    file_size INTEGER,
    width     INTEGER,
    height    INTEGER
);
CREATE TABLE IF NOT EXISTS collections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    UNIQUE NOT NULL,
    created_at  TEXT,
    modified_at TEXT
);
CREATE TABLE IF NOT EXISTS collection_items (
    collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    path          TEXT    NOT NULL,
    added_at      TEXT,
    sort_order    INTEGER,
    PRIMARY KEY (collection_id, path)
);
CREATE TABLE IF NOT EXISTS cull_list (
    path      TEXT PRIMARY KEY,
    marked_at TEXT
);
CREATE TABLE IF NOT EXISTS folder_bookmarks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT,
    path         TEXT UNIQUE NOT NULL,
    mode         TEXT,
    sort_order   INTEGER,
    last_visited TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

def _db_default_path():
    """Return default DB path: <script_dir>\\Database\\FileTagger.db"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    folder = os.path.join(script_dir, 'Database')
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, 'FileTagger.db')

def _script_dir():
    """Return the directory containing this script."""
    return os.path.dirname(os.path.abspath(__file__))

def _contact_sheets_dir():
    """Central contact sheets folder under script directory."""
    d = os.path.join(_script_dir(), 'ContactSheets')
    os.makedirs(d, exist_ok=True)
    return d

def _reports_dir():
    """Central reports folder — uses active project if available."""
    if _ACTIVE_PROJECT.get('path'):
        return _project_reports_dir(_ACTIVE_PROJECT['path'])
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'Reports')
    os.makedirs(d, exist_ok=True)
    return d

def db_open(path=None):
    """Open (or create) the SQLite database. Call once at startup.
    Uses ini [FileTagger] database= entry if set, else default location.
    Writes the path back to ini so it persists and shows in Settings."""
    global _db_conn, _DB_PATH
    if path is None:
        path = _DB_PATH if _DB_PATH else _db_default_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    _db_conn = _sqlite3.connect(path, check_same_thread=False)
    _db_conn.execute("PRAGMA journal_mode=WAL")
    _db_conn.execute("PRAGMA foreign_keys=ON")
    _db_conn.executescript(_DB_SCHEMA)
    _db_conn.commit()
    _DB_PATH = path
    # Write path to ini using line-by-line edit to preserve custom format
    # (configparser.write() would destroy the "root = path : name" format)
    try:
        ini = _INI_PATH or os.path.join(os.path.dirname(os.path.abspath(__file__)), "FileTagger.ini")
        if os.path.exists(ini):
            with open(ini, "r", encoding="utf-8") as f:
                lines = f.readlines()
            # Check if [FileTagger] section and database= already correct
            in_ft = False; found = False
            for line in lines:
                stripped = line.strip()
                if stripped.lower() == "[filetagger]":
                    in_ft = True
                elif stripped.startswith("["):
                    in_ft = False
                elif in_ft and stripped.lower().startswith("database"):
                    val = stripped.split("=", 1)[1].strip() if "=" in stripped else ""
                    if val == path:
                        found = True  # already correct, nothing to do
                    break
            if not found:
                # Rewrite: remove any existing [FileTagger] section, append fresh one
                new_lines = []
                skip = False
                for line in lines:
                    if line.strip().lower() == "[filetagger]":
                        skip = True; continue
                    if skip and line.strip().startswith("["):
                        skip = False
                    if not skip:
                        new_lines.append(line)
                # Ensure file ends with newline
                if new_lines and not new_lines[-1].endswith("\n"):
                    new_lines.append("\n")
                new_lines.append("[FileTagger]\n")
                new_lines.append(f"database = {path}\n")
                with open(ini, "w", encoding="utf-8") as f:
                    f.writelines(new_lines)
    except Exception as _e:
        print(f"db_open: could not write ini: {_e}")

def db_close():
    """Close the database connection. Call on app exit."""
    global _db_conn
    if _db_conn:
        _db_conn.close()
        _db_conn = None

# ── Thumbnail wrapper functions ────────────────────────────────────────────────

def thumb_get(source_path):
    """Return JPEG bytes for source_path, or None if not cached / stale."""
    if _db_conn is None: return None
    try:
        mtime = os.path.getmtime(source_path)
        row = _db_conn.execute(
            "SELECT jpeg, mtime FROM thumbnails WHERE path=?", (source_path,)
        ).fetchone()
        if row and abs(row[1] - mtime) < 1.0:
            return row[0]
        return None
    except Exception:
        return None

def thumb_get_many(source_paths):
    """Return dict {path: jpeg_bytes} for all cached, non-stale paths."""
    if _db_conn is None or not source_paths: return {}
    result = {}
    try:
        placeholders = ','.join('?' * len(source_paths))
        rows = _db_conn.execute(
            f"SELECT path, jpeg, mtime FROM thumbnails WHERE path IN ({placeholders})",
            source_paths
        ).fetchall()
        mtimes = {}
        for p in source_paths:
            try: mtimes[p] = os.path.getmtime(p)
            except: mtimes[p] = 0.0
        for path, jpeg, mtime in rows:
            if abs(mtimes.get(path, 0.0) - mtime) < 1.0:
                result[path] = jpeg
    except Exception:
        pass
    return result

def thumb_put(source_path, jpeg_bytes):
    """Store a single thumbnail."""
    if _db_conn is None: return
    try:
        mtime = os.path.getmtime(source_path)
        try: sz = os.path.getsize(source_path)
        except: sz = 0
        _db_conn.execute(
            "INSERT OR REPLACE INTO thumbnails (path, jpeg, mtime, file_size) VALUES (?,?,?,?)",
            (source_path, jpeg_bytes, mtime, sz)
        )
        _db_conn.commit()
    except Exception:
        pass

def thumb_put_many(items):
    """Store multiple thumbnails. items = list of (path, jpeg_bytes)."""
    if _db_conn is None or not items: return
    try:
        rows = []
        for path, jpeg in items:
            try: mtime = os.path.getmtime(path)
            except: mtime = 0.0
            try: sz = os.path.getsize(path)
            except: sz = 0
            rows.append((path, jpeg, mtime, sz))
        _db_conn.executemany(
            "INSERT OR REPLACE INTO thumbnails (path, jpeg, mtime, file_size) VALUES (?,?,?,?)",
            rows
        )
        _db_conn.commit()
    except Exception:
        pass

def thumb_gc(source_folder):
    """Remove stale thumbnail entries for files no longer in source_folder.
    Returns (live_count, removed_count)."""
    if _db_conn is None: return 0, 0
    try:
        rows = _db_conn.execute(
            "SELECT path FROM thumbnails WHERE path LIKE ?",
            (source_folder.rstrip('\\/') + '%',)
        ).fetchall()
        dead = [r[0] for r in rows if not os.path.exists(r[0])]
        if dead:
            _db_conn.executemany("DELETE FROM thumbnails WHERE path=?", [(p,) for p in dead])
            _db_conn.commit()
        return len(rows) - len(dead), len(dead)
    except Exception:
        return 0, 0

def thumb_move(source_paths, dest_folder):
    """Update thumbnail path keys when files are moved to dest_folder."""
    if _db_conn is None or not source_paths: return
    try:
        updates = []
        for old in source_paths:
            new = os.path.join(dest_folder, os.path.basename(old))
            updates.append((new, old))
        _db_conn.executemany(
            "UPDATE OR REPLACE thumbnails SET path=? WHERE path=?", updates
        )
        _db_conn.commit()
    except Exception:
        pass

def _scale_to_fit(img, sz):
    """Scale image to fit within sz×sz box, preserving aspect ratio. Scales both up and down."""
    from PIL import Image as _Img
    w, h = img.size
    if w == 0 or h == 0: return img
    scale = min(sz / w, sz / h)
    new_w = max(1, int(w * scale))
    new_h = max(1, int(h * scale))
    if new_w == w and new_h == h: return img
    return img.resize((new_w, new_h), _Img.BILINEAR)

def _file_size_info(path):
    try: sz = os.path.getsize(path)
    except: return (0, "?", "? KB")
    if sz < 50_000:      cat = "Tiny"
    elif sz < 150_000:   cat = "V.Small"
    elif sz < 300_000:   cat = "Small"
    elif sz < 1_500_000: cat = "Medium"
    elif sz < 6_000_000: cat = "Large"
    else:                cat = "Huge"
    disp = f"{sz//1024} KB" if sz < 1_000_000 else f"{sz/1_000_000:.1f} MB"
    return (sz, cat, disp)

_size_cache = {}   # path -> (sz, cat, disp) — module-level cache

# ── PhotoImage LRU cache ───────────────────────────────────────────────────────
# Keeps decoded PhotoImage objects alive so back-navigation is instant.
# Key: (path, disp_size)  Value: PhotoImage
# Limited to 200 entries (~50 MB at 250px). Oldest evicted first.
from collections import OrderedDict as _OD
_photo_cache: _OD = _OD()
_PHOTO_CACHE_MAX = 200

def _photo_cache_get(path, sz):
    key = (path, sz)
    if key in _photo_cache:
        _photo_cache.move_to_end(key)   # mark as recently used
        return _photo_cache[key]
    return None

def _photo_cache_put(path, sz, photo):
    key = (path, sz)
    _photo_cache[key] = photo
    _photo_cache.move_to_end(key)
    while len(_photo_cache) > _PHOTO_CACHE_MAX:
        _photo_cache.popitem(last=False)  # evict oldest

def _file_size_info_cached(path):
    if path not in _size_cache:
        _size_cache[path] = _file_size_info(path)
    return _size_cache[path]

def _fit_text(text, max_px, font_spec=("Segoe UI", 9)):
    """Return text truncated with '…' so it fits within max_px pixels.
    Uses tkinter font measurement — accurate for the actual render font."""
    try:
        import tkinter.font as tkfont
        f = tkfont.Font(family=font_spec[0], size=font_spec[1])
        if f.measure(text) <= max_px:
            return text
        # Binary-search the cut point
        lo, hi = 1, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if f.measure(text[:mid] + "…") <= max_px:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo] + "…"
    except Exception:
        # Fallback: rough char-based clip (approx 7px per char at size 9)
        max_chars = max(1, max_px // 7)
        return text if len(text) <= max_chars else text[:max_chars - 1] + "…"

def _get_pdf_info(path):
    if not HAVE_FITZ: return 0, ""
    try:
        doc = fitz.open(_longpath(path))
        pages = doc.page_count; doc.close()
        return pages, ""
    except: return 0, ""

# ── Collection helpers ─────────────────────────────────────────────────────────
# ── Collection helpers (SQLite) ───────────────────────────────────────────────
# Signatures unchanged — all call sites work without modification.
# root parameter is accepted for compatibility but not used (DB is global).

def _list_collections(root=None):
    """Return sorted list of collection names from DB."""
    if _db_conn is None: return []
    try:
        rows = _db_conn.execute("SELECT name FROM collections ORDER BY name").fetchall()
        return [r[0] for r in rows]
    except Exception:
        return []

def _read_collection(name, root=None):
    """Return {path: timestamp} dict for named collection."""
    if _db_conn is None: return {}
    try:
        row = _db_conn.execute("SELECT id FROM collections WHERE name=?", (name,)).fetchone()
        if not row: return {}
        rows = _db_conn.execute(
            "SELECT path, added_at FROM collection_items WHERE collection_id=? ORDER BY sort_order",
            (row[0],)
        ).fetchall()
        return {r[0]: (r[1] or '') for r in rows}
    except Exception:
        return {}

def _write_collection(name, root=None, tagged=None, tagged_at=None, order=None):
    """Write collection to DB. Creates collection if it doesn't exist."""
    if _db_conn is None: return
    if tagged is None: tagged = set()
    if tagged_at is None: tagged_at = {}
    try:
        import datetime as _dt2
        now = _dt2.datetime.now().isoformat(timespec='seconds')
        cur = _db_conn.execute("SELECT id FROM collections WHERE name=?", (name,))
        row = cur.fetchone()
        if row:
            cid = row[0]
            _db_conn.execute("UPDATE collections SET modified_at=? WHERE id=?", (now, cid))
        else:
            _db_conn.execute(
                "INSERT INTO collections (name, created_at, modified_at) VALUES (?,?,?)",
                (name, now, now)
            )
            cid = _db_conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        # Rebuild items: delete existing, re-insert in order
        _db_conn.execute("DELETE FROM collection_items WHERE collection_id=?", (cid,))
        paths = order if order else sorted(tagged)
        rows = [
            (cid, p, tagged_at.get(p, now), i)
            for i, p in enumerate(paths)
        ]
        _db_conn.executemany(
            "INSERT INTO collection_items (collection_id, path, added_at, sort_order) VALUES (?,?,?,?)",
            rows
        )
        _db_conn.commit()
    except Exception as e:
        print(f"Could not write collection {name}: {e}")

def _delete_collection(name, root=None):
    """Delete a collection from DB."""
    if _db_conn is None: return
    try:
        _db_conn.execute("DELETE FROM collections WHERE name=?", (name,))
        _db_conn.commit()
    except Exception as e:
        print(f"Could not delete collection {name}: {e}")

# ── Cull list helpers (SQLite) ─────────────────────────────────────────────────

def _read_cull_list(root=None):
    """Return {path: timestamp} for culled files that still exist on disk."""
    if _db_conn is None: return {}
    try:
        rows = _db_conn.execute("SELECT path, marked_at FROM cull_list").fetchall()
        return {r[0]: (r[1] or '') for r in rows if os.path.exists(r[0])}
    except Exception:
        return {}

def _write_cull_list(root=None, culled=None, culled_at=None):
    """Replace cull list in DB with current state."""
    if _db_conn is None: return
    if culled is None: culled = set()
    if culled_at is None: culled_at = {}
    try:
        import datetime as _dt2
        now = _dt2.datetime.now().isoformat(timespec='seconds')
        _db_conn.execute("DELETE FROM cull_list")
        rows = [(p, culled_at.get(p, now)) for p in culled]
        _db_conn.executemany("INSERT INTO cull_list (path, marked_at) VALUES (?,?)", rows)
        _db_conn.commit()
    except Exception as e:
        print(f"Could not write cull list: {e}")

# ── First-run migration from .txt files → DB ───────────────────────────────────

def _migrate_txt_to_db(root):
    """Import legacy .txt collections and cull list into DB if DB is empty.
    Safe to call on every startup — skips if DB already has data."""
    if _db_conn is None: return
    try:
        existing = _db_conn.execute("SELECT COUNT(*) FROM collections").fetchone()[0]
        if existing > 0: return  # already migrated
    except Exception:
        return
    import datetime as _dt2
    now = _dt2.datetime.now().isoformat(timespec='seconds')
    # Migrate collections from _Collections/*.txt
    cdir = os.path.join(root, FT_SYSTEM_DIR, COLL_SUBDIR)
    if os.path.isdir(cdir):
        for fname in sorted(os.listdir(cdir)):
            if not (fname.startswith(COLL_PREFIX) and fname.endswith('.txt')): continue
            name = fname[len(COLL_PREFIX):-4]
            data = {}
            try:
                with open(os.path.join(cdir, fname), 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith('#'): continue
                        parts = line.split('\t')
                        fp = parts[0]; ts = parts[1] if len(parts) > 1 else now
                        data[fp] = ts
            except Exception:
                continue
            if data:
                _write_collection(name, root, set(data.keys()), data)
                print(f"Migrated collection: {name} ({len(data)} files)")
    # Cull list is NOT migrated — fresh start for new DB

# ── Build date ─────────────────────────────────────────────────────────────────
import datetime as _dt
BUILD_DATE = "24 Apr 2026  09:21 AEST"  # set at save time — do not edit manually

# ── Splash ─────────────────────────────────────────────────────────────────────
def _show_startup_splash(root):
    """Splash shown before the main window appears — respects theme."""
    import time as _time2
    try: _tz = _time2.tzname[1] if _time2.daylight else _time2.tzname[0]
    except: _tz = ""
    _build = BUILD_DATE + (f"  {_tz}" if _tz else "")

    is_light = True
    _bg   = "#e8e8e8" if is_light else "#1a1a1a"
    _bd   = "#888888" if is_light else "#444444"
    _fg   = "#111111" if is_light else "white"
    _div  = "#aaaaaa" if is_light else "#444444"

    splash = tk.Toplevel(root)
    splash.overrideredirect(True)
    sw = root.winfo_screenwidth(); sh = root.winfo_screenheight()
    w, h = 680, 280
    splash.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")
    splash.configure(bg=_bg)
    border = tk.Frame(splash, bg=_bd, padx=2, pady=2)
    border.pack(fill="both", expand=True)
    inner = tk.Frame(border, bg=_bg)
    inner.pack(fill="both", expand=True)

    tk.Label(inner, text="FileTagger with SQLite",
             bg=_bg, fg=_fg,
             font=("Segoe UI", 36, "bold")).pack(pady=(24, 0))
    tk.Label(inner, text="📷  Photos    +    📄  PDFs    —    Tag  ·  Browse  ·  Export  ·  SQLite",
             bg=_bg, fg=_fg,
             font=("Segoe UI", 11)).pack(pady=(6, 0))
    tk.Frame(inner, bg=_div, height=1).pack(fill="x", padx=40, pady=12)
    tk.Label(inner, text=f"Build  {_build}",
             bg=_bg, fg=_fg,
             font=("Segoe UI", 11)).pack()
    tk.Label(inner, text="Loading...",
             bg=_bg, fg=_fg,
             font=("Segoe UI", 11, "italic")).pack(pady=(8, 0))
    splash.update()
    return splash

# ═══════════════════════════════════════════════════════════════════════════════
# ── GPS Map Window ─────────────────────────────────────────────────────────────
def _check_libraries():
    """Check all required and optional libraries at startup.
    Returns (ok, missing_required, missing_optional) where ok=True means safe to start."""
    import importlib
    REQUIRED = [
        ("PIL",   "Pillow",        "pip install Pillow",        "Image loading and thumbnailing"),
        ("numpy", "numpy",         "pip install numpy",         "Similarity scan, FFT filter, transforms"),
    ]
    OPTIONAL = [
        ("fitz",           "PyMuPDF",       "pip install pymupdf",       "PDF thumbnail rendering"),
        ("fpdf",           "fpdf2",         "pip install fpdf2",         "Contact sheet generation"),
        ("tkintermapview", "tkintermapview","pip install tkintermapview","GPS map window"),
    ]
    missing_req = []
    missing_opt = []
    for mod, pkg, cmd, purpose in REQUIRED:
        if importlib.util.find_spec(mod) is None:
            missing_req.append((pkg, cmd, purpose))
    for mod, pkg, cmd, purpose in OPTIONAL:
        if importlib.util.find_spec(mod) is None:
            missing_opt.append((pkg, cmd, purpose))
    return len(missing_req) == 0, missing_req, missing_opt


def _show_library_warning(root, missing_req, missing_opt):
    """Show a dialog listing missing libraries. Blocks if required libs missing."""
    import tkinter as _tk
    import tkinter.font as _tkf

    dlg = _tk.Toplevel(root)
    dlg.title("FileTagger — Library Check")
    dlg.configure(bg="#f0f4f8")
    dlg.resizable(False, False)
    dlg.transient(root)
    dlg.grab_set()

    # Centre on screen
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

    _tk.Label(dlg, text=hdr_text, bg="#f0f4f8", fg=hdr_col,
              font=("Segoe UI", 13, "bold")).pack(pady=(20, 4))
    _tk.Label(dlg, text=sub_text, bg="#f0f4f8", fg="#444444",
              font=("Segoe UI", 9)).pack(pady=(0, 12))

    frame = _tk.Frame(dlg, bg="#f0f4f8"); frame.pack(fill="x", padx=24)

    def section(title, items, title_col):
        if not items: return
        _tk.Label(frame, text=title, bg="#f0f4f8", fg=title_col,
                  font=("Segoe UI", 9, "bold"), anchor="w").pack(fill="x", pady=(8,2))
        for pkg, cmd, purpose in items:
            row = _tk.Frame(frame, bg="#e8eef4", highlightbackground="#ccd8e8",
                            highlightthickness=1)
            row.pack(fill="x", pady=2)
            _tk.Label(row, text=f"  {pkg}", bg="#e8eef4", fg="#1a3a6a",
                      font=("Segoe UI", 9, "bold"), width=16, anchor="w").pack(side="left", padx=(4,0), pady=4)
            _tk.Label(row, text=purpose, bg="#e8eef4", fg="#444444",
                      font=("Segoe UI", 8), anchor="w").pack(side="left", padx=8)
            cmd_fr = _tk.Frame(row, bg="#1a2a4a", padx=6, pady=2)
            cmd_fr.pack(side="right", padx=6, pady=4)
            _tk.Label(cmd_fr, text=cmd, bg="#1a2a4a", fg="#88ddff",
                      font=("Courier New", 8)).pack()

    section("Required — must install to run:", missing_req, "#cc2222")
    section("Optional — install to enable features:", missing_opt, "#cc7700")

    _tk.Frame(dlg, bg="#cccccc", height=1).pack(fill="x", padx=20, pady=(16, 0))

    bf = _tk.Frame(dlg, bg="#f0f4f8"); bf.pack(pady=12)
    can_continue = not missing_req

    if can_continue:
        _tk.Button(bf, text="  Continue  ", bg="#226633", fg="white",
                   font=("Segoe UI", 10, "bold"), relief="flat", padx=12, pady=4,
                   cursor="hand2", command=dlg.destroy).pack(side="left", padx=6)
    _tk.Button(bf, text="  Exit  ", bg="#662222", fg="white",
               font=("Segoe UI", 10, "bold"), relief="flat", padx=12, pady=4,
               cursor="hand2", command=lambda: (root.destroy(), sys.exit(1))).pack(side="left", padx=6)

    if not can_continue:
        dlg.protocol("WM_DELETE_WINDOW", lambda: (root.destroy(), sys.exit(1)))
        root.wait_window(dlg)
    else:
        dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)


# ═══════════════════════════════════════════════════════════════════════════════
# ── FileTaggerTree — FileCountTree subclass for FileTagger ─────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

class FileTaggerTree(FileCountTree):
    """
    FileCountTree subclass for FileTagger.

    Three columns:
        Files  — matching files directly in the folder
        Tagged — files in the active tag collection (via get_tagged_count callback)
        Thumbs — thumbnails cached in the database (via get_thumb_count callback)

    Parameters
    ----------
    extensions       : set of str — file extensions to count, e.g. {'.jpg', '.jpeg'}
    get_tagged_count : callable(path) -> int  — returns tagged file count for path
    get_thumb_count  : callable(path) -> int  — returns thumbnail count for path
    col_files        : str — heading for files column  (default "Files")
    col_tagged       : str — heading for tagged column (default "Tagged")
    col_thumbs       : str — heading for thumbs column (default "Thumbs")

    All other FolderTreeWidget parameters are passed through.

    Usage
    -----
        tree = FileTaggerTree(
            parent,
            extensions={'.jpg', '.jpeg'},
            get_tagged_count=lambda p: my_tagged_count(p),
            get_thumb_count=lambda p: my_thumb_count(p),
            on_select=my_callback,
            show_root_entry=False,
        )
        tree.pack(fill="y", side="left")
        tree.pack_propagate(False)
        tree.configure(width=tree.actual_width())
    """

    def __init__(self, parent, extensions=None,
                 get_tagged_count=None, get_thumb_count=None,
                 col_files="Files", col_tagged="Tagged", col_thumbs="Thumbs",
                 **kw):
        self._get_tagged_count = get_tagged_count or (lambda p: 0)
        self._get_thumb_count  = get_thumb_count  or (lambda p: 0)
        # Override columns — three columns instead of FileCountTree's two
        kw['columns'] = [
            (col_files,  TREE_COL_W, "e"),
            (col_tagged, TREE_COL_W, "e"),
            (col_thumbs, TREE_COL_W, "e"),
        ]
        # Skip FileCountTree.__init__ column injection — go straight to FolderTreeWidget
        self._extensions = {e.lower() for e in (extensions or {'.jpg', '.jpeg'})}
        FolderTreeWidget.__init__(self, parent, **kw)

    # ── Column filling ─────────────────────────────────────────────────────────

    def _fill_node(self, path):
        """Fill all three columns for a single node."""
        files  = self._count_own(path)
        tagged = self._get_tagged_count(path)
        thumbs = self._get_thumb_count(path)
        self.set_col(path, 0, self._fmt(files))
        self.set_col(path, 1, self._fmt(tagged))
        self.set_col(path, 2, self._fmt(thumbs))
        # Colour tag — blue if has files, grey otherwise
        if files > 0:
            self._tree.item(path, tags=("has_files",))
        else:
            self._tree.item(path, tags=("empty",))

    def _fill_children_of(self, path):
        """Fill all three columns for direct children of path."""
        for child in self._tree.get_children(path):
            if self.PLACEHOLDER not in child:
                self._fill_node(child)

    # ── Overrides ──────────────────────────────────────────────────────────────

    def _populate_root(self, path):
        """Populate root then fill columns for immediate children only."""
        super(FileCountTree, self)._populate_root(path)
        self._fill_children_of(path)

    def _on_node_open(self, path):
        """Expand node then fill columns for its children."""
        super(FileCountTree, self)._on_node_open(path)
        self._fill_children_of(path)

    def refresh_stats(self):
        """Refresh all three columns for all currently visible nodes.
        Call after tagged collection or thumbnail cache changes."""
        def _refresh(node):
            for child in self._tree.get_children(node):
                if self.PLACEHOLDER not in child:
                    self._fill_node(child)
                    _refresh(child)
        _refresh("")


class FileTagger:
    # Virtual scroll: render this many images per batch
    BATCH_SIZE = 9999  # replaced by _page_size at runtime

    def __init__(self, root=None):
        global _FileTagger_instance
        _FileTagger_instance = self
        self._root         = root
        self.mode          = "photos"
        self.mode_cfg      = _mode_cfg("photos")
        self.tagged        = set()
        self.tagged_at     = {}
        self.tagged_order  = []   # ordered list of paths for reorder view
        self.collection    = ""
        self.thumb_widgets = []
        self._photo_refs   = []
        self._cols         = 4
        self._recurse      = False
        self._tree_filter_mode = "all"   # "all" or "files"
        self._in_tagged_view  = False
        self._in_cull_view    = False
        self._in_located_view = False
        self._in_similar_view     = False
        self._similar_groups      = {}     # path -> group_index (int, 1-based)
        self._in_group_summary    = False  # compressed: one thumb per group
        self._group_clusters      = []     # list of [path, ...] per group
        self._pre_group_state     = None   # saved state for back-navigation
        self._view_mode       = None   # tk.StringVar — created in _build_ui
        self.view_coll_combo  = None   # always-visible collection combobox
        self.lbl_view_folder  = None   # folder label in view radio panel
        self.lbl_view_status  = None   # status label top of right panel
        self.lbl_cull_row     = None   # cull list row in left panel
        self.lbl_reorder      = None   # reorder row in left panel
        self.coll_listbox     = None   # collections listbox in left panel
        self._zoom_win     = None
        self._focused_orig = None
        self._load_gen     = 0
        self._all_files    = []
        self._page_num     = 0
        self._auto_cache   = False
        self._disp_size    = THUMB_SIZE
        self._user_cols    = 0   # 0 = auto — _compute_grid_dims calculates from actual canvas width
        global DEFAULT_COLS
        DEFAULT_COLS       = 0
        self._updating_spinners = False
        self._pending_jump      = None
        self._last_canvas_size  = None
        self._loading           = False
        self._page_start        = 0
        self._page_start_override = None
        self._delete_popup      = None
        self._selected          = set()   # paths currently selected (Ctrl/Shift+click)
        self._last_click_idx    = None    # index of last clicked cell for Shift+click range
        self._panel_mode        = "1"     # "1" = 1-panel, "2" = 2-panel Folder+Selection
        self._placed            = []      # ordered right-panel list (2-panel)
        self._placed_set        = set()   # fast membership for _placed
        self._right_sel         = set()   # paths selected in right panel
        self._ins_point         = None    # insertion index in right panel (None = append)
        self._scroll_after_id   = None
        self._move_mode         = False   # True when in tree-click move mode
        self._move_target       = None    # destination folder for move
        self._user_rows    = 0      # 0 = auto-compute from available height
        self._size_checks  = {}
        self._save_after_id = None   # debounce: pending after() id for collection save
        self._tree_after_id = None   # debounce: pending after() id for tree refresh
        self._closing_nodes = set()  # action lock: nodes being collapsed (race condition guard)
        self._tree_chevron_click = False  # True when last click was on disclosure arrow
        self._zoom_index   = 0       # index into _all_files for current zoom image
        self._culled       = set()   # paths marked for deletion
        self._culled_at    = {}      # timestamp per culled path
        self._edited       = set()   # paths modified by rotate/crop/straighten
        self._unreadable   = set()   # paths that could not be opened (persistent watermark)
        self._unreadable_reason = {}  # {path: label string}
        self._rot_delta    = {}      # path -> net rotation degrees (to detect net-zero)
        self._save_after_id_cull = None  # debounce for cull list save
        # Shadow list state
        self._shadow_active             = False
        self._shadow_source_collection  = ""
        self._shadow_files              = []
        self._shadow_tagged             = set()
        self._shadow_tagged_at          = {}
        # Stub widget refs — properly assigned when _coll_extras runs during _build_ui
        self.coll_var      = tk.StringVar() if root else None
        self.coll_combo    = None
        self.btn_shadow    = None
        self.btn_shadow_fork  = None
        self.btn_shadow_clear = None
        self.btn_commit    = None
        self.lbl_coll_info = None
        self.lbl_count     = None
        self.lbl_page      = None
        # Status bar label refs — assigned in _build_ui
        self.lbl_sb_folder       = None
        self.lbl_sb_fcounts      = None
        self.lbl_sb_tagged_local = None
        self.lbl_sb_culled_local = None
        self.lbl_sb_coll         = None
        self._welcome_card       = None
        self._jump_var     = tk.StringVar() if root else None
        self._build_ui(root)
        self.win.after(500, self._init_mode)
        self.win.after(600, self._set_tree_sash)

    def _set_tree_sash(self):
        """Force left panel to correct width after window renders."""
        try:
            self.win.update_idletasks()
            self._paned_main.sash_place(0, 455, 0)
            # Repeat after another layout pass to ensure it sticks
            self.win.after(500, self._set_tree_sash_final)
        except Exception: pass

    def _set_tree_sash_final(self):
        """Second pass — ensures sash stays at correct position after all layout completes."""
        try:
            self.win.update_idletasks()
            self._paned_main.sash_place(0, 455, 0)
        except Exception: pass

    def _init_mode(self):
        """Initialise or reinitialise for the current mode."""
        # Keep mode_cfg in sync with current mode and current root globals.
        self.mode_cfg = _mode_cfg(self.mode)
        # Flush any pending deferred save before switching mode/root
        if getattr(self, '_save_after_id', None):
            try: self.win.after_cancel(self._save_after_id)
            except: pass
            self._save_after_id = None
            self._save_current_collection()
        cfg = self.mode_cfg
        root_dir = cfg['root']
        self.tagged.clear(); self.tagged_at.clear(); self.collection = ""
        self._in_cull_view    = False
        self._in_tagged_view  = False
        self._in_located_view = False
        self._in_similar_view  = False
        self._similar_groups   = {}
        self._in_group_summary = False
        self._group_clusters   = []
        self._set_view_radio("folder")

        # Populate root selector combobox
        self._update_root_combobox()

        # Check accessibility of all roots — warn about offline ones non-blocking
        all_roots = PHOTOS_ROOTS if self.mode == "photos" else PDFS_ROOTS
        offline_roots = [name for path, name in all_roots if not _path_accessible(path)]
        online_roots  = [(path, name) for path, name in all_roots if _path_accessible(path)]
        if offline_roots:
            self._status(f"⚠  Offline: {', '.join(offline_roots)}")

        # If configured root is offline, switch to first accessible root
        if not _path_accessible(root_dir):
            if online_roots:
                root_dir = online_roots[0][0]
                self.mode_cfg['root'] = root_dir
                if self.mode == "photos":
                    global PHOTOS_ROOT; PHOTOS_ROOT = root_dir
                else:
                    global PDFS_ROOT; PDFS_ROOT = root_dir
                self._update_root_combobox()
            else:
                # All roots offline
                self._culled = set(); self._culled_at = {}
                self._show_access_error(root_dir)
                self.current_folder = root_dir
                self.lbl_folder.config(text=root_dir)
                self._all_files = []
                self._update_statusbar()
                self._update_tree_viewing()
                self.win.after(100, self._show_browse_prompt)
                return

        root_accessible = _path_accessible(root_dir)

        if root_accessible:
            try:
                _ft_dir(root_dir)
                _collections_dir(root_dir)
                _deleted_dir(root_dir)
                _sheets_dir(root_dir)
                cull_data = _read_cull_list(root_dir)
                self._culled = set(cull_data.keys())
                self._culled_at = dict(cull_data)
                self._refresh_collection_list()
                cols = _list_collections(root_dir)
                if "Temporary" not in cols:
                    _write_collection("Temporary", root_dir, set(), {})
                self._refresh_collection_list()
                self._switch_collection("Temporary", confirm=False)
                if not cols:
                    _write_collection("My Collection", root_dir, set(), {})
                    self._refresh_collection_list()
                self._populate_tree(root_dir)
            except Exception as _init_err:
                import traceback
                self._status(f"Init error: {_init_err}")
                print(f"_init_mode error: {traceback.format_exc()}")
        else:
            self._culled = set(); self._culled_at = {}
            self._status(f"⚠  Root folder not accessible: {root_dir}")
            self._show_access_error(root_dir)

        self.current_folder = root_dir
        self.lbl_folder.config(text=root_dir)
        self._all_files = []
        self._update_statusbar()
        self._update_tree_viewing()
        # Show simple browse prompt — user must choose a folder or collection
        self.win.after(100, self._show_browse_prompt)

    def _root_display_label(self, path, max_len=30):
        """Abbreviate a path for display: if short enough show as-is,
        otherwise show Drive:...FolderName."""
        if len(path) <= max_len:
            return path
        # Normalise slashes then split
        norm = path.replace('/', '\\')
        parts = [p for p in norm.split('\\') if p]
        if len(parts) >= 2:
            return parts[0] + '\\' + '...' + '\\' + parts[-1]
        return path

    def _update_root_combobox(self):
        """Populate the root selector combobox for the current mode.
        Uses friendly name if set, otherwise abbreviated path. Marks offline roots."""
        try:
            roots = PHOTOS_ROOTS if self.mode == "photos" else PDFS_ROOTS
            labels = []
            for path, name in roots:
                basename = os.path.basename(path.rstrip('/\\')) or path
                label = name if (name and name != basename) else self._root_display_label(path)
                if not _path_accessible(path):
                    label = f"[OFFLINE] {label}"
                labels.append(label)
            self.root_cb['values'] = labels
            current = self.mode_cfg['root']
            for i, (path, _) in enumerate(roots):
                if path == current:
                    self.root_cb.current(i)
                    break
            else:
                if labels: self.root_cb.current(0)
        except: pass

    def _on_root_select(self):
        """Called when user picks a different root from the combobox."""
        try:
            roots = PHOTOS_ROOTS if self.mode == "photos" else PDFS_ROOTS
            idx = self.root_cb.current()
            if idx < 0 or idx >= len(roots): return
            path, name = roots[idx]
            if path == self.mode_cfg['root']: return
            if not _path_accessible(path):
                messagebox.showwarning("Root Offline",
                    f"This root folder is not accessible on this computer:\n{path}\n\n"
                    "Check that the drive or network share is connected.",
                    parent=self.win)
                # Revert combobox to current root
                self._update_root_combobox()
                return
            self._switch_root(path)
        except Exception as e:
            print(f"_on_root_select error: {e}")

    def _switch_root(self, new_root):
        """Switch to a different root folder for the current mode, always starting in Temporary."""
        self._save_current_collection()
        # Update mode_cfg root
        self.mode_cfg['root'] = new_root
        if self.mode == "photos":
            global PHOTOS_ROOT; PHOTOS_ROOT = new_root
        else:
            global PDFS_ROOT; PDFS_ROOT = new_root
        # Reset all view state
        self._in_tagged_view  = False
        self._in_cull_view    = False
        self._in_located_view = False
        self._in_similar_view  = False
        self._similar_groups   = {}
        self._in_group_summary = False
        self._group_clusters   = []
        self._set_view_radio("folder")
        self._page_num = 0
        self._all_files = []
        self._clear_grid()
        self._init_mode()

    # ── UI ────────────────────────────────────────────────────────────────────
    def _build_ui(self, root=None):
        self.win = root if root else tk.Tk()
        self.win.overrideredirect(False)
        self.win.title("FileTagger")
        self.win.state("zoomed")
        self.win.configure(bg=BG3)
        for w in self.win.winfo_children(): w.destroy()
        self.win.protocol("WM_DELETE_WINDOW", self._on_close)

        # ══════════════════════════════════════════════════════════════════════
        # ── Two toolbar rows ─────────────────────────────────────────────────
        # Row 1: Mode+Folder (left-pinned) | Collection (dockable) | Tagging (dockable)
        # Row 2: View (dockable) | Output (dockable) | Thumbs (dockable)
        # ══════════════════════════════════════════════════════════════════════
        DOCK_BG  = BG3
        PANEL_BG = BG2
        dock_outer = tk.Frame(self.win, bg=DOCK_BG)
        dock_outer.pack(side="top", fill="x")

        # ── Row 1 ─────────────────────────────────────────────────────────────
        row1_outer = tk.Frame(dock_outer, bg=DOCK_BG, height=42)
        row1_outer.pack(fill="x")
        row1_outer.pack_propagate(False)

        # Mode + Folder — left-pinned, never moves
        _mf_bg  = "#cccccc"
        _lbl_fg = "#333333"
        _fg     = "#111111"
        mode_folder_frame = tk.Frame(row1_outer, bg=_mf_bg,
                                     highlightbackground="#555", highlightthickness=1)
        mode_folder_frame.pack(side="left", padx=(TREE_LEFT_W + 4, 8), pady=2)

        def _sep2(p): tk.Frame(p,bg="#555",width=1).pack(side="left",fill="y",padx=4,pady=2)

        # Style ttk comboboxes to match theme
        _cb_style = ttk.Style()
        _cb_style.configure("MF.TCombobox",
                            foreground=_fg,
                            fieldbackground=_mf_bg,
                            background=_mf_bg,
                            selectforeground=_fg,
                            selectbackground=_mf_bg)

        self._mode_var = tk.StringVar(value="Photos")
        self._mode_switching = False
        tk.Label(mode_folder_frame, text="Mode:", bg=_mf_bg, fg=_lbl_fg,
                 font=("Segoe UI",9,"bold")).pack(side="left", padx=(6,1))
        self.mode_cb = ttk.Combobox(mode_folder_frame, textvariable=self._mode_var,
                                    values=["Photos", "PDFs"], width=7, state="readonly")
        self.mode_cb.pack(side="left", padx=(0,3))
        self.mode_cb.bind("<<ComboboxSelected>>", lambda e: self._toggle_mode())
        self.mode_cb.bind("<Return>", lambda e: self._toggle_mode())
        self.mode_cb.bind("<FocusOut>", lambda e: self._mode_var_changed())
        self._mode_var.trace_add("write", self._mode_var_changed)
        _sep2(mode_folder_frame)
        tk.Label(mode_folder_frame, text="Root:", bg=_mf_bg, fg=_lbl_fg,
                 font=("Segoe UI",9,"bold")).pack(side="left", padx=(2,1))
        self._root_var = tk.StringVar()
        self.root_cb = ttk.Combobox(mode_folder_frame, textvariable=self._root_var,
                                    width=22, state="readonly", font=("Segoe UI",9),
                                    style="MF.TCombobox")
        self.root_cb.pack(side="left", padx=(0,4))
        self.root_cb.bind("<<ComboboxSelected>>", lambda e: self._on_root_select())
        # lbl_folder kept for compatibility with code that sets it directly
        self.lbl_folder = tk.Label(mode_folder_frame, text="", bg=_mf_bg, fg=_fg,
                                   font=("Segoe UI",9), anchor="w", width=0)
        self.lbl_folder.pack_forget()   # hidden — root_cb replaces it
        # Theme toggle removed from toolbar — use Settings to change theme

        # ── View radio panel — static, immediately left of Collection toolbar ──
        # Packed after row1_centre (the collection toolbar parent) so it precedes it visually.
        # We pack it onto row1_outer AFTER row1_centre is created, using pack(before=).
        _vp_bg  = "#c8d8c8"
        _vp_fg  = "#111111"
        _vp_dim = "#555555"
        view_panel = tk.Frame(row1_outer, bg=_vp_bg,
                              highlightbackground="#555", highlightthickness=1)
        # pack() called below after row1_centre exists so we can use before=

        self._view_mode = tk.StringVar(value='folder')

        # Custom scalable radio buttons — tk.Radiobutton indicator is fixed-size on Windows.
        # Use tk.Buttons with ● / ○ Unicode that scale perfectly with the font.
        _vrad_btns = {}  # value -> (button_widget, display_text)

        def _make_vrad(parent, text, value, padx_args):
            def _click(v=value):
                self._view_mode.set(v)
                for val2, (b2, t2) in _vrad_btns.items():
                    b2.config(text=f'\u25cf {t2}' if val2 == v else f'\u25cb {t2}')
                self._on_view_radio()
            btn = tk.Button(parent, text=f'\u25cb {text}',
                            bg=_vp_bg, fg=_vp_fg,
                            activebackground=_vp_bg, activeforeground=_vp_fg,
                            font=('Segoe UI', 13, 'bold'),
                            relief='flat', bd=0, cursor='hand2',
                            pady=4, command=_click)
            btn.pack(side='left', padx=padx_args)
            _vrad_btns[value] = (btn, text)
            return btn

        def _refresh_vrad():
            cur = self._view_mode.get()
            for val2, (b2, t2) in _vrad_btns.items():
                b2.config(text=f'\u25cf {t2}' if val2 == cur else f'\u25cb {t2}')
        self._refresh_vrad = _refresh_vrad

        _make_vrad(view_panel, '\U0001f4c1 Folder', 'folder', (8, 2))
        self.lbl_view_folder = tk.Label(view_panel, text='\u2014',
                                        bg=_vp_bg, fg=_vp_dim,
                                        font=('Segoe UI',12,'bold'))
        self.lbl_view_folder.pack(side='left', padx=(0,10))

        tk.Frame(view_panel, bg='#555', width=1).pack(side='left', fill='y', padx=4, pady=4)

        _make_vrad(view_panel, '\u2605 Collection', 'collection', (8, 2))
        _coll_cb_style = ttk.Style()
        _coll_cb_style.configure('Bold.TCombobox',
                                 font=('Segoe UI', 11, 'bold'),
                                 foreground=_vp_fg,
                                 fieldbackground=_vp_bg,
                                 background=_vp_bg,
                                 selectforeground=_vp_fg,
                                 selectbackground=_vp_bg)
        self.view_coll_combo = ttk.Combobox(view_panel, textvariable=self.coll_var,
                                            font=('Segoe UI',11,'bold'),
                                            width=20, state='readonly',
                                            style='Bold.TCombobox')
        self.view_coll_combo.pack(side='left', padx=(0,8))

        tk.Frame(view_panel, bg='#555', width=1).pack(side='left', fill='y', padx=4, pady=4)

        _make_vrad(view_panel, '\U0001f5d1 Deletion List', 'cull', (8, 10))

        _refresh_vrad()  # initialise ● / ○ display

        # view_panel is NOT packed — replaced by left-panel sections.
        # _view_mode, _refresh_vrad, view_coll_combo kept for internal logic.

        # Dockable panels — Collection + Tagging pack left after mode_folder_frame
        row1_centre = tk.Frame(row1_outer, bg=DOCK_BG)
        row1_centre.pack(side="left", pady=2)

        # ── Row 2 ─────────────────────────────────────────────────────────────
        row2_outer = tk.Frame(dock_outer, bg=DOCK_BG, height=38)
        row2_outer.pack(fill="x")
        row2_outer.pack_propagate(False)

        # ── Row 2 left-aligned frame: Columns + Size — packed FIRST ──────────
        _cs_bg  = "#cccccc"
        _cs_fg  = "#333333"
        cols_size_frame = tk.Frame(row2_outer, bg=_cs_bg,
                                   highlightbackground="#555", highlightthickness=1)
        cols_size_frame.pack(side="left", padx=(TREE_LEFT_W + 4, 8), pady=2)

        # Dockable toolbars row2 — packed after cols_size_frame
        row2_centre = tk.Frame(row2_outer, bg=DOCK_BG)
        row2_centre.pack(side="left", pady=2)

        def _recentre_rows(e=None):
            pass  # no longer needed — pack handles layout
        row1_outer.bind("<Configure>", lambda e: _recentre_rows())
        row2_outer.bind("<Configure>", lambda e: _recentre_rows())

        self._toolbars  = {}
        self._tb_extras = {}
        self._rows_var = tk.StringVar(value="")
        self._cols_var = tk.StringVar(value=str(self._user_cols))

        def _view_extras(parent, bg):
            pass  # Columns and Size controls are in the left-aligned row2 frame
        self._tb_extras["view"] = _view_extras

        tk.Label(cols_size_frame, text="Columns:", bg=_cs_bg, fg=_cs_fg,
                 font=("Segoe UI",9,"bold")).pack(side="left", padx=(6,1))
        ce = tk.Entry(cols_size_frame, textvariable=self._cols_var, width=3,
                      font=("Segoe UI",9), justify="center",
                      bg="white", fg="#111111", insertbackground="#111111",
                      relief="flat", highlightthickness=1, highlightbackground="#aaaaaa")
        ce.pack(side="left", padx=(0,6))
        ce.bind("<Return>", lambda e: self._on_grid_dims_change('cols'))
        tk.Frame(cols_size_frame, bg="#aaaaaa", width=1).pack(side="left", fill="y", padx=2, pady=3)
        tk.Label(cols_size_frame, text="Size:", bg=_cs_bg, fg=_cs_fg,
                 font=("Segoe UI",9,"bold")).pack(side="left", padx=(6,1))
        self.btn_size_filter = tk.Button(cols_size_frame, text="Size: All ▾",
                              bg="white", fg="#111111",
                              font=("Segoe UI",9), relief="flat", padx=6, pady=2,
                              cursor="hand2", command=self._show_size_popup)
        self.btn_size_filter.pack(side="left", padx=(0,6))

        # Collection extras — widgets inside the dockable Collection panel
        def _coll_extras(parent, bg):
            _cb = dict(bg="#333",fg="white",font=("Segoe UI",8,"bold"),relief="flat",
                       padx=5,pady=3,cursor="hand2",activebackground="#555",activeforeground="white")
            # coll_combo kept as alias to view_coll_combo for compatibility
            self.coll_combo = self.view_coll_combo
            tk.Button(parent,text="New",    command=self._new_collection,    **_cb).pack(side="left",padx=2)
            tk.Button(parent,text="Clone",  command=self._clone_collection,  **_cb).pack(side="left",padx=2)
            tk.Button(parent,text="Rename", command=self._rename_collection, **_cb).pack(side="left",padx=2)
            tk.Button(parent,text="Clear",  command=self._clear_collection,
                      bg="#664422",fg="white",font=("Segoe UI",8,"bold"),relief="flat",
                      padx=5,pady=3,cursor="hand2",activebackground="#885533",
                      activeforeground="white").pack(side="left",padx=2)
            tk.Button(parent,text="Delete", command=self._delete_collection,
                      bg="#882222",fg="white",font=("Segoe UI",8,"bold"),relief="flat",
                      padx=5,pady=3,cursor="hand2",activebackground="#aa3333",
                      activeforeground="white").pack(side="left",padx=2)
            tk.Frame(parent,bg="#555",width=1).pack(side="left",fill="y",padx=4,pady=2)
            tk.Button(parent,text="⇅ Sort", command=lambda: __import__("tkinter.messagebox",fromlist=["x"]).showinfo("Sort","Sort is disabled in this version — use 2-panel Reorder.",parent=self.win),
                      bg="#2255aa",fg="white",font=("Segoe UI",8,"bold"),relief="flat",
                      padx=5,pady=3,cursor="hand2",activebackground="#3366bb",
                      activeforeground="white").pack(side="left",padx=2)
            tk.Frame(parent,bg="#555",width=1).pack(side="left",fill="y",padx=4,pady=2)
            self.btn_shadow = tk.Button(parent,text="Shadow",command=self._toggle_shadow,
                      bg="#334455",fg="white",font=("Segoe UI",8,"bold"),relief="flat",
                      padx=5,pady=3,cursor="hand2",activebackground="#445566",activeforeground="white")
            self.btn_shadow.pack(side="left",padx=2)
            self.btn_shadow_fork = tk.Button(parent,text="Fork",
                      command=self._shadow_fork_dialog,bg="#226644",fg="white",
                      font=("Segoe UI",8,"bold"),relief="flat",padx=5,pady=3,cursor="hand2",
                      activebackground="#338855",activeforeground="white")
            self.btn_shadow_clear = tk.Button(parent,text="Clear Tags",
                      command=self._shadow_clear_tags,bg="#444455",fg="white",
                      font=("Segoe UI",8),relief="flat",padx=5,pady=3,cursor="hand2",
                      activebackground="#555566",activeforeground="white")
            tk.Frame(parent,bg="#555",width=1).pack(side="left",fill="y",padx=4,pady=2)
            self.btn_commit = tk.Button(parent,text="Commit",command=self._commit,
                      bg="#1a7a1a",fg="white",font=("Segoe UI",8,"bold"),relief="flat",
                      padx=5,pady=3,cursor="hand2",activebackground="#228822",activeforeground="white")
            self.btn_commit.pack(side="left",padx=2)
            tk.Frame(parent,bg="#555",width=1).pack(side="left",fill="y",padx=4,pady=2)
            self.lbl_coll_info = None
            self.lbl_count     = None
            self._size_checks = {k: tk.BooleanVar(value=True)
                                 for k in ["Tiny","V.Small","Small","Medium","Large","Huge"]}
            self.lbl_page = tk.Label(parent,text="",bg=bg,fg="#666",
                                     font=("Segoe UI",8),width=0,anchor="w")
        self._tb_extras["collection"] = _coll_extras

        # ── DockableToolbar class defined inline ──────────────────────────────
        _ft_self = self   # capture for use inside class

        class DockableToolbar:
            def __init__(self, dock_row, name, label, bg, buttons, extras_fn=None, tips=None, cmds=None):
                self.name      = name
                self.label     = label
                self.bg        = bg
                self.buttons   = buttons
                self.extras_fn = extras_fn
                self.tips      = tips or {}
                self._cmds     = cmds or {}
                self.floating  = False
                self.tl        = None
                self.named_btns = {}
                self._dragging  = False
                self._gx = self._gy = 0
                self.outer = tk.Frame(dock_row, bg=bg,
                                      highlightbackground="#888", highlightthickness=1)
                self.outer.pack(side="left", padx=4, pady=3)
                self._fill_docked()

            def _fill_docked(self):
                for w in self.outer.winfo_children(): w.destroy()
                self.named_btns.clear()
                row = tk.Frame(self.outer, bg=self.bg)
                row.pack(padx=2, pady=2)
                tk.Button(row, text="⇱", bg=self.bg, fg="white",
                          font=("Segoe UI",9), relief="flat", bd=0, padx=4,
                          cursor="hand2", activebackground=self.bg,
                          command=self.detach).pack(side="left", padx=(1,0))
                _lbl_bg = "#999999"
                _lbl_fg = "#111111"
                lbl = tk.Label(row, text=self.label, bg=_lbl_bg, fg=_lbl_fg,
                               font=("Segoe UI",8,"bold"), padx=6, pady=0, cursor="fleur")
                lbl.pack(side="left", fill="y")
                self._fill_buttons(row, floating=False)
                lbl.bind("<ButtonPress-1>",   self._press)
                lbl.bind("<B1-Motion>",       self._motion)
                lbl.bind("<ButtonRelease-1>", self._release)

            def _fill_buttons(self, parent, floating=False):
                sz,px,py = (9,8,4) if floating else (8,6,3)
                cmds = getattr(self, '_cmds', {})
                for text, btn_bg in self.buttons:
                    b = tk.Button(parent, text=text, bg=btn_bg, fg="white",
                                  font=("Segoe UI",sz,"bold"), relief="flat",
                                  padx=px, pady=py, cursor="hand2",
                                  activebackground="#555", activeforeground="white",
                                  command=cmds.get(text, lambda: None))
                    b.pack(side="left", padx=2, pady=2)
                    self.named_btns[text] = b
                    if text in self.tips:
                        _tip(b, self.tips[text])
                if self.extras_fn:
                    self.extras_fn(parent, self.bg)

            def _press(self, e):
                if self.floating: return
                self.outer.update_idletasks()
                self._gx = e.x_root - self.outer.winfo_rootx()
                self._gy = e.y_root - self.outer.winfo_rooty()
                self._dragging = False

            def _motion(self, e):
                if self.floating: return
                if not self._dragging:
                    if abs(e.x_root - self.outer.winfo_rootx() - self._gx) > 4 or                        abs(e.y_root - self.outer.winfo_rooty() - self._gy) > 4:
                        self._dragging = True
                        # Detach at current mouse position
                        self.detach(start_x=e.x_root - self._gx,
                                    start_y=e.y_root - self._gy,
                                    grab_x=self._gx, grab_y=self._gy)
                # NOTE: after detach, _motion stops because self.floating=True

            def _release(self, e):
                self._dragging = False

            def detach(self, start_x=None, start_y=None, grab_x=None, grab_y=None):
                if self.floating: return
                self.floating = True
                self.outer.update_idletasks()
                w = self.outer.winfo_width()
                h = self.outer.winfo_height()
                if start_x is None:
                    start_x = self.outer.winfo_rootx()
                    start_y = self.outer.winfo_rooty()
                    grab_x  = w // 2
                    grab_y  = h // 2
                # Freeze outer size in dock row — don't destroy children (causes blank)
                self.outer.pack_propagate(False)
                self.outer.configure(width=w, height=h, highlightbackground="#444")
                # Build Toplevel
                self.tl = tk.Toplevel()
                self.tl.overrideredirect(True)
                # transient keeps it above FT but not above other apps
                try: self.tl.transient(self.outer.winfo_toplevel())
                except: pass
                self.tl.configure(bg=self.bg,
                                  highlightbackground="#888", highlightthickness=1)
                trow = tk.Frame(self.tl, bg=self.bg)
                trow.pack(padx=2, pady=2)
                tlbl = tk.Label(trow, text=self.label, bg="#333", fg="white",
                                font=("Segoe UI",8,"bold"), padx=8, pady=0, cursor="fleur")
                tlbl.pack(side="left", fill="y")
                self._fill_buttons(trow, floating=True)
                tk.Button(trow, text="⊟", bg="#333", fg="white",
                          font=("Segoe UI",10), relief="flat", bd=0, padx=8,
                          cursor="hand2", activebackground="#555",
                          command=self.dock).pack(side="left", padx=(4,1), fill="y")
                # Position before binding so winfo_rootx is valid for drag
                self.tl.geometry(f"+{start_x}+{start_y}")
                self.tl.update_idletasks()
                # Drag bindings — on both tlbl and tl so drag works immediately after detach
                _p = [grab_x, grab_y]
                def fp(e): _p[0]=e.x_root-self.tl.winfo_rootx(); _p[1]=e.y_root-self.tl.winfo_rooty()
                def fd(e): self.tl.geometry(f"+{e.x_root-_p[0]}+{e.y_root-_p[1]}")
                tlbl.bind("<ButtonPress-1>", fp)
                tlbl.bind("<B1-Motion>",     fd)
                # Also bind to tl itself so drag initiated from docked label continues
                self.tl.bind("<B1-Motion>",  fd)
                self.tl.bind("<ButtonPress-1>", fp)

            def dock(self):
                if not self.floating: return
                self.floating = False
                if self.tl:
                    try: self.tl.destroy()
                    except: pass
                    self.tl = None
                self.outer.pack_propagate(True)
                self.outer.configure(width=1, height=1, highlightbackground="#888")
                self._fill_docked()

        # ── Create toolbars ───────────────────────────────────────────────────
        _tb_defs = [
            ("collection","Collection",PANEL_BG,row1_centre,"collection",[]),
            ("tagging",   "Tagging",   PANEL_BG,row1_centre,None,[
                ("Sel All",    "#225588"),("Sel Visible","#1a5577"),("Unsel All", "#553322"),
                ("Sel Filter", "#1a5a44"),
                ("🗑 Mark",    "#7a4400"),("↩ Unmark",   "#445522"),
                ("✎ Rename",   "#446644"),("📂 File Mgmt","#2a5a8a"),
            ]),
            ("view",      "View",      PANEL_BG,row2_centre,"view",[
                ("Located",   "#1a6655"),("Map","#1a5577"),("Folders","#445566"),
                ("Similar",   "#5a3a6a"),
                ("Compress",  "#3a3a6a"),
                ("⊞ 2-Panel", "#2a4a6a"),
            ]),
            ("output",    "Output",    PANEL_BG,row2_centre,None,[
                ("Contact Sheet","#444466"),("Export","#555555"),
                ("Copy/Move","#446644"),("Process","#1a5276"),
            ]),
            ("cache",     "Thumbs",    PANEL_BG,row2_centre,None,[
                ("Generate", "#226655"),
                ("Orphans",  "#662266"),("Auto OFF",  "#444444"),
                ("Settings", "#335577"),("About",    "#335577"),("DB Status", "#2d5a2d"),("Project", "#664422"),
            ]),
        ]

        # Map button text to actual commands
        _btn_cmds = {
            "Sel All":       self._sel_all,
            "Sel Visible":   self._sel_visible,
            "Unsel All":     self._sel_clear,
            "Sel Filter":    self._sel_filter,
            "🗑 Mark":       self._mark_selected,
            "↩ Unmark":      self._unmark_selected,
            "✎ Rename":      self._launch_ftfiler_rename,
            "📂 File Mgmt":  self._launch_ftfiler_filemgmt,
            "This Folder":   self._toggle_recurse,
            "Tagged View":   self._toggle_tagged_view,
            "Deletion List":     self._toggle_cull_view,
            "Located":       self._toggle_located_view,
            "Similar":       self._find_similar_dialog,
            "Compress":      self._toggle_group_summary,
            "Map":           lambda: self._launch_ftmapimg_from_selection(),
            "Folders":       self._show_folders_menu,
            "⊞ 2-Panel":     self._toggle_panel_mode,
            "Auto OFF":      self._toggle_auto_cache,
            "Theme":         self._toggle_theme,
            "Contact Sheet": lambda: __import__("tkinter.messagebox",fromlist=["x"]).showinfo("Use Operations","Select files first, then use the Operations panel.",parent=self.win),
            "Export":        lambda: __import__("tkinter.messagebox",fromlist=["x"]).showinfo("Use Operations","Select files first, then use the Operations panel.",parent=self.win),
            "Copy/Move":     lambda: __import__("tkinter.messagebox",fromlist=["x"]).showinfo("Use Operations","Select files first, then use the Operations panel.",parent=self.win),
            "Process":       lambda: __import__("tkinter.messagebox",fromlist=["x"]).showinfo("Use Operations","Select files first, then use the Operations panel.",parent=self.win),
            "Generate":      self._gen_cache_dialog,
            "Orphans":       self._clean_orphans_dialog,
            "Settings":      self._show_settings,
            "About":         self._show_about,
            "DB Status":     self._show_db_status,
            "Project":       self._switch_project,
        }

        _btn_tips = {
            "Sel All":       "Select all files in current folder",
            "Sel Visible":   "Select files visible in the current viewport",
            "Unsel All":     "Clear all selections",
            "Sel Filter":    "Select files whose names match the filter terms (comma-separated)",
            "🗑 Mark":       "Mark selected files for deletion",
            "↩ Unmark":      "Unmark selected files from deletion list",
            "✎ Rename":      "Rename selected files using FTFiler — works from folders and collections",
            "📂 File Mgmt":  "Open FTFiler file manager at current folder (not available in collection view)",
            "This Folder":   "Toggle: show this folder only / include subfolders",
            "Tagged View":   "Show all tagged files across all folders",
            "Deletion List":     "Show / hide files marked for deletion",
            "Located":       "Show only images with GPS location data",
            "Similar":       "Find visually similar / duplicate photos in this folder",
            "Compress":      "Collapse similar groups to one thumbnail each — click a group to review",
            "Map":           "Show all GPS-located images in this folder on a map",
            "Folders":       "Filter folder tree: All Folders or Files Only",
            "⊞ 2-Panel":     "Toggle 1-panel / 2-panel view",
            "Auto OFF":      "Automatically generate thumbnails when browsing",
            "Contact Sheet": "Select files first, then use Operations → Contact Sheet",
            "Export":        "Select files first, then use Operations → Export",
            "Copy/Move":     "Select files first, then use Operations → Copy/Move",
            "Process":       "Select files first, then use Operations → MGEN File Operations",
            "Generate":      "Pre-generate thumbnail cache for this folder",
            "Orphans":       "Remove thumbnails whose source files no longer exist",
            "Settings":      "Configure root folders, theme, thumbnail size and processing paths",
            "About":         "About FileTagger — paths, versions, library status",
            "DB Status":     "Show SQLite database status — tables and record counts",
            "Theme":         "Toggle Dark / Light theme (restart to apply)",
            "Project":       "Switch between projects or create a new one",
        }

        for tb_name, tb_label, tb_bg, tb_row, extras_key, tb_btns in _tb_defs:
            extras_fn = self._tb_extras.get(extras_key) if extras_key else None
            tb = DockableToolbar(tb_row, tb_name, tb_label, tb_bg, tb_btns,
                                 extras_fn, _btn_tips, _btn_cmds)
            self._toolbars[tb_name] = tb

        # Named button refs
        self.btn_located   = self._toolbars["view"].named_btns.get("Located")
        self.btn_similar   = self._toolbars["view"].named_btns.get("Similar")
        self.btn_compress  = self._toolbars["view"].named_btns.get("Compress")
        self.btn_autocache = self._toolbars["cache"].named_btns.get("Auto OFF")
        self.btn_contact   = self._toolbars["output"].named_btns.get("Contact Sheet")
        # Legacy refs set to None — replaced by view radio panel
        self.btn_recurse   = None
        self.btn_tagged    = None
        self.btn_cull_list = None

        # ── Main area — single background colour throughout ───────────────────
        # 1px divider between toolbar area and main content
        tk.Frame(self.win, bg=HOVER_BD, height=1).pack(side="top", fill="x")

        main = tk.Frame(self.win, bg=BG)
        main.pack(fill="both", expand=True)
        self._paned_main = tk.PanedWindow(main, orient="horizontal", bg=BG,
                               sashwidth=5, sashrelief="flat", handlesize=8)
        self._paned_main.pack(fill="both", expand=True)
        paned = self._paned_main

        tf = tk.Frame(paned, bg=BG)
        paned.add(tf, minsize=200, width=430, stretch='never')

        # ── Left panel layout ─────────────────────────────────────────────
        # Fixed top strip  : mode icon + Reorder row + Cull List row
        # Sash             : Collections listbox (always visible)
        # Sash             : Folder tree (scrollable)
        # The sash between Collections and Folders lets the user resize.

        # ── Top strip — mode label, Reorder, Cull ────────────────────────
        top_strip = tk.Frame(tf, bg=BG2)
        top_strip.pack(side='top', fill='x')

        _hdr_fg = '#1155aa'
        _act_bg = '#c0d0e8'
        _coll_tag_fg = '#227722'

        cfg = self.mode_cfg
        self.lbl_tree_header = tk.Label(top_strip,
            text=f"  {cfg['icon']}  {cfg['label']}",
            bg=BG2, fg=_hdr_fg,
            font=('Segoe UI',11,'bold'), anchor='w', padx=6, pady=4)
        self.lbl_tree_header.pack(fill='x')

        tk.Frame(top_strip, bg=HOVER_BD, height=1).pack(fill='x')

        self.lbl_reorder = tk.Label(top_strip,
            text='  ⇅  Sort',
            bg=BG2, fg=TEXT_DIM,
            font=('Segoe UI',9,'bold'), anchor='w', cursor='hand2', padx=6, pady=3)
        self.lbl_reorder.pack(fill='x')
        self.lbl_reorder.bind('<Button-1>', lambda e: __import__('tkinter.messagebox',fromlist=['x']).showinfo('Sort','Sort is disabled in this version — use 2-panel Reorder.',parent=self.win))

        cull_row_frame = tk.Frame(top_strip, bg=BG2)
        cull_row_frame.pack(fill='x')
        self.lbl_cull_row = tk.Label(cull_row_frame,
            text='  🗑  Deletion List  (0)',
            bg=BG2, fg=TEXT_DIM,
            font=('Segoe UI',9,'bold'), anchor='w', cursor='hand2', padx=6, pady=3)
        self.lbl_cull_row.pack(side='left', fill='x', expand=True)
        self.lbl_cull_row.bind('<Button-1>', lambda e: self._on_left_cull_click())
        # Delete Marked button — lives here, shown/hidden by _show/_hide_delete_popup
        self._delete_popup_parent = cull_row_frame

        tk.Frame(top_strip, bg=HOVER_BD, height=1).pack(fill='x')

        # ── Collections + Folder tree in a vertical PanedWindow ───────────
        left_paned = tk.PanedWindow(tf, orient='vertical', bg=BG,
                                    sashwidth=6, sashrelief='raised',
                                    handlesize=10, relief='flat', bd=0)
        left_paned.pack(fill='both', expand=True)

        # ── Collections panel ─────────────────────────────────────────────
        coll_frame = tk.Frame(left_paned, bg=BG)
        left_paned.add(coll_frame, minsize=40, height=320, stretch='never')

        coll_hdr = tk.Frame(coll_frame, bg='#223322', height=22)
        coll_hdr.pack(fill='x'); coll_hdr.pack_propagate(False)
        tk.Label(coll_hdr, text='  Collections', bg='#223322', fg='white',
                 font=('Segoe UI',9,'bold'), anchor='w').pack(side='left', fill='x')

        coll_sb = tk.Scrollbar(coll_frame, orient='vertical', bg=BG)
        coll_sb.pack(side='right', fill='y')
        self.coll_listbox = tk.Listbox(coll_frame,
            yscrollcommand=coll_sb.set,
            bg=BG, fg=_coll_tag_fg,
            font=('Segoe UI',9,'bold'),
            selectbackground=TREE_SEL_BG,
            selectforeground='white',
            activestyle='none',
            exportselection=False,
            relief='flat', bd=0, highlightthickness=0)
        coll_sb.config(command=self.coll_listbox.yview)
        self.coll_listbox.pack(fill='both', expand=True)
        self.coll_listbox.bind('<<ListboxSelect>>', self._on_coll_listbox_select)
        def _coll_wheel(e):
            self.coll_listbox.yview_scroll(-1 if e.delta > 0 else 1, 'units')
            return "break"
        self.coll_listbox.bind('<MouseWheel>', _coll_wheel)
        self.coll_listbox.bind('<Button-4>',
            lambda e: (self.coll_listbox.yview_scroll(-1, 'units'), "break")[1])
        self.coll_listbox.bind('<Button-5>',
            lambda e: (self.coll_listbox.yview_scroll(1, 'units'), "break")[1])

        # ── Folder tree — FileTaggerTree from FTWidgets ──────────────────────
        self._tree_widget = FileTaggerTree(
            left_paned,
            extensions=self.mode_cfg['exts'],
            get_tagged_count=self._count_tagged,
            get_thumb_count=self._count_thumbs,
            on_select=None,
            on_delete_folder=self._on_tree_folder_deleted,
            on_folders_changed=self._schedule_tree_refresh,
            show_root_entry=False,
            bg=BG
        )
        left_paned.add(self._tree_widget, minsize=200,
                       width=312, stretch='always')  # 390 screen px / 1.25 DPI scale

        # Point self.tree at the underlying ttk.Treeview
        self.tree = self._tree_widget.tree()
        self.lbl_col_head = 'JPGs'

        # Header bar with Folders label and refresh button
        tree_inner = self._tree_widget._tree.master
        tree_hdr = tk.Frame(self._tree_widget, bg="#1a3a5c", height=26)
        tree_hdr.pack(fill='x', before=tree_inner)
        tree_hdr.pack_propagate(False)
        tk.Label(tree_hdr, text=f"  {cfg['icon']}  Folders", bg="#1a3a5c", fg='white',
                 font=('Segoe UI', 9, 'bold'), anchor='w').pack(side='left', fill='x', expand=True)
        tk.Button(tree_hdr, text="\u27f3", bg="#1a3a5c", fg='white',
                  font=('Segoe UI', 10, 'bold'), relief='flat', bd=0,
                  cursor='hand2', activebackground='#2a5a8a', activeforeground='white',
                  command=self._refresh_tree).pack(side='right', padx=4)

        def _tree_wheel(e):
            self.tree.yview_scroll(-1 if (e.delta > 0 or e.num == 4) else 1, 'units')
            return "break"
        self.tree.bind('<MouseWheel>', _tree_wheel)
        self.tree.bind('<Button-4>',   _tree_wheel)
        self.tree.bind('<Button-5>',   _tree_wheel)
        self.tree.bind('<<TreeviewSelect>>', self._on_tree_select,  add='+')
        self.tree.bind('<<TreeviewOpen>>',   self._on_tree_open,    add='+')
        self.tree.bind('<<TreeviewClose>>',  self._on_tree_close,   add='+')
        self.tree.bind('<Button-3>',
            lambda e: self._tree_widget._handle_right_click(e), add='+')

        _view_bg = '#d0d8e8'
        self.tree.tag_configure('tagged',      foreground='#111111', font=('Segoe UI', 9))
        self.tree.tag_configure('cached_full', foreground='#111111', font=('Segoe UI', 9))
        self.tree.tag_configure('cached_part', foreground='#111111', font=('Segoe UI', 9))
        self.tree.tag_configure('passthrough', foreground='#0055cc', font=('Segoe UI', 9, 'bold'))
        self.tree.tag_configure('has_files',   foreground='#0055cc', font=('Segoe UI', 9, 'bold'))
        self.tree.tag_configure('viewing',     background=_view_bg,  font=('Segoe UI', 9, 'bold'))

        right = tk.Frame(paned, bg=BG)
        paned.add(right, minsize=200, stretch="always")

        # ── View status bar — top of right panel ──────────────────────────────
        _vs_bg = "#c8d8c8"
        _vs_fg = "#111111"
        view_status_bar = tk.Frame(right, bg=_vs_bg, height=28)
        view_status_bar.pack(side="top", fill="x")
        # pack_propagate left enabled so bar can accommodate all buttons

        # Left info label
        self.lbl_view_status = tk.Label(view_status_bar,
            text="📁  Select a folder to begin",
            bg=_vs_bg, fg=_vs_fg,
            font=("Segoe UI", 10, "bold"), anchor="center", padx=12)
        self.lbl_view_status.pack(side="left", fill="y")  # no expand — buttons placed on top
        # Back to Summary — only visible during group review
        self._btn_back_group = tk.Button(view_status_bar,
            text="◄ Back to Summary", bg="#3a3a6a", fg="white",
            font=("Segoe UI", 9, "bold"), relief="flat", padx=10,
            cursor="hand2", command=self._exit_group_review)
        # Not packed until _open_group_review activates it

        _mbtn = dict(fg="white", font=("Segoe UI", 9, "bold"),
                     relief="flat", padx=0, pady=0, cursor="hand2", bd=0,
                     bg="#111111", activebackground="#333333",
                     disabledforeground="#555555")
        # Move Right — left of divider
        self._btn_move_right = tk.Button(view_status_bar, text="Select ❯",
                  command=self._two_panel_move_right, state="disabled", **_mbtn)
        # Divider and buttons positioned via place() in _sync_status_divider
        self._status_divider = tk.Frame(view_status_bar, bg="#888888", width=2)

        # Deselect — right of divider (removes from right panel)
        self._btn_deselect = tk.Button(view_status_bar, text="Deselect",
                  command=self._two_panel_deselect, state="disabled", **_mbtn)
        # Reorder — right of Deselect (reorders within right panel)
        self._btn_reorder = tk.Button(view_status_bar, text="Reorder",
                  command=self._two_panel_reorder, state="disabled", **_mbtn)
        # Save Order — right of Reorder (saves _placed order to active collection)
        self._btn_save_order = tk.Button(view_status_bar, text="💾 Save Order",
                  command=self._two_panel_save_order, state="disabled", **_mbtn)
        # Right panel / selection count label
        self._lbl_right_status = tk.Label(view_status_bar,
            text="", bg=_vs_bg, fg=_vs_fg,
            font=("Segoe UI", 10, "bold"), anchor="center", padx=12)

        # 1-panel selected count — shown when selection > 0 in 1-panel mode
        self._lbl_sel_count = tk.Label(view_status_bar,
            text="", bg=_vs_bg, fg="#000000",
            font=("Segoe UI", 10, "bold"), anchor="e", padx=12)
        self._lbl_sel_count.pack(side="right")
        nav = tk.Frame(right, bg=BG3, height=56)
        nav.pack(side="bottom", fill="x")
        nav.pack_propagate(False)

        tk.Label(nav, text=BUILD_DATE, bg=BG3, fg=TEXT_BRIGHT,
                 font=("Segoe UI",10), padx=8).pack(side="left")
        tk.Frame(nav, bg=HOVER_BD, width=1).pack(side="left", fill="y", padx=2, pady=4)

        self.lbl_sb_folder  = tk.Label(nav, text="", bg=BG3, fg=TEXT_BRIGHT,
                                        font=("Segoe UI",10,"bold"), padx=4)
        self.lbl_sb_folder.pack(side="left")
        self.lbl_sb_fcounts = tk.Label(nav, text="", bg=BG3, fg=TEXT_BRIGHT,
                                        font=("Segoe UI",10), padx=2)
        self.lbl_sb_fcounts.pack(side="left")
        self.lbl_sb_tagged_local = tk.Label(nav, text="", bg=BG3, fg=TAGGED_BD,
                                             font=("Segoe UI",10), padx=2)
        self.lbl_sb_tagged_local.pack(side="left")
        self.lbl_sb_culled_local = tk.Label(nav, text="", bg=BG3, fg=CULLED_BD,
                                             font=("Segoe UI",10), padx=2)
        self.lbl_sb_culled_local.pack(side="left")
        tk.Frame(nav, bg=HOVER_BD, width=1).pack(side="left", fill="y", padx=4, pady=4)
        tk.Label(nav, text="Collection:", bg=BG3, fg=TEXT_BRIGHT,
                 font=("Segoe UI",10,"bold"), padx=4).pack(side="left")
        self.lbl_sb_coll = tk.Label(nav, text="", bg=BG3, fg=TEXT_BRIGHT,
                                     font=("Segoe UI",10), padx=2)
        self.lbl_sb_coll.pack(side="left")

        self.status = tk.Label(nav, text="", bg=BG3, fg=TEXT_DIM,
                               font=("Segoe UI",10), padx=8, anchor="e")
        self.status.pack(side="right", fill="x", expand=True)

        # ── Inline navigation bar — centred in nav, sits below status text ────
        _nav_bg = "#1a2a3a"
        nav_inner = tk.Frame(nav, bg=_nav_bg)
        nav_inner.place(relx=0.5, rely=1.0, anchor="s", y=-4)
        self._nav_inner = nav_inner

        _btn_cfg = dict(bg=_nav_bg, fg="white", font=("Segoe UI",9,"bold"),
                        relief="flat", padx=8, pady=2, cursor="hand2",
                        activebackground="#2a3a5a", activeforeground="white")
        tk.Button(nav_inner, text="<< First",
                  command=lambda: (setattr(self,'_page_num',0), self._show_page()),
                  **_btn_cfg).pack(side="left", padx=(0,1))
        tk.Button(nav_inner, text="< Prev",
                  command=self._page_prev, **_btn_cfg).pack(side="left", padx=1)

        self.lbl_nav = tk.Label(nav_inner, text="", bg=_nav_bg, fg="#aaccff",
                                font=("Segoe UI",9,"bold"), anchor="center", width=24)
        self.lbl_nav.pack(side="left", padx=(6,2))

        self._jump_entry = tk.Entry(nav_inner, textvariable=self._jump_var,
                                    width=5, font=("Segoe UI",9), justify="center",
                                    bg="#2a3a5a", fg="white", insertbackground="white",
                                    relief="flat", highlightthickness=1,
                                    highlightbackground="#4a6a9a")
        self._jump_entry.pack(side="left", padx=(0,6))
        self._jump_entry.bind("<Return>", lambda e: self._jump_to_image())

        tk.Button(nav_inner, text="Next >",
                  command=self._page_next, **_btn_cfg).pack(side="left", padx=1)
        tk.Button(nav_inner, text="Last >>",
                  command=self._page_last, **_btn_cfg).pack(side="left", padx=(1,2))

        # No floating Toplevel needed
        self._nav_float = None
        self.win.after(200, self._update_nav_label)

        # Position bar sits just above the thumbnail canvas
        self._posbar = tk.Canvas(right, height=8, bg=BG2,
                                 highlightthickness=0, cursor="hand2")
        self._posbar.pack(side="bottom", fill="x")
        self._posbar.bind("<Button-1>",  self._on_posbar_click)
        self._posbar.bind("<B1-Motion>", self._on_posbar_click)
        self._posbar_indicator = None

        # ── Grid area: PanedWindow holds left (main) and right (selection) panels ──
        # In 1-panel mode only left_frame is visible and fills the space.
        # In 2-panel mode both panels are shown side by side at 120px thumb size.
        self._grid_paned = tk.PanedWindow(right, orient="horizontal", bg=BG,
                                          sashwidth=6, sashrelief="flat",
                                          handlesize=0)
        self._grid_paned.pack(fill="both", expand=True)
        # Sync status bar divider position to sash whenever paned window geometry changes
        self._grid_paned.bind("<Configure>", lambda e: self.win.after(10, self._sync_status_divider))
        self._grid_paned.bind("<B1-Motion>", lambda e: self.win.after(10, self._sync_status_divider))

        # ── Left panel (main grid — always visible) ──
        self._left_frame = tk.Frame(self._grid_paned, bg=BG)
        self._grid_paned.add(self._left_frame, stretch="always", minsize=200)

        self.canvas = tk.Canvas(self._left_frame, bg=BG, highlightthickness=0)
        vsb2 = tk.Scrollbar(self._left_frame, orient="vertical")
        vsb2.pack(side="right", fill="y")
        self.canvas.pack(fill="both", expand=True)
        self.grid_frame = tk.Frame(self.canvas, bg=BG)
        self._cw = self.canvas.create_window(0, 0, anchor="nw", window=self.grid_frame)
        self.grid_frame.bind("<Configure>", self._on_frame_cfg)
        self.canvas.bind("<Configure>",     self._on_canvas_cfg)
        self.win.bind("<space>", self._on_spacebar)
        self.vsb2 = vsb2

        # ── Scrollbar wiring ──────────────────────────────────────────────────
        # The grid is PAGED — canvas.yview only covers within-page content.
        # vsb2 represents position across ALL files (all pages).
        # Clicking/dragging vsb2 triggers a page jump; within-page canvas
        # scroll is handled by canvas.yview normally.
        #
        # Strategy:
        #   • canvas.yscrollcommand = _canvas_scroll_cb  — keeps vsb2 thumb in
        #     sync with within-page scroll position, offset by page fraction
        #   • vsb2.command = _vsb_cmd  — translates scroll fraction to page jump
        #     + within-page yview_moveto

        _vsb_after = [None]

        def _canvas_scroll_cb(lo_str, hi_str):
            """Called by canvas whenever its yview changes.
            Translate within-page [0,1] fractions to whole-collection fractions
            so the scrollbar thumb reflects true position across all files."""
            try:
                total     = len(self._all_files)
                page_size = max(1, getattr(self, '_page_size', 40))
                n_pages   = max(1, (total + page_size - 1) // page_size)
                page      = self._page_num
                lo_page   = page / n_pages
                hi_page   = (page + 1) / n_pages
                span      = hi_page - lo_page
                lo = lo_page + float(lo_str) * span
                hi = lo_page + float(hi_str) * span
                vsb2.set(lo, min(1.0, hi))
            except Exception:
                pass

        def _vsb_cmd(*args):
            """Called when user interacts with vsb2 (click track, drag thumb,
            arrow buttons).  Translates to page jump + within-page scroll."""
            try:
                total     = len(self._all_files)
                if total == 0: return
                page_size = max(1, getattr(self, '_page_size', 40))
                n_pages   = max(1, (total + page_size - 1) // page_size)
                action = args[0]
                if action == "moveto":
                    frac     = max(0.0, min(1.0, float(args[1])))
                    new_page = max(0, min(int(frac * n_pages), n_pages - 1))
                    # Within-page fraction: where in this page does frac land?
                    page_frac = (frac * n_pages) - new_page
                    if _vsb_after[0]:
                        self.win.after_cancel(_vsb_after[0])
                    def _do_jump(p=new_page, pf=page_frac):
                        if p != self._page_num:
                            self._page_num = p
                            self._show_page()
                            self.win.after(150, lambda: self.canvas.yview_moveto(pf))
                        else:
                            self.canvas.yview_moveto(pf)
                    _vsb_after[0] = self.win.after(80, _do_jump)
                elif action == "scroll":
                    amount = int(args[1]); unit = args[2]
                    if unit == "pages":
                        for _ in range(abs(amount)):
                            if amount > 0: self._page_next()
                            else:          self._page_prev()
                    else:  # units
                        for _ in range(abs(amount)):
                            if amount > 0: self._scroll_rows(1)
                            else:          self._scroll_rows(-1)
            except Exception as ex:
                print(f"vsb cmd error: {ex}")

        vsb2.config(command=_vsb_cmd)
        self.canvas.configure(yscrollcommand=_canvas_scroll_cb)
        vsb2.set(0.0, 1.0)

        # ── Mousewheel ────────────────────────────────────────────────────────
        # On Windows, <MouseWheel> goes to the focused widget, not the hovered one.
        # Strategy: bind_all always scrolls the main canvas.
        # Widgets that need their own scroll (tree, listbox) bind locally and
        # call event.widget.yview_scroll then return "break" to stop propagation.

        def _on_wheel(e):
            delta = getattr(e, 'delta', 0)
            if e.num == 4 or delta > 0:
                self._scroll_rows(-1)
            else:
                self._scroll_rows(1)

        self.win.bind_all("<MouseWheel>", _on_wheel)
        self.win.bind_all("<Button-4>",   _on_wheel)
        self.win.bind_all("<Button-5>",   _on_wheel)

        # ── Right panel (shown in 2-panel mode only) ──
        self._right_frame = tk.Frame(self._grid_paned, bg=BG)
        # Not added to paned yet

        # ── Right panel scrollable canvas ──
        self._right_canvas = tk.Canvas(self._right_frame, bg=BG, highlightthickness=0)
        _rvsb = tk.Scrollbar(self._right_frame, orient="vertical",
                             command=self._right_canvas.yview)
        _rvsb.pack(side="right", fill="y")
        self._right_canvas.pack(fill="both", expand=True)
        self._right_canvas.configure(yscrollcommand=_rvsb.set)
        self._right_grid = tk.Frame(self._right_canvas, bg=BG)
        self._right_cw = self._right_canvas.create_window(
            0, 0, anchor="nw", window=self._right_grid)
        self._right_grid.bind("<Configure>",
            lambda e: self._right_canvas.configure(
                scrollregion=self._right_canvas.bbox("all")))
        self._right_canvas.bind("<Configure>",
            lambda e: self._right_canvas.itemconfig(self._right_cw, width=e.width))

    # ── Floating toolbar infrastructure ───────────────────────────────────────

    def _btn(self, parent, text, bg, cmd, side="right", ref=None, tip=None):
        b = tk.Button(parent,text=text,bg=bg,fg="white",font=("Segoe UI",9,"bold"),
                      relief="flat",padx=6,pady=4,cursor="hand2",command=cmd,
                      activebackground="#333",activeforeground="white")
        b.pack(side=side,padx=2,pady=4)
        if ref: setattr(self,ref,b)
        if tip: _tip(b, tip)
        return b

    def _sep(self, parent):
        tk.Frame(parent,bg="#ccc",width=1).pack(side="left",fill="y",padx=3,pady=4)

    # ── Mode toggle ────────────────────────────────────────────────────────────
    def _mode_var_changed(self, *args):
        """Ensure changing the mode combobox actually switches the app mode.

        Some Windows/Tk combinations update the combobox text without reliably
        firing <<ComboboxSelected>>. This trace makes the displayed option and
        the internal mode stay in sync.
        """
        try:
            if getattr(self, "_mode_switching", False):
                return
            desired = "pdfs" if self._mode_var.get() == "PDFs" else "photos"
            if desired != self.mode:
                self.win.after_idle(self._toggle_mode)
        except Exception:
            pass

    def _toggle_mode(self):
        """Switch between Photos and PDFs and force the correct mode root.

        The combobox text alone is not enough: when mode changes, the active
        root must also be reset from PHOTOS_ROOTS/PDFS_ROOTS.  Otherwise the UI
        can say PDFs while the tree still points at the Photos root.
        """
        if getattr(self, "_mode_switching", False):
            return
        self._mode_switching = True
        try:
            sel = self._mode_var.get()
            new_mode = "pdfs" if sel == "PDFs" else "photos"
            if new_mode == self.mode:
                # Even if mode is already correct, force root combobox/tree sync.
                self.mode_cfg = _mode_cfg(self.mode)
                self._update_root_combobox()
                return

            if new_mode == "pdfs" and not HAVE_FITZ:
                if not messagebox.askyesno("PyMuPDF not installed",
                    "PDF thumbnails require PyMuPDF (not installed in this Python).\n\n"
                    "Install with:\n"
                    "  python.exe -m pip install pymupdf\n\n"
                    "Continue in PDF mode without thumbnails?",
                    parent=self.win):
                    self._mode_var.set("Photos")
                    return

            # Exit shadow mode with warning if active
            if self._shadow_active:
                self._exit_shadow()
                if self._shadow_active:
                    return  # user cancelled

            # Exit cull view if active
            if self._in_cull_view:
                self._in_cull_view = False

            self._save_current_collection()
            self.mode = new_mode

            # Force root to the first configured root for the selected mode.
            global PHOTOS_ROOT, PDFS_ROOT
            if self.mode == "photos":
                if PHOTOS_ROOTS:
                    PHOTOS_ROOT = PHOTOS_ROOTS[0][0]
                self.mode_cfg = _mode_cfg("photos")
                self._mode_var.set("Photos")
            else:
                if PDFS_ROOTS:
                    PDFS_ROOT = PDFS_ROOTS[0][0]
                self.mode_cfg = _mode_cfg("pdfs")
                self._mode_var.set("PDFs")

            # Close zoom window when switching modes
            if self._zoom_win:
                try:
                    if self._zoom_win.winfo_exists():
                        self._zoom_win.destroy()
                except: pass
                self._zoom_win = None

            cfg = self.mode_cfg
            try:
                if self.lbl_tree_header:
                    self.lbl_tree_header.config(text=f"  {cfg['icon']}  {cfg['label']}")
            except Exception: pass

            try:
                self.tree.heading("files", text=cfg['col_head'])
            except Exception: pass

            self.win.title(f"FileTagger — {cfg['label']}")
            self._clear_tree()
            self._clear_grid()
            self._update_root_combobox()
            self._init_mode()
        finally:
            self._mode_switching = False


    def _clear_tree(self):
        for item in self.tree.get_children(""):
            self.tree.delete(item)

    def _refresh_tree(self):
        """Repopulate the folder tree from the current root."""
        root = self.mode_cfg.get('root', '')
        if root and os.path.isdir(root):
            self._populate_tree(root)

    # ── Collections ────────────────────────────────────────────────────────────
    def _refresh_collection_list(self):
        root = self.mode_cfg['root']
        cols = _list_collections(root)
        try:
            if self.view_coll_combo:
                self.view_coll_combo['values'] = cols
        except Exception: pass
        try:
            if self.coll_combo and self.coll_combo is not self.view_coll_combo:
                self.coll_combo['values'] = cols
        except Exception: pass
        if self.collection in cols: self.coll_var.set(self.collection)
        elif cols: self.coll_var.set(cols[0])
        self._refresh_coll_listbox()

    def _update_coll_info(self):
        if self._shadow_active:
            n = len(self._shadow_tagged)
            try:
                if self.lbl_count:
                    self.lbl_count.config(text=f"◈ {n} shadow tagged" if n else "◈ Shadow (empty)")
            except: pass
        else:
            n = len(self.tagged)
            try:
                if self.lbl_coll_info:
                    self.lbl_coll_info.config(text=f"{n} file{'s' if n!=1 else ''} tagged" if n else "empty collection")
            except: pass
            try:
                if self.lbl_count:
                    self.lbl_count.config(text=f"{n} tagged" if n else "No files tagged")
            except: pass
            self._update_tagged_btn()
        self._update_statusbar()

    def _update_statusbar(self):
        """Refresh the permanent status bar — folder counts, tagged/culled local, collection."""
        try:
            folder  = getattr(self, 'current_folder', '')
            recurse = getattr(self, '_recurse', False)
            exts    = self.mode_cfg['exts']
            mode    = self.mode
            label   = self.mode_cfg['label']   # "Photos" or "PDFs"
            word    = self.mode_cfg['file_word']  # "image" or "PDF"

            # ── Folder name ───────────────────────────────────────────────────
            fname = os.path.basename(folder) if folder else ""
            self.lbl_sb_folder.config(text=fname)

            # ── File counts in folder (mode files + others) ───────────────────
            mode_n  = 0
            other_n = 0
            tagged_local = 0
            culled_local = 0
            scan_folders = []
            if folder and os.path.isdir(folder):
                if recurse:
                    for root, dirs, files in os.walk(folder):
                        dirs.sort()
                        scan_folders.append((root, files))
                else:
                    try:
                        entries = list(os.scandir(folder))
                        scan_folders.append((folder, [e.name for e in entries if e.is_file()]))
                    except: pass

            for sf, fnames in scan_folders:
                for fn in fnames:
                    fp = os.path.normpath(os.path.join(sf, fn))
                    ext = os.path.splitext(fn)[1].lower()
                    if ext in exts:
                        mode_n += 1
                        if fp in self.tagged:   tagged_local += 1
                        if fp in self._culled:  culled_local += 1
                    else:
                        other_n += 1

            # e.g. "Images(45) Other(22)" or "PDFs(12) Other(3)"
            type_label = "Images" if mode == "photos" else "PDFs"
            counts_str = f"{type_label}({mode_n})  Other({other_n})"
            self.lbl_sb_fcounts.config(text=counts_str)

            # Tagged/culled — only show if > 0
            self.lbl_sb_tagged_local.config(
                text=f"Tagged({tagged_local})" if tagged_local else "")
            self.lbl_sb_culled_local.config(
                text=f"Culled({culled_local})" if culled_local else "")

            # ── Collection ────────────────────────────────────────────────────
            coll_name = self.collection or ""
            coll_n    = len(self._shadow_tagged if self._shadow_active else self.tagged)
            self.lbl_sb_coll.config(
                text=f"{coll_name} ({coll_n})" if coll_name else "")

        except Exception as _e:
            pass  # never crash the UI for a status bar update

    def _update_tree_viewing(self):
        """Apply/remove the 'viewing' background tag on tree nodes."""
        try:
            folder  = os.path.normpath(getattr(self, 'current_folder', ''))
            recurse = getattr(self, '_recurse', False)
            exts    = self.mode_cfg['exts']

            def _set_viewing(iid, on):
                if not self.tree.exists(iid): return
                cur = list(self.tree.item(iid, "tags") or ())
                if on and "viewing" not in cur:
                    cur.append("viewing")
                elif not on and "viewing" in cur:
                    cur.remove("viewing")
                self.tree.item(iid, tags=tuple(cur))

            def _walk_tree(iid):
                """Recursively walk visible tree nodes."""
                if "__ph__" in iid: return
                node_path = os.path.normpath(iid)
                if recurse:
                    # Highlight current folder and all visible descendants that have files
                    is_descendant = (node_path == folder or
                                     node_path.lower().startswith(folder.lower() + os.sep))
                    has_files = False
                    if is_descendant:
                        try:
                            has_files = any(
                                os.path.splitext(e.name)[1].lower() in exts
                                for e in os.scandir(node_path) if e.is_file())
                        except: pass
                    _set_viewing(iid, is_descendant and has_files)
                else:
                    _set_viewing(iid, node_path == folder)
                for child in self.tree.get_children(iid):
                    _walk_tree(child)

            for root_iid in self.tree.get_children(""):
                _walk_tree(root_iid)
        except Exception as _e:
            pass

    def _save_current_collection(self):
        if self._shadow_active: return
        if self.collection:
            _write_collection(self.collection, self.mode_cfg['root'],
                              self.tagged, self.tagged_at, self.tagged_order)

    def _switch_collection(self, name, confirm=True):
        # Cancel any pending deferred save and flush it to the CURRENT (outgoing) collection NOW
        # before we change self.collection — prevents tags bleeding into the incoming collection
        if self._save_after_id:
            try: self.win.after_cancel(self._save_after_id)
            except: pass
            self._save_after_id = None
            self._save_current_collection()   # flush to outgoing collection
        if confirm and self.collection and name != self.collection:
            self._save_current_collection()
        if getattr(self, '_reorder_active', False): return
        self.collection = name
        data = _read_collection(name, self.mode_cfg['root'])
        self.tagged = set(data.keys()); self.tagged_at = dict(data)
        self.tagged_order = list(data.keys())
        self.coll_var.set(name)
        self._update_coll_info(); self._reload_visible_tags()
        self._refresh_tree_stats()   # update tagged counts for new collection
        self._status(f"Collection: {name}  ({len(self.tagged)} files)")

    def _reload_visible_tags(self):
        for item in self.thumb_widgets:
            cv, _, orig = item[0], item[1], item[2]
            self._cv_repaint(cv, *self._cell_colours(orig))

    def _new_collection(self):
        dlg = tk.Toplevel(self.win); dlg.title("New Collection")
        dlg.configure(bg=BG3); dlg.grab_set(); dlg.transient(self.win)
        self._centre_window(dlg,340,130)
        tk.Label(dlg,text="Collection name:",bg=BG3,fg=TEXT_BRIGHT,font=("Segoe UI",10)).pack(pady=(16,6))
        name_var = tk.StringVar()
        entry = tk.Entry(dlg,textvariable=name_var,font=("Segoe UI",11),bg=BG2,fg=TEXT_BRIGHT,
                         insertbackground=TEXT_BRIGHT,relief="flat",bd=1,
                         highlightthickness=1,highlightbackground="#555",width=28)
        entry.pack(padx=20); entry.focus_set()
        def on_ok():
            name = name_var.get().strip()
            for ch in r'\/:*?"<>|': name = name.replace(ch,'')
            name = name.strip()
            if not name: return
            root = self.mode_cfg['root']
            if name not in _list_collections(root):
                _write_collection(name,root,set(),{})
            dlg.destroy()
            # Cancel pending deferred save and flush to the OUTGOING collection first
            if self._save_after_id:
                try: self.win.after_cancel(self._save_after_id)
                except: pass
                self._save_after_id = None
            if self.collection: self._save_current_collection()
            self.collection = name
            self.tagged.clear(); self.tagged_at.clear(); self.tagged_order.clear()
            self._refresh_collection_list(); self._update_coll_info()
            self._reload_visible_tags(); self._refresh_tree_stats()
        entry.bind("<Return>",lambda e: on_ok())
        bf = tk.Frame(dlg,bg=BG3); bf.pack(pady=8)
        tk.Button(bf,text="  Create  ",bg=GREEN,fg="white",font=("Segoe UI",9,"bold"),
                  relief="flat",padx=8,cursor="hand2",command=on_ok).pack(side="left",padx=6)
        tk.Button(bf,text="  Cancel  ",bg="#444",fg=TEXT_BRIGHT,font=("Segoe UI",9),
                  relief="flat",padx=8,cursor="hand2",command=dlg.destroy).pack(side="left",padx=6)
        dlg.wait_window()

    def _rename_collection(self):
        if not self.collection: messagebox.showinfo("No collection","No active collection.", parent=self.win); return
        dlg = tk.Toplevel(self.win); dlg.title("Rename Collection")
        dlg.configure(bg=BG3); dlg.grab_set(); dlg.transient(self.win)
        self._centre_window(dlg,340,130)
        tk.Label(dlg,text="New name:",bg=BG3,fg=TEXT_BRIGHT,font=("Segoe UI",10)).pack(pady=(16,6))
        name_var = tk.StringVar(value=self.collection)
        entry = tk.Entry(dlg,textvariable=name_var,font=("Segoe UI",11),bg=BG2,fg=TEXT_BRIGHT,
                         insertbackground=TEXT_BRIGHT,relief="flat",bd=1,
                         highlightthickness=1,highlightbackground="#555",width=28)
        entry.pack(padx=20); entry.select_range(0,"end"); entry.focus_set()
        def on_ok():
            new_name = name_var.get().strip()
            for ch in r'\/:*?"<>|': new_name = new_name.replace(ch,'')
            new_name = new_name.strip()
            if not new_name or new_name==self.collection: dlg.destroy(); return
            root = self.mode_cfg['root']
            try:
                if _db_conn is not None:
                    _db_conn.execute("UPDATE collections SET name=? WHERE name=?", (new_name, self.collection))
                    _db_conn.commit()
                self.collection = new_name
                self._refresh_collection_list(); self._update_coll_info()
            except Exception as e: messagebox.showerror("Rename failed",str(e), parent=self.win)
            dlg.destroy()
        entry.bind("<Return>",lambda e: on_ok())
        bf = tk.Frame(dlg,bg=BG3); bf.pack(pady=8)
        tk.Button(bf,text="  Rename  ",bg=AMBER,fg="white",font=("Segoe UI",9,"bold"),
                  relief="flat",padx=8,cursor="hand2",command=on_ok).pack(side="left",padx=6)
        tk.Button(bf,text="  Cancel  ",bg="#444",fg=TEXT_BRIGHT,font=("Segoe UI",9),
                  relief="flat",padx=8,cursor="hand2",command=dlg.destroy).pack(side="left",padx=6)

    def _clone_collection(self):
        if not self.collection:
            messagebox.showinfo("No collection", "No active collection to clone.", parent=self.win); return
        dlg = tk.Toplevel(self.win); dlg.title("Clone Collection")
        dlg.configure(bg=BG3); dlg.grab_set(); dlg.transient(self.win)
        self._centre_window(dlg, 360, 160)
        tk.Label(dlg, text=f"Clone  \"{self.collection}\"  as:",
                 bg=BG3, fg=TEXT_BRIGHT, font=("Segoe UI",10)).pack(pady=(16,6))
        name_var = tk.StringVar(value=self.collection + " copy")
        entry = tk.Entry(dlg, textvariable=name_var, font=("Segoe UI",11),
                         bg=BG2, fg=TEXT_BRIGHT, insertbackground=TEXT_BRIGHT,
                         relief="flat", bd=1, highlightthickness=1,
                         highlightbackground="#555", width=28)
        entry.pack(padx=20)
        entry.select_range(0, "end"); entry.focus_set()
        def on_ok():
            new_name = name_var.get().strip()
            for ch in r'\/:*?"<>|': new_name = new_name.replace(ch, '')
            new_name = new_name.strip()
            if not new_name: return
            if new_name == self.collection:
                messagebox.showwarning("Same name", "Clone name must differ from original.", parent=dlg); return
            root = self.mode_cfg['root']
            if new_name in _list_collections(root):
                if not messagebox.askyesno("Overwrite?",
                        f'"{new_name}" already exists.\nOverwrite it?', parent=dlg): return
            # Save current collection first, then write clone
            self._save_current_collection()
            _write_collection(new_name, root, set(self.tagged), dict(self.tagged_at))
            dlg.destroy()
            # Switch to the clone so the user can immediately start subsetting
            self.collection = new_name
            self._refresh_collection_list()
            self._update_coll_info()
            self._status(f"Cloned to \"{new_name}\" — {len(self.tagged)} files")
        entry.bind("<Return>", lambda e: on_ok())
        bf = tk.Frame(dlg, bg=BG3); bf.pack(pady=10)
        tk.Button(bf, text="  Clone  ", bg="#226688", fg="white",
                  font=("Segoe UI",9,"bold"), relief="flat", padx=8,
                  cursor="hand2", command=on_ok).pack(side="left", padx=6)
        tk.Button(bf, text="  Cancel  ", bg="#444", fg=TEXT_BRIGHT,
                  font=("Segoe UI",9), relief="flat", padx=8,
                  cursor="hand2", command=dlg.destroy).pack(side="left", padx=6)
        dlg.wait_window()

    def _delete_collection(self):
        if not self.collection: messagebox.showinfo("No collection","No active collection.", parent=self.win); return
        if not messagebox.askyesno("Delete Collection",
            f"Delete collection '{self.collection}'?\n\nText file will be deleted. Files are not affected.", parent=self.win): return
        try:
            _delete_collection(self.collection)
        except Exception as e: messagebox.showerror("Delete failed",str(e), parent=self.win); return
        self.collection=""; self.tagged.clear(); self.tagged_at.clear()
        self._refresh_collection_list()
        root = self.mode_cfg['root']
        cols = _list_collections(root)
        if cols: self._switch_collection(cols[0],confirm=False)
        else:
            _write_collection("My Collection",root,set(),{})
            self._refresh_collection_list(); self._switch_collection("My Collection",confirm=False)
        self._update_coll_info()

    def _clear_collection(self):
        """Remove all files from current collection, keeping the collection name."""
        if not self.collection:
            messagebox.showinfo("No collection", "No active collection.", parent=self.win); return
        n = len(self.tagged)
        if n == 0:
            messagebox.showinfo("Already empty",
                f"Collection '{self.collection}' is already empty.", parent=self.win); return
        if not messagebox.askyesno("Clear Collection",
            f"Remove all {n} file{'s' if n!=1 else ''} from '{self.collection}'?\n\n"
            "The collection name is kept. Files on disk are not affected.",
            parent=self.win): return
        self.tagged.clear(); self.tagged_at.clear(); self.tagged_order.clear()
        _write_collection(self.collection, self.mode_cfg['root'], set(), {})
        self._reload_visible_tags()
        self._update_coll_info()
        self._update_tagged_btn()
        self._refresh_coll_listbox()
        # If currently viewing this collection, empty the grid
        if self._in_tagged_view:
            self._all_files = []
            self._page_num = 0
            self._show_page()
        self._status(f"Collection '{self.collection}' cleared.")

    # ── Folder tree ────────────────────────────────────────────────────────────
    def _folder_stats(self, path):
        exts = self.mode_cfg['exts']
        path = os.path.normpath(path)
        try:
            entries = [e.name for e in os.scandir(path)
                       if e.is_file() and os.path.splitext(e.name)[1].lower() in exts]
        except PermissionError: return ("-","-","","")
        file_n   = len(entries)
        tagged_n = sum(1 for name in entries
                       if os.path.normpath(os.path.join(path, name)) in self.tagged)
        file_str   = str(file_n)   if file_n   else ""
        tagged_str = str(tagged_n) if tagged_n else ""
        cached_str = ""; cache_tag = ""
        if file_n > 0:
            if _db_conn is not None:
                row = _db_conn.execute(
                    "SELECT COUNT(*) FROM thumbnails WHERE path LIKE ?",
                    (os.path.normpath(path) + os.sep + '%',)
                ).fetchone()
                thumb_n = row[0] if row else 0
                if thumb_n > 0:
                    cached_str = str(thumb_n)
                    cache_tag  = "cached_full" if thumb_n >= file_n else "cached_part"
        return (file_str, tagged_str, cached_str, cache_tag)

    def _count_tagged(self, path):
        """Callback for FileTaggerTree — count tagged files directly in path."""
        path = os.path.normpath(path)
        exts = self.mode_cfg['exts']
        try:
            return sum(
                1 for e in os.scandir(path)
                if e.is_file()
                and os.path.splitext(e.name)[1].lower() in exts
                and os.path.normpath(e.path) in self.tagged
            )
        except Exception:
            return 0

    def _count_thumbs(self, path):
        """Callback for FileTaggerTree — count thumbnails cached in DB for path."""
        path = os.path.normpath(path)
        if _db_conn is None:
            return 0
        try:
            row = _db_conn.execute(
                "SELECT COUNT(*) FROM thumbnails WHERE path LIKE ?",
                (path + os.sep + '%',)
            ).fetchone()
            return row[0] if row else 0
        except Exception:
            return 0

    def _populate_tree(self, root_dir):
        root_dir = os.path.normpath(root_dir)
        # Update extensions for current mode before populating
        self._tree_widget._extensions = self.mode_cfg['exts']
        self._tree_widget.set_root(root_dir)
        # Focus root in tree but don't select — prevents auto-loading on mode switch
        try: self.tree.focus(root_dir)
        except: pass

    def _clear_tree(self):
        for item in self.tree.get_children(""): self.tree.delete(item)


    def _has_subdirs(self, path):
        try: return any(e.is_dir() for e in os.scandir(path))
        except: return False

    def _has_files_below(self, path):
        """Lazily check if any descendant folder contains files of the current mode.
        Stops at the first file found — does not count all files."""
        exts = self.mode_cfg['exts']
        try:
            for entry in os.scandir(path):
                if entry.is_file() and os.path.splitext(entry.name)[1].lower() in exts:
                    return True
                if entry.is_dir():
                    if self._has_files_below(entry.path):
                        return True
        except: pass
        return False

    def _folder_tags(self, path, stats):
        """Return tree tags: blue if folder has relevant files at any depth, black otherwise."""
        tags = []
        # Blue if has direct files or files somewhere below
        if stats[0] != "-" and stats[0] != "0":
            tags.append("has_files")   # has direct files
        elif self._has_files_below(path):
            tags.append("passthrough") # has files in subfolders only
        return tuple(tags)

    def _on_tree_folder_deleted(self, folder_path):
        """Called by FolderTreeWidget after a folder is deleted."""
        if getattr(self, 'current_folder', '') == folder_path:
            self._clear_grid()
            self._show_browse_prompt()

    def _collapse_node(self, iid):
        try:
            for child in self.tree.get_children(iid):
                self.tree.delete(child)
            if self._has_subdirs(iid):
                self.tree.insert(iid, "end", iid=iid + "/__ph__", text="")
            # Action lock: block spurious <<TreeviewOpen>> for 200ms after collapse
            self._closing_nodes.add(iid)
            self.win.after(200, lambda i=iid: self._closing_nodes.discard(i))
        except Exception: pass

    def _on_tree_open(self, event):
        self.tree.after(1, self._do_tree_open)

    def _on_tree_close(self, event):
        try:
            iid = self.tree.identify_row(event.y) if hasattr(event, 'y') else self.tree.focus()
            if not iid or "__ph__" in iid: return
            self._collapse_node(iid)
        except: pass

    def _is_visible(self, iid):
        """Return True only if iid and all its ancestors are open."""
        parent = self.tree.parent(iid)
        while parent:
            if not self.tree.item(parent, "open"):
                return False
            parent = self.tree.parent(parent)
        return True

    def _do_tree_open(self):
        def _check(iid):
            if "__ph__" in iid: return
            if not self.tree.item(iid, "open"): return
            if not self._is_visible(iid): return
            # Action lock: skip if this node was just collapsed
            if iid in self._closing_nodes: return
            ch = self.tree.get_children(iid)
            phs = [c for c in ch if c.endswith("/__ph__")]
            if phs and len(ch) == len(phs):
                for ph in phs: self.tree.delete(ph)
                self._tree_widget._on_node_open(iid); return
            for child in ch: _check(child)
        for iid in self.tree.get_children(""): _check(iid)
        self._update_tree_viewing()

    def _on_tree_button1(self, event):
        """Record whether the click was on the disclosure arrow — if so, suppress navigation."""
        try:
            region = self.tree.identify_region(event.x, event.y)
            self._tree_chevron_click = (region == "tree")
            if self._tree_chevron_click:
                # Clear flag after events have all fired
                self.win.after(250, self._clear_chevron_flag)
        except Exception:
            self._tree_chevron_click = False

    def _clear_chevron_flag(self):
        self._tree_chevron_click = False

    def _2panel_nav_guard(self):
        """If in 2-panel mode with items in right panel, warn user.
        Returns True if navigation should proceed, False if cancelled."""
        if self._panel_mode != "2":
            return True
        if not self._placed:
            # Nothing in right panel — just reset it silently
            self._placed = []
            self._placed_set = set()
            self._right_sel.clear()
            self._ins_point = None
            self._page_num = 0
            return True
        # Items in right panel — warn
        resp = messagebox.askyesno(
            "Leave 2-Panel?",
            f"You have {len(self._placed)} file{'s' if len(self._placed)!=1 else ''} "
            f"selected in the right panel.\n\n"
            "Navigating to a new folder or collection will clear the selection.\n\n"
            "Proceed?",
            parent=self.win)
        if resp:
            self._placed = []
            self._placed_set = set()
            self._right_sel.clear()
            self._ins_point = None
            self._page_num = 0
            self._two_panel_build_right()  # clear right panel display
        return resp

    def _on_tree_select(self, event):
        try:
            # If this node is being collapsed, don't navigate
            sel = self.tree.selection()
            if not sel: return
            path = sel[0]
            if path in self._closing_nodes: return
            if "__ph__" in path: return
            if not os.path.isdir(path): return
            if getattr(self, '_move_mode', False):
                self._sel_move_target_chosen(os.path.normpath(path))
                return
            if not self._2panel_nav_guard(): return
            self._in_tagged_view = False; self._update_tagged_btn()
            self._set_view_radio("folder")
            stats = self._folder_stats(path)
            already_open = self.tree.item(path, "open")
            collapsing   = path in self._closing_nodes
            if stats[0] == "-" and not already_open and not collapsing and self._has_files_below(path):
                first = self._find_first_populated(path)
                if first and first != path:
                    self._expand_tree_to(first)
                    self.tree.selection_set(first)
                    self.tree.focus(first)
                    self.tree.see(first)
                    self._load_folder(first)
                    return
            if stats[0] == "-" and already_open and not collapsing:
                self._tree_widget.collapse_node(path)
                return
            self._in_tagged_view = False
            self._in_collection_view = False if hasattr(self, '_in_collection_view') else False
            # Clear collection listbox highlight — folder and collection should never both be highlighted
            try:
                self.coll_listbox.unbind('<<ListboxSelect>>')
                self.coll_listbox.selection_clear(0, 'end')
                self.coll_listbox.bind('<<ListboxSelect>>', self._on_coll_listbox_select)
            except Exception: pass
            self._clear_grid()
            self._load_folder(path)
            self.win.after(200, lambda p=path: self._status_folder_counts(p))
        except Exception as _e:
            import traceback; self._status(f"Tree error: {_e} {traceback.format_exc()[:200]}")

    def _status_folder_counts(self, path):
        """Append own + children file counts to status bar — calculated asynchronously."""
        exts = self.mode_cfg['exts']
        current = self.status.cget("text")
        def _count():
            try:
                own = sum(
                    1 for e in os.scandir(path)
                    if e.is_file() and os.path.splitext(e.name)[1].lower() in exts
                )
                children = 0
                for root, dirs, files in os.walk(path):
                    if root == path: continue
                    children += sum(
                        1 for f in files
                        if os.path.splitext(f)[1].lower() in exts
                    )
                msg = self.status.cget("text")
                self.win.after(0, lambda: self._status(
                    f"{msg}   |   This folder: {own}   Sub-folders: {children}"
                ))
            except Exception:
                pass
        threading.Thread(target=_count, daemon=True).start()

    def _find_first_populated(self, path):
        """BFS to find the first folder that has direct files of the current mode."""
        exts = self.mode_cfg['exts']
        from collections import deque
        queue = deque([path])
        while queue:
            current = queue.popleft()
            try:
                entries = list(os.scandir(current))
                for e in entries:
                    if e.is_file() and os.path.splitext(e.name)[1].lower() in exts:
                        return current   # found files here
                # No files here — queue subdirectories
                for e in sorted(entries, key=lambda e: e.name.lower()):
                    if e.is_dir():
                        queue.append(e.path)
            except: pass
        return path   # fallback

    def _expand_tree_to(self, target_path):
        """Ensure all tree nodes from root down to target_path are expanded and populated."""
        target = os.path.normpath(target_path)
        root   = os.path.normpath(self.mode_cfg['root'])
        parts = []
        p = target
        while True:
            np = os.path.normpath(p)
            parts.append(np)
            if np.lower() == root.lower():
                break
            parent = os.path.normpath(os.path.dirname(np))
            if parent == np:
                break
            p = parent
        parts.reverse()
        for ancestor in parts:
            if not self.tree.exists(ancestor):
                break
            ch = self.tree.get_children(ancestor)
            phs = [c for c in ch if c.endswith("/__ph__")]
            if phs and len(ch) == len(phs):
                for ph in phs:
                    self.tree.delete(ph)
                self._tree_widget._on_node_open(ancestor)
            self.tree.item(ancestor, open=True)
        self.tree.update_idletasks()

    def _refresh_tree_stats(self):
        self._tree_widget.refresh_stats()

    def _update_tree_colours(self):
        try:
            tagged_folders = set()
            for path in self.tagged:
                tagged_folders.add(os.path.normpath(os.path.dirname(path)))
            def _colour(iid):
                if "__ph__" in iid: return
                folder = os.path.normpath(iid)
                has_tagged = any(tf==folder or tf.startswith(folder+os.sep) for tf in tagged_folders)
                try:
                    cur = [t for t in (self.tree.item(iid,"tags") or ())
                           if t in ("cached_full","cached_part","passthrough")]
                    if has_tagged: cur.append("tagged")
                    self.tree.item(iid,tags=tuple(cur))
                except: pass
                for child in self.tree.get_children(iid): _colour(child)
            for iid in self.tree.get_children(""): _colour(iid)
        except Exception as e: print(f"_update_tree_colours error: {e}")

    # ── Loading ────────────────────────────────────────────────────────────────
    def _load_folder(self, folder):
        folder = os.path.normpath(folder)
        # Flush any pending deferred save before changing folder
        if getattr(self, '_save_after_id', None):
            try: self.win.after_cancel(self._save_after_id)
            except: pass
            self._save_after_id = None
        self._save_current_collection()
        self.current_folder = folder
        self.lbl_folder.config(text=folder)
        self._hide_delete_popup()
        self._in_cull_view    = False
        self._in_tagged_view  = False
        self._in_similar_view  = False
        self._similar_groups   = {}
        self._in_group_summary = False
        self._group_clusters   = []
        try:
            if self.btn_similar: self.btn_similar.config(bg="#5a3a6a")
        except: pass
        exts = self.mode_cfg['exts']
        import re as _re
        _date_pat = _re.compile(r'^\d{4}-\d{2}-\d{2}')

        if self.mode == "photos":
            # Photos: simple filename ascending (case-insensitive)
            def _sort_key(path):
                return os.path.basename(path).lower()
        else:
            # PDFs: Scan* first, then dated newest-first, then mtime descending
            def _sort_key(path):
                fname = os.path.basename(path)
                if fname.lower().startswith('scan'):
                    return (0, fname.lower())
                m = _date_pat.match(fname)
                if m:
                    return (1, tuple(~ord(c) for c in m.group(0)))
                try:    mtime = os.path.getmtime(path)
                except: mtime = 0.0
                return (2, -mtime)

        files = []
        if self._recurse:
            all_paths = []
            for root,dirs,filenames in os.walk(folder):
                dirs.sort()
                for f in filenames:
                    if os.path.splitext(f)[1].lower() in exts:
                        all_paths.append(os.path.join(root, f))
            files = sorted(all_paths, key=_sort_key)
        else:
            try:
                entries = [e for e in os.scandir(folder)
                           if e.is_file() and os.path.splitext(e.name)[1].lower() in exts]
                files = sorted([e.path for e in entries], key=_sort_key)
            except PermissionError: self._status("Permission denied"); return
        if not files:
            # Try to find first populated child folder
            first = self._find_first_populated(folder)
            if first and first != folder:
                self._expand_tree_to(first)
                self.tree.selection_set(first)
                self.tree.focus(first)
                self.tree.see(first)
                self._load_folder(first)
                return
            # No images anywhere below — show centred message on grid
            self._clear_grid()
            self._show_no_images_message(folder)
            self._status(f"No {self.mode_cfg['file_word']} files in this folder or subfolders")
            return
        # No thumbnails yet — status bar message guides user to Generate button
        if _db_conn is not None:
            row = _db_conn.execute(
                "SELECT 1 FROM thumbnails WHERE path LIKE ? LIMIT 1",
                (os.path.normpath(folder) + '%',)
            ).fetchone()
            self._no_thumb_msg = (row is None)
        else:
            self._no_thumb_msg = True
        self._all_files = files; self._page_num = 0
        self._unfiltered_files = []
        self._selected.clear()       # new folder = new context, clear selection
        self._placed = []
        self._placed_set = set()
        self._right_sel.clear()
        self._ins_point = None
        self._last_click_idx = None
        self._update_sel_bar()
        self._update_statusbar()
        self._update_tree_viewing()
        self._update_folder_label()
        self._set_view_radio("folder")
        self._set_folder_label(folder, len(files))
        self._update_left_panel_highlight()
        self._show_page()

    def _compute_grid_dims(self):
        """Compute and store _disp_size and _cols from current canvas width/height."""
        self.canvas.update_idletasks()
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        canvas_ready = cw >= 10
        if not canvas_ready:
            cw = max(400, self.win.winfo_width() - TREE_LEFT_W - 30)
        if ch < 10: ch = max(400, self.win.winfo_height() - 120)

        scrollbar_w = 17
        avail_w = max(100, cw - scrollbar_w)
        avail_h = max(100, ch)

        # Both panels use THUMB_SIZE cells; 1-panel uses THUMB_SIZE
        sz = THUMB_SIZE
        # outer frame = sz+16, pad between cells = THUMB_PAD
        cell_w = sz + 16 + THUMB_PAD

        # In 2-panel mode always auto-compute cols (size changes, canvas width changes)
        # In 1-panel mode honour user's explicit column count if set
        max_cols = max(1, avail_w // cell_w)   # never show partial thumbs
        if self._user_cols == 0 or getattr(self, "_panel_mode", "1") == "2":
            cols = max_cols
            if getattr(self, "_panel_mode", "1") != "2":
                if canvas_ready:
                    self._user_cols = cols  # only store when canvas is properly measured
            self._updating_spinners = True
            try: self._cols_var.set(str(cols))
            except: pass
            finally: self._updating_spinners = False
        else:
            cols = min(max(1, self._user_cols), max_cols)

        # Cell height: image area (THUMB_IMG_H) + chrome strips + padding
        # 2-panel uses compact cells (full sz square + filename only)
        if getattr(self, "_panel_mode", "1") == "2":
            cell_h = sz + 24 + THUMB_PAD   # simple cell: image + filename only
        else:
            img_h  = THUMB_IMG_H if sz == THUMB_SIZE else int(sz * THUMB_IMG_H / THUMB_SIZE)
            cell_h = img_h + 70 + THUMB_PAD   # fixed image height + chrome
        rows = max(1, avail_h // cell_h)

        self._disp_size = sz
        self._cols      = cols
        self._page_size = cols * rows

        self._updating_spinners = True
        try:    self._cols_var.set(str(cols))
        except: pass
        finally: self._updating_spinners = False
        return cols, sz

    def _show_page(self):
        """Show current page of thumbnails."""
        self._loading = True
        self._load_gen += 1          # invalidate any in-flight renders from previous page
        # Selection persists across page turns — only cleared on folder/collection change
        self._compute_grid_dims()
        files = self._all_files
        if not files:
            self._clear_grid()
            self._status("No files")
            self._loading = False
            return
        total     = len(files)
        page_size = max(1, getattr(self, '_page_size', 40))
        cols      = max(1, self._cols)
        if hasattr(self, '_page_start_override') and self._page_start_override is not None:
            start = self._page_start_override
            self._page_start_override = None
        else:
            # Snap normal prev/next pages to row boundary for clean display
            raw_start = self._page_num * page_size
            start = (raw_start // cols) * cols
        start = max(0, min(start, total - 1))
        end       = min(start + page_size + cols, total)
        page      = files[start:end]
        self._clear_grid()
        self.canvas.yview_moveto(0)
        self._page_start = start  # store for _add_cell to compute relative position
        self._status(f"Loading {len(page)} of {total} {self.mode_cfg['file_word']}s...")
        gen = self._load_gen
        threading.Thread(
            target=self._load_page_thread,
            args=(page, start, gen),
            daemon=True
        ).start()

    def _load_page_thread(self, page, start_idx, gen):
        mode       = self.mode
        root       = self.mode_cfg['root']
        no_cache   = 0
        sz         = self._disp_size

        # Pre-cache file sizes and GPS coords so badges appear on first render
        for orig in page:
            _file_size_info_cached(orig)
            if self.mode == "photos":
                _get_gps_coords(orig)   # populates _gps_cache

        # Single blob read — one NAS round-trip for the whole page
        self.win.after(0, self._status, "Reading thumbnails...")
        jpeg_map = thumb_get_many(page)

        # Generate any missing thumbnails into memory
        new_items = []
        missing = [p for p in page if jpeg_map.get(p) is None]
        if missing and self._auto_cache:
            self.win.after(0, self._status, f"Generating {len(missing)} thumbnail{'s' if len(missing)!=1 else ''}...")
        for orig in page:
            if self._load_gen != gen: return
            if jpeg_map.get(orig) is None and self._auto_cache:
                try:
                    from PIL import ImageOps as _IOS
                    img = Image.open(_longpath(orig))
                    img = _IOS.exif_transpose(img)
                    img.thumbnail((THUMB_STORE_SIZE, THUMB_STORE_SIZE), Image.BILINEAR)
                    if img.mode != 'RGB': img = img.convert('RGB')
                    buf = _io.BytesIO()
                    img.save(buf, 'JPEG', quality=82, optimize=True)
                    jpeg_bytes = buf.getvalue()
                    jpeg_map[orig] = jpeg_bytes
                    new_items.append((orig, jpeg_bytes))
                except Exception:
                    jpeg_map[orig] = None

        # Write new thumbnails to blob in one shot
        if new_items:
            self.win.after(0, self._status, f"Saving {len(new_items)} thumbnail{'s' if len(new_items)!=1 else ''} to blob...")
            try:
                thumb_put_many(new_items)
            except Exception as _e:
                self.win.after(0, self._status, f"Blob write failed: {_e}")

        # Decode all images — every file gets a render entry, no skipping
        new_set = {p for p, _ in new_items}
        renders = []
        for i, orig in enumerate(page):
            if self._load_gen != gen: return
            jpeg = jpeg_map.get(orig)
            if jpeg:
                try:
                    ImageFile.LOAD_TRUNCATED_IMAGES = True
                    img = Image.open(_io.BytesIO(jpeg))
                    img.load()
                    ImageFile.LOAD_TRUNCATED_IMAGES = False
                    img = _scale_to_fit(img, sz)
                    renders.append((orig, start_idx + i, img, orig not in new_set, False))
                    continue
                except Exception:
                    pass
            # No cached thumbnail — open directly from disk.
            # Always try: auto_cache=off just means we don't save the result.
            # Try _longpath first (Windows long-path support), then plain path.
            img2 = None
            open_err = None
            for try_path in (_longpath(orig), orig):
                try:
                    ext = os.path.splitext(orig)[1].lower()
                    if ext in PDF_EXTS and HAVE_FITZ:
                        doc = fitz.open(try_path)
                        page0 = doc[0]
                        mat = fitz.Matrix(THUMB_SIZE / max(page0.rect.width, page0.rect.height),
                                          THUMB_SIZE / max(page0.rect.width, page0.rect.height))
                        pix = page0.get_pixmap(matrix=mat, alpha=False)
                        img2 = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                        doc.close()
                    else:
                        from PIL import ImageOps as _IOS2
                        ImageFile.LOAD_TRUNCATED_IMAGES = True
                        img2 = Image.open(try_path)
                        img2 = _IOS2.exif_transpose(img2)
                        img2.thumbnail((THUMB_STORE_SIZE, THUMB_STORE_SIZE), Image.BILINEAR)
                        if img2.mode != 'RGB': img2 = img2.convert('RGB')
                        ImageFile.LOAD_TRUNCATED_IMAGES = False
                    break   # success
                except Exception as _e:
                    open_err = _e
                    img2 = None
            if img2 is not None:
                img2 = _scale_to_fit(img2, sz)
                renders.append((orig, start_idx + i, img2, False, False))
                continue
            # Could not open — show grey placeholder, mark as unreadable (no console noise)
            try:
                if not os.path.exists(os.path.dirname(orig)):
                    self._unreadable_reason[orig] = "? FOLDER DELETED"
                elif not os.path.exists(orig):
                    self._unreadable_reason[orig] = "? FILE DELETED"
                else:
                    self._unreadable_reason[orig] = "? FILE UNREADABLE"
                self._unreadable.add(orig)
            except Exception: pass
            try:
                from PIL import Image as _PImg3
                ph = _PImg3.new('RGB', (sz, sz), (55, 55, 55))
                # Save grey placeholder to DB so it persists on next browse
                buf3 = _io.BytesIO()
                ph.save(buf3, 'JPEG', quality=60)
                new_items.append((orig, buf3.getvalue()))
                renders.append((orig, start_idx + i, _scale_to_fit(ph, sz), False, False))
            except Exception:
                no_cache += 1
                renders.append((orig, start_idx + i, None, False, False))

        # Single after() call renders all cells then signals completion
        def _render_all(renders=renders, gen=gen, no_cache=no_cache, new_items=new_items):
            if self._load_gen != gen: return
            try: self.grid_frame.pack_propagate(False)
            except: pass
            for orig, idx, img, from_cache, is_ghost in renders:
                if img is None:
                    try:
                        from PIL import Image as _PImg
                        col = (30, 0, 0) if is_ghost else (50, 50, 50)
                        img = _PImg.new('RGB', (self._disp_size, self._disp_size), col)
                    except: continue
                self._add_cell_from_img(orig, idx, img, from_cache, gen, is_ghost)
            try: self.grid_frame.pack_propagate(True)
            except: pass
            self._load_complete(gen, no_cache, 0, start_idx)
            # Only refresh tree tick if new thumbnails were written to blob
            if new_items:
                self.win.after(500, self._refresh_tree_stats)
        self.win.after(0, _render_all)

    def _render_one(self, orig, idx, img, from_cache, is_ghost, gen):
        """Called on main thread for each image as it arrives — pops in immediately."""
        if self._load_gen != gen: return
        self._add_cell_from_img(orig, idx, img, from_cache, gen, is_ghost)

    def _load_complete(self, gen, no_cache, ghosts, start_idx):
        """Called on main thread when loading thread finishes."""
        self._loading = False
        if self._load_gen != gen: return
        # Constrain scrollregion width to canvas width to prevent horizontal scrollbar
        self.grid_frame.update_idletasks()
        cw = max(100, self.canvas.winfo_width())
        # Compute height from actual grid geometry — more reliable than bbox
        # which can be 0 or stale before all cells are drawn
        cols   = max(1, self._cols)
        n_cells = len(self.thumb_widgets)
        rows   = max(1, (n_cells + cols - 1) // cols)
        row_h  = self._row_height_px()
        h_calc = rows * row_h + row_h // 2   # extra half-row so last row scrolls fully into view
        bb = self.canvas.bbox("all")
        h  = max(h_calc, bb[3] if bb else 0, 100)
        self.canvas.configure(scrollregion=(0, 0, cw, h))
        # Always scroll to top on page load
        self.canvas.yview_moveto(0.0)
        total   = len(self._all_files)
        n_shown = len(self.thumb_widgets)
        # Update nav label, posbar and scrollbar thumb
        self._update_nav_label()
        self._update_posbar()
        # vsb2 thumb position is updated automatically via canvas yscrollcommand callback
        # (no manual vsb2.set needed here)
        # Execute any pending jump now that all images are rendered
        pending = getattr(self, '_pending_jump', None)
        if pending:
            self._do_jump(pending)
        self._update_visible_label()
        parts = [f"{n_shown} of {total} {self.mode_cfg['file_word']}s loaded"]
        if ghosts: parts.append(f"{ghosts} deleted")
        self._status("   |   ".join(parts))
        if getattr(self, '_no_thumb_msg', False) and no_cache == total:
            self._status("Thumbnails speed up browsing — click Thumbs \u25b6 Generate to create them")
            self._no_thumb_msg = False

    def _display_page_results(self, results, gen, no_cache, ghosts):
        """Legacy entry point kept for compatibility — not used by new pop-in loader."""
        pass

    def _add_cell_from_img(self, orig, idx, img, from_cache, gen, ghost=False):
        try:
            sz = self._disp_size
            photo = _photo_cache_get(orig, sz)
            if photo is None:
                img_h = THUMB_IMG_H if sz == THUMB_SIZE else int(sz * THUMB_IMG_H / THUMB_SIZE)
                img.thumbnail((sz, img_h), Image.LANCZOS)
                photo = ImageTk.PhotoImage(img)
                _photo_cache_put(orig, sz, photo)
        except: return
        self._add_cell(orig, idx, photo, from_cache, gen, ghost)

    # ── Canvas-based cell ─────────────────────────────────────────────────────
    # One tk.Canvas per thumbnail replaces ~15 nested widgets.
    # State updates (tag/cull/select) use itemconfigure on named tags —
    # no widget tree walk needed.  thumb_widgets tuple is preserved unchanged.

    def _add_cell_2panel_left(self, orig, idx, photo, gen):
        """Left-panel cell for 2-panel mode — matches 1-panel layout and size."""
        if self._load_gen != gen: return
        page_start = getattr(self, '_page_start', 0)
        pos = idx - page_start
        if pos < 0: return
        col = pos % self._cols
        row = pos // self._cols

        sz      = self._disp_size
        IMG_H   = THUMB_IMG_H if sz == THUMB_SIZE else int(sz * THUMB_IMG_H / THUMB_SIZE)
        CELL_W  = sz + 16
        BD      = 3
        PAD     = 6
        IX      = PAD + 2
        IY      = PAD + 2
        CTL_Y   = IY + IMG_H
        CTL_H   = 18
        CAP_Y   = CTL_Y + CTL_H + 3
        STRIP_H = 15
        CAP_H   = 20
        sy      = CAP_Y + CAP_H       # size strip top
        CELL_H  = sy + STRIP_H + PAD

        is_placed   = orig in self._placed_set
        is_left_sel = (orig in self._selected) and not is_placed
        culled      = orig in self._culled
        bd_col      = SELECT_BD if is_left_sel else UNTAGGED_BD

        _, cat, disp_s = _file_size_info_cached(orig)
        size_info = f"{cat}  {disp_s}"
        if self.mode == "pdfs":
            pages, _ = _get_pdf_info(orig)
            if pages: size_info += f"  {pages}p"

        size_colours = {"Tiny":"#cc2200","V.Small":"#bb6600","Small":"#996600",
                        "Medium":"#336611","Large":"#1144aa","Huge":"#7722aa","?":"#555555"}
        fname = os.path.basename(orig)
        icon  = "📷 " if self.mode == "photos" else "📄 "

        cv = tk.Canvas(self.grid_frame, width=CELL_W, height=CELL_H,
                       bg=bd_col, highlightthickness=0, cursor="hand2")
        cv.grid(row=row, column=col, padx=THUMB_PAD // 2, pady=THUMB_PAD // 2)
        cv._photo = photo
        self._photo_refs.append(photo)

        # Background rectangles
        cv.create_rectangle(BD, BD, CELL_W - BD, CELL_H - BD,
                            fill=bd_col, outline="", tags="mid_rect")
        cv.create_rectangle(PAD, PAD, CELL_W - PAD, CELL_H - PAD,
                            fill=BG, outline="", tags="bg_rect")

        # Thumbnail image
        cv.create_image(IX + sz // 2, IY + IMG_H // 2,
                        anchor="center", image=photo, tags="thumb_img")

        # Sequence badge
        seq_num = idx + 1
        cv.create_rectangle(IX, IY, IX + 26, IY + 16, fill="#000000", outline="")
        cv.create_text(IX + 3, IY + 8, anchor="w", text=str(seq_num),
                       fill="white", font=("Segoe UI", 8, "bold"))

        # Scissors badge — top-right, double size of seq badge, launches FTEditI
        if self.mode == "photos":
            _sx0 = IX + sz - 52
            cv.create_rectangle(_sx0, IY, IX + sz, IY + 32, fill="#000000", outline="")
            cv.create_text(_sx0 + 26, IY + 16, anchor="center", text="✂",
                           fill="white", font=("Segoe UI", 16, "bold"),
                           tags="scissors_badge")

        # SELECTED watermark if in right panel
        if is_placed:
            WM_H = 20
            cv.create_rectangle(IX, IY + IMG_H // 2 - WM_H // 2,
                                IX + sz, IY + IMG_H // 2 + WM_H // 2,
                                fill="#000000", stipple="gray50", outline="", tags="sel_watermark_bg")
            cv.create_text(IX + sz // 2, IY + IMG_H // 2,
                           text="SELECTED", fill="white",
                           font=("Segoe UI", 9, "bold"), tags="sel_watermark")

        # DELETING watermark
        if culled:
            WM_H = 20
            cv.create_rectangle(IX, IY + IMG_H // 2 - WM_H // 2,
                                IX + sz, IY + IMG_H // 2 + WM_H // 2,
                                fill="#000000", stipple="gray50", outline="", tags="sel_watermark_bg")
            cv.create_text(IX + sz // 2, IY + IMG_H // 2,
                           text="DELETING", fill="#ffdd00",
                           font=("Segoe UI", 9, "bold"), tags="sel_watermark")

        # Controls row — Mark/Zoom (no rotation in 2-panel)
        _rm = sz // 2
        cv.create_rectangle(IX, CTL_Y, IX + sz, CTL_Y + CTL_H, fill="#e0e0e0", outline="")
        cv.create_line(IX + _rm, CTL_Y, IX + _rm, CTL_Y + CTL_H, fill="#bbbbbb")
        mark_text = "Unmark" if culled else "Mark"
        mark_fg   = "#994400" if culled else "#333333"
        cv.create_text(IX + _rm // 2, CTL_Y + CTL_H // 2, anchor="center",
                       text=mark_text, fill=mark_fg,
                       font=("Segoe UI", 8, "bold"), tags="mark_btn")
        cv.create_text(IX + _rm + _rm // 2, CTL_Y + CTL_H // 2, anchor="center",
                       text="Zoom", fill="#1a2a4a",
                       font=("Segoe UI", 8, "bold"), tags="zoom_btn")

        # Filename — clipped to single line with ellipsis if too long
        cv.create_text(IX + 2, CAP_Y + 2, anchor="nw",
                       text=_fit_text(icon + fname, sz - 22),
                       fill="#000000",
                       font=("Segoe UI", 9), tags="fname_text")

        # Size strip
        cv.create_rectangle(IX, sy, IX + sz, sy + STRIP_H, fill="#e8e8e8", outline="")
        cv.create_text(IX + 4, sy + STRIP_H // 2, anchor="w",
                       text=size_info, fill=size_colours.get(cat, "#555"),
                       font=("Segoe UI", 8, "bold"), tags="size_text")

        # Click routing
        def _click(e, o=orig, c=cv,
                   _IX=IX, _IY=IY, _sz=sz, _IMG_H=IMG_H,
                   _CTL_Y=CTL_Y, _CTL_H=CTL_H, _CAP_Y=CAP_Y,
                   _CELL_W=CELL_W, _PAD=PAD, _rm=_rm):
            xi, yi = e.x, e.y
            # Scissors badge — top-right, launches FTEditI
            if self.mode == "photos":
                _sx0 = _IX + _sz - 52
                if _sx0 <= xi <= _IX + _sz and _IY <= yi <= _IY + 32:
                    self._launch_ftediti(o); return
            # Controls row — Mark or Zoom
            if _CTL_Y <= yi <= _CTL_Y + _CTL_H:
                if xi <= _IX + _rm:
                    self._toggle_cull_canvas(o, c); return
                else:
                    self._zoom_and_focus(o); return
            # Image area — toggle left-panel selection
            if yi <= _IY + _IMG_H:
                shift = (e.state & 0x1) != 0
                try: cur_idx = self._all_files.index(o)
                except: cur_idx = 0
                if shift and self._last_click_idx is not None:
                    lo = min(self._last_click_idx, cur_idx)
                    hi = max(self._last_click_idx, cur_idx)
                    anchor = self._all_files[self._last_click_idx] if 0 <= self._last_click_idx < len(self._all_files) else None
                    adding = anchor in self._selected if anchor else True
                    for i in range(lo, hi + 1):
                        if 0 <= i < len(self._all_files):
                            f = self._all_files[i]
                            if adding: self._selected.add(f)
                            else:      self._selected.discard(f)
                else:
                    if o in self._selected: self._selected.discard(o)
                    else:                   self._selected.add(o)
                    self._last_click_idx = cur_idx
                self._repaint_selection()
                self._update_sel_bar()

        cv.bind("<Button-1>", _click)
        self.thumb_widgets.append((cv, cv, orig, None, None, None, None, None))

    def _add_cell(self, orig, idx, photo, from_cache, gen, ghost=False):
        if self._load_gen != gen: return
        try:
            if not self.grid_frame.winfo_exists(): return
        except: return

        # In 2-panel mode always use compact 120px cells
        if getattr(self, "_panel_mode", "1") == "2":
            self._add_cell_2panel_left(orig, idx, photo, gen)
            return

        page_start = getattr(self, '_page_start', 0)
        pos = idx - page_start
        if pos < 0: return
        col = pos % self._cols
        row = pos // self._cols
        seq_num = idx + 1
        culled  = orig in self._culled

        GHOST_BD = "#660000"
        selected = orig in self._selected
        bd_col, mid_col = GHOST_BD if ghost else UNTAGGED_BD, GHOST_BD if ghost else UNTAGGED_BD

        sz      = self._disp_size
        IMG_H   = THUMB_IMG_H if sz == THUMB_SIZE else int(sz * THUMB_IMG_H / THUMB_SIZE)
        CELL_W  = sz + 16
        BD      = 3          # outer border width
        PAD     = 6          # content inset (BD*2)
        IX      = PAD + 2    # image area left
        IY      = PAD + 2    # image area top
        CTL_Y   = IY + IMG_H          # controls row (rotate/mark/zoom) top — below image
        CTL_H   = 18
        CAP_Y   = CTL_Y + CTL_H + 3   # filename row top
        STRIP_H = 15
        CAP_H   = 20                   # caption area — single line
        sy      = CAP_Y + CAP_H        # size strip top
        CELL_H  = sy + STRIP_H + PAD  # bottom = size strip bottom + inset
        ACT_Y   = CAP_Y + CAP_H + STRIP_H * 2 + 2  # kept for click routing compat

        size_colours = {"Tiny":"#cc2200","V.Small":"#bb6600","Small":"#996600",
                        "Medium":"#336611","Large":"#1144aa","Huge":"#7722aa","?":"#555555"}
        _, cat, disp_s = _file_size_info_cached(orig)
        size_info = f"{cat}  {disp_s}"
        if self.mode == "pdfs":
            pages, _ = _get_pdf_info(orig)
            if pages: size_info += f"  {pages}p"

        fname    = os.path.basename(orig)
        icon     = ("🗑 " if ghost else
                    ("⚡ " if from_cache else ("📷 " if self.mode == "photos" else "📄 ")))
        fname_fg = "#ff6666" if ghost else ("#000000")
        rot_fg   = "#000000"
        rot_bg   = "#ffffff"
        # ── Single canvas replaces outer + mid + inner + all child widgets ──
        cv = tk.Canvas(self.grid_frame, width=CELL_W, height=CELL_H,
                       bg=bd_col, highlightthickness=0,
                       cursor="arrow" if ghost else "hand2")
        cv.grid(row=row, column=col, padx=THUMB_PAD // 2, pady=THUMB_PAD // 2)
        cv._photo = photo
        self._photo_refs.append(photo)
        # Mid-colour band and content background
        cv.create_rectangle(BD, BD, CELL_W - BD, CELL_H - BD,
                            fill=mid_col, outline="", tags="mid_rect")
        cv.create_rectangle(PAD, PAD, CELL_W - PAD, CELL_H - PAD,
                            fill=BG, outline="", tags="bg_rect")

        # Thumbnail image — centred in the fixed image area
        cv.create_image(IX + sz // 2, IY + IMG_H // 2,
                        anchor="center", image=photo, tags="thumb_img")

        # Sequence badge — top-left
        cv.create_rectangle(IX, IY, IX + 26, IY + 16, fill="#000000", outline="")
        cv.create_text(IX + 3, IY + 8, anchor="w", text=str(seq_num),
                       fill="white", font=("Segoe UI", 8, "bold"))

        # Scissors badge — top-right, double size of seq badge, launches FTEditI
        if self.mode == "photos" and not ghost:
            _sx0 = IX + sz - 52
            cv.create_rectangle(_sx0, IY, IX + sz, IY + 32, fill="#000000", outline="")
            cv.create_text(_sx0 + 26, IY + 16, anchor="center", text="✂",
                           fill="white", font=("Segoe UI", 16, "bold"),
                           tags="scissors_badge")



        # Group summary: large count circle on image + group label in caption
        _grp_caption = None
        if getattr(self, "_in_group_summary", False) and orig in self._similar_groups:
            _gi   = self._similar_groups[orig]
            if _gi <= len(self._group_clusters):
                _gcount = len(self._group_clusters[_gi - 1])
                _GRP_COLOURS = ["#7b3fa0","#1a6655","#1a5276","#7a4400","#1a4a1a","#5a2222"]
                _gcol = _GRP_COLOURS[(_gi - 1) % len(_GRP_COLOURS)]
                # Large count circle centred on image
                _bw = 56
                _bx = IX + sz // 2
                _by = IY + IMG_H // 2
                cv.create_oval(_bx - _bw//2, _by - _bw//2,
                               _bx + _bw//2, _by + _bw//2,
                               fill=_gcol, outline="#ffffff", width=2, tags="grp_badge")
                cv.create_text(_bx, _by, anchor="center",
                               text=str(_gcount), fill="white",
                               font=("Segoe UI", 22, "bold"), tags="grp_badge")
                cv.create_text(_bx, _by + 26, anchor="center",
                               text="photos", fill="white",
                               font=("Segoe UI", 9), tags="grp_badge")
                # Group label drawn over caption bar after image
                # (stored so it can be drawn after CAP_Y is known)
                # Drawn inline after the else block below via tag
                _grp_caption = (f"Group {_gi}", _gcol)

        # Watermark — 1-panel mode
        # Watermark: FILE DELETED / FOLDER DELETED / FILE UNREADABLE
        unreadable = orig in getattr(self, '_unreadable', set())
        if unreadable:
            wm_label = getattr(self, '_unreadable_reason', {}).get(orig, "? FILE UNREADABLE")
            WM_H = 28
            wm_y = IY + IMG_H // 6   # top third
            cv.create_rectangle(IX, wm_y - WM_H // 2,
                                IX + sz, wm_y + WM_H // 2,
                                fill="#000000", stipple="gray50", outline="",
                                tags="unreadable_bg")
            cv.create_text(IX + sz // 2, wm_y,
                           text=wm_label, fill="white",
                           font=("Segoe UI", 16, "bold"),
                           tags="unreadable_wm")

        # DELETING takes priority over SELECTED — drawn at vertical centre
        if not ghost and culled:
            WM_H = 28
            cv.create_rectangle(IX, IY + IMG_H // 2 - WM_H // 2,
                                IX + sz, IY + IMG_H // 2 + WM_H // 2,
                                fill="#000000", stipple="gray50", outline="",
                                tags="sel_watermark_bg")
            cv.create_text(IX + sz // 2, IY + IMG_H // 2,
                           text="DELETING", fill="#ffdd00",
                           font=("Segoe UI", 18, "bold"),
                           tags="sel_watermark")
        elif selected and not ghost:
            WM_H = 28
            cv.create_rectangle(IX, IY + IMG_H // 2 - WM_H // 2,
                                IX + sz, IY + IMG_H // 2 + WM_H // 2,
                                fill="#000000", stipple="gray50", outline="",
                                tags="sel_watermark_bg")
            cv.create_text(IX + sz // 2, IY + IMG_H // 2,
                           text="SELECTED", fill="white",
                           font=("Segoe UI", 18, "bold"),
                           tags="sel_watermark")

        if ghost:
            cv.create_rectangle(IX, IY, IX + sz, IY + IMG_H,
                                fill="#2a0000", stipple="gray50", outline="")
            cv.create_text(IX + sz // 2, IY + IMG_H // 2, text="✗  DELETED",
                           fill="#ff3333", font=("Segoe UI", 11, "bold"),
                           tags="status_lbl")
            path_display = os.path.dirname(orig).replace("\\", "\\\n")
            cv.create_text(IX + sz // 2, CAP_Y + 2, anchor="n",
                           text=path_display, fill="white",
                           font=("Segoe UI", 8), width=sz - 4)
        else:
            # GPS badge — top-centre (photos only)
            has_gps = self.mode == "photos" and bool(_get_gps_coords(orig))
            if has_gps:
                gx = IX + sz // 2
                cv.create_rectangle(gx - 16, IY, gx + 16, IY + 16,
                                    fill="#cc0000", outline="", tags="gps_badge")
                cv.create_text(gx, IY + 8, anchor="center", text="GPS",
                               fill="white", font=("Segoe UI", 8, "bold"),
                               tags="gps_badge")

            # Similarity group badge — top-LEFT of image, below sequence number
            if getattr(self, '_in_similar_view', False) and orig in self._similar_groups:
                _GRP_COLOURS = ["#7b3fa0","#1a6655","#1a5276","#7a4400","#1a4a1a","#5a2222"]
                _gi  = self._similar_groups[orig]
                _gcol = _GRP_COLOURS[(_gi - 1) % len(_GRP_COLOURS)]
                _glbl = f"G{_gi}"
                _gw   = max(44, len(_glbl) * 14 + 8)
                cv.create_rectangle(IX, IY + 16, IX + _gw, IY + 48,
                                    fill=_gcol, outline="", tags="grp_badge")
                cv.create_text(IX + _gw // 2, IY + 32, anchor="center",
                               text=_glbl, fill="white",
                               font=("Segoe UI", 16, "bold"), tags="grp_badge")

            # Rotate + Mark + Zoom row — below image area, both photos and PDFs
            _ry = CTL_Y
            _rh = CTL_H
            _rm = sz // 2        # midpoint
            # Background for whole row
            cv.create_rectangle(IX, _ry, IX + sz, _ry + _rh,
                                fill="#e0e0e0", outline="")
            if self.mode == "photos":
                # ↺ left corner
                cv.create_rectangle(IX, _ry, IX + 24, _ry + _rh,
                                    fill=rot_bg, outline="")
                cv.create_text(IX + 12, _ry + _rh // 2, anchor="center", text="↺",
                               fill=rot_fg, font=("Segoe UI", 11, "bold"), tags="rotL")
                # ↻ right corner
                cv.create_rectangle(IX + sz - 24, _ry, IX + sz, _ry + _rh,
                                    fill=rot_bg, outline="")
                cv.create_text(IX + sz - 12, _ry + _rh // 2, anchor="center", text="↻",
                               fill=rot_fg, font=("Segoe UI", 11, "bold"), tags="rotR")
                # Divider line at midpoint
                cv.create_line(IX + _rm, _ry, IX + _rm, _ry + _rh, fill="#bbbbbb")
                # Mark — left half between rotL and midpoint
                mark_text = "Unmark" if culled else "Mark"
                mark_fg   = "#994400" if culled else "#333333"
                cv.create_text(IX + 24 + (_rm - 24) // 2, _ry + _rh // 2, anchor="center",
                               text=mark_text, fill=mark_fg,
                               font=("Segoe UI", 8, "bold"), tags="mark_btn")
                # Zoom — right half between midpoint and rotR
                cv.create_text(IX + _rm + (_rm - 24) // 2, _ry + _rh // 2, anchor="center",
                               text="Zoom", fill="#1a2a4a",
                               font=("Segoe UI", 8, "bold"), tags="zoom_btn")
            else:
                # PDFs: no rotation — full width Mark and Zoom only
                cv.create_line(IX + _rm, _ry, IX + _rm, _ry + _rh, fill="#bbbbbb")
                mark_text = "Unmark" if culled else "Mark"
                mark_fg   = "#994400" if culled else "#333333"
                cv.create_text(IX + _rm // 2, _ry + _rh // 2, anchor="center",
                               text=mark_text, fill=mark_fg,
                               font=("Segoe UI", 8, "bold"), tags="mark_btn")
                cv.create_text(IX + _rm + _rm // 2, _ry + _rh // 2, anchor="center",
                               text="Zoom", fill="#1a2a4a",
                               font=("Segoe UI", 8, "bold"), tags="zoom_btn")

            # Filename + info "?" button (or Group label in summary mode)
            if _grp_caption:
                _gcap_text, _gcap_col = _grp_caption
                cv.create_text(IX + sz // 2, CAP_Y + 10, anchor="center",
                               text=_gcap_text, fill=_gcap_col,
                               font=("Segoe UI", 10, "bold"), tags="fname_text")
            else:
                cv.create_text(IX + 2, CAP_Y + 2, anchor="nw",
                               text=_fit_text(icon + fname, sz - 28), fill=fname_fg,
                               font=("Segoe UI", 9), tags="fname_text")
                # In cull view show folder path so user knows where the file lives
                if self._in_cull_view:
                    folder_name = os.path.basename(os.path.dirname(orig))
                    cv.create_text(IX + 2, CAP_Y + 14, anchor="nw",
                                   text=_fit_text(folder_name, sz - 10), fill="#888888",
                                   font=("Segoe UI", 7), tags="folder_text")
            cv.create_text(CELL_W - PAD - 3, CAP_Y + 9, anchor="e",
                           text="?", fill="#888888",
                           font=("Segoe UI", 9, "bold"), tags="info_btn")

            # Size strip
            cv.create_rectangle(IX, sy, IX + sz, sy + STRIP_H, fill="#e8e8e8", outline="")
            cv.create_text(IX + 4, sy + STRIP_H // 2, anchor="w",
                           text=size_info, fill=size_colours.get(cat, "#555"),
                           font=("Segoe UI", 8, "bold"), tags="size_text")

            # MARKED indicator on size strip when culled
            if culled:
                cv.create_text(IX + sz - 4, sy + STRIP_H // 2, anchor="e",
                               text="🗑 MARKED", fill=CULLED_BD,
                               font=("Segoe UI", 7, "bold"), tags="status_lbl")

            # DEBUG: red outline around image box — drawn last so it is topmost
            cv.create_rectangle(IX, IY, IX + sz, IY + IMG_H,
                                outline="#ff0000", width=1, fill="", tags="debug_img_box")

            # ── Click routing ─────────────────────────────────────────────────
            def _cv_click(e, o=orig, c=cv,
                          _IX=IX, _IY=IY, _sz=sz, _IMG_H=IMG_H, _ACT_Y=ACT_Y, _CAP_Y=CAP_Y,
                          _CTL_Y=CTL_Y, _CTL_H=CTL_H,
                          _CELL_W=CELL_W, _PAD=PAD, _has_gps=has_gps):
                xi, yi = e.x, e.y
                # Scissors badge — top-right, launches FTEditI
                if self.mode == "photos" and not ghost:
                    _sx0 = _IX + _sz - 52
                    if _sx0 <= xi <= _IX + _sz and _IY <= yi <= _IY + 32:
                        self._launch_ftediti(o); return
                # Group summary — click opens group review in 2-panel
                if getattr(self, "_in_group_summary", False) and o in self._similar_groups:
                    self._open_group_review(o); return
                # GPS badge — top-centre of image area
                if _has_gps:
                    gx = _IX + _sz // 2
                    if gx - 16 <= xi <= gx + 16 and _IY <= yi <= _IY + 16:
                        self._launch_ftmapimg_from_selection(center_path=o); return

                # Controls row — rotate/mark/zoom (below image)
                if _CTL_Y <= yi <= _CTL_Y + _CTL_H:
                    _rm = _sz // 2
                    if self.mode == "photos":
                        if xi <= _IX + 22:
                            threading.Thread(
                                target=lambda: self._rotate_thumb(o, None, 90), daemon=True).start()
                            return
                        if xi >= _IX + _sz - 22:
                            threading.Thread(
                                target=lambda: self._rotate_thumb(o, None, -90), daemon=True).start()
                            return
                        if xi <= _IX + _rm:
                            self._toggle_cull_canvas(o, c); return
                        else:
                            self._zoom_and_focus(o); return
                    else:
                        # PDFs: left half = Mark, right half = Zoom
                        if xi <= _IX + _rm:
                            self._toggle_cull_canvas(o, c); return
                        else:
                            self._zoom_and_focus(o); return
                # Info "?" — right end of filename row
                if xi >= _CELL_W - _PAD - 20 and _CAP_Y <= yi <= _CAP_Y + 18:
                    self._show_file_info(o, c); return
                # Image area click
                if yi <= _IY + _IMG_H:
                    # In cull view: click removes DELETING watermark and decrements count
                    # File stays visible in current session but is removed from _culled
                    if self._in_cull_view:
                        if o in self._culled:
                            self._culled.discard(o)
                            self._culled_at.pop(o, None)
                            _write_cull_list(self.mode_cfg['root'], self._culled, self._culled_at)
                            self._schedule_tree_refresh()
                            # Remove watermark from this cell only — don't rebuild page
                            c.delete("sel_watermark")
                            c.delete("sel_watermark_bg")
                            # Update folder/status bar count
                            n = len(self._culled)
                            self.lbl_folder.config(text=f"🗑  Cull List — {n} files marked for deletion")
                            self._update_view_status(f"🗑  Cull List  —  {n} marked for deletion")
                            if n == 0:
                                self._hide_delete_popup()
                        return
                    shift = (e.state & 0x1) != 0
                    try: cur_idx = self._all_files.index(o)
                    except: cur_idx = 0
                    if shift and self._last_click_idx is not None:
                        # Range toggle - match state of the anchor item
                        lo = min(self._last_click_idx, cur_idx)
                        hi = max(self._last_click_idx, cur_idx)
                        anchor = self._all_files[self._last_click_idx] if 0 <= self._last_click_idx < len(self._all_files) else None
                        adding = anchor in self._selected if anchor else True
                        for i in range(lo, hi + 1):
                            if 0 <= i < len(self._all_files):
                                f = self._all_files[i]
                                if adding: self._selected.add(f)
                                else:      self._selected.discard(f)
                    else:
                        # Plain click or Ctrl+click - toggle this item
                        if o in self._selected: self._selected.discard(o)
                        else:                   self._selected.add(o)
                        self._last_click_idx = cur_idx
                    # Only repaint affected cells — much faster than repainting all
                    if shift and self._last_click_idx is not None:
                        lo2 = min(self._last_click_idx, cur_idx)
                        hi2 = max(self._last_click_idx, cur_idx)
                        changed = {self._all_files[i] for i in range(lo2, hi2+1)
                                   if 0 <= i < len(self._all_files)}
                    else:
                        changed = {o}
                    self._repaint_selection(changed_only=changed)
                    self._update_sel_bar()

            def _cv_enter(e, o=orig, c=cv):
                if o not in self._selected:
                    c.configure(bg=HOVER_BD)
            def _cv_leave(e, o=orig, c=cv):
                if o in self._selected: return
                c.configure(bg=UNTAGGED_BD)

            cv.bind("<Button-1>", _cv_click)
            cv.bind("<Enter>",    _cv_enter)
            cv.bind("<Leave>",    _cv_leave)

        tag_var = tk.BooleanVar(value=orig in self.tagged)
        # thumb_widgets tuple is (outer, inner, orig, tl, nl, cb, tag_var, chk_lbl)
        # cv serves as both outer and inner; tl/nl/cb/chk_lbl are None —
        # all state is painted directly on the canvas via named item tags.
        self.thumb_widgets.append((cv, cv, orig, None, None, None, tag_var, None))

    # ── Canvas-native state helpers ────────────────────────────────────────────
    @staticmethod
    def _cv_repaint(cv, bd_col, mid_col, mark_text, mark_fg, status_text, status_fg,
                    culled=False, selected=False, sz=None):
        """Update all mutable visual elements on a cell canvas in one pass."""
        try:
            cv.configure(bg=bd_col)
            cv.itemconfigure("mid_rect",   fill=mid_col)
            cv.itemconfigure("mark_btn",   text=mark_text,  fill=mark_fg)
            cv.itemconfigure("status_lbl", text=status_text, fill=status_fg)
            cv.delete("sel_watermark"); cv.delete("sel_watermark_bg")
            cv.delete("del_watermark"); cv.delete("del_watermark_bg")
            if culled or selected:
                if sz is None:
                    try: sz = cv.winfo_width() - 16
                    except: sz = 200
                img_h = THUMB_IMG_H if sz == THUMB_SIZE else int(sz * THUMB_IMG_H / THUMB_SIZE)
                WM_H = 28
                PAD = 6; IX = PAD + 2; IY = PAD + 2
                mid_y = IY + img_h // 2

                if culled and selected:
                    del_y = mid_y - WM_H // 2 - 2
                    sel_y = mid_y + WM_H // 2 + 2
                    cv.create_rectangle(IX, del_y - WM_H//2, IX+sz, del_y + WM_H//2,
                                        fill="#000000", stipple="gray50", outline="",
                                        tags="del_watermark_bg")
                    cv.create_text(IX + sz//2, del_y, text="DELETING", fill="#ffdd00",
                                   font=("Segoe UI", 16, "bold"), tags="del_watermark")
                    cv.create_rectangle(IX, sel_y - WM_H//2, IX+sz, sel_y + WM_H//2,
                                        fill="#000000", stipple="gray50", outline="",
                                        tags="sel_watermark_bg")
                    cv.create_text(IX + sz//2, sel_y, text="SELECTED", fill="white",
                                   font=("Segoe UI", 16, "bold"), tags="sel_watermark")
                elif culled:
                    cv.create_rectangle(IX, mid_y - WM_H//2, IX+sz, mid_y + WM_H//2,
                                        fill="#000000", stipple="gray50", outline="",
                                        tags="del_watermark_bg")
                    cv.create_text(IX + sz//2, mid_y, text="DELETING", fill="#ffdd00",
                                   font=("Segoe UI", 18, "bold"), tags="del_watermark")
                else:
                    cv.create_rectangle(IX, mid_y - WM_H//2, IX+sz, mid_y + WM_H//2,
                                        fill="#000000", stipple="gray50", outline="",
                                        tags="sel_watermark_bg")
                    cv.create_text(IX + sz//2, mid_y, text="SELECTED", fill="white",
                                   font=("Segoe UI", 18, "bold"), tags="sel_watermark")
        except Exception: pass

    def _cell_colours(self, orig):
        """Return (bd_col, mid_col, mark_text, mark_fg,
                   status_text, status_fg, culled, selected) for the given path."""
        tagged = orig in (self._shadow_tagged if self._shadow_active else self.tagged)
        culled = orig in self._culled
        selected = orig in self._selected
        bd_col, mid_col = UNTAGGED_BD, UNTAGGED_BD
        mark_text = "Unmark" if culled else "Mark"
        mark_fg   = "#994400" if culled else "#222222"
        status    = "🗑  MARKED" if culled else ""
        status_fg = CULLED_BD
        return bd_col, mid_col, mark_text, mark_fg, status, status_fg, culled, selected

    def _toggle_tag_canvas(self, orig, cv):
        """Tag/untag a file and repaint its canvas cell."""
        from datetime import datetime
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        tagged_set = self._shadow_tagged if self._shadow_active else self.tagged
        tagged_at  = self._shadow_tagged_at if self._shadow_active else self.tagged_at
        if orig in tagged_set:
            tagged_set.discard(orig); tagged_at.pop(orig, None)
            if not self._shadow_active:
                try: self.tagged_order.remove(orig)
                except ValueError: pass
        else:
            tagged_set.add(orig); tagged_at[orig] = ts
            if not self._shadow_active and orig not in self.tagged_order:
                self.tagged_order.append(orig)
        # Update tag_var in thumb_widgets so spacebar and other callers stay in sync
        for item in self.thumb_widgets:
            if item[2] == orig:
                tv = item[6]
                if tv is not None:
                    tv.set(orig in tagged_set)
                break
        self._cv_repaint(cv, *self._cell_colours(orig))
        self._update_coll_info()
        if self._shadow_active: self._shadow_update_btn()
        else: self._schedule_save(); self._schedule_tree_refresh()

    def _toggle_cull_canvas(self, orig, cv):
        """Mark/unmark a file for culling and repaint its canvas cell."""
        from datetime import datetime
        if orig not in self._culled:
            if orig in self.tagged:
                if not messagebox.askyesno("Mark for deletion?",
                    f"This file is tagged as a keeper in collection '{self.collection}'.\n\n"
                    f"{os.path.basename(orig)}\n\n"
                    "Are you sure you want to mark it for deletion?",
                    parent=self.win): return
            self._culled.add(orig)
            self._culled_at[orig] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        else:
            self._culled.discard(orig); self._culled_at.pop(orig, None)
        self._cv_repaint(cv, *self._cell_colours(orig))
        self._schedule_cull_save(); self._update_statusbar()
        n = len(self._culled)
        self._update_cull_radio_hint()

    def _clear_grid(self):
        try: self.canvas.delete("welcome")
        except: pass
        try: self.canvas.delete("browse_prompt")
        except: pass
        try:
            if self._welcome_card and self._welcome_card.winfo_exists():
                self._welcome_card.destroy()
            self._welcome_card = None
        except: pass
        for w in self.grid_frame.winfo_children(): w.destroy()
        self.thumb_widgets.clear(); self._photo_refs.clear()
        for c in range(50):
            try: self.grid_frame.columnconfigure(c, minsize=0, weight=0)
            except: break

    def _show_access_error(self, root_dir):
        """Show a prominent error in the grid area when all root folders are inaccessible."""
        self._clear_grid()
        f = tk.Frame(self.grid_frame, bg=BG)
        f.pack(expand=True, fill="both", pady=80)
        tk.Label(f, text="⚠", font=("Segoe UI", 48), bg=BG, fg="#cc4444").pack()
        tk.Label(f,
            text="No accessible root folders",
            font=("Segoe UI", 14, "bold"), bg=BG, fg="#ff6666",
            justify="center").pack(pady=(8, 4))
        tk.Label(f,
            text=f"{root_dir}\n\nCheck that the drive or network share is connected.\n"
                 "Other roots in this project may still be accessible — check the root selector.",
            font=("Segoe UI", 10), bg=BG, fg=TEXT_DIM,
            justify="center", wraplength=500).pack()

    def _show_no_images_message(self, folder):
        """Show a centred 'no images' message on the grid canvas."""
        self._clear_grid()
        self.canvas.update_idletasks()
        cw = max(400, self.canvas.winfo_width())
        ch = max(300, self.canvas.winfo_height())
        name = os.path.basename(folder) or folder
        word = self.mode_cfg.get('file_word', 'image')

        card = tk.Frame(self.grid_frame, bg="#e8e8e8",
                        highlightbackground="#aaaaaa", highlightthickness=2)
        tk.Label(card, text="📁", bg="#e8e8e8", font=("Segoe UI", 48)).pack(pady=(30, 0))
        tk.Label(card, text=f"No {word}s found",
                 bg="#e8e8e8", fg="#333333",
                 font=("Segoe UI", 22, "bold")).pack(pady=(8, 0))
        tk.Label(card, text=f"Folder:  {name}",
                 bg="#e8e8e8", fg="#666666",
                 font=("Segoe UI", 11)).pack(pady=(4, 0))
        tk.Label(card, text="This folder and all subfolders contain no matching files.",
                 bg="#e8e8e8", fg="#888888",
                 font=("Segoe UI", 10),
                 wraplength=400, justify="center").pack(pady=(8, 30))

        self.canvas.create_window(cw // 2, ch // 2, anchor="center", window=card)
        self.canvas.configure(scrollregion=(0, 0, cw, ch))

    def _show_welcome_screen(self):
        """Draw a welcome panel centred on the full window."""
        self._clear_grid()
        self.canvas.yview_moveto(0)

        import time as _twc
        try:
            _tz = _twc.tzname[1] if _twc.daylight else _twc.tzname[0]
        except: _tz = ""
        _build = BUILD_DATE

        mode_label = self.mode_cfg['label']

        is_light = True
        _card_bg = "#e8e8e8" if is_light else "#1a1a1a"
        _card_bd = "#888888" if is_light else "#444444"
        _card_fg = "#111111" if is_light else "white"
        _div     = "#aaaaaa" if is_light else "#444444"

        card = tk.Frame(self.win, bg=_card_bg,
                        highlightbackground=_card_bd, highlightthickness=2)

        tk.Label(card, text="FileTagger",
                 bg=_card_bg, fg=_card_fg,
                 font=("Segoe UI", 52, "bold"),
                 padx=60).pack(pady=(30, 0))

        tk.Label(card,
                 text="📷  Photos    +    📄  PDFs    —    Tag  ·  Browse  ·  Export",
                 bg=_card_bg, fg=_card_fg,
                 font=("Segoe UI", 13),
                 padx=40).pack(pady=(8, 0))

        tk.Frame(card, bg=_div, height=1).pack(fill="x", padx=40, pady=16)

        tk.Label(card, text=f"Build  {_build}",
                 bg=_card_bg, fg=_card_fg,
                 font=("Segoe UI", 11),
                 padx=40).pack()

        tk.Frame(card, bg=_div, height=1).pack(fill="x", padx=40, pady=16)

        tk.Label(card,
                 text=f"← Select a folder in the tree to browse {mode_label}",
                 bg=_card_bg, fg=_card_fg,
                 font=("Segoe UI", 12, "italic"),
                 padx=40).pack(pady=(0, 30))

        # Centre within the full application window
        card.update_idletasks()
        cw = card.winfo_reqwidth()
        ch = card.winfo_reqheight()
        self.win.update_idletasks()
        ww = self.win.winfo_width()
        wh = self.win.winfo_height()
        x = max(0, (ww - cw) // 2)
        y = max(0, (wh - ch) // 2)
        card.place(x=x, y=y)
        card.lift()
        self._welcome_card = card

    def _show_browse_prompt(self):
        """Show a simple centred message prompting the user to select a folder or collection.
        Used on mode switch — no splash, no build info, just the instruction."""
        self._clear_grid()
        # Dismiss any existing welcome card
        wc = getattr(self, '_welcome_card', None)
        if wc:
            try: wc.destroy()
            except: pass
            self._welcome_card = None

        mode_word = "Photos" if self.mode == "photos" else "PDFs"
        msg = f"Select a Folder or Collection to browse {mode_word}"

        self.canvas.update_idletasks()
        cw = max(400, self.canvas.winfo_width())
        ch = max(300, self.canvas.winfo_height())

        lbl = tk.Label(self.canvas, text=msg,
                       bg=BG, fg=TEXT_DIM,
                       font=("Segoe UI", 16, "italic"),
                       wraplength=cw - 60, justify="center")
        self.canvas.create_window(cw // 2, ch // 2, anchor="center",
                                  window=lbl, tags="browse_prompt")
        self.canvas.configure(scrollregion=(0, 0, cw, ch))
        self.canvas.yview_moveto(0)

    def _regrid(self):
        try: ypos = self.canvas.yview()[0]
        except: ypos = 0
        items = self.thumb_widgets[:]
        for outer,inner,orig,tl,nl,*_ in items: outer.grid_forget()
        for idx,(outer,inner,orig,tl,nl,*_) in enumerate(items):
            outer.grid(row=idx//self._cols,column=idx%self._cols,
                       padx=THUMB_PAD//2,pady=THUMB_PAD//2)
        self.canvas.update_idletasks(); self.canvas.yview_moveto(ypos)

    # ── Tagging ────────────────────────────────────────────────────────────────
    def _set_cell_state(self, outer, inner, tl, nl, tagged, cb=None, tag_var=None, orig=None):
        """Update cell visual state. outer==inner==canvas for canvas cells."""
        cv = outer  # outer IS the canvas in the new implementation
        culled = (orig in self._culled) if orig else False
        mark_text  = "Unmark"  if culled else "Mark"
        mark_fg    = "#994400" if culled else "#222222"
        status     = "🗑  MARKED" if culled else ""
        status_fg  = CULLED_BD
        try:
            cv.configure(bg=UNTAGGED_BD)
            cv.itemconfigure("mid_rect",   fill=UNTAGGED_BD)
            cv.itemconfigure("mark_btn",   text=mark_text, fill=mark_fg)
            cv.itemconfigure("status_lbl", text=status,    fill=status_fg)
        except Exception: pass
        if tag_var is not None:
            try: tag_var.set(tagged)
            except Exception: pass

    def _zoom_and_focus(self, orig):
        self._focused_orig = orig; self._zoom(orig)

    # ── Debounced save / tree refresh ─────────────────────────────────────────
    def _schedule_save(self):
        """Debounced collection save — coalesces rapid tag/untag into one disk write."""
        if self._save_after_id:
            try: self.win.after_cancel(self._save_after_id)
            except: pass
        self._save_after_id = self.win.after(1500, self._do_deferred_save)

    def _do_deferred_save(self):
        self._save_after_id = None
        self._save_current_collection()

    def _schedule_tree_refresh(self):
        """Debounced tree refresh — avoids repeated os.scandir on every tag over network."""
        if self._tree_after_id:
            try: self.win.after_cancel(self._tree_after_id)
            except: pass
        self._tree_after_id = self.win.after(1500, self._do_deferred_tree_refresh)

    def _do_deferred_tree_refresh(self):
        self._tree_after_id = None
        self._update_tree_colours()
        self._refresh_tree_stats()

    # ── Cull list helpers ──────────────────────────────────────────────────────
    def _schedule_cull_save(self):
        if self._save_after_id_cull:
            try: self.win.after_cancel(self._save_after_id_cull)
            except: pass
        self._save_after_id_cull = self.win.after(1000, self._do_cull_save)

    def _do_cull_save(self):
        self._save_after_id_cull = None
        _write_cull_list(self.mode_cfg['root'], self._culled, self._culled_at)

    def _toggle_cull_cb(self, orig, mark_btn, outer, inner):
        """Toggle cull state. In canvas cells outer==inner==canvas; mark_btn is ignored."""
        from datetime import datetime
        if orig not in self._culled:
            if orig in self.tagged:
                if not messagebox.askyesno("Mark for deletion?",
                    f"This file is tagged as a keeper in collection '{self.collection}'.\n\n"
                    f"{os.path.basename(orig)}\n\n"
                    "Are you sure you want to mark it for deletion?",
                    parent=self.win): return
            self._culled.add(orig)
            self._culled_at[orig] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        else:
            self._culled.discard(orig); self._culled_at.pop(orig, None)
        self._cv_repaint(outer, *self._cell_colours(orig))
        self._schedule_cull_save(); self._update_statusbar()
        n = len(self._culled)
        self._update_cull_radio_hint()

    def _set_view_radio(self, mode):
        """Update internal _view_mode state. No visible radio panel — left panel handles UX."""
        try:
            if self._view_mode:
                self._view_mode.set(mode)
            if hasattr(self, '_refresh_vrad'):
                self._refresh_vrad()
        except Exception: pass

    def _on_view_radio(self):
        """Called from the hidden view radio buttons (kept for internal logic)."""
        pass

    def _mode_word(self):
        """Returns 'Photos' or 'PDFs' based on current mode."""
        return self.mode_cfg.get('label', 'Photos')

    def _set_folder_label(self, folder=None, count=None):
        """Set lbl_folder and view_status to standardised Folder format."""
        if folder is None: folder = self.current_folder
        if count is None: count = len(self._all_files)
        name = os.path.basename(folder) or folder
        mode = self._mode_word()
        text = f"Folder  {name}  —  {mode}  ({count})"
        self.lbl_folder.config(text=text)
        self._update_view_status(text)

    def _set_collection_label(self, count=None):
        """Set lbl_folder and view_status to standardised Collection format."""
        if count is None: count = len(self._all_files)
        mode = self._mode_word()
        text = f"Collection  {self.collection}  —  {mode}  ({count})"
        self.lbl_folder.config(text=text)
        self._update_view_status(text)

    def _update_view_status(self, text):
        """Update the view status label at the top of the right panel."""
        try:
            if self.lbl_view_status:
                self.lbl_view_status.config(text=text)
        except Exception: pass

    def _update_folder_label(self):
        """Update folder label in view radio panel if it exists."""
        try:
            if self.lbl_view_folder:
                name  = os.path.basename(self.current_folder) or self.current_folder
                count = len(self._all_files)
                self.lbl_view_folder.config(text=f"{name}  ({count})")
        except Exception: pass

    def _toggle_cull_view(self):
        """Legacy toggle — now driven by view radio."""
        if self._in_cull_view:
            self._exit_special_view()
        else:
            self._set_view_radio("cull")
            self._show_cull_view()

    def _toggle_located_view(self):
        """Show only images that have GPS coordinates."""
        if self._in_located_view:
            self._in_located_view = False
            self._in_similar_view = False
            self._similar_groups  = {}
            try: self.btn_located.config(bg="#1a6655")
            except: pass
            self._load_folder(self.current_folder)
        else:
            if self.mode != "photos":
                messagebox.showinfo("Located view",
                    "GPS location data is only available for Photos.", parent=self.win); return
            self._status("Scanning for GPS data...")
            self.win.update_idletasks()
            located = [f for f in self._all_files if _get_gps_coords(f)]
            if not located:
                self._status("No images with GPS data found in this folder.")
                return
            self._in_located_view = True
            self._in_tagged_view  = False
            self._in_cull_view    = False
            self._update_tagged_btn()
            try: self.btn_located.config(bg="#22aa77")
            except: pass
            self._all_files = located
            self._page_num  = 0
            self.lbl_folder.config(text=f"📍  Located — {len(located)} images with GPS data")
            self._show_page()

    # ── Find Similar ──────────────────────────────────────────────────────────
    def _find_similar_dialog(self):
        """Show threshold slider and launch similarity scan on current folder."""
        if self.mode != "photos":
            messagebox.showinfo("Find Similar",
                "Find Similar is only available in Photos mode.", parent=self.win)
            return
        if not self._all_files and not getattr(self, 'current_folder', ''):
            messagebox.showinfo("Find Similar",
                "Open a folder first.", parent=self.win)
            return
        # If already in similar view, exit it
        if self._in_similar_view:
            self._exit_special_view()
            try: self.btn_similar.config(bg="#5a3a6a")
            except: pass
            try: self.btn_compress.config(bg="#3a3a6a")
            except: pass
            return

        dlg = tk.Toplevel(self.win)
        dlg.title("Find Similar Photos")
        dlg.configure(bg=BG3)
        dlg.resizable(False, False)
        dlg.transient(self.win)
        dlg.grab_set()
        self._centre_window(dlg, 440, 360)

        tk.Label(dlg, text="🔍  Find Similar Photos",
                 bg=BG3, fg="#111111",
                 font=("Segoe UI", 12, "bold")).pack(pady=(16, 2))
        tk.Label(dlg, text=f"Scan {len(self._all_files)} images in current folder",
                 bg=BG3, fg="#333333", font=("Segoe UI", 9)).pack()

        tk.Frame(dlg, bg="#aaaaaa", height=1).pack(fill="x", padx=20, pady=(10,0))

        # ── Similarity type — radio buttons ───────────────────────────────
        sim_type = tk.StringVar(value="framing")

        rb_fr = tk.Frame(dlg, bg=BG3); rb_fr.pack(fill="x", padx=24, pady=(8,0))
        tk.Label(rb_fr, text="Find by:", bg=BG3, fg="#111111",
                 font=("Segoe UI", 9, "bold")).pack(anchor="w")

        TYPE_OPTS = [
            ("framing",  "Similar framing / composition  (pHash)",
                         "Best for duplicates and near-identical shots"),
            ("colours",  "Similar colours / palette  (histogram)",
                         "Best for photos from the same scene or setting"),
            ("dates",    "Similar dates taken  (EXIF)",
                         "Groups photos taken within a chosen time window"),
        ]
        for val, label, tip in TYPE_OPTS:
            row = tk.Frame(rb_fr, bg=BG3); row.pack(fill="x", pady=1)
            tk.Radiobutton(row, text=label, variable=sim_type, value=val,
                           bg=BG3, fg="#111111", selectcolor="#ffffff",
                           activebackground=BG3, activeforeground="#111111",
                           font=("Segoe UI", 9, "bold"),
                           command=lambda: _on_type_change()).pack(side="left")
            tk.Label(row, text=tip, bg=BG3, fg="#555555",
                     font=("Segoe UI", 8)).pack(side="left", padx=(4,0))

        tk.Frame(dlg, bg="#aaaaaa", height=1).pack(fill="x", padx=20, pady=(8,0))

        # ── Options area — changes per type ───────────────────────────────
        options_fr = tk.Frame(dlg, bg=BG3); options_fr.pack(fill="x", padx=24, pady=6)

        # Framing/Colours slider
        thresh_var = tk.IntVar(value=7)
        slider_row = tk.Frame(options_fr, bg=BG3)
        thresh_name_lbl = tk.Label(slider_row, text="Strictness:", bg=BG3,
                                   fg="#111111", font=("Segoe UI", 9, "bold"), width=12, anchor="w")
        thresh_name_lbl.pack(side="left")
        thresh_val_lbl = tk.Label(slider_row, text="7", bg=BG3, fg="#1144cc",
                                  font=("Segoe UI", 9, "bold"), width=4)
        thresh_val_lbl.pack(side="right")
        slider = tk.Scale(options_fr, from_=0, to=15, orient="horizontal",
                          variable=thresh_var, bg=BG3, fg="#111111",
                          troughcolor="#bbbbbb", highlightthickness=0,
                          showvalue=False, length=380,
                          command=lambda v: thresh_val_lbl.config(text=str(int(float(v)))))
        slider_hint = tk.Label(options_fr, text="", bg=BG3, fg="#555555",
                               font=("Segoe UI", 8))

        # Date range radios
        date_range = tk.StringVar(value="same day")
        date_row = tk.Frame(options_fr, bg=BG3)
        tk.Label(date_row, text="Time window:", bg=BG3, fg="#111111",
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(0,4))
        date_inner = tk.Frame(date_row, bg=BG3); date_inner.pack(anchor="w")
        for label, val in [("Same day","same day"),("± 1 day","1 day"),
                           ("± 2 days","2 days"),("± 5 days","5 days")]:
            tk.Radiobutton(date_inner, text=label, variable=date_range, value=val,
                           bg=BG3, fg="#111111", selectcolor="#ffffff",
                           activebackground=BG3, activeforeground="#111111",
                           font=("Segoe UI", 9)).pack(side="left", padx=(0,12))

        CONFIGS = {
            "framing": {
                "slider_lbl": "Strictness:",
                "hint": "Low = exact duplicates only   |   High = similar framing",
                "range": (0, 15), "default": 7, "use_date": False,
            },
            "colours": {
                "slider_lbl": "Threshold:",
                "hint": "Low = very similar colours only   |   High = loosely similar",
                "range": (70, 98), "default": 88, "use_date": False,
            },
            "dates": {
                "slider_lbl": "", "hint": "", "range": None,
                "default": None, "use_date": True,
            },
        }

        def _on_type_change(*_):
            cfg = CONFIGS[sim_type.get()]
            # Clear options area
            slider_row.pack_forget(); slider.pack_forget()
            slider_hint.pack_forget(); date_row.pack_forget()
            if cfg["use_date"]:
                date_row.pack(fill="x")
            else:
                lo, hi = cfg["range"]
                slider.config(from_=lo, to=hi)
                thresh_var.set(cfg["default"])
                thresh_val_lbl.config(text=str(cfg["default"]))
                thresh_name_lbl.config(text=cfg["slider_lbl"])
                slider_hint.config(text=cfg["hint"])
                slider_row.pack(fill="x")
                slider.pack(fill="x")
                slider_hint.pack(anchor="w")

        _on_type_change()  # initialise

        bf = tk.Frame(dlg, bg=BG3); bf.pack(pady=12)

        def on_scan():
            stype     = sim_type.get()
            threshold = thresh_var.get()
            drange    = date_range.get()
            dlg.destroy()
            self._run_find_similar(threshold, sim_type=stype, date_range=drange)

        tk.Button(bf, text="  Scan  ", bg="#5a3a6a", fg="white",
                  font=("Segoe UI", 10, "bold"), relief="flat",
                  padx=14, pady=5, cursor="hand2",
                  command=on_scan).pack(side="left", padx=6)
        tk.Button(bf, text="  Cancel  ", bg=BG2, fg=TEXT_BRIGHT,
                  font=("Segoe UI", 10), relief="flat",
                  padx=10, pady=5, cursor="hand2",
                  command=dlg.destroy).pack(side="left", padx=6)

    def _run_find_similar(self, threshold, sim_type="framing", date_range="same day"):
        """Background thread: find similar images by chosen method.
        sim_type: framing | colours | dates
        """
        import numpy as _np
        from PIL import Image as _PILi, ImageFile as _PILif

        def _phash(img, hash_size=8):
            """Pure Pillow/numpy perceptual hash — fully size-invariant.
            Resize to 32x32 greyscale, apply 2D DCT via FFT mirror method,
            keep top-left 8x8 low-frequency block (minus DC), threshold
            against median. Returns a 63-bit integer."""
            n = hash_size
            img = img.convert('L').resize((n * 4, n * 4), _PILi.LANCZOS)
            pixels = _np.array(img, dtype=_np.float32)

            def _dct_rows(block):
                """DCT-II of every row simultaneously via FFT mirror trick."""
                rows, N = block.shape
                v = _np.concatenate([block, block[:, ::-1]], axis=1)
                V = _np.fft.rfft(v)[:, :N]
                k = _np.arange(N, dtype=_np.float32)
                return _np.real(V * _np.exp(-1j * _np.pi * k / (2 * N))) * 2

            # 2D DCT: apply to rows, transpose, apply to rows again, transpose back
            dct2d = _dct_rows(_dct_rows(pixels).T).T

            # Top-left n x n block, skip DC term [0,0]
            low = dct2d[:n, :n].flatten()[1:]
            median = _np.median(low)
            bits = (low > median).astype(_np.uint8)
            h = 0
            for b in bits:
                h = (h << 1) | int(b)
            return h

        def _hamming(a, b):
            """Hamming distance between two integers."""
            x = a ^ b
            count = 0
            while x:
                count += x & 1
                x >>= 1
            return count

        # pHash only — threshold maps directly to Hamming distance (0-15 bits)
        # hash_size=12 gives 143 bits, much more discriminating than hash_size=8
        phash_thresh = threshold
        use_hist     = False

        files = list(self._all_files)
        if not files:
            self._status("No files to scan.")
            return

        # ── Route to appropriate worker ───────────────────────────────────────
        if sim_type == "colours":
            self._run_find_similar_colours(threshold / 100.0, files)
            return
        if sim_type == "dates":
            self._run_find_similar_dates(date_range, files)
            return
        # else: framing (pHash) — continues below

        # ── Progress window ───────────────────────────────────────────────────
        pw = tk.Toplevel(self.win)
        pw.title("Finding Similar Photos")
        pw.configure(bg=BG3)
        pw.resizable(False, False)
        pw.transient(self.win)
        pw.grab_set()
        self._centre_window(pw, 420, 130)
        pw.protocol("WM_DELETE_WINDOW", lambda: None)

        tk.Label(pw, text="🔍  Finding Similar Photos…",
                 bg=BG3, fg=TEXT_BRIGHT,
                 font=("Segoe UI", 11, "bold")).pack(pady=(18, 4))
        lbl_phase = tk.Label(pw, text="Phase 1 of 3 — Computing hashes", bg=BG3, fg=ACCENT,
                             font=("Segoe UI", 9, "bold"))
        lbl_phase.pack()
        lbl_prog = tk.Label(pw, text="", bg=BG3, fg=TEXT_DIM,
                            font=("Segoe UI", 9))
        lbl_prog.pack()
        bar_outer = tk.Frame(pw, bg="#333", height=12)
        bar_outer.pack(fill="x", padx=24, pady=8)
        bar_outer.pack_propagate(False)
        bar_fill = tk.Label(bar_outer, bg="#5a3a6a", height=12)
        bar_fill.place(x=0, y=0, relheight=1.0, width=0)

        cancel_flag = [False]

        def _do_cancel():
            cancel_flag[0] = True
            btn_cancel.config(state="disabled", text="Cancelling…")

        btn_cancel = tk.Button(pw, text="Cancel", bg="#442222", fg="white",
                               font=("Segoe UI", 9, "bold"), relief="flat",
                               padx=16, pady=3, cursor="hand2",
                               command=_do_cancel)
        btn_cancel.pack(pady=(4, 8))
        pw.protocol("WM_DELETE_WINDOW", _do_cancel)

        def worker():
            total  = len(files)
            hashes = {}   # path -> int (64-bit pHash)

            # ── Phase 1: compute hashes from blob thumbnails ──────────────
            # Read blobs in folder-grouped batches for NAS efficiency
            jpeg_map = thumb_get_many(files)

            for i, fpath in enumerate(files):
                if cancel_flag[0]: break
                try:
                    jpeg = jpeg_map.get(fpath)
                    if jpeg:
                        _PILif.LOAD_TRUNCATED_IMAGES = True
                        img = _PILi.open(_io.BytesIO(jpeg))
                        img.load()
                        _PILif.LOAD_TRUNCATED_IMAGES = False
                    else:
                        img = _PILi.open(_longpath(fpath))
                    hashes[fpath] = _phash(img, hash_size=8)
                except Exception:
                    hashes[fpath] = None

                if i % 10 == 0 or i == total - 1:
                    pct = (i + 1) / total
                    def _upd(p=pct, n=i+1, t=total):
                        try:
                            bar_fill.place(x=0, y=0, relheight=1.0,
                                           width=int((bar_outer.winfo_width() or 370) * p))
                            lbl_prog.config(text=f"{n} / {t} hashed")
                        except: pass
                    self.win.after(0, _upd)

            if cancel_flag[0]:
                self.win.after(0, pw.destroy)
                return

            # ── Phase 2: find pairs — pHash + optional histogram ──────────
            valid = [(p, h) for p, h in hashes.items() if h is not None]
            pairs = []
            n = len(valid)
            for i in range(n):
                if cancel_flag[0]: break
                for j in range(i + 1, n):
                    dist = _hamming(valid[i][1], valid[j][1])
                    if dist <= phash_thresh:
                        pairs.append((valid[i][0], valid[j][0]))

            # Update phase label
            self.win.after(0, lambda: lbl_phase.config(
                text=f"Phase 2 of 3 — Comparing pairs ({len(valid)} images)"))

            if not pairs:
                def _none():
                    try: pw.destroy()
                    except: pass
                    messagebox.showinfo("Find Similar",
                        f"No similar images found at sensitivity {threshold}.\n\n"
                        f"Try increasing the sensitivity slider.",
                        parent=self.win)
                    try: self.btn_similar.config(bg="#5a3a6a")
                    except: pass
                self.win.after(0, _none)
                return

            # ── Phase 3: cluster and finish ───────────────────────────────
            self.win.after(0, lambda: lbl_phase.config(text="Phase 3 of 3 — Clustering groups"))
            self._finish_similar_scan(pw, pairs, files, cancel_flag,
                                      threshold_desc=f"framing threshold {threshold}")

        import threading as _thr
        _thr.Thread(target=worker, daemon=True).start()


    def _toggle_group_summary(self):
        """Toggle between flat similar view and compressed group-summary view."""
        if not self._in_similar_view:
            messagebox.showinfo("Find Similar first",
                "Run Find Similar first, then use Compress to collapse groups.",
                parent=self.win)
            return

        if self._in_group_summary:
            # Return to flat similar view
            self._in_group_summary = False
            try: self.btn_compress.config(bg="#3a3a6a", text="Compress")
            except: pass
            # Rebuild flat sorted_files from clusters + singles
            flat = []
            clustered = set()
            for grp in self._group_clusters:
                flat.extend(grp)
                clustered.update(grp)
            # Append singles (files in _all_files not in any cluster)
            singles = [p for p in self._all_files if p not in clustered
                       and p not in {pp for grp in self._group_clusters for pp in grp}]
            # Re-derive from _similar_groups — files with no group entry are singles
            all_known = {p for grp in self._group_clusters for p in grp}
            # Rebuild similar_groups map
            sg = {}
            for gi, grp in enumerate(self._group_clusters, start=1):
                for p in grp:
                    sg[p] = gi
            self._similar_groups = sg
            # Singles: files not in any cluster
            current_singles = [p for p in self._all_files if p not in all_known]
            self._all_files = flat + current_singles
            self._page_num  = 0
            n_groups = len(self._group_clusters)
            n_dupes  = len(all_known)
            self.lbl_folder.config(
                text=f"🔍  Similar — {n_groups} group{'s' if n_groups!=1 else ''}, "
                     f"{n_dupes} images")
            self._update_view_status(
                f"🔍  Similar  —  {n_groups} group{'s' if n_groups!=1 else ''}, "
                f"{n_dupes} similar images")
            self._show_page()
        else:
            # Enter group summary — one representative thumb per group
            if not self._group_clusters:
                messagebox.showinfo("No groups",
                    "No similar groups found. Run Find Similar first.",
                    parent=self.win)
                return
            self._in_group_summary = True
            try: self.btn_compress.config(bg="#7a6aba", text="Expand")
            except: pass
            # One representative (first file) per group
            representatives = [grp[0] for grp in self._group_clusters]
            # Keep singles at end
            all_clustered = {p for grp in self._group_clusters for p in grp}
            singles = [p for p in self._all_files if p not in all_clustered]
            self._all_files = representatives + singles
            self._page_num  = 0
            n = len(self._group_clusters)
            self.lbl_folder.config(
                text=f"🔍  Group Summary — {n} group{'s' if n!=1 else ''} "
                     f"— click a group to review")
            self._update_view_status(
                f"🔍  Group Summary  —  {n} group{'s' if n!=1 else ''}  "
                f"—  click to review")
            self._show_page()

    def _open_group_review(self, representative_path):
        """Enter 2-panel group review for the group containing representative_path."""
        if representative_path not in self._similar_groups:
            return
        gi = self._similar_groups[representative_path]
        if gi < 1 or gi > len(self._group_clusters):
            return
        group_files = list(self._group_clusters[gi - 1])
        n = len(group_files)

        # Save current state for back-navigation
        self._pre_group_state = {
            "all_files":        list(self._all_files),
            "page_num":         self._page_num,
            "in_similar_view":  self._in_similar_view,
            "in_group_summary": self._in_group_summary,
            "similar_groups":   dict(self._similar_groups),
            "group_clusters":   [list(g) for g in self._group_clusters],
            "lbl_folder":       self.lbl_folder.cget("text"),
            "view_status":      (self.lbl_view_status.cget("text")
                                 if self.lbl_view_status else ""),
            "reviewing_gi":     gi,
        }

        # Load group files into main grid (1-panel — user can enter 2-panel themselves)
        # Exit 2-panel first if active, to start clean
        if self._panel_mode == "2":
            self._toggle_panel_mode()

        self._all_files        = group_files
        self._page_num         = 0
        self._selected.clear()
        self._last_click_idx   = None
        self._in_group_review  = True
        self._in_group_summary = False

        label = f"🔍  Similar Group {gi}  —  {n} image{'s' if n!=1 else ''}"
        self.lbl_folder.config(text=label)
        self._update_view_status(
            f"🔍  Group {gi}  —  {n} images  "
            f"—  select then use ⊞ 2-Panel or Operations")
        try: self._btn_back_group.pack(side="left", padx=(0,4))
        except: pass
        self._show_page()

    def _exit_group_review(self):
        """Return from group review back to group summary (or flat similar view)."""
        if not getattr(self, "_in_group_review", False): return
        if self._pre_group_state is None:
            self._in_group_review = False
            self._exit_special_view(); return

        s  = self._pre_group_state
        gc = [list(g) for g in s["group_clusters"]]
        gi = s.get("reviewing_gi", None)

        # Update the reviewed group based on surviving files
        if gi is not None and 1 <= gi <= len(gc):
            surviving = [p for p in gc[gi-1] if os.path.exists(_longpath(p))]
            if len(surviving) <= 1:
                gc.pop(gi - 1)
            else:
                gc[gi - 1] = surviving

        # Rebuild similar_groups map
        sg = {}
        for i, grp in enumerate(gc, start=1):
            for p in grp: sg[p] = i

        all_clustered = {p for grp in gc for p in grp}
        singles = [p for p in s["all_files"]
                   if p not in {pp for grp in s["group_clusters"] for pp in grp}
                   and p not in all_clustered]

        self._group_clusters   = gc
        self._similar_groups   = sg
        self._in_group_review  = False
        try: self._btn_back_group.pack_forget()
        except: pass
        self._in_similar_view  = s["in_similar_view"]
        self._in_group_summary = s["in_group_summary"]
        self._page_num         = 0
        self._pre_group_state  = None
        self._selected.clear()

        if s["in_group_summary"]:
            reps = [grp[0] for grp in gc]
            self._all_files = reps + singles
            n = len(gc)
            self.lbl_folder.config(
                text=f"🔍  Group Summary — {n} group{'s' if n!=1 else ''} — click a group to review")
            self._update_view_status(
                f"🔍  Group Summary  —  {n} group{'s' if n!=1 else ''}  —  click to review")
        else:
            flat = [p for grp in gc for p in grp]
            self._all_files = flat + singles
            self.lbl_folder.config(text=s["lbl_folder"])
            self._update_view_status(s["view_status"])

        self._show_page()


    def _run_find_similar_colours(self, threshold, files):
        """Find similar photos by colour histogram correlation.
        threshold: float 0..1 (e.g. 0.88 = 88% similarity required).
        The dialog passes threshold/100 so 88 -> 0.88.
        """
        import numpy as _np2
        from PIL import Image as _PILi2, ImageFile as _PILif2

        pw = tk.Toplevel(self.win)
        pw.title("Finding Similar Photos")
        pw.configure(bg=BG3)
        pw.resizable(False, False)
        pw.transient(self.win)
        pw.grab_set()
        self._centre_window(pw, 420, 150)

        tk.Label(pw, text="🎨  Finding Similar Colours…",
                 bg=BG3, fg=TEXT_BRIGHT,
                 font=("Segoe UI", 11, "bold")).pack(pady=(18, 4))
        lbl_phase = tk.Label(pw, text="Phase 1 of 2 — Computing histograms",
                             bg=BG3, fg=ACCENT, font=("Segoe UI", 9, "bold"))
        lbl_phase.pack()
        lbl_prog = tk.Label(pw, text="", bg=BG3, fg=TEXT_DIM, font=("Segoe UI", 9))
        lbl_prog.pack()
        bar_outer = tk.Frame(pw, bg="#333", height=12)
        bar_outer.pack(fill="x", padx=24, pady=8)
        bar_outer.pack_propagate(False)
        bar_fill = tk.Label(bar_outer, bg="#4488cc", height=12)
        bar_fill.place(x=0, y=0, relheight=1.0, width=0)

        cancel_flag = [False]
        def _do_cancel():
            cancel_flag[0] = True
            btn_cancel.config(state="disabled", text="Cancelling…")
        btn_cancel = tk.Button(pw, text="Cancel", bg="#442222", fg="white",
                               font=("Segoe UI", 9, "bold"), relief="flat",
                               padx=16, pady=3, cursor="hand2", command=_do_cancel)
        btn_cancel.pack(pady=(4, 8))
        pw.protocol("WM_DELETE_WINDOW", _do_cancel)

        def worker():
            total = len(files)
            hists = {}  # path -> (h_r, h_g, h_b) normalised histograms

            jpeg_map = thumb_get_many(files)

            for i, fpath in enumerate(files):
                if cancel_flag[0]: break
                try:
                    jpeg = jpeg_map.get(fpath)
                    if jpeg:
                        _PILif2.LOAD_TRUNCATED_IMAGES = True
                        img = _PILi2.open(__import__('io').BytesIO(jpeg))
                        img.load()
                        _PILif2.LOAD_TRUNCATED_IMAGES = False
                    else:
                        img = _PILi2.open(_longpath(fpath))
                    img = img.convert('RGB').resize((64, 64), _PILi2.BILINEAR)
                    arr = _np2.array(img, dtype=_np2.float32)
                    bins = 64
                    channels = []
                    for c in range(3):
                        h, _ = _np2.histogram(arr[:,:,c].flatten(), bins=bins, range=(0,256))
                        h = h.astype(_np2.float32)
                        h /= (h.sum() + 1e-10)
                        channels.append(h)
                    hists[fpath] = channels
                except Exception:
                    hists[fpath] = None

                if i % 10 == 0 or i == total - 1:
                    pct = (i+1) / total
                    def _upd(p=pct, n=i+1, t=total):
                        try:
                            bar_fill.place(x=0, y=0, relheight=1.0,
                                           width=int((bar_outer.winfo_width() or 370) * p))
                            lbl_prog.config(text=f"{n} / {t} processed")
                        except: pass
                    self.win.after(0, _upd)

            if cancel_flag[0]:
                self.win.after(0, pw.destroy); return

            self.win.after(0, lambda: lbl_phase.config(
                text=f"Phase 2 of 2 — Comparing colour similarity"))

            valid = [(p, h) for p, h in hists.items() if h is not None]
            pairs = []
            n = len(valid)
            for i in range(n):
                if cancel_flag[0]: break
                for j in range(i+1, n):
                    # Mean correlation across R,G,B channels
                    scores = []
                    for c in range(3):
                        h1 = valid[i][1][c]; h2 = valid[j][1][c]
                        m1 = h1.mean(); m2 = h2.mean()
                        num = _np2.sum((h1-m1)*(h2-m2))
                        den = _np2.sqrt(_np2.sum((h1-m1)**2) * _np2.sum((h2-m2)**2)) + 1e-10
                        scores.append(float(num/den))
                    if _np2.mean(scores) >= threshold:
                        pairs.append((valid[i][0], valid[j][0]))

            self._finish_similar_scan(pw, pairs, files, cancel_flag,
                                      threshold_desc=f"colour similarity ≥ {int(threshold*100)}%")

        import threading as _thr2
        _thr2.Thread(target=worker, daemon=True).start()

    def _run_find_similar_dates(self, date_range, files):
        """Find photos taken within date_range of each other using EXIF."""
        from PIL import Image as _PILi3, ExifTags as _ET3

        range_days = {"same day": 0, "1 day": 1, "2 days": 2, "5 days": 5}
        max_delta = range_days.get(date_range, 0)

        pw = tk.Toplevel(self.win)
        pw.title("Finding Similar Photos")
        pw.configure(bg=BG3)
        pw.resizable(False, False)
        pw.transient(self.win)
        pw.grab_set()
        self._centre_window(pw, 420, 150)

        tk.Label(pw, text="📅  Finding Photos by Date…",
                 bg=BG3, fg=TEXT_BRIGHT,
                 font=("Segoe UI", 11, "bold")).pack(pady=(18, 4))
        lbl_prog = tk.Label(pw, text="Reading EXIF dates…",
                            bg=BG3, fg=TEXT_DIM, font=("Segoe UI", 9))
        lbl_prog.pack()
        bar_outer = tk.Frame(pw, bg="#333", height=12)
        bar_outer.pack(fill="x", padx=24, pady=8)
        bar_outer.pack_propagate(False)
        bar_fill = tk.Label(bar_outer, bg="#44aa66", height=12)
        bar_fill.place(x=0, y=0, relheight=1.0, width=0)

        cancel_flag = [False]
        def _do_cancel():
            cancel_flag[0] = True
        btn_cancel = tk.Button(pw, text="Cancel", bg="#442222", fg="white",
                               font=("Segoe UI", 9, "bold"), relief="flat",
                               padx=16, pady=3, cursor="hand2", command=_do_cancel)
        btn_cancel.pack(pady=(4, 8))
        pw.protocol("WM_DELETE_WINDOW", _do_cancel)

        def worker():
            import datetime as _dt
            total = len(files)
            dates = {}  # path -> date object

            for i, fpath in enumerate(files):
                if cancel_flag[0]: break
                try:
                    with _PILi3.open(_longpath(fpath)) as im:
                        exif = im._getexif() or {}
                    # Tag 36867 = DateTimeOriginal, 36868 = DateTimeDigitised, 306 = DateTime
                    for tag in (36867, 36868, 306):
                        if tag in exif:
                            try:
                                dt = _dt.datetime.strptime(exif[tag], "%Y:%m:%d %H:%M:%S")
                                dates[fpath] = dt.date()
                                break
                            except: pass
                except: pass

                if i % 20 == 0 or i == total - 1:
                    pct = (i+1) / total
                    def _upd(p=pct, n=i+1, t=total):
                        try:
                            bar_fill.place(x=0, y=0, relheight=1.0,
                                           width=int((bar_outer.winfo_width() or 370) * p))
                            lbl_prog.config(text=f"{n} / {t}  ({len(dates)} with dates)")
                        except: pass
                    self.win.after(0, _upd)

            if cancel_flag[0]:
                self.win.after(0, pw.destroy); return

            if not dates:
                def _none():
                    try: pw.destroy()
                    except: pass
                    messagebox.showinfo("No EXIF dates",
                        "No photos with EXIF date information found in this folder.",
                        parent=self.win)
                self.win.after(0, _none); return

            # Build pairs within date_range
            valid = [(p, d) for p, d in dates.items()]
            pairs = []
            import datetime as _dt2
            for i in range(len(valid)):
                if cancel_flag[0]: break
                for j in range(i+1, len(valid)):
                    delta = abs((valid[i][1] - valid[j][1]).days)
                    if delta <= max_delta:
                        pairs.append((valid[i][0], valid[j][0]))

            lbl = "same day" if max_delta == 0 else f"within {max_delta} day{'s' if max_delta>1 else ''}"
            self._finish_similar_scan(pw, pairs, files, cancel_flag,
                                      threshold_desc=f"date {lbl}")

        import threading as _thr3
        _thr3.Thread(target=worker, daemon=True).start()

    def _finish_similar_scan(self, pw, pairs, files, cancel_flag, threshold_desc=""):
        """Common finish logic for all similarity modes — clusters pairs and shows results."""
        import numpy as _npf

        # Union-find clustering
        parent = {p: p for p in files}
        def find(x):
            while parent[x] != x: parent[x] = parent[parent[x]]; x = parent[x]
            return x
        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb: parent[ra] = rb

        for a, b in pairs:
            union(a, b)

        clusters = {}
        for p in files:
            r = find(p)
            clusters.setdefault(r, []).append(p)

        dup_clusters = sorted([v for v in clusters.values() if len(v) > 1],
                               key=lambda g: -len(g))
        singles = [p for p in files if len(clusters.get(find(p), [])) == 1]

        if not dup_clusters:
            def _none():
                try: pw.destroy()
                except: pass
                messagebox.showinfo("Find Similar",
                    f"No similar images found ({threshold_desc}).\n\nTry a looser sensitivity setting.",
                    parent=self.win)
                try: self.btn_similar.config(bg="#5a3a6a")
                except: pass
            self.win.after(0, _none); return

        sorted_files = [p for grp in dup_clusters for p in grp] + singles
        sg = {}
        for gi, grp in enumerate(dup_clusters, start=1):
            for p in grp: sg[p] = gi

        n_groups = len(dup_clusters)
        n_dupes  = sum(len(g) for g in dup_clusters)

        def _finish(sf=sorted_files, s=sg, ng=n_groups, nd=n_dupes,
                    dc=dup_clusters):
            try: pw.destroy()
            except: pass
            self._in_similar_view  = True
            self._in_tagged_view   = False
            self._in_cull_view     = False
            self._in_located_view  = False
            self._in_group_summary = False
            self._similar_groups   = s
            self._group_clusters   = dc
            self._all_files        = sf
            self._page_num         = 0
            self.lbl_folder.config(
                text=f"🔍  Similar ({threshold_desc}) — "
                     f"{ng} group{'s' if ng!=1 else ''}, {nd} images")
            self._update_view_status(
                f"🔍  Similar  —  {ng} group{'s' if ng!=1 else ''}, "
                f"{nd} images found")
            try: self.btn_similar.config(bg="#9a6aba")
            except: pass
            self._show_page()
            self._status(
                f"Find Similar: {ng} group{'s' if ng!=1 else ''} found "
                f"({nd} images). Non-similar images shown at end.")
        self.win.after(0, _finish)


    def _ftediti_ipc_dir(self):
        """Return IPC folder path, creating it if needed. Matches FTEditI logic."""
        import configparser as _cp
        ini = os.path.join(os.path.dirname(os.path.abspath(__file__)), "FileTagger.ini")
        path = None
        if os.path.exists(ini):
            cfg = _cp.ConfigParser(strict=False)
            cfg.read(ini)
            path = cfg.get("FileTagger", "ipc_folder", fallback="").strip()
        if not path:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "FT_IPC")
        os.makedirs(path, exist_ok=True)
        return path

    def _ftediti_script(self):
        """Return path to FTEditI.py alongside this script."""
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "FTImgedit.py")

    def _launch_ftediti(self, path):
        """Launch FTEditI if not running, then send it the file path."""
        import subprocess
        script = self._ftediti_script()
        if not os.path.isfile(script):
            messagebox.showwarning("FTEditI not found",
                f"FTImgedit.py not found alongside this script:\n{script}",
                parent=self.win)
            return
        # Check if already running by looking for a live process handle
        proc = getattr(self, '_ftediti_proc', None)
        if proc is None or proc.poll() is not None:
            # Not running — launch in embedded mode
            try:
                self._ftediti_proc = subprocess.Popen(
                    [sys.executable, script, "--embedded"])
                # Give it a moment to start before sending the request
                self.win.after(800, lambda: self._send_to_ftediti(path))
            except Exception as e:
                messagebox.showerror("Cannot launch FTEditI", str(e), parent=self.win)
                return
        else:
            # Already running — send immediately
            self._send_to_ftediti(path)

    def _send_to_ftediti(self, path, files=None):
        """Write request file for FTImgedit — single file or file list with optional CENTER."""
        if not path or not os.path.isfile(path): return
        self._ftediti_seq = getattr(self, '_ftediti_seq', 0) + 1
        # If no explicit list, use current selection (filtered to existing files)
        if files is None:
            sel = [f for f in self._selected if os.path.isfile(f)]
            files = sel if sel else [path]
        try:
            req_path = os.path.join(self._ftediti_ipc_dir(), "FTImgedit_request.csv")
            with open(req_path, "w", encoding="utf-8") as f:
                f.write(f"SEQ,{self._ftediti_seq}\n")
                if path and path in files and len(files) > 1:
                    f.write(f"CENTER,{path}\n")
                for fp in files:
                    f.write(f"{fp}\n")
        except Exception as e:
            print(f"FTDBX: could not write FTImgedit request: {e}")
            return
        # Start polling for result
        if not getattr(self, '_ftediti_polling', False):
            self._ftediti_polling = True
            self.win.after(500, self._poll_ftediti_result)

    def _poll_ftediti_result(self):
        """Check for FTImgedit result file every 500ms."""
        res_path = os.path.join(self._ftediti_ipc_dir(), "FTImgedit_result.csv")
        if os.path.exists(res_path):
            try:
                with open(res_path, encoding="utf-8") as f:
                    lines = [l.strip() for l in f if l.strip()]
                os.remove(res_path)
                seq     = int(lines[0].split(",", 1)[1]) if lines and lines[0].startswith("SEQ,") else -1
                outcome = lines[1].split(",", 1)[1] if len(lines) > 1 and lines[1].startswith("OUTCOME,") else "DISCARDED"
                path    = lines[2] if len(lines) > 2 else ""
                if seq == getattr(self, '_ftediti_seq', -1):
                    self._handle_ftediti_result({"outcome": outcome, "path": path})
                    self._ftediti_polling = False
                    return
            except Exception as e:
                print(f"FTDBX: FTImgedit result error: {e}")
        self.win.after(500, self._poll_ftediti_result)

    def _handle_ftediti_result(self, res):
        """Handle OVERWRITE / SAVED_NEW / DISCARDED from FTEditI."""
        outcome = res.get("outcome", "DISCARDED")
        path    = res.get("path", "")
        if outcome == "DISCARDED":
            return
        if outcome == "OVERWRITE":
            if path:
                self._make_thumb(path)
                self._refresh_thumb_cell(path)
        elif outcome == "SAVED_NEW":
            if path and os.path.isfile(path):
                self._make_thumb(path)
                orig = path.replace("_ed.jpg", ".jpg")  # best-effort
                try:
                    idx = self._all_files.index(orig)
                    if path not in self._all_files:
                        self._all_files.insert(idx + 1, path)
                except ValueError:
                    if path not in self._all_files:
                        self._all_files.append(path)
                self._refresh_thumb_cell(path) if path in self._all_files else None
                # Reload the current page so new thumb appears
                self.win.after(100, self._reload_page)

    def _reload_page(self):
        """Soft reload — re-renders current page without clearing scroll position."""
        try:
            self._load_page_thread(self._page_start if hasattr(self, '_page_start') else 0)
        except Exception:
            pass

    # ── FTMapimg integration ──────────────────────────────────────────────────

    def _ftmapimg_script(self):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "FTMap.py")

    def _launch_ftmapimg_from_selection(self, center_path=None):
        """Launch FTMap with GPS files from current selection, or all files in folder."""
        # Use selection if any, else fall back to all files in current folder
        candidates = list(self._selected) if self._selected else list(self._all_files)
        gps_files  = [f for f in candidates if _get_gps_coords(f)]
        if not gps_files:
            messagebox.showinfo("No GPS images",
                "No GPS-tagged images found in the current selection.",
                parent=self.win)
            return
        folder = os.path.dirname(gps_files[0]) if gps_files else ""
        self._launch_ftmapimg(folder, center_path=center_path, files=gps_files)

    def _launch_ftmapimg(self, folder, center_path=None, files=None):
        """Launch FTMap if not running, then send it the file list."""
        import subprocess
        script = self._ftmapimg_script()
        if not os.path.isfile(script):
            messagebox.showwarning("FTMap not found",
                f"FTMap.py not found alongside this script:\n{script}",
                parent=self.win)
            return
        proc = getattr(self, '_ftmapimg_proc', None)
        if proc is None or proc.poll() is not None:
            try:
                self._ftmapimg_proc = subprocess.Popen(
                    [sys.executable, script, "--embedded"])
                self.win.after(1000, lambda: self._send_to_ftmapimg(folder, center_path, files))
            except Exception as e:
                messagebox.showerror("Cannot launch FTMap", str(e), parent=self.win)
        else:
            self._send_to_ftmapimg(folder, center_path, files)

    def _send_to_ftmapimg(self, folder, center_path=None, files=None):
        """Write IPC request for FTMap — sends file list + optional CENTER."""
        self._ftmapimg_seq = getattr(self, '_ftmapimg_seq', 0) + 1
        # Build file list: use provided list, else scan folder for GPS files
        if files is None:
            files = _scan_folder_for_gps(folder) if folder else []
        try:
            ipc_dir  = self._ftediti_ipc_dir()
            req_path = os.path.join(ipc_dir, "FTMap_request.csv")
            with open(req_path, "w", encoding="utf-8") as f:
                f.write(f"SEQ,{self._ftmapimg_seq}\n")
                if center_path:
                    f.write(f"CENTER,{center_path}\n")
                for fp in files:
                    f.write(f"{fp}\n")
        except Exception as e:
            print(f"FTDBX: could not write FTMap request: {e}")

    # ── FTFiler integration ───────────────────────────────────────────────────

    def _ftfiler_script(self):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "FTFiler.py")

    def _launch_ftfiler_rename(self):
        """✎ Rename — launch FTFiler in RENAME mode with current selection.
        Works from both folder view and collection view."""
        files = [f for f in self._selected if os.path.isfile(f)]
        if not files:
            messagebox.showinfo("No files selected",
                "Select files to rename first.", parent=self.win)
            return
        self._launch_ftfiler(files, mode="RENAME")

    def _launch_ftfiler_filemgmt(self):
        """📂 File Mgmt — launch FTFiler in FILEMGMT mode at current folder.
        Not available from collection view."""
        if getattr(self, '_in_tagged_view', False):
            messagebox.showinfo("File Management",
                "File Management is only available when browsing a folder.\n\n"
                "Switch to folder view first.", parent=self.win)
            return
        folder = getattr(self, 'current_folder', '')
        root   = self.mode_cfg.get('root', '')
        if not folder or not os.path.isdir(folder):
            messagebox.showinfo("File Management",
                "No folder is currently selected.", parent=self.win)
            return
        self._launch_ftfiler([], mode="FILEMGMT", root=root, folder=folder)

    def _launch_ftfiler_from_selection(self):
        """Legacy — kept for any remaining call sites."""
        self._launch_ftfiler_rename()

    def _launch_ftfiler(self, files, mode="RENAME", root="", folder=""):
        """Launch FTFiler if not running, then send request."""
        import subprocess
        script = self._ftfiler_script()
        if not os.path.isfile(script):
            messagebox.showwarning("FTFiler not found",
                f"FTFiler.py not found alongside this script:\n{script}",
                parent=self.win)
            return
        proc = getattr(self, '_ftfiler_proc', None)
        if proc is None or proc.poll() is not None:
            # Write request FIRST so FTFiler can read it immediately on startup
            self._send_to_ftfiler(files, mode=mode, root=root, folder=folder)
            try:
                flag = "--rename" if mode == "RENAME" else "--embedded"
                self._ftfiler_proc = subprocess.Popen(
                    [sys.executable, script, flag])
            except Exception as e:
                messagebox.showerror("Cannot launch FTFiler", str(e), parent=self.win)
        else:
            self._send_to_ftfiler(files, mode=mode, root=root, folder=folder)

    def _send_to_ftfiler(self, files, mode="RENAME", root="", folder=""):
        """Write FTFiler_request.csv."""
        self._ftfiler_seq = getattr(self, '_ftfiler_seq', 0) + 1
        try:
            req_path = os.path.join(self._ftediti_ipc_dir(), "FTFiler_request.csv")
            with open(req_path, "w", encoding="utf-8") as f:
                f.write(f"SEQ,{self._ftfiler_seq}\n")
                f.write(f"MODE,{mode}\n")
                if root:
                    f.write(f"ROOT,{root}\n")
                if folder:
                    f.write(f"FOLDER,{folder}\n")
                for fp in files:
                    f.write(f"{fp}\n")
        except Exception as e:
            print(f"FTDBX: could not write FTFiler request: {e}")
            return
        # Only poll for results in RENAME mode
        if mode == "RENAME" and not getattr(self, '_ftfiler_polling', False):
            self._ftfiler_polling = True
            self.win.after(500, self._poll_ftfiler_result)

    def _poll_ftfiler_result(self):
        """Check for FTFiler_result.csv every 500ms — keeps polling until proc exits."""
        res_path = os.path.join(self._ftediti_ipc_dir(), "FTFiler_result.csv")
        if os.path.exists(res_path):
            try:
                with open(res_path, encoding="utf-8") as f:
                    lines = [l.strip() for l in f if l.strip()]
                os.remove(res_path)
                seq = int(lines[0].split(",", 1)[1]) if lines and lines[0].startswith("SEQ,") else -1
                if seq == getattr(self, '_ftfiler_seq', -1):
                    self._handle_ftfiler_result(lines[1:])
            except Exception as e:
                print(f"FTDBX: FTFiler result error: {e}")
        # Keep polling while FTFiler process is alive
        proc = getattr(self, '_ftfiler_proc', None)
        if proc is not None and proc.poll() is None:
            self.win.after(500, self._poll_ftfiler_result)
        else:
            self._ftfiler_polling = False

    def _handle_ftfiler_result(self, lines):
        """Process rename pairs from FTFiler result — each line is old_path,new_path."""
        renamed = 0
        for line in lines:
            if "," not in line: continue
            old, new = line.split(",", 1)
            old = old.strip(); new = new.strip()
            if old and new and old != new:
                self._handle_rename(old, new)
                renamed += 1
        if renamed:
            self._status(f"✓  FTFiler: {renamed} file{'s' if renamed!=1 else ''} renamed")
            self._schedule_tree_refresh()
            self.win.after(200, self._reload_page)

    def _handle_rename(self, old_path, new_path):
        """Update all FTDB references when a file is renamed externally."""
        # tagged collection
        if old_path in self.tagged:
            self.tagged.discard(old_path)
            self.tagged.add(new_path)
            if old_path in self.tagged_at:
                self.tagged_at[new_path] = self.tagged_at.pop(old_path)
            try:
                idx = self.tagged_order.index(old_path)
                self.tagged_order[idx] = new_path
            except ValueError: pass
        # current grid
        try:
            idx = self._all_files.index(old_path)
            self._all_files[idx] = new_path
        except ValueError: pass
        # deletion list
        if old_path in self._culled:
            self._culled.discard(old_path)
            self._culled.add(new_path)
            if old_path in self._culled_at:
                self._culled_at[new_path] = self._culled_at.pop(old_path)
        # selection
        if old_path in self._selected:
            self._selected.discard(old_path)
            self._selected.add(new_path)
        # thumbnail cache — rekey
        if _db_conn is not None:
            try:
                _db_conn.execute(
                    "UPDATE thumbnails SET path=? WHERE path=?", (new_path, old_path))
                _db_conn.execute(
                    "UPDATE cull_list SET path=? WHERE path=?", (new_path, old_path))
                _db_conn.commit()
            except Exception as e:
                print(f"_handle_rename DB error: {e}")

    def _show_folders_menu(self):
        btn = self._toolbars["view"].named_btns.get("Folders")
        if not btn: return
        menu = tk.Menu(self.win, tearoff=0)
        menu.add_command(label="🌲  All Folders",
                         command=lambda: self._set_tree_filter("all"))
        menu.add_command(label="📁  Files Only  (hide empty folders)",
                         command=lambda: self._set_tree_filter("files"))
        try:
            x = btn.winfo_rootx()
            y = btn.winfo_rooty() + btn.winfo_height()
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _set_tree_filter(self, mode):
        self._tree_filter_mode = mode
        btn = self._toolbars["view"].named_btns.get("Folders")
        if btn:
            label = "📁 Files Only" if mode == "files" else "🌲 All Folders"
            btn.config(text=label)
        # Rebuild the tree with new filter
        root = self.mode_cfg['root']
        if root and os.path.isdir(root):
            self._populate_tree(root)

    def _show_delete_popup(self):
        """Show Delete Marked button next to the Deletion List label in the left panel."""
        self._hide_delete_popup()
        try:
            parent = getattr(self, '_delete_popup_parent', None)
            if parent is None: return
            btn = tk.Button(parent,
                            text="⚠  Delete",
                            bg="#cc2222", fg="white",
                            font=("Segoe UI", 8, "bold"), relief="flat",
                            padx=6, pady=2, cursor="hand2",
                            activebackground="#991111", activeforeground="white",
                            command=lambda: (self._hide_delete_popup(), self._delete_culled_dialog()))
            btn.pack(side="right", padx=(0, 4), pady=2)
            self._delete_popup = btn
            # Also add Clear List button
            btn2 = tk.Button(parent,
                             text="✖ Clear",
                             bg="#444444", fg="white",
                             font=("Segoe UI", 8, "bold"), relief="flat",
                             padx=6, pady=2, cursor="hand2",
                             activebackground="#222222", activeforeground="white",
                             command=self._ops_clear_cull_list)
            btn2.pack(side="right", padx=(0, 2), pady=2)
            self._delete_popup_clear = btn2
        except: pass

    def _hide_delete_popup(self):
        """Remove the delete button if visible."""
        try:
            if self._delete_popup and self._delete_popup.winfo_exists():
                self._delete_popup.destroy()
        except: pass
        self._delete_popup = None
        try:
            if getattr(self, '_delete_popup_clear', None) and self._delete_popup_clear.winfo_exists():
                self._delete_popup_clear.destroy()
        except: pass
        self._delete_popup_clear = None

    def _delete_culled_dialog(self):
        """Show confirmation dialog and execute deletion of culled files."""
        if not self._culled:
            messagebox.showinfo("Deletion list empty", "No files marked for deletion.", parent=self.win); return
        root_dir = self.mode_cfg['root']
        # Build list of files to act on — those currently visible/selected in cull view
        candidates = sorted(self._culled)
        # Cross-collection check
        other_colls = set()
        for name in _list_collections(root_dir):
            if name == self.collection: continue
            data = _read_collection(name, root_dir)
            other_colls.update(data.keys())
        in_other = [p for p in candidates if p in other_colls]
        missing  = [p for p in candidates if not os.path.exists(p)]

        dlg = tk.Toplevel(self.win); dlg.title("Delete Marked Files")
        dlg.configure(bg=BG3); dlg.grab_set(); dlg.transient(self.win)
        self._centre_window(dlg, 500, 400)
        tk.Label(dlg, text=f"🗑  Delete {len(candidates)} marked files?",
                 bg=BG3, fg=TAGGED_BD, font=("Segoe UI",13,"bold")).pack(pady=(16,6))
        tk.Label(dlg, text=f"{len(candidates)} files marked for deletion",
                 bg=BG3, fg=TEXT_BRIGHT, font=("Segoe UI",10)).pack()
        if in_other:
            tk.Label(dlg, text=f"⚠  {len(in_other)} appear in other collections",
                     bg=BG3, fg=AMBER, font=("Segoe UI",10,"bold")).pack(pady=2)
        if missing:
            tk.Label(dlg, text=f"  {len(missing)} already missing from disk (will be removed from cull list only)",
                     bg=BG3, fg=TEXT_DIM, font=("Segoe UI",9)).pack()
        tk.Frame(dlg, bg="#444", height=1).pack(fill="x", padx=20, pady=8)

        # No default — user must explicitly choose
        action_var = tk.StringVar(value="none")

        # Hidden radio set to "none" — ensures all visible buttons start unselected
        tk.Radiobutton(dlg, variable=action_var, value="none").pack_forget()

        options = [
            ("cull_only",
             "Remove from deletion list only",
             "⚠  Files are NOT moved — they stay in place on disk"),
            ("delete",
             "Move files to _Deleted_Files folder",
             "Files moved to _FileTagger\\_Deleted_Files. Recover from there if needed."),
            ("delete_all",
             "Move to _Deleted_Files and remove from all collections",
             "Same as above, plus removes files from every collection."),
        ]

        for val, label, sublabel in options:
            fr = tk.Frame(dlg, bg=BG3); fr.pack(fill="x", padx=20, pady=3)
            tk.Radiobutton(fr, text=label, variable=action_var, value=val,
                           bg=BG3, fg=TEXT_BRIGHT, selectcolor=BG2,
                           activebackground=BG3, activeforeground=TEXT_BRIGHT,
                           font=("Segoe UI",10,"bold"),
                           command=lambda: btn_confirm.config(state="normal")
                           ).pack(anchor="w")
            tk.Label(fr, text="    " + sublabel, bg=BG3,
                     fg=AMBER if "NOT deleted" in sublabel else TEXT_DIM,
                     font=("Segoe UI",8,"italic")).pack(anchor="w")

        tk.Frame(dlg, bg="#444", height=1).pack(fill="x", padx=20, pady=8)

        def on_confirm():
            action = action_var.get()
            if action == "none":
                messagebox.showwarning("Select an option",
                    "Please select what you want to do.", parent=dlg); return
            if action in ("delete", "delete_all"):
                if not messagebox.askyesno("Move files to _Deleted_Files?",
                    f"This will move {len([c for c in candidates if c not in missing])} files "
                    f"to _FileTagger\\_Deleted_Files.\n\n"
                    "Files on disk are not permanently deleted — you can recover them from that folder.\n\n"
                    "Are you sure?",
                    parent=dlg): return
            dlg.destroy()
            self._execute_cull_delete(candidates, action, missing, in_other, root_dir)

        bf = tk.Frame(dlg, bg=BG3); bf.pack(pady=10)
        btn_confirm = tk.Button(bf, text="  Confirm  ", bg="#cc2222", fg="white",
                  font=("Segoe UI",10,"bold"), relief="flat", padx=10,
                  cursor="hand2", state="disabled", command=on_confirm)
        btn_confirm.pack(side="left", padx=8)
        tk.Button(bf, text="  Cancel  ", bg="#444", fg=TEXT_BRIGHT,
                  font=("Segoe UI",10), relief="flat", padx=10,
                  cursor="hand2", command=dlg.destroy).pack(side="left", padx=8)

    def _execute_cull_delete(self, candidates, action, missing, in_other, root_dir):
        """Perform the actual deletion after confirmation."""
        import shutil as _shutil
        deleted = 0; errors = 0; skipped = 0
        for p in missing:
            self._culled.discard(p); self._culled_at.pop(p, None); skipped += 1
        if action == "cull_only":
            for p in candidates:
                self._culled.discard(p); self._culled_at.pop(p, None)
        else:
            deleted_dir = _deleted_dir(root_dir)
            # Files already IN _Deleted_Files are physically deleted
            # All others are moved there
            for p in candidates:
                if p in missing: continue
                try:
                    norm = os.path.normpath(p)
                    deleted_norm = os.path.normpath(deleted_dir)
                    # Always move to _Deleted_Files — user manages that folder themselves
                    fname = os.path.basename(p)
                    dest  = os.path.join(deleted_dir, fname)
                    if os.path.exists(dest):
                        parent = os.path.basename(os.path.dirname(p))
                        fname  = f"{parent}_{fname}"
                        dest   = os.path.join(deleted_dir, fname)
                    _shutil.move(_longpath(p), dest)
                    self._culled.discard(p); self._culled_at.pop(p, None)
                    self.tagged.discard(p); self.tagged_at.pop(p, None)
                    deleted += 1
                except Exception as e:
                    print(f"Could not move {p}: {e}"); errors += 1
            if action == "delete_all":
                if _db_conn is not None:
                    try:
                        cull_set = {c for c in candidates}
                        _db_conn.executemany(
                            "DELETE FROM collection_items WHERE path=?",
                            [(p,) for p in cull_set]
                        )
                        _db_conn.commit()
                    except Exception as e:
                        print(f"Could not remove culled files from collections: {e}")
        _write_cull_list(root_dir, self._culled, self._culled_at)
        self._save_current_collection()
        self._refresh_tree_stats()
        self._update_cull_radio_hint()
        self._in_cull_view = False
        self._load_folder(self.current_folder)
        msg = f"Cull operation complete.\n\n"
        if action == "cull_only":
            msg += f"{len(candidates)} files removed from cull list."
        else:
            moved = deleted
            if moved:   msg += f"{moved} file{'s' if moved!=1 else ''} moved to _Deleted_Files."
            if skipped: msg += f"\n{skipped} already missing — removed from deletion list."
            if errors:  msg += f"\n{errors} could not be moved."
        messagebox.showinfo("Cull Complete", msg, parent=self.win)

    def _toggle_tag_cb(self, orig, tag_var, outer, inner):
        """Called from spacebar and zoom-window tag checkbox. outer==inner==canvas."""
        from datetime import datetime
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        tagged_set = self._shadow_tagged if self._shadow_active else self.tagged
        tagged_at  = self._shadow_tagged_at if self._shadow_active else self.tagged_at
        if tag_var.get():
            tagged_set.add(orig); tagged_at[orig] = ts
            if not self._shadow_active and orig not in self.tagged_order:
                self.tagged_order.append(orig)
        else:
            tagged_set.discard(orig); tagged_at.pop(orig, None)
            if not self._shadow_active:
                try: self.tagged_order.remove(orig)
                except ValueError: pass
        self._cv_repaint(outer, *self._cell_colours(orig))
        self._update_coll_info()
        if self._shadow_active: self._shadow_update_btn()
        else: self._schedule_save(); self._schedule_tree_refresh()

    def _on_spacebar(self, event):
        orig = getattr(self, '_focused_orig', None)
        if not orig: return
        for item in self.thumb_widgets:
            if item[2] == orig:
                cv, tv = item[0], item[6]
                if tv is not None:
                    tv.set(not tv.get())
                    self._toggle_tag_cb(orig, tv, cv, cv)
                break

    def _tag_all_visible(self):
        from datetime import datetime
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        tagged_set = self._shadow_tagged if self._shadow_active else self.tagged
        tagged_at  = self._shadow_tagged_at if self._shadow_active else self.tagged_at
        for orig in self._all_files:
            if orig not in tagged_set:
                tagged_set.add(orig); tagged_at[orig] = ts
                if not self._shadow_active and orig not in self.tagged_order:
                    self.tagged_order.append(orig)
        for item in self.thumb_widgets:
            self._cv_repaint(item[0], *self._cell_colours(item[2]))
        self._update_coll_info()
        if self._shadow_active: self._shadow_update_btn()
        else: self._schedule_save(); self._schedule_tree_refresh()

    def _untag_all_visible(self):
        tagged_set = self._shadow_tagged if self._shadow_active else self.tagged
        tagged_at  = self._shadow_tagged_at if self._shadow_active else self.tagged_at
        for orig in self._all_files:
            tagged_set.discard(orig); tagged_at.pop(orig, None)
            if not self._shadow_active:
                try: self.tagged_order.remove(orig)
                except ValueError: pass
        for item in self.thumb_widgets:
            self._cv_repaint(item[0], *self._cell_colours(item[2]))
        self._update_coll_info()
        if self._shadow_active: self._shadow_update_btn()
        else: self._schedule_save(); self._schedule_tree_refresh()

    def _on_thumb_click(self, event, orig, idx):
        """Click = select only this. Ctrl+click = toggle. Shift+click = range. Double-click = zoom."""
        ctrl  = (event.state & 0x4) != 0
        shift = (event.state & 0x1) != 0 or (event.state & 0x100) != 0
        # On some platforms Button-1 state bleeds into 0x100 — strip it for cleaner check
        shift = shift and not ctrl  # ctrl takes priority
        if ctrl:
            if orig in self._selected:
                self._selected.discard(orig)
            else:
                self._selected.add(orig)
                self._last_click_idx = idx
            self._repaint_selection()
            self._show_selection_panel()
        elif shift and self._last_click_idx is not None:
            lo = min(self._last_click_idx, idx)
            hi = max(self._last_click_idx, idx)
            # Build set of visible files (not hidden by size filter)
            visible_files = {item[2] for item in self.thumb_widgets
                             if item[0].winfo_ismapped()}
            for i in range(lo, hi + 1):
                if 0 <= i < len(self._all_files):
                    f = self._all_files[i]
                    if f in visible_files:  # only select if visible
                        self._selected.add(f)
            self._repaint_selection()
            self._show_selection_panel()
        else:
            # Plain click — select only this one
            self._selected.clear()
            self._selected.add(orig)
            self._last_click_idx = idx
            self._repaint_selection()
            self._show_selection_panel()

    def _repaint_selection(self, changed_only=None):
        """Update border colours on left-panel cells to reflect selection state.
        changed_only: set of paths whose state changed — only repaint those (2-panel fast path)."""
        two_panel = getattr(self, "_panel_mode", "1") == "2"
        for item in self.thumb_widgets:
            cv, _, orig = item[0], item[1], item[2]
            try:
                if two_panel:
                    if changed_only is not None and orig not in changed_only:
                        continue
                    is_placed = orig in self._placed_set
                    is_sel    = (orig in self._selected) and not is_placed
                    cv.configure(highlightthickness=3 if is_sel else 0,
                                 highlightbackground=SELECT_BD)
                else:
                    is_sel  = orig in self._selected
                    is_cull = orig in self._culled
                    cv.configure(bg=UNTAGGED_BD, highlightthickness=0)
                    cv.delete("sel_watermark")
                    cv.delete("sel_watermark_bg")
                    cv.delete("del_watermark")
                    cv.delete("del_watermark_bg")
                    SZ    = self._disp_size
                    PAD   = 8; IX = PAD; IY = PAD
                    img_h = THUMB_IMG_H if SZ == THUMB_SIZE else int(SZ * THUMB_IMG_H / THUMB_SIZE)
                    WM_H  = 28
                    mid_y = IY + img_h // 2

                    if is_cull and is_sel:
                        # Both — DELETING upper, SELECTED lower
                        del_y = mid_y - WM_H // 2 - 2
                        sel_y = mid_y + WM_H // 2 + 2
                        cv.create_rectangle(IX, del_y - WM_H//2, IX+SZ, del_y + WM_H//2,
                                            fill="#000000", stipple="gray50", outline="",
                                            tags="del_watermark_bg")
                        cv.create_text(IX + SZ//2, del_y, text="DELETING", fill="#ffdd00",
                                       font=("Segoe UI", 16, "bold"), tags="del_watermark")
                        cv.create_rectangle(IX, sel_y - WM_H//2, IX+SZ, sel_y + WM_H//2,
                                            fill="#000000", stipple="gray50", outline="",
                                            tags="sel_watermark_bg")
                        cv.create_text(IX + SZ//2, sel_y, text="SELECTED", fill="white",
                                       font=("Segoe UI", 16, "bold"), tags="sel_watermark")
                    elif is_cull:
                        cv.create_rectangle(IX, mid_y - WM_H//2, IX+SZ, mid_y + WM_H//2,
                                            fill="#000000", stipple="gray50", outline="",
                                            tags="del_watermark_bg")
                        cv.create_text(IX + SZ//2, mid_y, text="DELETING", fill="#ffdd00",
                                       font=("Segoe UI", 18, "bold"), tags="del_watermark")
                    elif is_sel:
                        cv.create_rectangle(IX, mid_y - WM_H//2, IX+SZ, mid_y + WM_H//2,
                                            fill="#000000", stipple="gray50", outline="",
                                            tags="sel_watermark_bg")
                        cv.create_text(IX + SZ//2, mid_y, text="SELECTED", fill="white",
                                       font=("Segoe UI", 18, "bold"), tags="sel_watermark")
            except Exception: pass
        self._update_sel_buttons()
    def _update_sel_buttons(self):
        """Enable/disable Tag Sel and Untag Sel based on selection count."""
        n = len(self._selected)
        for btn_name in ("Tag Sel", "Untag Sel"):
            try:
                btn = self._toolbars["tagging"].named_btns.get(btn_name)
                if btn:
                    btn.config(state="normal" if n > 0 else "disabled")
            except: pass



    # ══════════════════════════════════════════════════════════════════════════
    # 2-PANEL VIEW
    # ══════════════════════════════════════════════════════════════════════════

    def _sync_status_divider(self):
        """Position divider and all 2-panel buttons via place(), aligned to sash."""
        if self._panel_mode != "2": return
        try:
            bar = self._status_divider.master
            bar.update_idletasks()
            sash_x       = self._grid_paned.sash_coord(0)[0]
            paned_root_x = self._grid_paned.winfo_rootx()
            bar_root_x   = bar.winfo_rootx()
            bar_h        = bar.winfo_height() or 28
            div_x        = sash_x + paned_root_x - bar_root_x
            PAD = 6

            # Use font metrics to calculate reliable button widths
            import tkinter.font as tkfont
            f = tkfont.Font(family="Segoe UI", size=9, weight="bold")
            w_mr = f.measure("Select ❯") + 30
            w_ds = f.measure("Deselect") + 30
            w_ro = f.measure("Reorder") + 30

            # Divider at sash_x
            self._status_divider.place(x=div_x, y=2, width=2, height=bar_h - 4)

            # Select (Move Right): ends PAD before divider
            self._btn_move_right.place(x=div_x - PAD - w_mr, y=0, width=w_mr, height=bar_h)

            # Deselect: starts PAD after divider
            self._btn_deselect.place(x=div_x + 2 + PAD, y=0, width=w_ds, height=bar_h)

            # Reorder: PAD after Deselect
            self._btn_reorder.place(x=div_x + 2 + PAD + w_ds + PAD, y=0,
                                    width=w_ro, height=bar_h)

            # Save Order: PAD after Reorder
            w_so = f.measure("💾 Save Order") + 30
            self._btn_save_order.place(x=div_x + 2 + PAD + w_ds + PAD + w_ro + PAD, y=0,
                                       width=w_so, height=bar_h)
        except: pass

    def _stub_operations(self):
        import tkinter.messagebox as mb
        mb.showinfo("Operations", "Here is where the operation will be selected.",
                    parent=self.win)

    def _update_sel_bar(self):
        """Update button states and selection count labels."""
        n_sel = len(self._selected)
        n_placed = len(self._placed) if getattr(self, "_panel_mode", "1") == "2" else 0
        # Show Operations panel whenever anything is selected or placed
        if n_sel > 0 or n_placed > 0:
            self._show_selection_panel()
        else:
            self._hide_selection_panel()
        if getattr(self, "_panel_mode", "1") == "2":
            pass  # selection panel already handled above
            left_has_new  = bool(self._selected) and any(f not in self._placed_set for f in self._selected)
            right_has_sel = bool(self._right_sel)
            ins_set       = self._ins_point is not None
            # Move Right: left has unplaced selection
            can_move_right = left_has_new
            # Deselect: right has selection
            can_deselect   = right_has_sel
            # Reorder: right has selection AND insertion point set
            can_reorder    = right_has_sel and ins_set
            # Save Order: active when collection is loaded and right panel has files
            can_save_order = bool(self.collection) and bool(self._placed)
            def _btn_state(btn, active):
                try: btn.config(state="normal" if active else "disabled",
                                font=("Segoe UI", 9, "bold") if active else ("Segoe UI", 9))
                except: pass
            _btn_state(self._btn_move_right, can_move_right)
            _btn_state(self._btn_deselect,   can_deselect)
            _btn_state(self._btn_reorder,    can_reorder)
            _btn_state(self._btn_save_order, can_save_order)
        else:
            # 1-panel — show/hide selected count label
            self._show_selection_panel()
            try:
                if n_sel > 0:
                    noun = "file" if n_sel == 1 else "files"
                    self._lbl_sel_count.config(text=f"Selected: {n_sel} {noun}")
                else:
                    self._lbl_sel_count.config(text="")
            except: pass

    def _toggle_panel_mode(self):
        """Toggle between 1-panel and 2-panel view."""
        btn = self._toolbars["view"].named_btns.get("⊞ 2-Panel")
        if self._panel_mode == "1":
            # Entering 2-panel
            self._panel_mode = "2"
            if btn: btn.config(text="■ 1-Panel", bg="#4a2a2a")
            # Right panel pre-populated with selected files (excluding culled).
            # Left panel shows ALL files; culled files stay left only.
            self._placed = [f for f in self._all_files
                            if f in self._selected and f not in self._culled]
            self._placed_set = set(self._placed)
            self._right_sel.clear()
            self._ins_point = None
            self._user_cols = 0
            self._page_num  = 0   # always start from first file in left panel
            # Show 2-panel buttons via place() — _sync_status_divider positions them
            self._lbl_sel_count.pack_forget()
            self._lbl_right_status.pack(side="right", fill="y", padx=8)
            # Buttons and divider positioned by _sync_status_divider after geometry settles
            self.win.after(80, self._sync_status_divider)
            _total_w = self._grid_paned.winfo_width() or 1200
            self._grid_paned.add(self._right_frame, stretch="never", minsize=160,
                                 width=max(200, _total_w // 2))
            self.win.after(150, self._two_panel_refresh)
        else:
            # Leaving 2-panel — right panel contents become _selected
            self._panel_mode = "1"
            if btn: btn.config(text="⊞ 2-Panel", bg="#2a4a6a")
            self._selected = set(self._placed)
            self._last_click_idx = None
            # Hide 2-panel widgets, restore 1-panel count label
            for w in (self._btn_move_right, self._status_divider,
                      self._btn_deselect, self._btn_reorder, self._btn_save_order):
                try: w.place_forget()
                except: pass
            self._lbl_right_status.pack_forget()
            self._lbl_sel_count.pack(side="right")
            try: self._grid_paned.remove(self._right_frame)
            except: pass
            self._user_cols = 0
            # Force a full recompute at 1-panel size after geometry settles
            def _restore_1panel():
                if self._panel_mode != "1": return
                self._user_cols = 0
                if self._in_tagged_view and self.collection and self.tagged_order:
                    self._all_files = list(self.tagged_order)
                    self._page_num  = 0
                self._show_page()
                self.win.after(600, self._repaint_selection)
            self.win.after(100, _restore_1panel)

    def _two_panel_refresh(self):
        """Rebuild both panels in 2-panel mode."""
        try:
            self.canvas.update_idletasks()
            self._grid_paned.update_idletasks()
        except: pass
        self._user_cols = 0
        self._show_page()
        # Delay right panel build so geometry settles and winfo_width is correct
        self.win.after(200, self._two_panel_build_right)

    def _two_panel_build_right(self):
        """Populate right panel from self._placed."""
        for w in self._right_grid.winfo_children():
            w.destroy()

        n = len(self._placed)
        noun = "file" if n == 1 else "files"
        try: self._lbl_right_status.config(text=f"Selected Items ({n})")
        except: pass

        if not self._placed:
            tk.Label(self._right_grid,
                     text="No files — select on left and click Move Right ❯",
                     bg=BG, fg=TEXT_DIM, font=("Segoe UI", 9),
                     wraplength=160, justify="center").pack(pady=20, padx=8)
            self._update_sel_bar()
            return

        SZ      = THUMB_SIZE
        IMG_H   = THUMB_IMG_H
        CW      = SZ + 16
        CH_IMG  = IMG_H
        CTL_H   = 18
        CAP_H   = 20                   # caption area — single line
        STRIP_H = 15
        BD      = 3; RPAD = 6; IX = RPAD + 2; IY = RPAD + 2
        CTL_Y   = IY + IMG_H
        CAP_Y   = CTL_Y + CTL_H + 3
        sy      = CAP_Y + CAP_H
        CH      = sy + STRIP_H + RPAD
        PAD = 5        # gap between cells
        INS_ZONE = max(8, CW // 15)

        self._right_canvas.update_idletasks()
        rw = self._right_canvas.winfo_width()
        if rw < CW + PAD * 2:
            rw = self._right_frame.winfo_width() or 500
        avail = max(CW + PAD * 2, rw)
        cols = max(1, avail // (CW + PAD * 2))

        self._right_cvs = {}   # path -> canvas widget for fast border update

        for idx, fpath in enumerate(self._placed):
            row, col = divmod(idx, cols)
            is_rsel = fpath in self._right_sel
            show_ins_right = (self._ins_point == idx + 1)
            show_ins_left  = (self._ins_point == idx)

            cv = tk.Canvas(self._right_grid, width=CW, height=CH,
                           bg=UNTAGGED_BD,
                           highlightthickness=3 if is_rsel else 0,
                           highlightbackground=SELECT_BD, cursor="hand2")
            cv.grid(row=row, column=col, padx=PAD, pady=PAD)
            self._right_cvs[fpath] = cv

            # Background
            cv.create_rectangle(BD, BD, CW - BD, CH - BD, fill=UNTAGGED_BD, outline="")
            cv.create_rectangle(RPAD, RPAD, CW - RPAD, CH - RPAD, fill=BG, outline="")

            # Sequence badge
            cv.create_rectangle(IX, IY, IX + 20, IY + 14, fill="#000", outline="")
            cv.create_text(IX + 10, IY + 7, text=str(idx + 1), fill="#aaccff",
                           font=("Segoe UI", 7, "bold"))

            # ── Mark / Zoom controls row (below image, above filename) ─────────
            _rm = SZ // 2
            cv.create_rectangle(IX, CTL_Y, IX + SZ, CTL_Y + CTL_H,
                                 fill="#e0e0e0", outline="")
            cv.create_line(IX + _rm, CTL_Y, IX + _rm, CTL_Y + CTL_H, fill="#bbbbbb")
            _culled = fpath in self._culled
            _mark_text = "Unmark" if _culled else "Mark"
            _mark_fg   = "#994400" if _culled else "#333333"
            cv.create_text(IX + _rm // 2, CTL_Y + CTL_H // 2, anchor="center",
                           text=_mark_text, fill=_mark_fg,
                           font=("Segoe UI", 8, "bold"), tags="mark_btn")
            cv.create_text(IX + _rm + _rm // 2, CTL_Y + CTL_H // 2, anchor="center",
                           text="Zoom", fill="#1a2a4a",
                           font=("Segoe UI", 8, "bold"), tags="zoom_btn")

            def _rctl_click(e, cv=cv, fp=fpath, cw=CW, iy=CTL_Y, ih=CTL_H, rm=_rm, ix=IX):
                if not (iy <= e.y <= iy + ih): return
                if e.x < ix + rm:
                    # Mark/Unmark
                    if fp in self._culled:
                        self._culled.discard(fp)
                        if fp in self._culled_at: del self._culled_at[fp]
                    else:
                        from datetime import datetime
                        self._culled.add(fp)
                        self._culled_at[fp] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    _write_cull_list(self.mode_cfg['root'], self._culled, self._culled_at)
                    new_culled = fp in self._culled
                    cv.itemconfigure("mark_btn",
                                     text="Unmark" if new_culled else "Mark",
                                     fill="#994400" if new_culled else "#333333")
                else:
                    # Zoom
                    try:
                        idx2 = self._all_files.index(fp)
                        self._zoom_index = idx2
                    except ValueError:
                        pass
                    self._open_zoom(fp)

            # Filename
            fname = os.path.basename(fpath)
            icon  = "📷 " if self.mode == "photos" else "📄 "
            cv.create_text(IX + 2, CAP_Y + 2, anchor="nw",
                           text=_fit_text(icon + fname, SZ - 22), fill="#000000",
                           font=("Segoe UI", 9), tags="fname_text")

            # Size strip
            _, cat, disp_s = _file_size_info_cached(fpath)
            size_colours = {"Tiny":"#cc2200","V.Small":"#bb6600","Small":"#996600",
                            "Medium":"#336611","Large":"#1144aa","Huge":"#7722aa","?":"#555555"}
            size_info = f"{cat}  {disp_s}"
            cv.create_rectangle(IX, sy, IX + SZ, sy + STRIP_H, fill="#e8e8e8", outline="")
            cv.create_text(IX + 4, sy + STRIP_H // 2, anchor="w",
                           text=size_info, fill=size_colours.get(cat, "#555"),
                           font=("Segoe UI", 8, "bold"))

            # Insertion bar — red filled band, half INS_ZONE wide
            BAR_W = max(4, INS_ZONE // 2)
            if show_ins_left:
                cv.create_rectangle(0, 0, BAR_W, CH,
                                    fill="#cc2222", outline="", tags="ins_bar")
            if show_ins_right:
                cv.create_rectangle(CW - BAR_W, 0, CW, CH,
                                    fill="#cc2222", outline="", tags="ins_bar")

            # Load thumbnail
            def _load(cv=cv, fp=fpath, sz=SZ, img_h=IMG_H, cw=CW, iy=IY):
                try:
                    jmap = thumb_get_many([fp])
                    jd = jmap.get(fp)
                    if jd:
                        img = Image.open(_io.BytesIO(jd))
                    else:
                        ext = os.path.splitext(fp)[1].lower()
                        if ext in PDF_EXTS and HAVE_FITZ:
                            doc = fitz.open(_longpath(fp))
                            page0 = doc[0]
                            scale = sz / max(page0.rect.width, page0.rect.height)
                            mat = fitz.Matrix(scale, scale)
                            pix = page0.get_pixmap(matrix=mat, alpha=False)
                            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                            doc.close()
                        else:
                            img = Image.open(_longpath(fp))
                    img.thumbnail((sz, img_h), Image.BILINEAR)
                    tk_img = ImageTk.PhotoImage(img)
                    def _place(tk_img=tk_img, cv=cv, cw=cw, sz=sz, img_h=img_h, iy=iy):
                        try:
                            cv._tk_img = tk_img
                            cv.create_image(cw // 2, iy + img_h // 2,
                                            image=tk_img, anchor="center", tags="thumb_img")
                        except: pass
                    self.win.after(0, _place)
                except: pass
            threading.Thread(target=_load, daemon=True).start()

            # Click handler
            # Insertion zone: right INS_ZONE px of this cell = insert after (ins_point=idx+1)
            #                  left INS_ZONE px of this cell = insert before (ins_point=idx)
            # Clicking same zone again clears insertion point
            # Centre = toggle selection (shift = range)
            def _rclick(e, fp=fpath, cv=cv, i=idx, cw=CW, iz=INS_ZONE):
                # Left/right edge = insertion zone — rebuild needed to show ins bar
                if e.x <= iz:
                    new_ip = i
                    self._ins_point = None if self._ins_point == new_ip else new_ip
                    self._two_panel_build_right(); self._update_sel_bar(); return
                if e.x >= cw - iz:
                    new_ip = i + 1
                    self._ins_point = None if self._ins_point == new_ip else new_ip
                    self._two_panel_build_right(); self._update_sel_bar(); return
                # Centre click — toggle selection, update borders only (no rebuild)
                shift = (e.state & 0x1) != 0
                if shift and hasattr(self, "_right_last_click"):
                    lo = min(self._right_last_click, i)
                    hi = max(self._right_last_click, i)
                    anchor = self._placed[self._right_last_click] if 0 <= self._right_last_click < len(self._placed) else None
                    adding = anchor in self._right_sel if anchor else True
                    for j in range(lo, hi + 1):
                        if 0 <= j < len(self._placed):
                            f = self._placed[j]
                            if adding: self._right_sel.add(f)
                            else:      self._right_sel.discard(f)
                else:
                    if fp in self._right_sel: self._right_sel.discard(fp)
                    else:                     self._right_sel.add(fp)
                    self._right_last_click = i
                # Update only visible borders — no rebuild
                for path, widget in getattr(self, "_right_cvs", {}).items():
                    try:
                        sel = path in self._right_sel
                        widget.configure(highlightthickness=3 if sel else 0)
                    except: pass
                self._update_sel_bar()

            def _dispatch(e, _ctl_y=CTL_Y, _ctl_h=CTL_H,
                          _rc=_rclick, _rctl=_rctl_click):
                if _ctl_y <= e.y <= _ctl_y + _ctl_h:
                    _rctl(e)
                else:
                    _rc(e)
            cv.bind("<Button-1>", _dispatch)

        # Bind clicks on the grid background (gaps between cells) for insertion
        def _grid_bg_click(e, cols=cols, cw=CW, ch=CH, pad=PAD, iz=INS_ZONE, n=len(self._placed)):
            # Determine which insertion point the click is nearest to
            cell_w = cw + pad * 2
            cell_h = ch + pad * 2
            col_click = e.x // cell_w
            row_click = e.y // cell_h
            x_in_cell = e.x % cell_w - pad
            idx = row_click * cols + col_click
            # If click is in right half of gap (closer to next cell), ins_point = idx+1
            if x_in_cell < 0:           # in left padding — insert before this col
                new_ip = max(0, idx)
            elif x_in_cell > cw:        # in right padding — insert after this col
                new_ip = min(n, idx + 1)
            else:
                return  # click was on a cell, handled by cell binding
            self._ins_point = None if self._ins_point == new_ip else new_ip
            self._two_panel_build_right()
            self._update_sel_bar()
        self._right_grid.bind("<Button-1>", _grid_bg_click)

        self._right_canvas.update_idletasks()
        self._right_canvas.configure(scrollregion=self._right_canvas.bbox("all"))
        self._update_sel_bar()

    
    def _two_panel_move_right(self):
        """Move selected left-panel items to right panel.
        Adds SELECTED watermarks to moved cells without full redraw."""
        to_move = [f for f in self._all_files
                   if f in self._selected and f not in self._placed_set]
        if not to_move:
            return
        if self._ins_point is not None:
            ip = max(0, min(self._ins_point, len(self._placed)))
            self._placed = self._placed[:ip] + to_move + self._placed[ip:]
        else:
            self._placed.extend(to_move)
        self._placed_set = set(self._placed)
        self._ins_point = None
        for f in to_move:
            self._selected.discard(f)
        self._last_click_idx = None
        # Paint watermarks on moved cells without rebuilding the page
        SZ = self._disp_size; PAD = 8; IX = PAD; IY = PAD; WM_H = 28
        for item in self.thumb_widgets:
            cv, _, orig = item[0], item[1], item[2]
            if orig in self._placed_set:
                try:
                    cv.delete("sel_watermark"); cv.delete("sel_watermark_bg")
                    cv.create_rectangle(IX, IY + SZ//2 - WM_H//2,
                                        IX + SZ, IY + SZ//2 + WM_H//2,
                                        fill="#000000", stipple="gray50",
                                        outline="", tags="sel_watermark_bg")
                    cv.create_text(IX + SZ//2, IY + SZ//2,
                                   text="SELECTED", fill="white",
                                   font=("Segoe UI", 18, "bold"),
                                   tags="sel_watermark")
                except: pass
        self._two_panel_build_right()
        self._update_sel_bar()

    def _two_panel_deselect(self):
        """Remove selected right-panel items back to left panel.
        Removes watermarks from returned cells without full redraw."""
        if not self._right_sel:
            return
        returning = set(self._right_sel)
        for f in returning:
            self._selected.discard(f)
        self._placed = [f for f in self._placed if f not in returning]
        self._placed_set = set(self._placed)
        self._right_sel.clear()
        self._ins_point = None
        self._last_click_idx = None
        # Remove watermarks from returned cells only
        for item in self.thumb_widgets:
            cv, _, orig = item[0], item[1], item[2]
            if orig in returning:
                try:
                    cv.delete("sel_watermark")
                    cv.delete("sel_watermark_bg")
                    cv.configure(highlightthickness=0)
                except: pass
        self._two_panel_build_right()
        self._update_sel_bar()

    def _two_panel_reorder(self):
        """Reorder selected right-panel items to insertion point."""
        if not self._right_sel or self._ins_point is None:
            return
        moving = [f for f in self._placed if f in self._right_sel]
        rest   = [f for f in self._placed if f not in self._right_sel]
        ip = max(0, min(self._ins_point, len(rest)))
        self._placed = rest[:ip] + moving + rest[ip:]
        self._placed_set = set(self._placed)
        self._right_sel.clear()
        self._ins_point = None
        self._two_panel_build_right()
        self._update_sel_bar()

    
    def _two_panel_save_order(self):
        """Save the right-panel order to the active collection, then:
        - clear right panel
        - reload left panel showing all collection files in new order
        """
        if not self.collection:
            messagebox.showinfo("No collection",
                "No collection is active — open a collection first.",
                parent=self.win)
            return
        if not self._placed:
            return

        # Build new order: _placed first, then any collection files not in _placed
        placed_set    = set(self._placed)
        remaining     = [p for p in self.tagged_order if p not in placed_set]
        new_order     = list(self._placed) + remaining

        # Update in-memory state
        self.tagged_order = new_order
        self.tagged       = set(new_order)

        # Persist to disk
        _write_collection(self.collection, self.mode_cfg['root'],
                          self.tagged, self.tagged_at, self.tagged_order)

        # Clear right panel and selection state
        self._placed     = []
        self._placed_set = set()
        self._right_sel.clear()
        self._ins_point  = None
        self._selected.clear()
        self._last_click_idx = None

        # Reload left panel with collection files in new order
        self._in_tagged_view = True
        self._all_files      = list(self.tagged_order)
        self._page_num       = 0

        self.lbl_folder.config(
            text=f"★  Collection: {self.collection}  —  {len(self.tagged_order)} files  (order saved)")
        self._update_view_status(
            f"★  Collection  —  {self.collection}  ({len(self.tagged_order)})  ✓ Order saved")

        self._status(f"✓  Order saved for '{self.collection}'  —  {len(self.tagged_order)} files")

        # Exit 2-panel and show standard 1-panel collection view
        if self._panel_mode == "2":
            self._toggle_panel_mode()
        else:
            self._show_page()
        self._update_sel_bar()

    def _show_selection_panel(self):
        """Show or update the Operations panel with collapsible categories."""
        n = len(self._selected)
        n_placed = len(getattr(self, "_placed", []))
        total = n + n_placed
        if total == 0:
            self._hide_selection_panel()
            return
        n = total
        if not getattr(self, '_sel_panel', None) or not self._sel_panel.winfo_exists():

            HDR_BG   = "#1a3a6a"   # dark blue — header and category bars
            HDR_FG   = "white"
            CAT_BG   = "#2255aa"   # slightly lighter blue for categories
            CAT_FG   = "white"
            OPS_BG   = "white"
            OPS_FG   = "black"
            OPS_ABG  = "#dde8f8"

            self._sel_panel = tk.Toplevel(self.win)
            self._sel_panel.transient(self.win)
            self._sel_panel.title("Operations")
            self._sel_panel.configure(bg=OPS_BG)
            self._sel_panel.resizable(True, True)
            self._ops_user_moved = False   # reset on each new panel creation

            # ── Header ───────────────────────────────────────────────────────
            self._sel_panel_lbl = tk.Label(self._sel_panel, text="",
                                           bg=HDR_BG, fg=HDR_FG,
                                           font=("Segoe UI", 10, "bold"),
                                           padx=12, pady=5, anchor="center")
            self._sel_panel_lbl.pack(fill="x")

            # ── Always-visible selection buttons ─────────────────────────────
            always_fr = tk.Frame(self._sel_panel, bg=OPS_BG)
            always_fr.pack(fill="x", padx=2, pady=(2,0))
            for text, cmd in [
                ("☑  Select All",  self._sel_all),
                ("☐  Clear All",   self._sel_clear),
            ]:
                tk.Button(always_fr, text=text, command=cmd,
                          bg=OPS_BG, fg=OPS_FG, font=("Segoe UI", 9, "bold"),
                          relief="flat", padx=10, pady=5, cursor="hand2",
                          activebackground=OPS_ABG, activeforeground=OPS_FG,
                          anchor="w", width=24).pack(fill="x", pady=1)

            tk.Frame(self._sel_panel, bg="#cccccc", height=1).pack(fill="x", padx=4, pady=2)

            # ── Category definitions — edit here to reorganise ────────────────
            # Format: (category_label, [(op_label, command), ...])
            CATEGORIES = [
                ("Tag / Collection", [
                    ("★  Add to Collection",      self._ops_add_to_collection),
                    ("✖  Remove from Collection", self._ops_remove_from_collection),
                ]),
                ("Rotate / Flip", [
                    ("↺  Rotate Left (CCW)",  lambda: self._ops_rotate(90)),
                    ("↻  Rotate Right (CW)",  lambda: self._ops_rotate(-90)),
                    ("↕↔  Rotate 180°",  lambda: self._ops_rotate(180)),
                    ("↕  Flip Vertical",   lambda: self._ops_flip("v")),
                    ("↔  Flip Horizontal", lambda: self._ops_flip("h")),
                ]),
                ("Copy / Move", [
                    ("📁  Copy/Move to Folder",  self._ops_copy_move),
                ]),
                ("Deletion", [
                    ("🗑  Mark for Deletion",          self._ops_mark_deletion),
                    ("↩  Remove from Deletion List",   self._ops_remove_from_cull),
                    ("✖  Clear Entire Deletion List",  self._ops_clear_cull_list),
                ]),
                ("Output", [
                    ("📄  Contact Sheet",        self._ops_contact_sheet),
                    ("📤  Export",               self._export_list),
                    ("📋  Collection CSV",       self._export_collection_csv),
                    ("⚙  MGEN File Operations",  self._ops_mgen),
                ]),
            ]

            # ── Build collapsible tree ────────────────────────────────────────
            if not hasattr(self, '_ops_expanded'):
                self._ops_expanded = {}   # category label -> BooleanVar

            scroll_fr = tk.Frame(self._sel_panel, bg=OPS_BG)
            scroll_fr.pack(fill="x")

            for row_idx, (cat_label, ops) in enumerate(CATEGORIES):
                if cat_label not in self._ops_expanded:
                    self._ops_expanded[cat_label] = False

                cat_var = tk.BooleanVar(value=self._ops_expanded[cat_label])

                # Category header — use grid so rows stay in fixed order
                cat_hdr = tk.Frame(scroll_fr, bg=CAT_BG, cursor="hand2")
                cat_hdr.grid(row=row_idx*2, column=0, sticky="ew", padx=2, pady=1)
                scroll_fr.columnconfigure(0, weight=1)

                arrow_lbl = tk.Label(cat_hdr,
                                     text="▼" if cat_var.get() else "▶",
                                     bg=CAT_BG, fg=CAT_FG,
                                     font=("Segoe UI", 8), width=2)
                arrow_lbl.pack(side="left", padx=(6,2), pady=4)
                tk.Label(cat_hdr, text=cat_label, bg=CAT_BG, fg=CAT_FG,
                         font=("Segoe UI", 9, "bold"), anchor="w").pack(side="left", pady=4)

                # Ops frame always in row_idx*2+1 — show/hide by grid_remove/grid
                ops_fr = tk.Frame(scroll_fr, bg=OPS_BG)
                ops_fr.grid(row=row_idx*2+1, column=0, sticky="ew", padx=2)
                if not cat_var.get():
                    ops_fr.grid_remove()

                for op_text, op_cmd in ops:
                    tk.Button(ops_fr, text="    " + op_text, command=op_cmd,
                              bg=OPS_BG, fg=OPS_FG,
                              font=("Segoe UI", 9), relief="flat",
                              padx=8, pady=5, cursor="hand2",
                              activebackground=OPS_ABG, activeforeground=OPS_FG,
                              anchor="w", width=26).pack(fill="x", pady=0)

                def _make_toggle(cl, cv, of, al):
                    def _toggle(e=None):
                        new_state = not cv.get()
                        cv.set(new_state)
                        self._ops_expanded[cl] = new_state
                        al.config(text="▼" if new_state else "▶")
                        if new_state:
                            of.grid()
                        else:
                            of.grid_remove()
                        self._sel_panel.update_idletasks()
                        self._reposition_selection_panel()
                    return _toggle

                _tog = _make_toggle(cat_label, cat_var, ops_fr, arrow_lbl)
                cat_hdr.bind("<Button-1>", _tog)
                arrow_lbl.bind("<Button-1>", _tog)
                for child in cat_hdr.winfo_children():
                    child.bind("<Button-1>", _tog)

            # ── Footer ───────────────────────────────────────────────────────
            tk.Frame(self._sel_panel, bg=HDR_BG, height=1).pack(fill="x", pady=(4,0))
            tk.Label(self._sel_panel, text="FileTagger Operations",
                     bg=HDR_BG, fg=HDR_FG,
                     font=("Segoe UI", 8), padx=12, pady=3).pack(fill="x")

            self._reposition_selection_panel()
            self.win.after(100, self._bind_sel_panel_move)

        noun = "file" if n == 1 else "files"
        self._sel_panel_lbl.config(text=f"  {n} {noun} selected")

    def _ops_mark_deletion(self):
        """Mark for Deletion — adds selected to cull list, shows DELETING watermark."""
        files = list(self._selected) or list(self._placed)
        if not files: return
        from datetime import datetime
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        for orig in files:
            self._culled.add(orig)
            self._culled_at[orig] = ts
        _write_cull_list(self.mode_cfg['root'], self._culled, self._culled_at)
        # Full repaint — updates mark_btn text AND watermarks
        for item in self.thumb_widgets:
            cv, _, orig = item[0], item[1], item[2]
            try: self._cv_repaint(cv, *self._cell_colours(orig))
            except Exception: pass
        self._update_cull_radio_hint()
        self._schedule_tree_refresh()
        n = len(files)
        self._status(f"✓  {n} file{'s' if n!=1 else ''} marked for deletion")

    def _ops_copy_move(self):
        """Copy/Move to Folder — simple dialog with folder selection and confirmation."""
        files = list(self._selected) or list(self._placed)
        if not files:
            return

        HDR_BG = "#1a3a6a"
        dlg = tk.Toplevel(self.win)
        dlg.title("Copy / Move to Folder")
        dlg.transient(self.win)
        dlg.grab_set()
        dlg.resizable(True, False)
        dlg.configure(bg=BG3)
        self._centre_window(dlg, 480, 260)

        # Header
        tk.Label(dlg, text=f"Copy or Move {len(files)} file{'s' if len(files)!=1 else ''}",
                 bg=HDR_BG, fg="white", font=("Segoe UI", 10, "bold"),
                 padx=12, pady=6).pack(fill="x")

        # Operation choice
        op_var = tk.StringVar(value="copy")
        op_frame = tk.Frame(dlg, bg=BG3)
        op_frame.pack(fill="x", padx=16, pady=(12, 4))
        tk.Label(op_frame, text="Operation:", bg=BG3, fg=TEXT_BRIGHT,
                 font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 12))
        tk.Radiobutton(op_frame, text="Copy", variable=op_var, value="copy",
                       bg=BG3, fg=TEXT_BRIGHT, selectcolor=BG,
                       font=("Segoe UI", 9), activebackground=BG3).pack(side="left", padx=8)
        tk.Radiobutton(op_frame, text="Move", variable=op_var, value="move",
                       bg=BG3, fg=TEXT_BRIGHT, selectcolor=BG,
                       font=("Segoe UI", 9), activebackground=BG3).pack(side="left", padx=8)

        # Folder selection
        folder_var = tk.StringVar(value="")
        f_frame = tk.Frame(dlg, bg=BG3)
        f_frame.pack(fill="x", padx=16, pady=4)
        tk.Label(f_frame, text="Destination:", bg=BG3, fg=TEXT_BRIGHT,
                 font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 8))
        folder_entry = tk.Entry(f_frame, textvariable=folder_var,
                                font=("Segoe UI", 9), bg=BG, fg=TEXT_BRIGHT,
                                insertbackground=TEXT_BRIGHT, relief="flat",
                                highlightthickness=1, highlightbackground="#555")
        folder_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))

        def _browse():
            from tkinter import filedialog
            path = filedialog.askdirectory(parent=dlg, title="Select destination folder")
            if path:
                folder_var.set(os.path.normpath(path))
                _update_summary()

        tk.Button(f_frame, text="Browse...", command=_browse,
                  bg="#335577", fg="white", font=("Segoe UI", 9, "bold"),
                  relief="flat", padx=8, pady=2, cursor="hand2",
                  activebackground="#446688").pack(side="left")

        # Summary label
        summary_lbl = tk.Label(dlg, text="Select a destination folder",
                               bg=BG3, fg=TEXT_DIM, font=("Segoe UI", 9),
                               padx=16, pady=4, anchor="w")
        summary_lbl.pack(fill="x")

        def _update_summary(*args):
            dest = folder_var.get().strip()
            if dest and os.path.isdir(_longpath(dest)):
                op = op_var.get().capitalize()
                n = len(files)
                fname = os.path.basename(dest) or dest
                summary_lbl.config(
                    text=f"{op} {n} file{'s' if n!=1 else ''} to:  {fname}",
                    fg=TEXT_BRIGHT)
            else:
                summary_lbl.config(text="Select a destination folder", fg=TEXT_DIM)

        folder_var.trace_add("write", _update_summary)
        op_var.trace_add("write", _update_summary)

        # Buttons
        bf = tk.Frame(dlg, bg=BG3)
        bf.pack(pady=10)

        def _execute():
            import shutil
            dest = folder_var.get().strip()
            if not dest or not os.path.isdir(_longpath(dest)):
                import tkinter.messagebox as mb
                mb.showwarning("No folder", "Please select a valid destination folder.", parent=dlg)
                return
            op = op_var.get()
            errors = []
            moved_log = []
            for orig in files:
                try:
                    fname = os.path.basename(orig)
                    dst = os.path.join(dest, fname)
                    if os.path.exists(_longpath(dst)):
                        stem, ext = os.path.splitext(fname)
                        n = 1
                        while os.path.exists(_longpath(dst)):
                            dst = os.path.join(dest, f"{stem}_{n}{ext}"); n += 1
                    if op == "copy":
                        shutil.copy2(_longpath(orig), _longpath(dst))
                    else:
                        shutil.move(_longpath(orig), _longpath(dst))
                        moved_log.append((orig, dst))
                except Exception as ex:
                    errors.append(f"{os.path.basename(orig)}: {ex}")

            # Update blob and state for moves
            if moved_log:
                srcs = [s for s, d in moved_log]
                try: thumb_move(srcs, dest)
                except: pass
                for orig, dst in moved_log:
                    # Update tagged_order, tagged, _all_files
                    if orig in self.tagged:
                        self.tagged.discard(orig); self.tagged.add(dst)
                        ts = self.tagged_at.pop(orig, "")
                        self.tagged_at[dst] = ts
                        try:
                            i = self.tagged_order.index(orig)
                            self.tagged_order[i] = dst
                        except: pass
                    try:
                        i = self._all_files.index(orig)
                        self._all_files[i] = dst
                    except: pass

            dlg.destroy()
            n_done = len(files) - len(errors)
            msg = f"{'Moved' if op=='move' else 'Copied'} {n_done} file{'s' if n_done!=1 else ''} to {os.path.basename(dest) or dest}"
            if errors:
                msg += f"\n\n{len(errors)} error(s):\n" + "\n".join(errors[:5])
            import tkinter.messagebox as mb
            mb.showinfo("Complete", msg, parent=self.win)
            if op == "move":
                self._selected.clear()
                self._load_folder(self.current_folder)
                self._refresh_tree_stats()

        tk.Button(bf, text="Execute", command=_execute,
                  bg=HDR_BG, fg="white", font=("Segoe UI", 9, "bold"),
                  relief="flat", padx=16, pady=4, cursor="hand2",
                  activebackground="#2a5a9a").pack(side="left", padx=8)
        tk.Button(bf, text="Cancel", command=dlg.destroy,
                  bg="#555", fg="white", font=("Segoe UI", 9),
                  relief="flat", padx=12, pady=4, cursor="hand2").pack(side="left", padx=8)

    def _ops_add_to_collection(self):
        """Add to Collection — user selects from existing collections with confirmation."""
        files = list(self._selected) or list(self._placed)
        if not files:
            return
        root = self.mode_cfg['root']
        cols = _list_collections(root)
        # Remove Temporary from the list for this operation
        cols = [c for c in cols if c != "Temporary"]
        if not cols:
            messagebox.showinfo("No Collections",
                "No named collections exist yet. Create one first using the Collection toolbar.",
                parent=self.win)
            return

        dlg = tk.Toplevel(self.win)
        dlg.title("Add to Collection")
        dlg.transient(self.win)
        dlg.grab_set()
        dlg.resizable(False, False)
        dlg.configure(bg=BG3)
        self._centre_window(dlg, 400, 340)

        HDR_BG = "#1a3a6a"
        tk.Label(dlg, text=f"Add {len(files)} file{'s' if len(files)!=1 else ''} to collection:",
                 bg=HDR_BG, fg="white", font=("Segoe UI", 10, "bold"),
                 padx=12, pady=6).pack(fill="x")

        # Info label (updates on selection) — packed before listbox so it's always visible
        info_lbl = tk.Label(dlg, text="", bg=BG3, fg=TEXT_DIM,
                            font=("Segoe UI", 9), padx=12, pady=4, wraplength=370)
        info_lbl.pack(fill="x")

        # Button frame — packed before listbox so it's always visible at bottom
        bf = tk.Frame(dlg, bg=BG3)
        bf.pack(side="bottom", pady=8)

        # Collection listbox — fills remaining space
        lf = tk.Frame(dlg, bg=BG3)
        lf.pack(fill="both", expand=True, padx=12, pady=(4, 0))
        lb = tk.Listbox(lf, font=("Segoe UI", 10), selectmode="single",
                        bg="white",
                        fg=TEXT_BRIGHT if THEME=="dark" else "black",
                        selectbackground="#1a3a6a", selectforeground="white",
                        relief="flat", bd=1, highlightthickness=1)
        sb = tk.Scrollbar(lf, command=lb.yview)
        sb.pack(side="right", fill="y")
        lb.pack(fill="both", expand=True)
        lb.configure(yscrollcommand=sb.set)
        for c in cols:
            lb.insert("end", c)
        if cols:
            lb.selection_set(0)

        def _update_info(e=None):
            sel = lb.curselection()
            if not sel: info_lbl.config(text=""); return
            cname = cols[sel[0]]
            try:
                data = _read_collection(cname, root)
                existing = set(data.keys())
                already  = len([f for f in files if f in existing])
                new_     = len(files) - already
                total_after = len(existing) + new_
                info_lbl.config(text=
                    f"Collection has {len(existing)} files.  "
                    f"Adding {new_} new  ({already} already in it).  "
                    f"Total after: {total_after}")
            except: info_lbl.config(text="")
        lb.bind("<<ListboxSelect>>", _update_info)
        _update_info()

        def _do_add():
            sel = lb.curselection()
            if not sel: return
            cname = cols[sel[0]]
            from datetime import datetime
            ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            try:
                data = _read_collection(cname, root)
            except:
                data = {}
            added = 0
            for f in files:
                if f not in data:
                    data[f] = ts
                    added += 1
            order = list(data.keys())
            _write_collection(cname, root, set(data.keys()), data, order)
            dlg.destroy()
            self._status(f"✓  {added} file{'s' if added!=1 else ''} added to '{cname}'")
            self._refresh_collection_list()
            self._schedule_tree_refresh()

        tk.Button(bf, text="Add", command=_do_add,
                  bg="#1a3a6a", fg="white", font=("Segoe UI", 9, "bold"),
                  relief="flat", padx=16, pady=4, cursor="hand2").pack(side="left", padx=6)
        tk.Button(bf, text="Cancel", command=dlg.destroy,
                  bg="#555", fg="white", font=("Segoe UI", 9),
                  relief="flat", padx=12, pady=4, cursor="hand2").pack(side="left", padx=6)

    def _ops_remove_from_collection(self):
        """Remove selected files from the active collection."""
        if not self.collection:
            messagebox.showinfo("No collection",
                "No collection is active. Open a collection first.",
                parent=self.win)
            return
        files = list(self._selected) or list(self._placed)
        if not files:
            return
        # Filter to only files that are actually in the collection
        in_coll = [f for f in files if f in self.tagged]
        if not in_coll:
            messagebox.showinfo("Not in collection",
                "None of the selected files are in the current collection.",
                parent=self.win)
            return
        if not messagebox.askyesno("Remove from collection",
            f"Remove {len(in_coll)} file{'s' if len(in_coll)!=1 else ''} "
            f"from '{self.collection}'?\n\nFiles will not be deleted from disk.",
            parent=self.win):
            return
        for f in in_coll:
            self.tagged.discard(f)
            self.tagged_at.pop(f, None)
            try: self.tagged_order.remove(f)
            except ValueError: pass
        self._selected -= set(in_coll)
        _write_collection(self.collection, self.mode_cfg['root'],
                          self.tagged, self.tagged_at, self.tagged_order)
        self._update_coll_info()
        self._schedule_tree_refresh()
        # Stay on collection view — update files list and label
        if self._in_tagged_view:
            self._all_files = list(self.tagged_order)
            self._page_num  = 0
            self._set_collection_label()
            self._show_page()
        else:
            self._reload_visible_tags()
        self._status(f"✖  {len(in_coll)} file{'s' if len(in_coll)!=1 else ''} "
                     f"removed from '{self.collection}'")

    def _ops_rotate(self, degrees):
        """Permanently rotate all selected files by degrees (90, -90, 180)."""
        files = sorted(self._selected)
        if not files: return
        dirn = {90: "90° counter-clockwise", -90: "90° clockwise", 180: "180°"}.get(degrees, f"{degrees}°")
        if not messagebox.askyesno("Rotate Files",
            f"Permanently rotate {len(files)} file{'s' if len(files)!=1 else ''} {dirn}?\n\n"
            "This modifies the original files and cannot be undone.",
            parent=self.win): return
        import threading as _thr
        done = [0]; errors = []
        def _worker():
            from PIL import ImageOps as _IOS, ExifTags as _ET
            for path in files:
                try:
                    img = Image.open(_longpath(path))
                    img = _IOS.exif_transpose(img)
                    img = img.rotate(degrees, expand=True)
                    if img.mode != 'RGB': img = img.convert('RGB')
                    try:
                        exif = img.getexif()
                        ot = next((k for k,v in _ET.TAGS.items() if v=='Orientation'), None)
                        if ot and ot in exif: del exif[ot]
                        img.save(_longpath(path), quality=95, optimize=True, exif=exif.tobytes())
                    except:
                        img.save(_longpath(path), quality=95, optimize=True)
                    _size_cache.pop(path, None)
                    sz = self._disp_size
                    thumb = img.copy()
                    _th = int(sz * THUMB_IMG_H / THUMB_SIZE)
                    thumb.thumbnail((sz, _th), Image.BILINEAR)
                    buf = _io.BytesIO()
                    thumb.save(buf, 'JPEG', quality=82, optimize=True)
                    thumb_put_many([(path, buf.getvalue())])
                    done[0] += 1
                except Exception as e:
                    errors.append(f"{os.path.basename(path)}: {e}")
            def _finish():
                self._show_page()
                msg = f"✓ {done[0]} file{'s' if done[0]!=1 else ''} rotated {dirn}"
                if errors: msg += f"\n{len(errors)} errors"
                self._status(msg)
            self.win.after(0, _finish)
        _thr.Thread(target=_worker, daemon=True).start()
        self._status(f"Rotating {len(files)} files…")

    def _ops_flip(self, axis):
        """Permanently flip all selected files."""
        files = sorted(self._selected)
        if not files: return
        dirn = "horizontally" if axis == "h" else "vertically"
        if not messagebox.askyesno("Flip Files",
            f"Permanently flip {len(files)} file{'s' if len(files)!=1 else ''} {dirn}?\n\n"
            "This modifies the original files and cannot be undone.",
            parent=self.win): return
        import threading as _thr
        done = [0]; errors = []
        def _worker():
            from PIL import ImageOps as _IOS, ExifTags as _ET
            for path in files:
                try:
                    img = Image.open(_longpath(path))
                    img = _IOS.exif_transpose(img)
                    img = _IOS.mirror(img) if axis == "h" else _IOS.flip(img)
                    if img.mode != 'RGB': img = img.convert('RGB')
                    try:
                        exif = img.getexif()
                        ot = next((k for k,v in _ET.TAGS.items() if v=='Orientation'), None)
                        if ot and ot in exif: del exif[ot]
                        img.save(_longpath(path), quality=95, optimize=True, exif=exif.tobytes())
                    except:
                        img.save(_longpath(path), quality=95, optimize=True)
                    _size_cache.pop(path, None)
                    sz = self._disp_size
                    thumb = img.copy()
                    _th = int(sz * THUMB_IMG_H / THUMB_SIZE)
                    thumb.thumbnail((sz, _th), Image.BILINEAR)
                    buf = _io.BytesIO()
                    thumb.save(buf, 'JPEG', quality=82, optimize=True)
                    thumb_put_many([(path, buf.getvalue())])
                    done[0] += 1
                except Exception as e:
                    errors.append(f"{os.path.basename(path)}: {e}")
            def _finish():
                self._show_page()
                msg = f"✓ {done[0]} file{'s' if done[0]!=1 else ''} flipped {dirn}"
                if errors: msg += f"\n{len(errors)} errors"
                self._status(msg)
            self.win.after(0, _finish)
        _thr.Thread(target=_worker, daemon=True).start()
        self._status(f"Flipping {len(files)} files…")

    def _ops_contact_sheet(self):
        """Contact Sheet from current selection."""
        files = list(self._selected) or list(self._placed)
        if not files:
            return
        orig_order     = self.tagged_order[:]
        orig_tagged    = set(self.tagged)
        orig_tagged_at = dict(self.tagged_at)
        from datetime import datetime
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.tagged       = set(files)
        self.tagged_order = list(files)
        self.tagged_at    = {f: ts for f in files}
        def _restore():
            self.tagged       = orig_tagged
            self.tagged_order = orig_order
            self.tagged_at    = orig_tagged_at
        dlg = self._contact_sheet_dialog()
        if dlg and hasattr(dlg, 'protocol'):
            orig_destroy = dlg.destroy
            def _on_close():
                _restore()
                orig_destroy()
            dlg.protocol("WM_DELETE_WINDOW", _on_close)
        else:
            _restore()

    def _ops_remove_from_cull(self):
        """Remove selected files from Deletion List — keeps selection state intact."""
        files = list(self._selected) or list(self._placed)
        if not files: return
        removed = 0
        for f in files:
            if f in self._culled:
                self._culled.discard(f)
                self._culled_at.pop(f, None)
                removed += 1
        if not removed:
            self._status("No marked files in current selection")
            return
        _write_cull_list(self.mode_cfg['root'], self._culled, self._culled_at)
        # Full repaint — updates mark_btn text AND watermarks, preserves SELECTED
        for item in self.thumb_widgets:
            cv, _, orig = item[0], item[1], item[2]
            try: self._cv_repaint(cv, *self._cell_colours(orig))
            except Exception: pass
        self._update_cull_radio_hint()
        self._schedule_tree_refresh()
        # If in cull view, refresh display — unmarked files leave the list
        if self._in_cull_view:
            self._all_files = sorted(self._culled)
            self._page_num = 0
            self._update_view_status(f"🗑  Cull List  —  {len(self._culled)} marked for deletion")
            self.lbl_folder.config(text=f"🗑  Cull List — {len(self._culled)} files marked for deletion")
            self._show_page()
            if not self._culled:
                self._hide_delete_popup()
        self._status(f"✓  {removed} file{'s' if removed!=1 else ''} removed from Deletion List")

    def _ops_clear_cull_list(self):
        """Remove ALL files from the Deletion List."""
        if not self._culled:
            messagebox.showinfo("Deletion List", "The Deletion List is already empty.",
                                parent=self.win)
            return
        n = len(self._culled)
        if not messagebox.askyesno("Clear Deletion List",
                f"Remove all {n} file{'s' if n!=1 else ''} from the Deletion List?\n\n"
                "No files will be deleted from disk — this only clears the list.",
                parent=self.win):
            return
        self._culled.clear()
        self._culled_at.clear()
        _write_cull_list(self.mode_cfg['root'], self._culled, self._culled_at)
        # Full repaint — updates mark_btn text ("Unmark"→"Mark") AND watermarks
        for item in self.thumb_widgets:
            cv, _, orig = item[0], item[1], item[2]
            try:
                self._cv_repaint(cv, *self._cell_colours(orig))
            except Exception: pass
        self._update_statusbar()
        self._update_cull_radio_hint()
        self._schedule_tree_refresh()
        if self._in_cull_view:
            self._hide_delete_popup()
            self._exit_special_view()
        self._status(f"✓  Deletion List cleared ({n} file{'s' if n!=1 else ''} unmarked)")

    def _ops_mgen(self):
        """MGEN File Operations — process dialog for JPGs, not implemented for PDFs."""
        if self.mode == "pdfs":
            messagebox.showinfo("Not Implemented",
                "MGEN File Operations is not available for PDFs.",
                parent=self.win)
            return
        files = list(self._selected) or list(self._placed)
        if not files:
            return
        # Set tagged_order to selection — restore after dialog closes (via protocol)
        orig_order  = self.tagged_order[:]
        orig_tagged = set(self.tagged)
        orig_tagged_at = dict(self.tagged_at)
        from datetime import datetime
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.tagged     = set(files)
        self.tagged_order = list(files)
        self.tagged_at  = {f: ts for f in files}
        def _restore():
            self.tagged       = orig_tagged
            self.tagged_order = orig_order
            self.tagged_at    = orig_tagged_at
        # Open process dialog; bind restore to its close event
        dlg = self._process_dialog()
        if dlg and hasattr(dlg, 'protocol'):
            orig_destroy = dlg.destroy
            def _on_close():
                _restore()
                orig_destroy()
            dlg.protocol("WM_DELETE_WINDOW", _on_close)
        else:
            # If dialog returned None or no protocol support, restore now
            _restore()

    def _reposition_selection_panel(self):
        """Resize panel to fit content. Reposition only if user hasn't moved it."""
        try:
            if not self._sel_panel or not self._sel_panel.winfo_exists(): return
            self._sel_panel.update_idletasks()
            ph = self._sel_panel.winfo_reqheight() or 300
            pw = self._sel_panel.winfo_reqwidth() or 220

            if getattr(self, '_ops_user_moved', False):
                # User has positioned it — only resize height, keep x/y
                cx = self._sel_panel.winfo_rootx()
                cy = self._sel_panel.winfo_rooty()
                self._sel_panel.geometry(f"{pw}x{ph}+{cx}+{cy}")
            else:
                wx = self.win.winfo_rootx()
                wy = self.win.winfo_rooty()
                x  = wx + 8
                y  = wy + 80
                self._sel_panel.geometry(f"{pw}x{ph}+{x}+{y}")
        except: pass

    def _bind_sel_panel_move(self):
        """Bind user drag detection to Operations panel after first placement."""
        if not self._sel_panel or not self._sel_panel.winfo_exists(): return
        last_pos = [None]
        def _on_configure(e):
            # Only set user_moved if x/y changed (drag), not if only size changed
            try:
                pos = (self._sel_panel.winfo_rootx(), self._sel_panel.winfo_rooty())
                if last_pos[0] is not None and pos != last_pos[0]:
                    self._ops_user_moved = True
                last_pos[0] = pos
            except: pass
        self._sel_panel.bind("<Configure>", _on_configure)

    def _hide_selection_panel(self):
        try:
            if getattr(self, '_sel_panel', None) and self._sel_panel.winfo_exists():
                self._sel_panel.destroy()
        except: pass
        self._sel_panel = None

    def _sel_all(self):
        """Select all — ask visible or all in folder."""
        visible = [item[2] for item in self.thumb_widgets if item[0].winfo_ismapped()]
        all_files = self._all_files
        if len(visible) == len(all_files):
            # No filter active — just select all
            for f in all_files: self._selected.add(f)
        else:
            # Filter active — ask
            dlg = tk.Toplevel(self.win); dlg.title("Select All")
            dlg.configure(bg=BG3); dlg.grab_set(); dlg.transient(self.win)
            self._centre_window(dlg, 320, 150)
            tk.Label(dlg, text="Select which files?", bg=BG3, fg=TEXT_BRIGHT,
                     font=("Segoe UI",10,"bold")).pack(pady=(16,8))
            bf = tk.Frame(dlg, bg=BG3); bf.pack(pady=4)
            def _do(files):
                dlg.destroy()
                for f in files: self._selected.add(f)
                self._repaint_selection(); self._show_selection_panel()
            tk.Button(bf, text=f"Visible only  ({len(visible)})", bg="#226688", fg="white",
                      font=("Segoe UI",9,"bold"), relief="flat", padx=8, pady=4,
                      cursor="hand2", command=lambda: _do(visible)).pack(side="left", padx=6)
            tk.Button(bf, text=f"All in folder  ({len(all_files)})", bg="#335599", fg="white",
                      font=("Segoe UI",9,"bold"), relief="flat", padx=8, pady=4,
                      cursor="hand2", command=lambda: _do(all_files)).pack(side="left", padx=6)
            return  # early return — callback handles the rest
        self._repaint_selection(); self._show_selection_panel()

    def _sel_clear(self):
        """Clear All — in 2-panel clears the right panel; in cull view clears cull list; otherwise deselects."""
        # ── 2-panel: clear the right panel ───────────────────────────────────
        if getattr(self, "_panel_mode", "1") == "2":
            if not self._placed and not self._selected: return
            self._placed.clear()
            self._placed_set.clear()
            self._right_sel.clear()
            self._ins_point = None
            self._selected.clear()
            self._two_panel_build_right()
            self._repaint_selection()
            self._hide_selection_panel()
            self._update_sel_bar()
            return
        if self._in_cull_view:
            if not self._culled: return
            self._culled.clear()
            self._culled_at.clear()
            _write_cull_list(self.mode_cfg['root'], self._culled, self._culled_at)
            self._schedule_tree_refresh()
            self._hide_delete_popup()
            self._exit_special_view()
            self._status("Deletion List cleared")
            return
        visible_sel = {item[2] for item in self.thumb_widgets
                       if item[0].winfo_ismapped() and item[2] in self._selected}
        hidden_sel  = self._selected - visible_sel
        if not hidden_sel:
            # Nothing hidden selected — just clear all
            self._selected.clear()
            self._repaint_selection(); self._hide_selection_panel()
            return
        # Some hidden files selected — ask
        dlg = tk.Toplevel(self.win); dlg.title("Clear Selection")
        dlg.configure(bg=BG3); dlg.grab_set(); dlg.transient(self.win)
        self._centre_window(dlg, 340, 150)
        tk.Label(dlg, text="Deselect which files?", bg=BG3, fg=TEXT_BRIGHT,
                 font=("Segoe UI",10,"bold")).pack(pady=(16,8))
        bf = tk.Frame(dlg, bg=BG3); bf.pack(pady=4)
        def _do_vis():
            dlg.destroy()
            for f in visible_sel: self._selected.discard(f)
            self._repaint_selection(); self._show_selection_panel()
        def _do_all():
            dlg.destroy()
            self._selected.clear()
            self._repaint_selection(); self._hide_selection_panel()
        tk.Button(bf, text=f"Visible only  ({len(visible_sel)})", bg="#226688", fg="white",
                  font=("Segoe UI",9,"bold"), relief="flat", padx=8, pady=4,
                  cursor="hand2", command=_do_vis).pack(side="left", padx=6)
        tk.Button(bf, text=f"All selected  ({len(self._selected)})", bg="#553333", fg="white",
                  font=("Segoe UI",9,"bold"), relief="flat", padx=8, pady=4,
                  cursor="hand2", command=_do_all).pack(side="left", padx=6)

    def _sel_exit(self):
        """Exit selection mode — clear selection and hide panel."""
        self._selected.clear()
        self._repaint_selection()
        self._hide_selection_panel()

    def _sel_visible(self):
        """Select files currently visible in the scroll viewport."""
        cy = self.canvas.winfo_rooty()
        ch = self.canvas.winfo_height()
        for item in self.thumb_widgets:
            try:
                wy = item[0].winfo_rooty()
                wh = item[0].winfo_height()
                if wy + wh > cy and wy < cy + ch:
                    self._selected.add(item[2])
            except: pass
        self._repaint_selection()
        self._show_selection_panel()

    def _sel_filter(self):
        """Open Select Filter dialog — Any/All buttons + entry field."""
        dlg = tk.Toplevel(self.win)
        dlg.title("Select Filter")
        dlg.configure(bg=BG3)
        dlg.resizable(False, False)
        dlg.grab_set()
        dlg.transient(self.win)
        self._centre_window(dlg, 380, 160)

        # ── Mode row ──────────────────────────────────────────────────────────
        tk.Label(dlg, text="Match:", bg=BG3, fg=TEXT_BRIGHT,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=16, pady=(14,2))
        mode_row = tk.Frame(dlg, bg=BG3); mode_row.pack(anchor="w", padx=16)
        mode_var = tk.StringVar(value="Any")

        def _set_mode(m):
            mode_var.set(m)
            btn_any.config(relief="sunken" if m=="Any" else "raised",
                           bg="#225588" if m=="Any" else BG3)
            btn_all.config(relief="sunken" if m=="All" else "raised",
                           bg="#225588" if m=="All" else BG3)

        btn_any = tk.Button(mode_row, text="Any", bg="#225588", fg="white",
                            font=("Segoe UI", 9, "bold"), relief="sunken",
                            padx=14, pady=3, cursor="hand2",
                            command=lambda: _set_mode("Any"))
        btn_any.pack(side="left", padx=(0,4))
        btn_all = tk.Button(mode_row, text="All", bg=BG3, fg=TEXT_BRIGHT,
                            font=("Segoe UI", 9, "bold"), relief="raised",
                            padx=14, pady=3, cursor="hand2",
                            command=lambda: _set_mode("All"))
        btn_all.pack(side="left")

        # ── Filter entry ──────────────────────────────────────────────────────
        tk.Label(dlg, text="Terms (comma-separated):", bg=BG3, fg=TEXT_BRIGHT,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=16, pady=(10,2))
        terms_var = tk.StringVar()
        entry = tk.Entry(dlg, textvariable=terms_var,
                         bg="white", fg="#111111", insertbackground="#111111",
                         font=("Segoe UI", 10), relief="solid", bd=1)
        entry.pack(fill="x", padx=16, pady=(0,10))

        # ── Select button ─────────────────────────────────────────────────────
        def _do_select():
            raw = terms_var.get().strip()
            if not raw:
                self._status("Sel Filter: enter at least one term"); dlg.destroy(); return
            terms = [t.strip().lower() for t in raw.split(",") if t.strip()]
            mode  = mode_var.get()
            matched = []
            for path in self._all_files:
                name = os.path.basename(path).lower()
                hit = any(t in name for t in terms) if mode == "Any" else all(t in name for t in terms)
                if hit: matched.append(path)
            dlg.destroy()
            if not matched:
                self._status(f"Sel Filter ({mode}): no files matched {terms}"); return
            for path in matched:
                self._selected.add(path)
            self._repaint_selection()
            self._show_selection_panel()
            self._status(f"Sel Filter ({mode}): selected {len(matched)} of {len(self._all_files)} file(s)")

        entry.bind("<Return>", lambda e: _do_select())
        bf = tk.Frame(dlg, bg=BG3); bf.pack(pady=(0,12))
        tk.Button(bf, text="Select", bg=GREEN, fg="white",
                  font=("Segoe UI", 9, "bold"), relief="flat",
                  padx=16, pady=4, cursor="hand2",
                  command=_do_select).pack(side="left", padx=(0,8))
        tk.Button(bf, text="Cancel", bg=BG3, fg=TEXT_BRIGHT,
                  font=("Segoe UI", 9), relief="flat",
                  padx=16, pady=4, cursor="hand2",
                  command=dlg.destroy).pack(side="left")

        entry.focus_set()

    def _sel_filter_impl(self):
        pass  # no longer used — logic is inline in _sel_filter dialog

    def _mark_selected(self):
        """Mark selected files for deletion."""
        if not self._selected:
            self._status("No files selected — use Sel All or Sel Visible first"); return
        from datetime import datetime
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        for orig in self._selected:
            self._culled.add(orig); self._culled_at[orig] = ts
        for item in self.thumb_widgets:
            cv, _, orig = item[0], item[1], item[2]
            if orig in self._selected:
                self._cv_repaint(cv, *self._cell_colours(orig))
        self._selected.clear()
        self._repaint_selection()
        self._schedule_cull_save()
        self._update_statusbar()

    def _unmark_selected(self):
        """Remove selected files from cull list."""
        if not self._selected:
            self._status("No files selected — use Sel All or Sel Visible first"); return
        for orig in list(self._selected):
            self._culled.discard(orig); self._culled_at.pop(orig, None)
        for item in self.thumb_widgets:
            cv, _, orig = item[0], item[1], item[2]
            if orig in self._selected:
                self._cv_repaint(cv, *self._cell_colours(orig))
        self._selected.clear()
        self._repaint_selection()
        self._schedule_cull_save()
        self._update_statusbar()

    def _sel_tag(self):
        import tkinter.messagebox as mb
        mb.showinfo("Tag", "Tag is disabled in this version — use Operations.", parent=self.win)

    def _sel_untag(self):
        import tkinter.messagebox as mb
        mb.showinfo("Untag", "Untag is disabled in this version — use Operations.", parent=self.win)

    def _sel_unmark(self):
        self._ops_remove_from_cull()

    def _sel_mark(self):
        self._ops_mark_deletion()

    def _sel_move(self):
        """Enter move mode — next tree click selects destination folder."""
        if not self._selected:
            return
        n = len(self._selected)
        self._move_mode = True
        self._move_target = None
        # Update selection panel to move mode
        if getattr(self, '_sel_panel', None) and self._sel_panel.winfo_exists():
            self._sel_panel_lbl.config(
                text=f"  ☰  Click a folder in the tree to move {n} file{'s' if n!=1 else ''}",
                bg="#4a2800")
            self._sel_panel.configure(highlightbackground="#ff8800")
            # Replace buttons with just Cancel
            for w in list(self._sel_panel.pack_slaves()):
                if isinstance(w, tk.Button): w.destroy()
            tk.Button(self._sel_panel, text="✕  Cancel Move",
                      command=self._sel_move_cancel,
                      bg="#553333", fg="white", font=("Segoe UI",9,"bold"),
                      relief="flat", padx=8, pady=6, cursor="hand2",
                      activebackground="#774444", activeforeground="white",
                      anchor="w", width=16).pack(fill="x", padx=1, pady=1)
        # Highlight tree to indicate it's active for folder selection
        self.tree.configure(style="MoveMode.Treeview")
        self._status(f"Move mode — click a destination folder in the tree")

    def _sel_move_cancel(self):
        """Cancel move mode and restore normal selection panel."""
        self._move_mode = False
        self._move_target = None
        self.tree.configure(style="Treeview")
        self._show_selection_panel()

    def _sel_move_target_chosen(self, dest_folder):
        """Called when user clicks a folder during move mode."""
        n = len(self._selected)
        dest_name = os.path.basename(dest_folder) or dest_folder
        self._move_target = dest_folder
        # Update panel to show target and Confirm/Cancel
        if getattr(self, '_sel_panel', None) and self._sel_panel.winfo_exists():
            self._sel_panel_lbl.config(
                text=f"  ☰  Move {n} file{'s' if n!=1 else ''} to:  {dest_name}",
                bg="#1a3a1a")
            self._sel_panel.configure(highlightbackground="#27ae60")
            for w in list(self._sel_panel.pack_slaves()):
                if isinstance(w, tk.Button): w.destroy()
            tk.Button(self._sel_panel, text="✔  Confirm Move",
                      command=self._sel_move_confirm,
                      bg="#1a6a1a", fg="white", font=("Segoe UI",9,"bold"),
                      relief="flat", padx=8, pady=6, cursor="hand2",
                      activebackground="#228822", activeforeground="white",
                      anchor="w", width=16).pack(fill="x", padx=1, pady=1)
            tk.Button(self._sel_panel, text="↩  Choose different folder",
                      command=lambda: (setattr(self,'_move_target',None),
                                       self._sel_move_restart()),
                      bg="#2a2a2a", fg="#aaa", font=("Segoe UI",9),
                      relief="flat", padx=8, pady=6, cursor="hand2",
                      activebackground="#444", activeforeground="white",
                      anchor="w", width=16).pack(fill="x", padx=1, pady=1)
            tk.Button(self._sel_panel, text="✕  Cancel",
                      command=self._sel_move_cancel,
                      bg="#553333", fg="white", font=("Segoe UI",9,"bold"),
                      relief="flat", padx=8, pady=6, cursor="hand2",
                      activebackground="#774444", activeforeground="white",
                      anchor="w", width=16).pack(fill="x", padx=1, pady=1)

    def _sel_move_restart(self):
        """Go back to choosing a folder."""
        self._move_target = None
        n = len(self._selected)
        if getattr(self, '_sel_panel', None) and self._sel_panel.winfo_exists():
            self._sel_panel_lbl.config(
                text=f"  ☰  Click a folder in the tree to move {n} file{'s' if n!=1 else ''}",
                bg="#4a2800")
            self._sel_panel.configure(highlightbackground="#ff8800")
            for w in list(self._sel_panel.pack_slaves()):
                if isinstance(w, tk.Button): w.destroy()
            tk.Button(self._sel_panel, text="✕  Cancel Move",
                      command=self._sel_move_cancel,
                      bg="#553333", fg="white", font=("Segoe UI",9,"bold"),
                      relief="flat", padx=8, pady=6, cursor="hand2",
                      activebackground="#774444", activeforeground="white",
                      anchor="w", width=16).pack(fill="x", padx=1, pady=1)

    def _sel_move_confirm(self):
        """Execute the move."""
        import shutil
        dest = self._move_target
        if not dest: return
        files = list(self._selected)
        move_log = []  # list of (src, dst) for undo
        errors = []
        for orig in files:
            try:
                fname = os.path.basename(orig)
                dst = os.path.join(dest, fname)
                # Handle collisions — auto-rename
                if os.path.exists(_longpath(dst)):
                    stem, ext = os.path.splitext(fname)
                    n = 1
                    while os.path.exists(_longpath(dst)):
                        dst = os.path.join(dest, f"{stem}_{n}{ext}"); n += 1
                shutil.move(_longpath(orig), _longpath(dst))
                move_log.append((orig, dst))
            except Exception as ex:
                errors.append(f"{os.path.basename(orig)}: {ex}")

        # Transfer thumbnail entries in blobs — copy to dest, soft-delete from source
        moved_srcs = [src for src, dst in move_log]
        if moved_srcs:
            try: thumb_move(moved_srcs, dest)
            except Exception as ex: pass  # non-fatal — thumbs will regenerate on next browse

        # Exit move mode
        self._move_mode = False
        self._move_target = None
        self.tree.configure(style="Treeview")
        self._selected.clear()
        self._hide_selection_panel()
        self._load_folder(self.current_folder)
        self._refresh_tree_stats()

        # Result message with Undo option
        moved = len(move_log)
        dest_name = os.path.basename(dest) or dest
        msg = f"Moved {moved} file{'s' if moved!=1 else ''} to:  {dest_name}"
        if errors:
            msg += f"\n\n{len(errors)} error(s):\n" + "\n".join(errors[:5])

        # Show result with Undo button
        rdlg = tk.Toplevel(self.win); rdlg.title("Move Complete")
        rdlg.configure(bg=BG3); rdlg.transient(self.win); rdlg.grab_set()
        self._centre_window(rdlg, 420, 180)
        tk.Label(rdlg, text=msg, bg=BG3, fg=TEXT_BRIGHT,
                 font=("Segoe UI",10), wraplength=380, justify="left").pack(pady=(16,8), padx=16)
        bf = tk.Frame(rdlg, bg=BG3); bf.pack(pady=8)
        def do_undo():
            rdlg.destroy()
            undo_errors = []
            moved_back = []
            for src, dst in move_log:
                try:
                    shutil.move(_longpath(dst), _longpath(src))
                    moved_back.append((dst, src))
                except Exception as ex:
                    undo_errors.append(f"{os.path.basename(dst)}: {ex}")
            # Move blob entries back
            if moved_back:
                try:
                    dsts = [d for d, s in moved_back]
                    src_folder = os.path.dirname(move_log[0][0])
                    thumb_move(dsts, src_folder)
                except: pass
            self._load_folder(self.current_folder)
            self._refresh_tree_stats()
            um = f"Undone — {len(move_log)} file{'s' if len(move_log)!=1 else ''} moved back."
            if undo_errors: um += f"\n\n{len(undo_errors)} could not be undone."
            messagebox.showinfo("Undo Complete", um, parent=self.win)
        tk.Button(bf, text="↩  Undo Move", command=do_undo,
                  bg="#444466", fg="white", font=("Segoe UI",9,"bold"),
                  relief="flat", padx=10, pady=4, cursor="hand2").pack(side="left", padx=6)
        tk.Button(bf, text="  OK  ", command=rdlg.destroy,
                  bg="#444", fg=TEXT_BRIGHT, font=("Segoe UI",10),
                  relief="flat", padx=10, pady=4, cursor="hand2").pack(side="left", padx=6)

    def _tag_visible(self):
        """Tag all thumbnails currently rendered on screen (the current page)."""
        from datetime import datetime
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        tagged_set = self._shadow_tagged if self._shadow_active else self.tagged
        tagged_at  = self._shadow_tagged_at if self._shadow_active else self.tagged_at
        for item in self.thumb_widgets:
            cv, _, orig = item[0], item[1], item[2]
            if orig not in tagged_set:
                tagged_set.add(orig); tagged_at[orig] = ts
                if not self._shadow_active and orig not in self.tagged_order:
                    self.tagged_order.append(orig)
                self._cv_repaint(cv, *self._cell_colours(orig))
        self._update_coll_info()
        if self._shadow_active: self._shadow_update_btn()
        else: self._schedule_save(); self._schedule_tree_refresh()

    def _tag_selected(self):
        """Tag all selected thumbnails — add to current collection."""
        if not self._selected:
            self._status("No files selected — use Sel All or Sel Visible first"); return
        if not self.collection:
            messagebox.showinfo("No collection", "Create or select a collection first.", parent=self.win); return
        from datetime import datetime
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        tagged_set = self._shadow_tagged if self._shadow_active else self.tagged
        tagged_at  = self._shadow_tagged_at if self._shadow_active else self.tagged_at
        for orig in self._selected:
            if orig not in tagged_set:
                tagged_set.add(orig); tagged_at[orig] = ts
                if not self._shadow_active and orig not in self.tagged_order:
                    self.tagged_order.append(orig)
        for item in self.thumb_widgets:
            cv, _, orig = item[0], item[1], item[2]
            if orig in self._selected:
                self._cv_repaint(cv, *self._cell_colours(orig))
        n = len(self._selected)
        self._selected.clear()
        self._repaint_selection()
        self._update_coll_info()
        self._status(f"✓  {n} file{'s' if n!=1 else ''} added to '{self.collection}'")
        if self._shadow_active: self._shadow_update_btn()
        else: self._schedule_save(); self._schedule_tree_refresh()

    def _untag_selected(self):
        """Untag all selected thumbnails."""
        if not self._selected:
            self._status("No files selected — use Sel All or Sel Visible first"); return
        tagged_set = self._shadow_tagged if self._shadow_active else self.tagged
        tagged_at  = self._shadow_tagged_at if self._shadow_active else self.tagged_at
        for orig in self._selected:
            tagged_set.discard(orig); tagged_at.pop(orig, None)
            if not self._shadow_active:
                try: self.tagged_order.remove(orig)
                except ValueError: pass
        for item in self.thumb_widgets:
            cv, _, orig = item[0], item[1], item[2]
            if orig in self._selected:
                self._cv_repaint(cv, *self._cell_colours(orig))
        n = len(self._selected)
        self._selected.clear()
        self._repaint_selection()
        self._update_coll_info()
        self._status(f"✓  {n} file{'s' if n!=1 else ''} removed from '{self.collection}'")
        if self._shadow_active: self._shadow_update_btn()
        else: self._schedule_save(); self._schedule_tree_refresh()

    def _commit(self):
        if not self.collection: messagebox.showinfo("No collection","Create a collection first.", parent=self.win); return
        self._save_current_collection(); self._update_coll_info()
        messagebox.showinfo("Saved",f"Collection '{self.collection}' saved.\n\n{len(self.tagged)} files tagged.", parent=self.win)

    # ── Tagged view ────────────────────────────────────────────────────────────
    def _on_left_cull_click(self):
        if self._in_cull_view:
            self._exit_special_view()
        else:
            self._show_cull_view()

    def _on_coll_listbox_select(self, event=None):
        """Collection name clicked — switch and display it."""
        try:
            sel = self.coll_listbox.curselection()
            if not sel: return
            if not self._2panel_nav_guard(): return
            item_text = self.coll_listbox.get(sel[0])
            name = item_text.split('  (')[0].strip() if '  (' in item_text else item_text.strip()
            if not name: return
            # Clear folder tree highlight — folder and collection mutually exclusive
            try:
                self.tree.selection_remove(self.tree.selection())
            except Exception: pass
            self._switch_collection(name)
            self._show_collection_view()
        except Exception: pass

    def _update_left_panel_highlight(self):
        # Cull row
        try:
            n = len(self._culled)
            if self._in_cull_view:
                self.lbl_cull_row.config(
                    bg=CULLED_BD, fg='white',
                    text=f'  🗑  Deletion List  ({n})')
            else:
                self.lbl_cull_row.config(
                    bg=BG2,
                    fg=CULLED_BD if n else TEXT_DIM,
                    text=f'  🗑  Deletion List  ({n})' if n else '  🗑  Deletion List')
        except Exception: pass
        # Reorder row
        try:
            active = bool(self.collection and self.tagged)
            self.lbl_reorder.config(fg=TEXT_BRIGHT if active else TEXT_DIM)
        except Exception: pass
        # Collection listbox — highlight active (unbind to prevent re-triggering _on_coll_listbox_select)
        try:
            if not self.coll_listbox: return
            root = self.mode_cfg['root']
            cols = _list_collections(root)
            self.coll_listbox.unbind('<<ListboxSelect>>')
            self.coll_listbox.selection_clear(0, 'end')
            if self.collection in cols:
                idx = cols.index(self.collection)
                self.coll_listbox.selection_set(idx)
                self.coll_listbox.see(idx)
            self.coll_listbox.bind('<<ListboxSelect>>', self._on_coll_listbox_select)
        except Exception: pass

    def _refresh_coll_listbox(self):
        try:
            if self.coll_listbox is None: return
            root = self.mode_cfg['root']
            cols = _list_collections(root)
            self.coll_listbox.delete(0, 'end')
            for name in cols:
                try:
                    n = len(_read_collection(name, root))
                except Exception:
                    n = 0
                label = f"    {name}  ({n})" if n else f"    {name}"
                self.coll_listbox.insert('end', label)
            self._update_left_panel_highlight()
        except Exception as _e:
            import traceback; print(f"_refresh_coll_listbox error: {_e}\n{traceback.format_exc()}")

    def _update_cull_radio_hint(self):
        self._update_left_panel_highlight()
        self._update_statusbar()
        # Belt-and-braces: directly update the count label
        try:
            n = len(self._culled)
            self.lbl_cull_row.config(
                fg=CULLED_BD if n else TEXT_DIM,
                text=f'  🗑  Deletion List  ({n})' if n else '  🗑  Deletion List')
        except Exception: pass

    def _show_collection_view(self):
        """Switch grid to show current collection files."""
        self._hide_delete_popup()
        # If tagged appears empty, try reloading from file — handles stale state
        if not self.tagged and self.collection:
            data = _read_collection(self.collection, self.mode_cfg['root'])
            if data:
                self.tagged      = set(data.keys())
                self.tagged_at   = dict(data)
                self.tagged_order = list(data.keys())
        if not self.tagged:
            messagebox.showinfo("Empty collection",
                f"Collection '{self.collection}' has no tagged files.", parent=self.win)
            self._set_view_radio("folder"); return
        self._in_tagged_view = True
        self._in_cull_view   = False
        self._all_files = list(self.tagged_order)
        self._page_num  = 0
        self._set_collection_label()
        self._update_left_panel_highlight()
        self._show_page()

    def _show_cull_view(self):
        """Switch grid to show cull list."""
        if not self._culled:
            messagebox.showinfo("Cull list empty",
                "No files are marked for deletion.\n\n"
                "Click the 🗑 Mark button on a thumbnail to mark it.",
                parent=self.win)
            self._set_view_radio("folder"); return
        self._in_cull_view = True
        self._show_delete_popup()
        self._all_files = sorted(self._culled)
        self._page_num  = 0
        self.lbl_folder.config(
            text=f"🗑  Cull List — {len(self._culled)} files marked for deletion")
        self._update_view_status(f"🗑  Cull List  —  {len(self._culled)} marked for deletion")
        self._update_left_panel_highlight()
        self._show_page()

    def _exit_special_view(self):
        """Return to folder view — called when cull/collection view should close."""
        self._in_tagged_view  = False
        self._in_cull_view    = False
        self._in_group_review = False
        try: self._btn_back_group.pack_forget()
        except: pass
        self._in_located_view = False
        self._in_similar_view  = False
        self._similar_groups   = {}
        self._in_group_summary = False
        self._group_clusters   = []
        self._hide_delete_popup()
        self._set_view_radio("folder")
        self._load_folder(self.current_folder)

    # ── _toggle_tagged_view kept for any remaining internal callers ───────────
    def _toggle_tagged_view(self):
        if self._in_tagged_view:
            self._exit_special_view()
        else:
            self._set_view_radio("collection")
            self._show_collection_view()

    def _update_tagged_btn(self):
        """No-op — replaced by view radio panel. Kept for call-site compatibility."""
        # Update contact sheet button state
        try:
            if self.tagged:
                self.btn_contact.config(state="normal", bg="#446688")
            else:
                self.btn_contact.config(state="disabled", bg="#333344")
        except Exception: pass

    # ── Shadow List ────────────────────────────────────────────────────────────
    def _toggle_shadow(self):
        """Enter or exit shadow mode."""
        if self._shadow_active:
            self._exit_shadow()
        else:
            self._enter_shadow()

    def _enter_shadow(self):
        if not self.tagged:
            messagebox.showinfo("Nothing to shadow",
                "No files are tagged in the current collection.\n\n"
                "Tag some files first, then create a shadow to subset them.", parent=self.win); return
        self._shadow_source_collection = self.collection
        self._shadow_files = list(self.tagged)
        self._shadow_tagged.clear(); self._shadow_tagged_at.clear()
        self._shadow_active = True
        # Update UI
        try: self.btn_shadow.config(text="◈  Shadow (empty)", bg="#aa6600")
        except: pass
        try: self.btn_shadow_fork.pack(side="left", padx=3, pady=5)
        except: pass
        try: self.btn_shadow_clear.pack(side="left", padx=3, pady=5)
        except: pass
        # Disable collection controls
        try: self.coll_combo.config(state="disabled")
        except: pass
        try:
            if self.lbl_coll_info:
                self.lbl_coll_info.config(
                    text=f"◈  Shadow — sourced from: {self._shadow_source_collection}",
                    fg="#cc8800")
        except: pass
        # Load shadow files into grid
        self._all_files = self._shadow_files[:]
        self._page_num = 0
        self.lbl_folder.config(
            text=f"◈  Shadow List — {len(self._shadow_files)} files from "
                 f"collection '{self._shadow_source_collection}'  |  "
                 f"Click folder tree to browse originals in context")
        self._show_page()

    def _exit_shadow(self, force=False):
        """Exit shadow mode. If shadow has tags and force=False, warn first."""
        if not force and self._shadow_tagged:
            resp = messagebox.askyesnocancel("Unsaved shadow tags",
                f"◈  Shadow has {len(self._shadow_tagged)} tagged files that have not been "
                f"saved as a collection.\n\nIf you exit shadow mode these tags will be lost.\n\n"
                f"Save as collection before exiting?", parent=self.win)
            if resp is None: return   # Cancel
            if resp:                  # Yes — fork first
                self._shadow_fork_dialog()
                return
            # No — discard and exit
        self._shadow_active = False
        self._shadow_tagged.clear(); self._shadow_tagged_at.clear()
        self._shadow_files = []
        # Restore UI
        try: self.btn_shadow.config(text="◈  Shadow", bg="#334455")
        except: pass
        try: self.btn_shadow_fork.pack_forget()
        except: pass
        try: self.btn_shadow_clear.pack_forget()
        except: pass
        try: self.coll_combo.config(state="readonly")
        except: pass
        self._update_coll_info()
        self._load_folder(self.current_folder)

    def _shadow_clear_tags(self):
        """Clear all tags in the shadow without exiting."""
        self._shadow_tagged.clear(); self._shadow_tagged_at.clear()
        try: self.btn_shadow.config(text="◈  Shadow (empty)")
        except: pass
        self._update_coll_info()
        self._show_page()

    def _shadow_fork_dialog(self):
        """Prompt for name and fork shadow to a new named collection."""
        dlg = tk.Toplevel(self.win); dlg.title("Fork Shadow to Collection")
        dlg.configure(bg=BG3); dlg.grab_set(); dlg.transient(self.win)
        self._centre_window(dlg, 360, 150)
        tk.Label(dlg, text="Save shadow as collection:",
                 bg=BG3, fg=TEXT_BRIGHT, font=("Segoe UI",10)).pack(pady=(16,6))
        name_var = tk.StringVar()
        entry = tk.Entry(dlg, textvariable=name_var, font=("Segoe UI",11),
                         bg=BG2, fg=TEXT_BRIGHT, insertbackground=TEXT_BRIGHT,
                         relief="flat", bd=1, highlightthickness=1,
                         highlightbackground="#555", width=28)
        entry.pack(padx=20); entry.focus_set()
        def on_ok():
            name = name_var.get().strip()
            for ch in r'\/:*?"<>|': name = name.replace(ch,'')
            name = name.strip()
            if not name: return
            root = self.mode_cfg['root']
            _write_collection(name, root, self._shadow_tagged, self._shadow_tagged_at)
            dlg.destroy()
            # Exit shadow cleanly before switching
            self._shadow_active = False
            self._shadow_tagged.clear(); self._shadow_tagged_at.clear()
            self._shadow_files = []
            try: self.btn_shadow.config(text="◈  Shadow", bg="#334455")
            except: pass
            try: self.btn_shadow_fork.pack_forget()
            except: pass
            try: self.btn_shadow_clear.pack_forget()
            except: pass
            try: self.coll_combo.config(state="readonly")
            except: pass
            self._refresh_collection_list()
            self._switch_collection(name, confirm=False)
            self._status(f"Collection created: {name} — {len(self.tagged)} files")
        entry.bind("<Return>", lambda e: on_ok())
        bf = tk.Frame(dlg, bg=BG3); bf.pack(pady=10)
        tk.Button(bf, text="  Save  ", bg=GREEN, fg="white",
                  font=("Segoe UI",9,"bold"), relief="flat", padx=8,
                  cursor="hand2", command=on_ok).pack(side="left", padx=6)
        tk.Button(bf, text="  Cancel  ", bg="#444", fg=TEXT_BRIGHT,
                  font=("Segoe UI",9), relief="flat", padx=8,
                  cursor="hand2", command=dlg.destroy).pack(side="left", padx=6)
        dlg.wait_window()

    def _shadow_update_btn(self):
        n = len(self._shadow_tagged)
        if not self._shadow_active: return
        try:
            if n == 0: self.btn_shadow.config(text="◈  Shadow (empty)")
            else:      self.btn_shadow.config(text=f"◈  Shadow ({n} tagged)")
        except: pass

    # ── Zoom ──────────────────────────────────────────────────────────────────

    # ── _ZoomState — shared mutable state for the zoom window ─────────────────
    class _ZoomState:
        """Holds all shared mutable state for a zoom window session."""
        __slots__ = [
            'cur_path', 'z_scale', 'z_offset', 'z_full',
            'render_id', 'zoom_ref', 'z_lbl_var',
            'rename_var', 'rename_entry', 'lbl_path', 'lbl_info',
            'struct_dlg_update', 'renaming', 'focusout_id', 'mode',
            '_canvas', '_btn_prev', '_btn_next', '_btn_map', '_drag',
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

        st = FileTagger._ZoomState(orig_path, self.mode)

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
                    with Image.open(_longpath(path)) as im:
                        from PIL import ImageOps as _IOS
                        return _IOS.exif_transpose(im).size
                except: return (800, 600)
            else:
                if HAVE_FITZ:
                    try:
                        doc = fitz.open(_longpath(path)); pg = doc[0]
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
        """Create the image canvas. Must be called AFTER top bar and bottom bar are packed."""
        _img_bg = "#fff" if st.mode == "pdfs" else "#000"
        canvas_frame = tk.Frame(zw, bg="#000")
        canvas_frame.pack(fill="both", expand=True)
        canvas = tk.Canvas(canvas_frame, bg=_img_bg, highlightthickness=0, cursor="hand2")
        canvas.pack(fill="both", expand=True)
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
                    if os.name == "nt": os.startfile(_longpath(st.cur_path[0]))
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
        """Bind zoom/pan mouse events to canvas."""
        canvas.bind("<MouseWheel>",       lambda e: self._zoom_on_wheel(canvas, e, st))
        canvas.bind("<ButtonPress-1>",    lambda e: self._zoom_on_press(canvas, e, st))
        canvas.bind("<B1-Motion>",        lambda e: self._zoom_on_drag(canvas, e, st))
        canvas.bind("<ButtonRelease-1>",  lambda e: self._zoom_on_release(canvas, st))
        canvas.bind("<Double-Button-1>",  lambda e: self._zoom_fit(canvas, st))

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
                img = Image.open(_longpath(path))
                img = _IOS.exif_transpose(img)
                img = img.rotate(degrees, expand=True)
                try:
                    from PIL import ExifTags
                    exif = img.getexif()
                    ot = next((k for k,v in ExifTags.TAGS.items() if v=="Orientation"), None)
                    if ot and ot in exif: del exif[ot]
                    img.save(_longpath(path), quality=95, optimize=True, exif=exif.tobytes())
                except:
                    img.save(_longpath(path), quality=95, optimize=True)
                self._make_thumb(path)
                st.z_full[0] = None
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
                img = Image.open(_longpath(path))
                img = _IOS.exif_transpose(img)
                img = _IOS.mirror(img) if axis == "h" else _IOS.flip(img)
                try:
                    from PIL import ExifTags
                    exif = img.getexif()
                    ot = next((k for k,v in ExifTags.TAGS.items() if v=="Orientation"), None)
                    if ot and ot in exif: del exif[ot]
                    img.save(_longpath(path), quality=95, optimize=True, exif=exif.tobytes())
                except:
                    img.save(_longpath(path), quality=95, optimize=True)
                self._make_thumb(path)
                st.z_full[0] = None
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
        if not st.z_full[0]: return 1.0
        cw = canvas.winfo_width() or 800
        ch = canvas.winfo_height() or 600
        iw, ih = st.z_full[0].size
        return min(cw / iw, ch / ih)

    def _zoom_update_zlbl(self, canvas, st):
        if st.z_lbl_var is None: return
        if st.z_scale[0] is None:
            st.z_lbl_var.set("Fit")
        else:
            st.z_lbl_var.set(f"{int(st.z_scale[0] * 100)}%")

    def _zoom_fit(self, canvas, st):
        st.z_scale[0] = None; st.z_offset[0] = 0; st.z_offset[1] = 0
        self._zoom_update_zlbl(canvas, st)
        if canvas: self._zoom_do_render(canvas, st)

    def _zoom_step(self, canvas, direction, st):
        if canvas is None:
            # Called from button before canvas created — find it
            return
        current = st.z_scale[0] or self._zoom_fit_scale(canvas, st)
        st.z_scale[0] = max(0.05, min(16.0, current * (1.25 if direction > 0 else 0.8)))
        self._zoom_update_zlbl(canvas, st)
        self._zoom_do_render(canvas, st)

    def _zoom_on_wheel(self, canvas, event, st):
        current = st.z_scale[0] or self._zoom_fit_scale(canvas, st)
        factor  = 1.15 if event.delta > 0 else (1/1.15)
        new_s   = max(0.05, min(16.0, current * factor))
        cw = canvas.winfo_width(); ch = canvas.winfo_height()
        mx = event.x - cw//2 - st.z_offset[0]
        my = event.y - ch//2 - st.z_offset[1]
        ratio = new_s / current
        st.z_offset[0] = int(st.z_offset[0] - mx * (ratio - 1))
        st.z_offset[1] = int(st.z_offset[1] - my * (ratio - 1))
        st.z_scale[0]  = new_s
        self._zoom_update_zlbl(canvas, st)
        self._zoom_do_render(canvas, st)

    def _zoom_on_press(self, canvas, event, st):
        st._drag = (event.x, event.y, st.z_offset[0], st.z_offset[1])
        canvas.config(cursor="fleur")

    def _zoom_on_drag(self, canvas, event, st):
        if not hasattr(st, "_drag") or not st._drag: return
        sx, sy, ox, oy = st._drag
        st.z_offset[0] = ox + event.x - sx
        st.z_offset[1] = oy + event.y - sy
        self._zoom_do_render(canvas, st)

    def _zoom_on_release(self, canvas, st):
        st._drag = None
        canvas.config(cursor="hand2")

    def _zoom_load_full(self, path, canvas, st):
        """Load full-res image into st.z_full, reset zoom."""
        try:
            if st.mode == "photos":
                from PIL import ImageOps as _IOS
                ImageFile.LOAD_TRUNCATED_IMAGES = True
                img = Image.open(_longpath(path))
                try: img = _IOS.exif_transpose(img)
                except: pass
                img.load()   # force full decode — prevents lazy-load file handle closure
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGB")
                ImageFile.LOAD_TRUNCATED_IMAGES = False
                st.z_full[0] = img
            else:
                if HAVE_FITZ:
                    doc  = fitz.open(_longpath(path)); page = doc[0]
                    mat  = fitz.Matrix(3.0, 3.0)
                    pix  = page.get_pixmap(matrix=mat, alpha=False)
                    st.z_full[0] = Image.frombytes("RGB", [pix.width,pix.height], pix.samples)
                    doc.close()
        except Exception as _e:
            print(f"_zoom_load_full error: {_e}")
            st.z_full[0] = None
        st.z_scale[0] = None; st.z_offset[0] = 0; st.z_offset[1] = 0
        self._zoom_update_zlbl(canvas, st)

    def _zoom_render(self, event, canvas, st):
        """Debounced render triggered by Configure."""
        if st.render_id[0]:
            try: canvas.winfo_toplevel().after_cancel(st.render_id[0])
            except: pass
        st.render_id[0] = canvas.winfo_toplevel().after(80, lambda: self._zoom_do_render(canvas, st))

    def _zoom_do_render(self, canvas, st):
        st.render_id[0] = None
        canvas.winfo_toplevel().update_idletasks()
        cw = canvas.winfo_width(); ch = canvas.winfo_height()
        if cw < 2 or ch < 2:
            st.render_id[0] = canvas.winfo_toplevel().after(80, lambda: self._zoom_do_render(canvas, st))
            return
        path = st.cur_path[0]
        if st.z_full[0] is None:
            self._zoom_load_full(path, canvas, st)
        if st.z_full[0] is None:
            canvas.delete("all")
            canvas.create_text(cw//2, ch//2, anchor="center",
                               text=f"Cannot display:\n{os.path.basename(path)}",
                               fill="#ff6666", font=("Segoe UI",10), justify="center", width=cw-40)
            return
        scale = st.z_scale[0] if st.z_scale[0] else min(cw/st.z_full[0].size[0], ch/st.z_full[0].size[1])
        iw, ih = st.z_full[0].size
        dw = max(1, int(iw*scale)); dh = max(1, int(ih*scale))
        ox = max(-(dw-40), min(cw-40, st.z_offset[0]))
        oy = max(-(dh-40), min(ch-40, st.z_offset[1]))
        st.z_offset[0] = ox; st.z_offset[1] = oy
        x = (cw-dw)//2+ox; y = (ch-dh)//2+oy
        try:
            img = st.z_full[0].resize((dw,dh), Image.BILINEAR)
            photo = ImageTk.PhotoImage(img)
            st.zoom_ref[0] = photo
            canvas.delete("all")
            canvas.create_image(x, y, anchor="nw", image=photo)
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
            has_gps = _get_gps_coords(st.cur_path[0]) is not None
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
            if st.mode == "pdfs" and HAVE_FITZ:
                pages, _ = _get_pdf_info(path)
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
        update_fn = _show_structured_rename_dialog(self.win, stem, ext, _on_apply, zoom_win=zw)
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
            if os.path.exists(_longpath(new_path)):
                messagebox.showerror("Name conflict", f"{new_stem+ext} already exists.", parent=zw)
                self._zoom_revert_entry(st); return False
            os.rename(_longpath(path), _longpath(new_path))
            self._handle_rename(path, new_path)
            # Update canvas cell filename label
            icon = "📷 " if self.mode == "photos" else "📄 "
            new_fname = os.path.basename(new_path)
            for i, item in enumerate(self.thumb_widgets):
                if item[2] == path:
                    try:
                        cv = item[0]
                        cv.itemconfigure("fname_text",
                                         text=_fit_text(icon+new_fname, cv.winfo_width()-28))
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


    def _rotate_thumb(self, path, lbl_widget, degrees):
        """Silently rotate source image, update blob and displayed thumbnail.
        lbl_widget is now unused (canvas cells update via itemconfigure)."""
        try:
            from PIL import ImageOps as _IOS, ExifTags as _ET
            img = Image.open(_longpath(path))
            img = _IOS.exif_transpose(img)
            img = img.rotate(degrees, expand=True)
            if img.mode != 'RGB': img = img.convert('RGB')
            try:
                exif = img.getexif()
                orient_tag = next((k for k,v in _ET.TAGS.items() if v=='Orientation'), None)
                if orient_tag and orient_tag in exif: del exif[orient_tag]
                img.save(_longpath(path), quality=95, optimize=True, exif=exif.tobytes())
            except:
                img.save(_longpath(path), quality=95, optimize=True)
            _size_cache.pop(path, None)   # invalidate cached size
            sz = self._disp_size
            thumb = img.copy()
            _th = int(sz * THUMB_IMG_H / THUMB_SIZE)
            thumb.thumbnail((sz, _th), Image.BILINEAR)
            buf = _io.BytesIO()
            thumb.save(buf, 'JPEG', quality=82, optimize=True)
            thumb_put_many([(path, buf.getvalue())])
            photo = ImageTk.PhotoImage(_scale_to_fit(thumb, sz))
            _photo_cache_put(path, sz, photo)
            def _update(photo=photo):
                self._photo_refs.append(photo)
                for item in self.thumb_widgets:
                    if item[2] == path:
                        try:
                            item[0].itemconfigure("thumb_img", image=photo)
                            item[0]._photo = photo   # keep alive on canvas
                        except Exception: pass
                        break
            self.win.after(0, _update)
        except Exception as e:
            self.win.after(0, lambda: self._status(f"Rotate failed: {e}"))

    def _make_thumb(self, path):
        """Generate a thumbnail for path and write it to the blob. Returns PIL Image or None."""
        try:
            from PIL import ImageOps as _IOS
            img = Image.open(_longpath(path))
            img = _IOS.exif_transpose(img)
            img.thumbnail((THUMB_STORE_SIZE, THUMB_STORE_SIZE), Image.BILINEAR)
            if img.mode != 'RGB': img = img.convert('RGB')
            buf = _io.BytesIO()
            img.save(buf, 'JPEG', quality=82, optimize=True)
            jpeg_bytes = buf.getvalue()
            thumb_put_many([(path, jpeg_bytes)])
            return img
        except Exception as e:
            print(f"_make_thumb failed for {path}: {e}")
            return None

    def _refresh_thumb_cell(self, orig_path):
        """Reload thumbnail for an existing canvas cell from the blob."""
        try:
            jpeg = thumb_get(orig_path)
            if not jpeg: return
            ImageFile.LOAD_TRUNCATED_IMAGES = True
            img = Image.open(_io.BytesIO(jpeg)); img.load()
            ImageFile.LOAD_TRUNCATED_IMAGES = False
            sz = self._disp_size
            img = _scale_to_fit(img, sz)
            photo = ImageTk.PhotoImage(img)
            _photo_cache_put(orig_path, sz, photo)
            for item in self.thumb_widgets:
                if item[2] == orig_path:
                    try:
                        item[0].itemconfigure("thumb_img", image=photo)
                        item[0]._photo = photo   # keep alive on canvas
                        self._photo_refs.append(photo)
                    except Exception: pass
                    return
        except Exception: pass

    def _refresh_cell_size(self, orig_path):
        """Update the size strip text on a canvas cell after a file has been modified."""
        try:
            _, cat, disp_s = _file_size_info_cached(orig_path)
            size_info = f"{cat}  {disp_s}"
            size_colours = {"Tiny":"#cc2200","V.Small":"#bb6600","Small":"#996600",
                            "Medium":"#88cc44","Large":"#44aaff","Huge":"#cc88ff","?":"#888888"}
            colour = size_colours.get(cat, "#555")
            for item in self.thumb_widgets:
                if item[2] == orig_path:
                    try:
                        item[0].itemconfigure("size_text", text=size_info, fill=colour)
                    except Exception: pass
                    return
        except Exception: pass

    def _scroll_grid_to_file(self, path):
        try:
            if path not in self._all_files: return
            idx = self._all_files.index(path)
            target_page = idx // max(1, getattr(self, '_page_size', 40))
            if target_page != self._page_num:
                # Need to load the correct page first, then scroll
                self._page_num = target_page
                self._show_page()
                # Schedule scroll after page renders
                self.win.after(300, lambda: self._scroll_to_idx(idx % max(1, getattr(self, '_page_size', 40))))
            else:
                self._scroll_to_idx(idx % max(1, getattr(self, '_page_size', 40)))
        except Exception: pass

    def _scroll_to_idx(self, page_idx):
        """Scroll canvas so the thumbnail at page_idx is visible."""
        try:
            if page_idx < 0 or page_idx >= len(self.thumb_widgets): return
            widget = self.thumb_widgets[page_idx][0]  # outer frame
            self.canvas.update_idletasks()
            # Get widget y position relative to grid_frame
            wy = widget.winfo_y()
            total_h = self.canvas.bbox("all")
            if not total_h: return
            total_h = total_h[3] - total_h[1]
            if total_h <= 0: return
            canvas_h = self.canvas.winfo_height()
            # Centre the row containing this widget
            target_y = max(0.0, (wy - canvas_h // 2) / total_h)
            target_y = min(target_y, 1.0)
            self.canvas.yview_moveto(target_y)
            # Also highlight briefly so user can see which image was zoomed
            self._focused_orig = self._all_files[self._page_num * max(1, getattr(self, '_page_size', 40)) + page_idx]
        except Exception: pass

    # ── Page navigation ────────────────────────────────────────────────────────
    def _update_visible_label(self):
        try:
            total = len(self._all_files)
            if total == 0 or not self.thumb_widgets:
                try: self.lbl_page.config(text=""); self._jump_var.set("")
                except: pass
                return
            page_size  = max(1, getattr(self, '_page_size', 40))
            page_start = getattr(self, '_page_start', self._page_num * page_size)
            bb = self.canvas.bbox("all")
            if not bb or bb[3] <= 0:
                first = page_start + 1
                last  = min(page_start + page_size, total)
                try: self._jump_var.set(str(first)); self.lbl_page.config(text=f"{first}-{last} of {total}")
                except: pass
                return
            grid_h = bb[3]
            row_h  = self._row_height_px()
            cols   = max(1, self._cols)
            top_frac, bot_frac = self.canvas.yview()
            top_px = top_frac * grid_h
            bot_px = bot_frac * grid_h
            first_row = max(0, int((top_px - GRID_TOP_PAD) / row_h))
            last_row  = max(first_row, int((bot_px - GRID_TOP_PAD - row_h) / row_h))
            first_vis = page_start + first_row * cols + 1
            last_vis  = min(total, page_start + (last_row + 1) * cols)
            first_vis = max(page_start + 1, min(first_vis, total))
            last_vis  = max(first_vis, min(last_vis, total))
            try:
                if not getattr(self, '_loading', False) and not getattr(self, '_jump_locked', False):
                    self._jump_var.set(str(first_vis))
                self.lbl_page.config(text=f"{first_vis}-{last_vis} of {total}")
            except: pass
        except: pass

    def _jump_to_image(self):
        try:    n = int(self._jump_var.get())
        except: return
        total = len(self._all_files)
        if total == 0: return
        n = max(1, min(n, total))
        page_size = max(1, getattr(self, '_page_size', 40))
        self._page_start_override = n - 1
        self._page_num = (n - 1) // page_size
        self._show_page()
        # Keep jump entry showing the requested number after load
        self._jump_var.set(str(n))

    def _page_last(self):
        total = len(self._all_files)
        if not total: return
        page_size = max(1, getattr(self, '_page_size', 40))
        self._page_num = (total - 1) // page_size
        self._show_page()

    def _page_prev(self):
        if self._page_num > 0:
            self._page_num -= 1
            self._show_page()

    def _update_posbar(self):
        """Update position bar indicator to show current position in collection."""
        try:
            if not self._posbar.winfo_exists(): return
            total = len(self._all_files)
            self._posbar.delete("all")
            w = self._posbar.winfo_width()
            h = self._posbar.winfo_height()
            if w < 2: return
            # Background
            self._posbar.create_rectangle(0, 0, w, h, fill="#333333", outline="")
            if total < 1: return
            page_size = max(1, getattr(self, '_page_size', 40))
            # Indicator showing current page position
            lo = (self._page_num * page_size) / total
            hi = min(1.0, ((self._page_num + 1) * page_size) / total)
            x0 = int(lo * w)
            x1 = max(x0 + 4, int(hi * w))
            self._posbar.create_rectangle(x0, 1, x1, h - 1, fill="#4488cc", outline="")
        except: pass

    def _on_posbar_click(self, event):
        """Click on position bar — jump to that position in the collection."""
        try:
            total = len(self._all_files)
            if not total: return
            w = self._posbar.winfo_width()
            if w < 2: return
            page_size = max(1, getattr(self, '_page_size', 40))
            if total <= page_size: return  # fits on one screen — no jump needed
            frac = max(0.0, min(1.0, event.x / w))
            target = int(frac * total)
            max_page = (total - 1) // page_size
            new_page = max(0, min(target // page_size, max_page))
            if new_page != self._page_num:
                self._page_num = new_page
                self._show_page()
            else:
                self._update_posbar()
        except: pass

    def _update_nav_label(self):
        """Update the inline navigation bar label."""
        try:
            total = len(self._all_files)
            page_size = max(1, getattr(self, '_page_size', 40))
            first = self._page_num * page_size + 1
            last  = min(total, (self._page_num + 1) * page_size)

            if self._in_tagged_view:
                context = "Tagged"
            elif self._in_cull_view:
                context = "Deletion List"
            elif getattr(self, "_in_group_review", False):
                context = "Group Review"
            elif getattr(self, "_in_group_summary", False):
                context = "Group Summary"
            elif self._in_similar_view:
                context = "Similar"
            else:
                context = os.path.basename(getattr(self, 'current_folder', '')) or "Folder"

            txt = f"{context}  {first}–{last} of {total}" if total else ""
            if self.lbl_nav and self.lbl_nav.winfo_exists():
                self.lbl_nav.config(text=txt)
        except: pass

    def _page_next(self):
        total = len(self._all_files)
        page_size = max(1, getattr(self, '_page_size', 40))
        max_page = (total - 1) // page_size
        if self._page_num < max_page:
            self._page_num += 1
            self._show_page()

    def _scroll_up(self):
        try:
            vh=self.canvas.winfo_height(); total=self.canvas.bbox("all")
            if not total: return
            total_h=total[3]-total[1]
            if total_h<=0: return
            self.canvas.yview_moveto(max(0.0,self.canvas.yview()[0]-vh/total_h))
        except: pass

    def _scroll_down(self):
        try:
            vh=self.canvas.winfo_height(); total=self.canvas.bbox("all")
            if not total: return
            total_h=total[3]-total[1]
            if total_h<=0: return
            self.canvas.yview_moveto(min(1.0,self.canvas.yview()[0]+vh/total_h))
        except: pass

    # ── Display size ───────────────────────────────────────────────────────────
    def _on_grid_dims_change(self, changed=None):
        if getattr(self, '_updating_spinners', False):
            return
        self.canvas.update_idletasks()
        cw = self.canvas.winfo_width()
        if cw < 10: cw = max(400, self.win.winfo_width() - TREE_LEFT_W - 30)

        try:    cols = max(1, int(self._cols_var.get()))
        except: cols = self._user_cols

        # Clamp to maximum columns that fit cleanly — cell width = disp_size + 16 + padx*2
        cell_w = self._disp_size + 20
        max_cols = max(1, cw // cell_w)
        if cols > max_cols:
            cols = max_cols
            self._updating_spinners = True
            self._cols_var.set(str(cols))
            self._updating_spinners = False

        self._user_cols = cols

        # Persist so next launch uses same column count
        global DEFAULT_COLS
        DEFAULT_COLS = self._user_cols
        try:
            ini = os.path.join(os.path.dirname(os.path.abspath(__file__)), "FileTagger.ini")
            cfg = configparser.ConfigParser(strict=False)
            if os.path.exists(ini): cfg.read(ini)
            if not cfg.has_section("display"): cfg.add_section("display")
            cfg.set("display", "cols", str(DEFAULT_COLS))
            # Write only [display] section update without destroying multi-root entries
            # Re-read raw, update display section only
            if os.path.exists(ini):
                with open(ini,'r',encoding='utf-8') as f: raw = f.readlines()
            else:
                raw = []
            new_raw = []; in_disp = False
            for line in raw:
                if line.strip().lower() == '[display]': in_disp = True; continue
                if in_disp and line.strip().startswith('['): in_disp = False
                if not in_disp: new_raw.append(line)
            if new_raw and new_raw[-1].strip(): new_raw.append('\n')
            new_raw.append('[display]\n')
            new_raw.append(f'cols       = {DEFAULT_COLS}\n')
            new_raw.append(f'thumb_size = {self._disp_size}\n')
            with open(ini,'w',encoding='utf-8') as f: f.writelines(new_raw)
        except: pass

        # Validate — thumbnails need at least 150px to show controls properly
        MIN_SZ   = 150
        cell_px  = cw // self._user_cols
        sz_check = cell_px - THUMB_PAD
        if sz_check < MIN_SZ:
            max_cols = max(1, cw // (MIN_SZ + THUMB_PAD))
            self._warn_thumbnail_size(self._user_cols, max_cols)
            self._user_cols = max_cols
            self._updating_spinners = True
            try:    self._cols_var.set(str(max_cols))
            except: pass
            finally: self._updating_spinners = False

        if getattr(self, '_all_files', []):
            self._show_page()

    def _warn_thumbnail_size(self, requested, maximum):
        """Warn user their column count makes thumbnails too small."""
        import tkinter.messagebox as mb
        mb.showwarning(
            "Too many columns",
            f"{requested} columns makes thumbnails too small to show the Mark and Tag controls.\n\n"
            f"Maximum for this window width is {maximum} columns.\n\n"
            f"Columns set to {maximum}.",
            parent=self.win
        )




    # ── Size filter ────────────────────────────────────────────────────────────
    def _show_size_popup(self):
        popup=tk.Toplevel(self.win); popup.overrideredirect(True)
        popup.configure(bg="white",relief="solid",bd=1)
        bx=self.btn_size_filter.winfo_rootx()
        by=self.btn_size_filter.winfo_rooty()+self.btn_size_filter.winfo_height()
        popup.geometry(f"+{bx}+{by}")
        cats=[("Tiny",   "<50 KB"),
              ("V.Small","50–150 KB"),
              ("Small",  "150–300 KB"),
              ("Medium", "300 KB–1.5 MB"),
              ("Large",  "1.5–6 MB"),
              ("Huge",   ">6 MB")]
        for cat,rng in cats:
            tk.Checkbutton(popup,text=f"{cat}  ({rng})",variable=self._size_checks[cat],
                           bg="white",fg="#333",activebackground="#eef",
                           font=("Segoe UI",9),anchor="w",padx=10,
                           command=self._apply_size_filter).pack(fill="x",pady=1)
        bf=tk.Frame(popup,bg="#f0f0f0"); bf.pack(fill="x",pady=(2,0))
        def _reset_all():
            for v in self._size_checks.values(): v.set(True)
            self._apply_size_filter()
        def _reset_none():
            for v in self._size_checks.values(): v.set(False)
            self._apply_size_filter()
        tk.Button(bf,text="All",bg="#f0f0f0",fg="#333",font=("Segoe UI",8),relief="flat",padx=6,
                  command=_reset_all).pack(side="left",padx=4,pady=2)
        tk.Button(bf,text="None",bg="#f0f0f0",fg="#333",font=("Segoe UI",8),relief="flat",padx=6,
                  command=_reset_none).pack(side="left",padx=2,pady=2)
        tk.Button(bf,text="Close",bg="#f0f0f0",fg="#333",font=("Segoe UI",8),relief="flat",padx=6,
                  command=popup.destroy).pack(side="right",padx=4,pady=2)
        popup.bind("<FocusOut>",lambda e: popup.destroy() if popup.winfo_exists() else None)
        popup.focus_set()

    def _apply_size_filter(self):
        bands={"Tiny":   (0,       50_000),
               "V.Small":(50_000,  150_000),
               "Small":  (150_000, 300_000),
               "Medium": (300_000, 1_500_000),
               "Large":  (1_500_000,6_000_000),
               "Huge":   (6_000_000,float('inf'))}
        active=[cat for cat,var in self._size_checks.items() if var.get()]
        show_all  = len(active) == len(self._size_checks)
        show_none = len(active) == 0
        if show_all:   lbl = "Size: All ▾"
        elif show_none:lbl = "Size: None ▾"
        else:          lbl = f"Size: {', '.join(active)} ▾"
        self.btn_size_filter.config(text=lbl)

        if show_all:
            # Restore full file list
            if self._in_tagged_view:
                self._all_files = list(self.tagged_order) if self.tagged_order else sorted(self.tagged)
            else:
                self._all_files = self._unfiltered_files if hasattr(self,'_unfiltered_files') and self._unfiltered_files else self._all_files
            self._page_num = 0
            self._show_page()
            self._status(f"Size filter cleared — {len(self._all_files)} files")
            self._update_visible_label()
            return

        # Save unfiltered list on first filter application
        if not hasattr(self,'_unfiltered_files') or not self._unfiltered_files:
            self._unfiltered_files = list(self._all_files)
        source = self._unfiltered_files

        if show_none:
            filtered = []
        else:
            filtered = []
            for fp in source:
                sz, cat, disp = _file_size_info_cached(fp)
                for ac in active:
                    lo, hi = bands[ac]
                    if hi == float('inf'):
                        if sz >= lo: filtered.append(fp); break
                    else:
                        if lo <= sz < hi: filtered.append(fp); break

        self._all_files = filtered
        self._page_num = 0
        self._show_page()

        if not filtered:
            self._status(f"Size filter: {', '.join(active)}  —  no images in this size range")
        else:
            self._status(f"Size filter: {', '.join(active)}  —  {len(filtered)} of {len(source)} shown")
        self._update_visible_label()

    # ── File info popup ────────────────────────────────────────────────────────
    def _show_file_info(self, orig, anchor_btn):
        """Show a persistent floating info popup. First call creates and positions it;
        subsequent calls update content in place without moving the window."""
        import datetime as _dt2

        def _fmt_date(s):
            """Format any date value — float timestamp, EXIF string, or ISO string."""
            import datetime as _dt3
            if not s: return ""
            try:
                # Float/int = filesystem timestamp
                if isinstance(s, (int, float)):
                    return _dt3.datetime.fromtimestamp(float(s)).strftime('%d %b %Y  %H:%M')
                s = str(s).strip()
                # EXIF format: '2020:04:05 10:25:00' — colons in date part
                if len(s) >= 10 and s[4] == ':' and s[7] == ':':
                    s = s[:10].replace(':', '-') + s[10:]
                for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
                    try: return _dt3.datetime.strptime(s[:19].strip(), fmt).strftime('%d %b %Y  %H:%M')
                    except: pass
            except: pass
            return str(s)[:16]

        def _dms_to_decimal(dms, ref):
            try:
                def v(x): return float(x.numerator)/float(x.denominator) if hasattr(x,'numerator') else float(x)
                val = v(dms[0]) + v(dms[1])/60 + v(dms[2])/3600
                if ref in ('S','W'): val = -val
                return val
            except: return None

        # ── Gather metadata ────────────────────────────────────────────────────
        fname    = os.path.basename(orig)
        sz, cat, disp = _file_size_info(orig)
        w_px = h_px = 0
        exif_date_taken = ""   # EXIF DateTimeOriginal — when shutter fired
        fs_modified     = ""   # filesystem mtime
        fs_created      = ""   # filesystem ctime (creation on Windows, last metadata change on Linux)
        lat = lon = None
        pdf_pages = 0

        try:
            stat = os.stat(_longpath(orig))
            fs_modified = _fmt_date(stat.st_mtime)
            fs_created  = _fmt_date(stat.st_ctime)
        except: pass

        if self.mode == "photos":
            try:
                with Image.open(_longpath(orig)) as im:
                    w_px, h_px = im.size
                    try:
                        from PIL.ExifTags import TAGS, GPSTAGS
                        raw_exif = im._getexif()
                        if raw_exif:
                            gps_data = {}
                            for tag_id, val in raw_exif.items():
                                tag = TAGS.get(tag_id, tag_id)
                                if tag == 'DateTimeOriginal':
                                    exif_date_taken = _fmt_date(str(val))
                                elif tag == 'GPSInfo':
                                    for gps_id, gps_val in val.items():
                                        gps_tag = GPSTAGS.get(gps_id, gps_id)
                                        gps_data[gps_tag] = gps_val
                            if gps_data:
                                lat_dms = gps_data.get('GPSLatitude')
                                lat_ref = gps_data.get('GPSLatitudeRef', 'N')
                                lon_dms = gps_data.get('GPSLongitude')
                                lon_ref = gps_data.get('GPSLongitudeRef', 'E')
                                if lat_dms and lon_dms:
                                    lat = _dms_to_decimal(lat_dms, lat_ref)
                                    lon = _dms_to_decimal(lon_dms, lon_ref)
                    except: pass
            except: pass

        elif self.mode == "pdfs" and HAVE_FITZ:
            try:
                doc = fitz.open(_longpath(orig))
                pdf_pages = doc.page_count
                page = doc[0]
                w_px, h_px = int(page.rect.width), int(page.rect.height)
                meta = doc.metadata
                if meta.get('creationDate'):
                    exif_date_taken = _fmt_date(meta['creationDate'][:10].replace('D:','').replace("'",''))
                doc.close()
            except: pass

        IBG  = "white"
        IBG2 = "#eeeeee"
        IFG  = "#111111"
        IDIM = "#444444"
        ISEP = "#bbbbbb"
        IGRN = "#1a6633"

        # ── Create popup window on first call ──────────────────────────────────
        popup_exists = (hasattr(self, '_info_popup') and
                        self._info_popup and
                        self._info_popup.winfo_exists())

        if not popup_exists:
            popup = tk.Toplevel(self.win)
            popup.overrideredirect(True)
            popup.configure(bg=IBG)
            popup.transient(self.win)
            self._info_popup = popup

            # Title bar — drag handle + close
            title_bar = tk.Frame(popup, bg=IBG2, pady=4)
            title_bar.pack(fill="x")
            tk.Label(title_bar, text="  File Info", bg=IBG2, fg=IDIM,
                     font=("Segoe UI",12,"bold")).pack(side="left", padx=4)
            tk.Button(title_bar, text="✕", bg=IBG2, fg=IDIM,
                      font=("Segoe UI",11), relief="flat", bd=0, padx=8, pady=0,
                      cursor="hand2", activebackground="#cc2222", activeforeground="white",
                      command=lambda: (self._info_popup.destroy(),
                                       setattr(self, '_info_popup', None),
                                       setattr(self, '_info_popup_orig', None))
                      ).pack(side="right")

            # Drag logic on title bar
            _drag = [0, 0]
            def _start_drag(e): _drag[0]=e.x_root; _drag[1]=e.y_root
            def _do_drag(e):
                dx=e.x_root-_drag[0]; dy=e.y_root-_drag[1]
                x=self._info_popup.winfo_x()+dx; y=self._info_popup.winfo_y()+dy
                self._info_popup.geometry(f"+{x}+{y}")
                _drag[0]=e.x_root; _drag[1]=e.y_root
            title_bar.bind("<ButtonPress-1>", _start_drag)
            title_bar.bind("<B1-Motion>",     _do_drag)

            # Position near anchor button — only on first creation
            popup.update_idletasks()
            try:
                bx = anchor_btn.winfo_rootx()
                by = anchor_btn.winfo_rooty() + anchor_btn.winfo_height()
            except:
                bx, by = 100, 100
            pw = popup.winfo_reqwidth() or 220
            ph = popup.winfo_reqheight() or 200
            sw = self.win.winfo_screenwidth(); sh = self.win.winfo_screenheight()
            bx = min(bx, sw - pw - 4)
            by = min(by, sh - ph - 4)
            popup.geometry(f"+{bx}+{by}")
        else:
            popup = self._info_popup
            popup.configure(bg=IBG)
            # Destroy old content frame, rebuild in place — window doesn't move
            if hasattr(self, '_info_content_frame'):
                try: self._info_content_frame.destroy()
                except: pass

        self._info_popup_orig = orig

        # ── Build content frame (rebuilt on every call) ────────────────────────
        pad = tk.Frame(popup, bg=IBG, padx=14, pady=10)
        pad.pack()
        self._info_content_frame = pad

        def row(label, value, fg=IFG, italic=False):
            r = tk.Frame(pad, bg=IBG); r.pack(fill="x", pady=2)
            tk.Label(r, text=label, bg=IBG, fg=IDIM,
                     font=("Segoe UI",11), width=14, anchor="w").pack(side="left")
            tk.Label(r, text=value, bg=IBG, fg=fg,
                     font=("Segoe UI",11,"bold","italic" if italic else "normal"),
                     anchor="w").pack(side="left")

        def sep():
            tk.Frame(pad, bg=ISEP, height=1).pack(fill="x", pady=4)

        row("File:", fname, IFG)
        row("Size:", disp)
        if w_px and h_px:
            row("Pixels:" if self.mode=="photos" else "Page size:",
                f"{w_px} × {h_px}" + (" pt" if self.mode=="pdfs" else ""))
        if pdf_pages:
            row("Pages:", str(pdf_pages))
        sep()
        if exif_date_taken:
            row("Date Taken:",  exif_date_taken, "#88cc88")
        else:
            row("Date Taken:",  "No EXIF data", IDIM, italic=True)
        row("File Created:",  fs_created  if fs_created  else "Unknown")
        row("File Modified:", fs_modified if fs_modified else "Unknown")
        if lat is not None and lon is not None:
            sep()
            row("Latitude:",  f"{lat:.6f}°")
            row("Longitude:", f"{lon:.6f}°")

        popup.update_idletasks()
        popup.lift()
    # ── MGEN Process Dialog ────────────────────────────────────────────────────
    def _process_dialog(self):
        """Show the MGEN processing dialog."""
        import tkinter.filedialog as fd
        if not self.tagged:
            messagebox.showinfo("Nothing to process",
                "No files selected. Select files and use Operations → MGEN File Operations.",
                parent=self.win); return

        dlg = tk.Toplevel(self.win)
        dlg.title("Process Collection with MGEN")
        dlg.configure(bg=BG3); dlg.grab_set(); dlg.transient(self.win)
        self._centre_window(dlg, 700, 880)
        dlg.resizable(True, True)
        dlg.minsize(600, 780)

        # ── Pinned button row at BOTTOM ────────────────────────────────────────
        bf = tk.Frame(dlg, bg=BG3); bf.pack(side="bottom", pady=12)

        # ── Scrollable content area ────────────────────────────────────────────
        outer = tk.Frame(dlg, bg=BG3); outer.pack(fill="both", expand=True)
        cv = tk.Canvas(outer, bg=BG3, highlightthickness=0)
        vsb = tk.Scrollbar(outer, orient="vertical", command=cv.yview)
        cv.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        cv.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(cv, bg=BG3)
        cv_win = cv.create_window((0,0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
        cv.bind("<Configure>", lambda e: cv.itemconfig(cv_win, width=e.width))
        cv.bind("<MouseWheel>", lambda e: cv.yview_scroll(-1 if e.delta>0 else 1, "units"))

        # All content goes into 'inner' not 'dlg'
        dlg_inner = inner  # alias so section/hline helpers use inner

        def section(text):
            tk.Label(dlg_inner, text=text, bg=BG3, fg=ACCENT,
                     font=("Segoe UI",9,"bold")).pack(anchor="w", padx=16, pady=(10,2))

        def hline():
            tk.Frame(dlg_inner, bg="#333", height=1).pack(fill="x", padx=16, pady=2)

        tk.Label(dlg_inner, text=f"Process: {self.collection}  —  {len(self.tagged)} files",
                 bg=BG3, fg=TEXT_BRIGHT, font=("Segoe UI",12,"bold")).pack(pady=(14,4))

        # ── Output folder ──────────────────────────────────────────────────────
        section("Output folder")
        _default_out = r"C:\Users\Ian\OneDrive\Python Programs\FileTagger release 1"
        out_var = tk.StringVar(value=_default_out)
        fr = tk.Frame(dlg_inner, bg=BG3); fr.pack(fill="x", padx=16, pady=2)
        tk.Entry(fr, textvariable=out_var, bg=BG2, fg=TEXT_BRIGHT,
                 font=("Segoe UI",9), insertbackground=TEXT_BRIGHT,
                 relief="flat", highlightthickness=1, highlightbackground="#555"
                 ).pack(side="left", fill="x", expand=True, padx=(0,6))
        tk.Button(fr, text="Browse", bg="#335577", fg="white", font=("Segoe UI",9,"bold"),
                  relief="flat", padx=6, cursor="hand2",
                  command=lambda: out_var.set(fd.askdirectory(title="Output folder") or out_var.get())
                  ).pack(side="left")

        # ── Temp folder ────────────────────────────────────────────────────────
        section("Temp folder  (manifest and job files)")
        tmp_var = tk.StringVar(value=PROC_TEMP_FOLDER)
        fr2 = tk.Frame(dlg_inner, bg=BG3); fr2.pack(fill="x", padx=16, pady=2)
        tk.Entry(fr2, textvariable=tmp_var, bg=BG2, fg=TEXT_BRIGHT,
                 font=("Segoe UI",9), insertbackground=TEXT_BRIGHT,
                 relief="flat", highlightthickness=1, highlightbackground="#555"
                 ).pack(side="left", fill="x", expand=True, padx=(0,6))
        tk.Button(fr2, text="Browse", bg="#335577", fg="white", font=("Segoe UI",9,"bold"),
                  relief="flat", padx=6, cursor="hand2",
                  command=lambda: tmp_var.set(fd.askdirectory(title="Temp folder") or tmp_var.get())
                  ).pack(side="left")

        # ── MGEN executable ───────────────────────────────────────────────────
        section("MGEN engine  (mgen.py)")
        exe_var = tk.StringVar(value=PROC_MGEN_EXE)
        fr_exe = tk.Frame(dlg_inner, bg=BG3); fr_exe.pack(fill="x", padx=16, pady=2)
        tk.Entry(fr_exe, textvariable=exe_var, bg=BG2, fg=TEXT_BRIGHT,
                 font=("Segoe UI",9), insertbackground=TEXT_BRIGHT,
                 relief="flat", highlightthickness=1, highlightbackground="#555"
                 ).pack(side="left", fill="x", expand=True, padx=(0,6))
        tk.Button(fr_exe, text="Browse", bg="#335577", fg="white", font=("Segoe UI",9,"bold"),
                  relief="flat", padx=6, cursor="hand2",
                  command=lambda: exe_var.set(
                      fd.askopenfilename(title="mgen.py", filetypes=[("Python","*.py"),("All","*.*")])
                      or exe_var.get())
                  ).pack(side="left")

        # Show auto-derived macro paths as read-only info
        mgen_dir = os.path.dirname(exe_var.get()) if exe_var.get() else "..."
        info_fr = tk.Frame(dlg_inner, bg=BG3); info_fr.pack(fill="x", padx=16, pady=(0,4))
        lbl_macros = tk.Label(info_fr,
            text=f"FT_FileOps.msf: FT_IPC\\  |  StringHelpers.msf: {mgen_dir}\\SystemMacros\\",
            bg=BG3, fg=TEXT_DIM, font=("Segoe UI",8), justify="left", anchor="w")
        lbl_macros.pack(side="left")
        def _update_macro_info(*a):
            d = os.path.dirname(exe_var.get()) if exe_var.get() else "..."
            lbl_macros.config(text=f"FT_FileOps.msf: FT_IPC\\  |  StringHelpers.msf: {d}\\SystemMacros\\")
        exe_var.trace_add("write", _update_macro_info)

        # ── Operations ────────────────────────────────────────────────────────
        hline()
        section("Operations")
        ops_frame = tk.Frame(dlg_inner, bg=BG3); ops_frame.pack(fill="x", padx=20)

        def op_row(parent, label, var, param_widgets=None):
            r = tk.Frame(parent, bg=BG3); r.pack(fill="x", pady=2)
            tk.Checkbutton(r, variable=var, bg=BG3, fg=TEXT_BRIGHT,
                           selectcolor=BG2, activebackground=BG3,
                           font=("Segoe UI",9)).pack(side="left")
            tk.Label(r, text=label, bg=BG3, fg=TEXT_BRIGHT,
                     font=("Segoe UI",9), width=20, anchor="w").pack(side="left")
            if param_widgets:
                for pw in param_widgets: pw(r)
            return r

        def num_entry(var, width=5):
            def _make(parent):
                tk.Entry(parent, textvariable=var, bg=BG2, fg=TEXT_BRIGHT,
                         font=("Segoe UI",9), width=width,
                         insertbackground=TEXT_BRIGHT, relief="flat",
                         highlightthickness=1, highlightbackground="#555").pack(side="left", padx=4)
            return _make

        v_autorotate = tk.BooleanVar(); v_aspectratio = tk.BooleanVar()
        v_bw = tk.BooleanVar(); v_fitwidth = tk.BooleanVar()
        v_optimise = tk.BooleanVar(); v_strip = tk.BooleanVar()
        v_scale = tk.BooleanVar()
        v_copyonly = tk.BooleanVar(); v_delete = tk.BooleanVar()

        ar_w_var  = tk.StringVar(value="16"); ar_h_var = tk.StringVar(value="9")
        fw_var    = tk.StringVar(value="2000")
        opt_var   = tk.StringVar(value="85")
        scale_var = tk.StringVar(value="50")

        def ar_widgets(parent):
            tk.Label(parent, text="W:", bg=BG3, fg=TEXT_DIM, font=("Segoe UI",9)).pack(side="left")
            tk.Entry(parent, textvariable=ar_w_var, bg=BG2, fg=TEXT_BRIGHT,
                     font=("Segoe UI",9), width=4, insertbackground=TEXT_BRIGHT,
                     relief="flat", highlightthickness=1, highlightbackground="#555").pack(side="left", padx=2)
            tk.Label(parent, text="×  H:", bg=BG3, fg=TEXT_DIM, font=("Segoe UI",9)).pack(side="left")
            tk.Entry(parent, textvariable=ar_h_var, bg=BG2, fg=TEXT_BRIGHT,
                     font=("Segoe UI",9), width=4, insertbackground=TEXT_BRIGHT,
                     relief="flat", highlightthickness=1, highlightbackground="#555").pack(side="left", padx=2)

        op_row(ops_frame, "Auto-rotate (EXIF)",   v_autorotate)
        op_row(ops_frame, "Crop to aspect ratio", v_aspectratio, [ar_widgets])
        op_row(ops_frame, "Convert to greyscale", v_bw)
        op_row(ops_frame, "Fit to width (px)",    v_fitwidth,    [num_entry(fw_var)])
        op_row(ops_frame, "Scale (%)",             v_scale,       [num_entry(scale_var, 4)])
        op_row(ops_frame, "Optimise JPEG",         v_optimise,    [num_entry(opt_var, 4)])
        op_row(ops_frame, "Strip metadata",        v_strip)
        hline()
        op_row(ops_frame, "Copy files only  (no image processing)", v_copyonly)
        op_row(ops_frame, "Delete files in list",  v_delete)

        # ── Mode ──────────────────────────────────────────────────────────────
        hline()
        mode_frame = tk.Frame(dlg_inner, bg=BG3); mode_frame.pack(fill="x", padx=20, pady=6)
        v_mode = tk.StringVar(value="test")

        here = os.path.dirname(os.path.abspath(__file__))
        mgen_available = os.path.exists(PROC_MGEN_EXE) or os.path.exists(os.path.join(here, "mgen.py"))

        tk.Label(mode_frame, text="Run mode:", bg=BG3, fg=TEXT_BRIGHT,
                 font=("Segoe UI",9,"bold")).pack(anchor="w", padx=(0,0), pady=(0,4))

        rb_test = tk.Radiobutton(mode_frame, text="Test  —  write manifest and job script, inspect files, no MGEN launched",
                                  variable=v_mode, value="test",
                                  bg=BG3, fg=TEXT_BRIGHT, selectcolor=BG2,
                                  activebackground=BG3, font=("Segoe UI",9))
        rb_test.pack(anchor="w", pady=2)

        rb_prod_text = "Production  —  write files and run MGEN  (disabled — pending validation)"
        rb_prod = tk.Radiobutton(mode_frame,
                                  text=rb_prod_text,
                                  variable=v_mode, value="production",
                                  bg=BG3, fg=TEXT_DIM,
                                  selectcolor=BG2, activebackground=BG3,
                                  font=("Segoe UI",9),
                                  state="disabled")
        rb_prod.pack(anchor="w", pady=2)

        # ── Buttons ────────────────────────────────────────────────────────────

        def on_run():
            mode = v_mode.get()
            outfolder = out_var.get().strip()
            temp_folder = tmp_var.get().strip()
            if not temp_folder:
                messagebox.showerror("Temp folder required",
                    "Please specify a Temp folder — the manifest and job script are written there.", parent=dlg); return
            if mode == 'production' and not outfolder:
                messagebox.showerror("Output folder required",
                    "Please specify an output folder for Production mode.", parent=dlg); return
            if not v_delete.get() and not v_copyonly.get():
                if not any([v_autorotate.get(), v_aspectratio.get(), v_bw.get(),
                            v_fitwidth.get(), v_scale.get(), v_optimise.get(), v_strip.get()]):
                    messagebox.showerror("No operations selected",
                        "Please select at least one operation.", parent=dlg); return
            if v_delete.get():
                if not messagebox.askyesno("Confirm delete",
                    f"This will delete {len(self.tagged)} files.\n\nAre you sure?",
                    parent=dlg): return

            global PROC_TEMP_FOLDER, PROC_MGEN_EXE
            PROC_TEMP_FOLDER = tmp_var.get().strip()
            PROC_MGEN_EXE    = exe_var.get().strip()
            self._save_proc_config()

            ops = {
                'autorotate':  v_autorotate.get(),
                'aspectratio': v_aspectratio.get(),
                'ar_w':        ar_w_var.get(),
                'ar_h':        ar_h_var.get(),
                'bw':          v_bw.get(),
                'fitwidth':    v_fitwidth.get(),
                'fw_pixels':   fw_var.get(),
                'optimise':    v_optimise.get(),
                'opt_quality': opt_var.get(),
                'strip':       v_strip.get(),
                'scale':       v_scale.get(),
                'scale_pct':   scale_var.get(),
                'copyonly':    v_copyonly.get(),
                'delete':      v_delete.get(),
                'debug':       False,
                'outfolder':   outfolder,
                'temp_folder': tmp_var.get().strip(),
                'mode':        mode,
            }
            dlg.destroy()
            self._run_process_job(ops)

        tk.Button(bf, text="  Run  ", bg="#1a5276", fg="white",
                  font=("Segoe UI",11,"bold"), relief="flat", padx=20, pady=6,
                  cursor="hand2", command=on_run).pack(side="left", padx=8)
        tk.Button(bf, text="  Cancel  ", bg="#7b241c", fg="white",
                  font=("Segoe UI",11,"bold"), relief="flat", padx=16, pady=6,
                  cursor="hand2", command=dlg.destroy).pack(side="left", padx=8)
        return dlg


    def _save_proc_config(self):
        """Save processing config — updates only [processing] section, preserves rest of ini."""
        ini = os.path.join(os.path.dirname(os.path.abspath(__file__)), "FileTagger.ini")
        try:
            # Read raw lines to preserve multi-root entries
            if os.path.exists(ini):
                with open(ini, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
            else:
                lines = []

            # Remove existing [processing] section entirely
            new_lines = []
            in_proc = False
            for line in lines:
                if line.strip().lower() == '[processing]':
                    in_proc = True
                    continue
                if in_proc and line.strip().startswith('['):
                    in_proc = False
                if not in_proc:
                    new_lines.append(line)

            # Append fresh [processing] section
            if new_lines and new_lines[-1].strip():
                new_lines.append('\n')
            new_lines.append('[processing]\n')
            if PROC_TEMP_FOLDER:
                new_lines.append(f'temp_folder = {PROC_TEMP_FOLDER}\n')
            if PROC_MGEN_EXE:
                new_lines.append(f'mgen_exe    = {PROC_MGEN_EXE}\n')

            with open(ini, 'w', encoding='utf-8') as f:
                f.writelines(new_lines)
        except Exception as e:
            print(f"Could not save proc config: {e}")

    def _write_manifest(self, temp_folder):
        """Write FTMgen_request.csv from current tagged collection."""
        os.makedirs(temp_folder, exist_ok=True)
        path = os.path.join(self._ftediti_ipc_dir(), "FTMgen_request.csv")
        with open(path, "w", encoding="utf-8") as f:
            f.write("FILEPATH,TAGGED_DATE,FILESIZE\n")
            for fp in (list(self.tagged_order) if self.tagged_order else sorted(self.tagged)):
                fp_win = fp.replace("/", "\\")
                ts = self.tagged_at.get(fp, "")
                try:    sz = str(os.path.getsize(fp))
                except: sz = "0"
                f.write(f'"{fp_win}","{ts}",{sz}\n')
        return path

    def _write_job_script(self, ops, manifest_path, temp_folder):
        """Write ft_job.msf to temp folder."""
        job_path    = os.path.join(temp_folder, "FT_job.msf")
        status_path = os.path.join(temp_folder, "ft_status.txt")

        def win(p): return p.replace("/", "\\")

        lines = []
        lines.append(f'manifest   = "{win(manifest_path)}"')
        lines.append(f'outfolder  = "{win(ops["outfolder"])}"')
        lines.append(f'statusfile = "{win(status_path)}"')
        lines.append(f'debug      = {"sON" if ops["debug"] else "sOFF"}')
        lines.append("")
        # FT_FileOps.msf lives in FT_IPC\ alongside other FT IPC files
        # StringHelpers.msf lives under MGEN\SystemMacros\ (general, not FT-specific)
        ft_fileops  = os.path.join(self._ftediti_ipc_dir(), "FT_FileOps.msf")
        mgen_dir    = os.path.dirname(PROC_MGEN_EXE)
        str_helpers = os.path.join(mgen_dir, "SystemMacros", "StringHelpers.msf")
        lines.append(f'INCLUDE "{win(ft_fileops)}"')
        lines.append(f'INCLUDE "{win(str_helpers)}"')
        lines.append("")
        if ops['delete']:
            lines.append('DeleteFiles')
        elif ops['copyonly']:
            lines.append('CopyOnly')
        else:
            if ops['autorotate']:  lines.append('AutoRotate')
            if ops['aspectratio']: lines.append(f'AspectRatio "{ops["ar_w"]}x{ops["ar_h"]}"')
            if ops['bw']:          lines.append('MakeBW')
            if ops['fitwidth']:    lines.append(f'FitWidth "{ops["fw_pixels"]}"')
            if ops['optimise']:    lines.append(f'Optimise "{ops["opt_quality"]}"')
            if ops['strip']:       lines.append('Strip')
            if ops['scale']:       lines.append(f'Scale "{ops["scale_pct"]}"')
        lines.append("")
        lines.append("RunAll")

        with open(job_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return job_path, status_path

    def _run_process_job(self, ops):
        """Write manifest and job script, then branch on test vs production mode."""
        import subprocess
        temp_folder = ops.get('temp_folder', PROC_TEMP_FOLDER).strip()
        if not temp_folder:
            messagebox.showerror("Temp folder missing", "No temp folder specified.", parent=self.win); return
        try:
            os.makedirs(temp_folder, exist_ok=True)
        except Exception as e:
            messagebox.showerror("Cannot create temp folder",
                f"Failed to create:\n{temp_folder}\n\n{e}", parent=self.win); return

        manifest_path = self._write_manifest(temp_folder)
        job_path, status_path = self._write_job_script(ops, manifest_path, temp_folder)

        if ops.get('mode') == 'test':
            # Close any stale progress window from a previous run
            pw = getattr(self, '_proc_progress_win', None)
            if pw:
                try:
                    if pw.winfo_exists(): pw.destroy()
                except: pass
                self._proc_progress_win = None
            self._process_test_window(manifest_path, job_path, temp_folder)
        else:
            try:
                if os.path.exists(status_path): os.remove(status_path)
            except: pass
            here    = os.path.dirname(os.path.abspath(__file__))
            mgen_py = PROC_MGEN_EXE if os.path.exists(PROC_MGEN_EXE) else os.path.join(here, "mgen.py")
            try:
                proc = subprocess.Popen(
                    [sys.executable, mgen_py, job_path],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, encoding="utf-8", errors="replace")
            except Exception as e:
                messagebox.showerror("Failed to launch MGEN", str(e), parent=self.win); return
            self._process_progress_window(proc, status_path, ops)

    def _process_test_window(self, manifest_path, job_path, temp_folder):
        """Test mode — display generated manifest and job script for inspection."""
        pw = tk.Toplevel(self.win)
        pw.title("Process — Test Mode")
        pw.configure(bg="white")
        pw.resizable(True, True)
        pw.grab_set(); pw.transient(self.win)
        self._centre_window(pw, 680, 560)

        tk.Label(pw, text="⚙  Test Mode — Files Written (MGEN not launched)",
                 bg="white", fg="#1a5276", font=("Segoe UI",12,"bold")).pack(pady=(14,4))

        # Summary
        summary_frame = tk.Frame(pw, bg="#ddeeff", padx=12, pady=8)
        summary_frame.pack(fill="x", padx=16, pady=(0,8))
        tk.Label(summary_frame, text="Files written to:", bg="#ddeeff", fg="#000000",
                 font=("Segoe UI",9,"bold")).grid(row=0, column=0, sticky="w", pady=1)
        tk.Label(summary_frame, text=temp_folder, bg="#ddeeff", fg="#000000",
                 font=("Courier New",9)).grid(row=0, column=1, sticky="w", padx=(8,0), pady=1)
        tk.Label(summary_frame, text="Manifest:", bg="#ddeeff", fg="#000000",
                 font=("Segoe UI",9,"bold")).grid(row=1, column=0, sticky="w", pady=1)
        tk.Label(summary_frame, text=os.path.basename(manifest_path), bg="#ddeeff", fg="#000000",
                 font=("Courier New",9)).grid(row=1, column=1, sticky="w", padx=(8,0), pady=1)
        tk.Label(summary_frame, text="Job script:", bg="#ddeeff", fg="#000000",
                 font=("Segoe UI",9,"bold")).grid(row=2, column=0, sticky="w", pady=1)
        tk.Label(summary_frame, text=os.path.basename(job_path), bg="#ddeeff", fg="#000000",
                 font=("Courier New",9)).grid(row=2, column=1, sticky="w", padx=(8,0), pady=1)
        tk.Label(summary_frame, text=f"{len(self.tagged)} files in manifest",
                 bg="#ddeeff", fg="#333333",
                 font=("Segoe UI",8,"italic")).grid(row=3, column=0, columnspan=2, sticky="w", pady=(4,0))

        tk.Label(pw, text="Click a tab below to inspect the file contents:",
                 bg="white", fg="#333333", font=("Segoe UI",9,"italic")).pack(pady=(0,4))

        # Tab buttons
        tab_frame = tk.Frame(pw, bg="white"); tab_frame.pack(fill="x", padx=16, pady=(0,4))

        content_frame = tk.Frame(pw, bg="white")
        content_frame.pack(fill="both", expand=True, padx=16)

        txt = tk.Text(content_frame, bg="white", fg="black",
                      font=("Courier New",9), relief="solid", bd=1,
                      state="disabled", wrap="none")
        sb_v = tk.Scrollbar(content_frame, command=txt.yview)
        sb_h = tk.Scrollbar(content_frame, orient="horizontal", command=txt.xview)
        txt.configure(yscrollcommand=sb_v.set, xscrollcommand=sb_h.set)
        sb_v.pack(side="right", fill="y")
        sb_h.pack(side="bottom", fill="x")
        txt.pack(fill="both", expand=True)

        def show_file(path):
            txt.configure(state="normal")
            txt.delete("1.0", "end")
            try:
                with open(path, "r", encoding="utf-8") as f:
                    txt.insert("end", f.read())
            except Exception as e:
                txt.insert("end", f"Error reading file: {e}")
            txt.configure(state="disabled")

        tab_btns = {}
        def make_tab(label, path):
            def _click():
                show_file(path)
                for t, b in tab_btns.items():
                    b.config(bg="#1a5276" if t == label else "#aaaaaa", fg="white")
            b = tk.Button(tab_frame, text=label, bg="#aaaaaa", fg="white",
                          font=("Segoe UI",9,"bold"), relief="flat",
                          padx=10, pady=4, cursor="hand2", command=_click)
            b.pack(side="left", padx=(0,2))
            tab_btns[label] = b
            return _click

        click_manifest = make_tab("FT_manifest.csv", manifest_path)
        click_job      = make_tab("FT_job.msf",      job_path)
        click_manifest()

        bf = tk.Frame(pw, bg="white"); bf.pack(pady=8)

        def open_folder():
            try:
                if os.name == 'nt':
                    os.startfile(temp_folder)
                else:
                    import subprocess as _sp
                    _sp.Popen(['open' if sys.platform=='darwin' else 'xdg-open', temp_folder])
            except Exception as e:
                messagebox.showerror("Cannot open folder", str(e), parent=pw)

        tk.Button(bf, text="📁  Open Folder", bg="#1a5276", fg="white",
                  font=("Segoe UI",9,"bold"), relief="flat", padx=10, pady=4,
                  cursor="hand2", command=open_folder).pack(side="left", padx=6)
        tk.Button(bf, text="  Close  ", bg="#7b241c", fg="white",
                  font=("Segoe UI",9,"bold"), relief="flat", padx=10, pady=4,
                  cursor="hand2", command=pw.destroy).pack(side="left", padx=6)
        """Test mode — display generated manifest and job script for inspection."""
        pw = tk.Toplevel(self.win)
        pw.title("Process — Test Mode")
        pw.configure(bg=BG3)
        pw.resizable(True, True)
        pw.grab_set(); pw.transient(self.win)
        self._centre_window(pw, 680, 520)

        tk.Label(pw, text="⚙  Test Mode — Files Written (MGEN not launched)",
                 bg=BG3, fg=AMBER, font=("Segoe UI",12,"bold")).pack(pady=(14,4))

        # Summary of what was written and where
        summary_frame = tk.Frame(pw, bg="#1a2a1a", padx=12, pady=8)
        summary_frame.pack(fill="x", padx=16, pady=(0,8))
        tk.Label(summary_frame, text="Files written to:", bg="#1a2a1a", fg="#88cc88",
                 font=("Segoe UI",9,"bold")).grid(row=0, column=0, sticky="w", pady=1)
        tk.Label(summary_frame, text=temp_folder, bg="#1a2a1a", fg="#ccffcc",
                 font=("Courier New",9)).grid(row=0, column=1, sticky="w", padx=(8,0), pady=1)
        tk.Label(summary_frame, text="Manifest:", bg="#1a2a1a", fg="#88cc88",
                 font=("Segoe UI",9,"bold")).grid(row=1, column=0, sticky="w", pady=1)
        tk.Label(summary_frame, text=os.path.basename(manifest_path), bg="#1a2a1a", fg="#ccffcc",
                 font=("Courier New",9)).grid(row=1, column=1, sticky="w", padx=(8,0), pady=1)
        tk.Label(summary_frame, text="Job script:", bg="#1a2a1a", fg="#88cc88",
                 font=("Segoe UI",9,"bold")).grid(row=2, column=0, sticky="w", pady=1)
        tk.Label(summary_frame, text=os.path.basename(job_path), bg="#1a2a1a", fg="#ccffcc",
                 font=("Courier New",9)).grid(row=2, column=1, sticky="w", padx=(8,0), pady=1)
        tk.Label(summary_frame, text=f"{len(self.tagged)} files in manifest",
                 bg="#1a2a1a", fg="#aaaaaa",
                 font=("Segoe UI",8,"italic")).grid(row=3, column=0, columnspan=2, sticky="w", pady=(4,0))

        tk.Label(pw, text="Click a tab below to inspect the file contents:",
                 bg=BG3, fg=TEXT_DIM, font=("Segoe UI",9,"italic")).pack(pady=(0,4))

        # Tab buttons
        tab_frame = tk.Frame(pw, bg=BG3); tab_frame.pack(fill="x", padx=16, pady=(0,4))

        content_frame = tk.Frame(pw, bg=BG3)
        content_frame.pack(fill="both", expand=True, padx=16)

        txt = tk.Text(content_frame, bg="#111", fg="#cccccc",
                      font=("Courier New",9,"bold"), relief="flat",
                      state="disabled", wrap="none")
        sb_v = tk.Scrollbar(content_frame, command=txt.yview)
        sb_h = tk.Scrollbar(content_frame, orient="horizontal", command=txt.xview)
        txt.configure(yscrollcommand=sb_v.set, xscrollcommand=sb_h.set)
        sb_v.pack(side="right", fill="y")
        sb_h.pack(side="bottom", fill="x")
        txt.pack(fill="both", expand=True)

        def show_file(path):
            txt.configure(state="normal")
            txt.delete("1.0", "end")
            try:
                with open(path, "r", encoding="utf-8") as f:
                    txt.insert("end", f.read())
            except Exception as e:
                txt.insert("end", f"Error reading file: {e}")
            txt.configure(state="disabled")

        tab_btns = {}
        def make_tab(label, path):
            def _click():
                show_file(path)
                for t, b in tab_btns.items():
                    b.config(bg="#1a5276" if t == label else "#333344")
            b = tk.Button(tab_frame, text=label, bg="#333344", fg=TEXT_BRIGHT,
                          font=("Segoe UI",9,"bold"), relief="flat",
                          padx=10, pady=4, cursor="hand2", command=_click)
            b.pack(side="left", padx=(0,2))
            tab_btns[label] = b
            return _click

        click_manifest = make_tab("FT_manifest.csv", manifest_path)
        click_job      = make_tab("FT_job.msf",      job_path)
        click_manifest()

        bf = tk.Frame(pw, bg=BG3); bf.pack(pady=8)

        def open_folder():
            try:
                if os.name == 'nt':
                    os.startfile(temp_folder)
                else:
                    import subprocess as _sp
                    _sp.Popen(['open' if sys.platform=='darwin' else 'xdg-open', temp_folder])
            except Exception as e:
                messagebox.showerror("Cannot open folder", str(e), parent=pw)

        tk.Button(bf, text="📁  Open Temp Folder", bg="#335577", fg="white",
                  font=("Segoe UI",9,"bold"), relief="flat", padx=10,
                  cursor="hand2", command=open_folder).pack(side="left", padx=6)
        tk.Button(bf, text="  Close  ", bg="#444", fg=TEXT_BRIGHT,
                  font=("Segoe UI",9), relief="flat", padx=10,
                  cursor="hand2", command=pw.destroy).pack(side="left", padx=6)

    def _process_progress_window(self, proc, status_path, ops):
        """Modal progress window polling ft_status.txt."""
        total_files = len(self.tagged)
        debug_mode  = ops.get('debug', False)

        pw = tk.Toplevel(self.win)
        pw.title("Processing — MGEN")
        pw.configure(bg=BG3)
        pw.resizable(True, True)
        pw.grab_set(); pw.transient(self.win)
        pw.protocol("WM_DELETE_WINDOW", lambda: None)
        self._centre_window(pw, 620, 420)

        tk.Label(pw, text="⚙  Processing with MGEN" + ("  [DEBUG — no files written]" if debug_mode else ""),
                 bg=BG3, fg=AMBER if debug_mode else TEXT_BRIGHT,
                 font=("Segoe UI",12,"bold")).pack(pady=(14,4))

        lbl_cur = tk.Label(pw, text="Starting...", bg=BG3, fg=TEXT_DIM,
                           font=("Segoe UI",9), wraplength=580)
        lbl_cur.pack(pady=2, padx=16)

        lbl_counts = tk.Label(pw, text=f"0 / {total_files}", bg=BG3,
                              fg=ACCENT, font=("Segoe UI",10,"bold"))
        lbl_counts.pack(pady=4)

        bar_outer = tk.Frame(pw, bg="#333", height=18); bar_outer.pack(fill="x", padx=24, pady=4)
        bar_outer.pack_propagate(False)
        bar_fill = tk.Label(bar_outer, bg="#1a5276" if not debug_mode else AMBER, height=18)
        bar_fill.place(x=0, y=0, relheight=1.0, width=0)

        # Scrollable log
        log_frame = tk.Frame(pw, bg=BG3); log_frame.pack(fill="both", expand=True, padx=16, pady=4)
        log_sb = tk.Scrollbar(log_frame); log_sb.pack(side="right", fill="y")
        log_box = tk.Text(log_frame, bg="white", fg="black", font=("Segoe UI", 10),
                          yscrollcommand=log_sb.set, state="disabled", height=10,
                          relief="solid", bd=1, wrap="word")
        log_sb.config(command=log_box.yview)
        log_box.pack(fill="both", expand=True)

        def _log(text, tag=None):
            log_box.configure(state="normal")
            log_box.insert("end", text + "\n")
            log_box.see("end")
            log_box.configure(state="disabled")

        log_box.tag_configure("error",   foreground="#cc0000", font=("Segoe UI", 10, "bold"))
        log_box.tag_configure("success", foreground="#226622", font=("Segoe UI", 10, "bold"))
        log_box.tag_configure("debug",   foreground="#996600")

        bf = tk.Frame(pw, bg=BG3); bf.pack(pady=8)
        cancelled = [False]

        def on_cancel():
            cancelled[0] = True
            try: proc.terminate()
            except: pass
            btn_cancel.config(text="Cancelling...", state="disabled")

        btn_cancel = tk.Button(bf, text="  Cancel  ", bg="#cc3333", fg="white",
                               font=("Segoe UI", 9, "bold"), relief="flat", padx=12,
                               cursor="hand2", activebackground="#aa2222",
                               activeforeground="white", command=on_cancel)
        btn_cancel.pack(side="left", padx=6)
        btn_close = tk.Button(bf, text="  Close  ", bg="#336699", fg="white",
                              font=("Segoe UI", 9, "bold"), relief="flat", padx=12,
                              cursor="hand2", activebackground="#225588",
                              activeforeground="white", state="disabled",
                              command=pw.destroy)
        btn_close.pack(side="left", padx=6)

        # Read stdout in background thread, append to log
        import threading
        def _read_output():
            for line in proc.stdout:
                line = line.rstrip()
                if not line: continue
                lu = line.upper()
                if "ERROR" in lu or "FAILED" in lu:
                    pw.after(0, lambda l=line: _log(l, "error"))
                elif "DEBUG" in lu:
                    pw.after(0, lambda l=line: _log(l, "debug"))
                else:
                    pw.after(0, lambda l=line: _log(l))
            proc.wait()
        threading.Thread(target=_read_output, daemon=True).start()

        # Poll status file
        def _poll():
            if not pw.winfo_exists(): return
            try:
                if os.path.exists(status_path):
                    vals = {}
                    with open(status_path, "r", encoding="utf-8") as f:
                        for line in f:
                            if "=" in line:
                                k, _, v = line.strip().partition("=")
                                vals[k.strip()] = v.strip()
                    status   = vals.get("STATUS", "Running")
                    done     = int(vals.get("PROCESSED", 0))
                    total    = int(vals.get("TOTAL", total_files)) or total_files
                    cur      = vals.get("CURRENT", "")
                    message  = vals.get("MESSAGE", "")
                    pct = done / total if total else 0
                    try:
                        bar_fill.place(x=0, y=0, relheight=1.0,
                                       width=int((bar_outer.winfo_width() or 570) * pct))
                        lbl_counts.config(text=f"{done} / {total}")
                        if cur: lbl_cur.config(text=cur)
                    except: pass

                    if status in ("Complete", "Error", "Cancelled"):
                        try:
                            bar_fill.place(x=0, y=0, relheight=1.0,
                                           width=bar_outer.winfo_width() if status=="Complete" else 0)
                            btn_cancel.config(state="disabled")
                            btn_close.config(state="normal")
                            pw.protocol("WM_DELETE_WINDOW", pw.destroy)
                            if status == "Complete":
                                lbl_cur.config(text="✓  Complete")
                                lbl_counts.config(text=f"{done} / {total}  —  Done")
                            else:
                                lbl_cur.config(text=f"✗  {status}")
                        except: pass
                        return  # stop polling

                # Check if process died without writing complete status
                if proc.poll() is not None and not cancelled[0]:
                    try:
                        btn_cancel.config(state="disabled")
                        btn_close.config(state="normal")
                        pw.protocol("WM_DELETE_WINDOW", pw.destroy)
                    except: pass
                    return

            except Exception as e:
                _log(f"Poll error: {e}")

            pw.after(1000, _poll)

        pw.after(500, _poll)

    def _gen_cache_dialog(self):
        import tkinter.filedialog as fd
        mode=self.mode; mode_cfg=self.mode_cfg
        dlg=tk.Toplevel(self.win); dlg.title("Generate Thumbnail Cache")
        dlg.configure(bg=BG3); dlg.grab_set(); dlg.transient(self.win)
        self._centre_window(dlg,500,360)
        tk.Label(dlg,text=f"Generate {mode_cfg['label']} Thumbnail Cache",bg=BG3,fg=TEXT_BRIGHT,
                 font=("Segoe UI",12,"bold")).pack(pady=(16,8))
        fr=tk.Frame(dlg,bg=BG3); fr.pack(fill="x",padx=20,pady=4)
        tk.Label(fr,text="Folder:",bg=BG3,fg=TEXT_DIM,font=("Segoe UI",9),width=8,anchor="w").pack(side="left")
        folder_var=tk.StringVar(value=self.current_folder)
        entry=tk.Entry(fr,textvariable=folder_var,bg=BG2,fg=TEXT_BRIGHT,font=("Segoe UI",9),
                       insertbackground=TEXT_BRIGHT,relief="flat",bd=1,highlightthickness=1,highlightbackground=HOVER_BD)
        entry.pack(side="left",fill="x",expand=True,padx=(0,6))
        def browse():
            f=fd.askdirectory(title="Select folder",initialdir=folder_var.get())
            if f: folder_var.set(os.path.normpath(f)); _refresh_count()
        tk.Button(fr,text="Choose Folder...",bg=BG2,fg=TEXT_BRIGHT,font=("Segoe UI",9),
                  relief="flat",padx=8,cursor="hand2",command=browse).pack(side="left")
        scope_var=tk.StringVar(value="tree")
        tk.Label(dlg,text="Include:",bg=BG3,fg=TEXT_DIM,font=("Segoe UI",9,"bold"),
                 anchor="w").pack(anchor="w",padx=28,pady=(8,2))
        rb_frame=tk.Frame(dlg,bg=BG3); rb_frame.pack(anchor="w",padx=28,pady=2)
        rb_refs={}
        def on_scope():
            for val,rb in rb_refs.items():
                rb.config(font=("Segoe UI",10,"bold") if scope_var.get()==val else ("Segoe UI",10),
                          fg=TEXT_BRIGHT if scope_var.get()==val else TEXT_DIM)
            self.win.after(10,_refresh_count)
        for row_i,(val,lbl) in enumerate([("folder","This folder only  (no subfolders)"),
                                           ("tree","Entire tree  (folder and all subfolders)")]):
            rb=tk.Radiobutton(rb_frame,text=lbl,variable=scope_var,value=val,
                              bg=BG3,selectcolor=BG2,activebackground=BG3,
                              activeforeground=TEXT_BRIGHT,
                              font=("Segoe UI",10,"bold") if val=="tree" else ("Segoe UI",10),
                              fg=TEXT_BRIGHT if val=="tree" else TEXT_DIM,
                              anchor="w",padx=4,pady=5,command=on_scope)
            rb.grid(row=row_i,column=0,sticky="w"); rb_refs[val]=rb
        ow_var=tk.BooleanVar(value=False)
        or_=tk.Frame(dlg,bg=BG3); or_.pack(fill="x",padx=20,pady=2)
        tk.Label(or_,text="",bg=BG3,width=8).pack(side="left")
        tk.Checkbutton(or_,text="Overwrite existing thumbnails",variable=ow_var,
                       bg=BG3,fg=TEXT_BRIGHT,selectcolor=BG2,activebackground=BG3,
                       font=("Segoe UI",9)).pack(side="left")
        count_var=tk.StringVar(value="")
        tk.Label(dlg,textvariable=count_var,bg=BG3,fg=ACCENT,font=("Segoe UI",9,"bold")).pack(pady=6)
        exts=mode_cfg['exts']
        def _refresh_count(*_):
            folder=os.path.normpath(folder_var.get())
            if not os.path.isdir(folder): count_var.set("Folder not found"); return
            count_var.set("Counting..."); dlg.update_idletasks()
            if scope_var.get()=="tree":
                n=sum(1 for r,d,files in os.walk(folder)
                      for f in files if os.path.splitext(f)[1].lower() in exts)
            else:
                try: n=sum(1 for e in os.scandir(folder)
                           if e.is_file() and os.path.splitext(e.name)[1].lower() in exts)
                except: n=0
            word=mode_cfg['file_word']; count_var.set(f"{n:,} {word} files found")
        folder_var.trace_add("write",lambda *_: self.win.after(200,_refresh_count))
        _refresh_count()
        bf=tk.Frame(dlg,bg=BG3); bf.pack(side="bottom",pady=14)
        def on_ok():
            folder=os.path.normpath(folder_var.get())
            if not os.path.isdir(folder):
                messagebox.showerror("Invalid folder",f"Not found:\n{folder}",parent=dlg); return
            dlg.destroy(); self._run_gen_cache(folder,scope_var.get(),ow_var.get())
        tk.Button(bf,text="  Generate  ",bg=GREEN,fg="white",font=("Segoe UI",10,"bold"),
                  relief="flat",padx=8,cursor="hand2",command=on_ok).pack(side="left",padx=8)
        tk.Button(bf,text="  Cancel  ",bg=BG2,fg=TEXT_BRIGHT,font=("Segoe UI",10),
                  relief="flat",padx=8,cursor="hand2",command=dlg.destroy).pack(side="left",padx=8)

    def _run_gen_cache(self, folder, scope, overwrite):
        mode=self.mode; exts=self.mode_cfg['exts']; word=self.mode_cfg['file_word']
        self._status("Counting files..."); self.win.update_idletasks()
        if scope=="tree":
            files=sorted([os.path.join(r,f) for r,d,fs in os.walk(folder)
                          for f in fs if os.path.splitext(f)[1].lower() in exts],
                         key=lambda p:(os.path.dirname(p).lower(),os.path.basename(p).lower()))
        else:
            try: files=sorted([e.path for e in os.scandir(folder)
                               if e.is_file() and os.path.splitext(e.name)[1].lower() in exts],
                              key=lambda p: p.lower())
            except: files=[]
        total=len(files)
        if total==0: messagebox.showinfo(f"No {word}s",f"No {word} files found.", parent=self.win); return
        root=self.mode_cfg['root']
        pw=tk.Toplevel(self.win); pw.title("Generating Thumbnail Cache")
        pw.configure(bg=BG3); self._centre_window(pw,600,240)
        pw.resizable(False,False); pw.grab_set(); pw.transient(self.win)
        tk.Label(pw,text=f"Generating {self.mode_cfg['label']} Thumbnail Cache",
                 bg=BG3,fg=TEXT_BRIGHT,font=("Segoe UI",12,"bold")).pack(pady=(14,4))
        lbl_file=tk.Label(pw,text="",bg=BG3,fg=TEXT_DIM,font=("Segoe UI",8),wraplength=560)
        lbl_file.pack(pady=2,padx=16)
        lbl_counts=tk.Label(pw,text=f"0 / {total:,}",bg=BG3,fg=ACCENT,font=("Segoe UI",10,"bold"))
        lbl_counts.pack(pady=4)
        bar_outer=tk.Frame(pw,bg="#333",height=20); bar_outer.pack(fill="x",padx=24,pady=4)
        bar_outer.pack_propagate(False)
        bar_fill=tk.Label(bar_outer,bg=GREEN,height=20)
        bar_fill.place(x=0,y=0,relheight=1.0,width=0)
        cancel_flag=[False]; error_list=[]; skipped_list=[]
        def update_ui(done_n,gen_n,sk_n,err_n,cur_folder,cur_file):
            try:
                pct=done_n/total if total else 0
                bar_fill.place(x=0,y=0,relheight=1.0,width=int((bar_outer.winfo_width() or 550)*pct))
                lbl_file.config(text=f"{cur_folder}   ›   {cur_file}")
                lbl_counts.config(text=f"{done_n:,} / {total:,}   —   {gen_n} generated,  {sk_n} skipped,  {err_n} errors")
                pw.update_idletasks()
            except: pass
        def on_cancel():
            cancel_flag[0]=True
            try: btn_cancel.config(text="Cancelling...",state="disabled")
            except: pass
        btn_cancel=tk.Button(pw,text="  Cancel  ",bg="#555",fg=TEXT_BRIGHT,font=("Segoe UI",9),
                             relief="flat",padx=8,cursor="hand2",command=on_cancel)
        btn_cancel.pack(pady=6); pw.protocol("WM_DELETE_WINDOW",on_cancel)
        def worker():
            generated=0; skipped=0; last_folder=""
            # Group files by folder for efficient blob writing
            from itertools import groupby
            folder_groups = {}
            for orig in files:
                folder_groups.setdefault(os.path.dirname(orig), []).append(orig)

            for folder, folder_files in folder_groups.items():
                if cancel_flag[0]: break

                # ── Folder-level skip — if blob exists and not overwriting,
                # skip the entire folder without reading individual entries ──────
                if not overwrite:
                    if _db_conn is not None:
                        row = _db_conn.execute(
                            "SELECT 1 FROM thumbnails WHERE path LIKE ? LIMIT 1",
                            (os.path.normpath(folder) + '%',)
                        ).fetchone()
                        folder_cached = (row is not None)
                    else:
                        folder_cached = False
                    if folder_cached:
                        n = len(folder_files)
                        skipped += n
                        for orig in folder_files: skipped_list.append(orig)
                        done = generated + skipped
                        self.win.after(0, update_ui, done, generated, skipped,
                                       len(error_list),
                                       os.path.basename(folder), f"skipped ({n} already done)")
                        continue

                pending_items = []   # (orig, jpeg_bytes) to write to blob

                for orig in folder_files:
                    if cancel_flag[0]: break
                    cur_folder = folder

                    already = thumb_get(orig) if not overwrite else None
                    if already is not None:
                        skipped += 1
                        skipped_list.append(orig)
                    else:
                        try:
                            ext = os.path.splitext(orig)[1].lower()
                            norm = orig.replace('/', '\\')
                            if ext == '.pdf':
                                if not HAVE_FITZ:
                                    error_list.append((os.path.basename(orig), "PyMuPDF not installed"))
                                else:
                                    doc = fitz.open(_longpath(norm))
                                    pg = doc[0]
                                    mat = fitz.Matrix(THUMB_SIZE / max(pg.rect.width, pg.rect.height),
                                                      THUMB_SIZE / max(pg.rect.width, pg.rect.height))
                                    pix = pg.get_pixmap(matrix=mat, alpha=False)
                                    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                                    doc.close()
                                    buf = _io.BytesIO()
                                    img.save(buf, 'JPEG', quality=82, optimize=True)
                                    pending_items.append((orig, buf.getvalue()))
                                    generated += 1
                            else:
                                from PIL import ImageOps as _IOS2
                                img = Image.open(_longpath(norm))
                                img = _IOS2.exif_transpose(img)
                                img.thumbnail((THUMB_STORE_SIZE, THUMB_STORE_SIZE), Image.BILINEAR)
                                if img.mode != 'RGB': img = img.convert('RGB')
                                buf = _io.BytesIO()
                                img.save(buf, 'JPEG', quality=82, optimize=True)
                                pending_items.append((orig, buf.getvalue()))
                                generated += 1
                        except Exception as ex:
                            error_list.append((os.path.basename(orig), str(ex)))

                    done = generated + skipped + len(error_list)
                    if cur_folder != last_folder or done % 5 == 0:
                        last_folder = cur_folder
                        self.win.after(0, update_ui, done, generated, skipped, len(error_list),
                                       cur_folder, os.path.basename(orig))

                # Write entire folder's thumbnails to blob in one shot
                if pending_items:
                    try:
                        thumb_put_many(pending_items)
                    except Exception as ex:
                        error_list.append((os.path.basename(folder), f"Blob write error: {ex}"))

            self.win.after(0, finish, generated, skipped, "Cancelled" if cancel_flag[0] else "Complete")
        def finish(gen,skipped,status):
            try: pw.grab_release(); pw.destroy()
            except: pass
            n_err=len(error_list)
            self._status(f"Thumbnails {status.lower()}: {gen} generated, {skipped} skipped, {n_err} errors")
            rdlg=tk.Toplevel(self.win); rdlg.title("Generate Thumbnails — "+status)
            rdlg.configure(bg=BG3); rdlg.transient(self.win); rdlg.grab_set()
            self._centre_window(rdlg,440,240)
            tk.Label(rdlg,text=f"Status:     {status}",bg=BG3,fg=TEXT_BRIGHT,
                     font=("Segoe UI",11,"bold")).pack(pady=(16,4))
            tk.Label(rdlg,text=f"Generated:  {gen:,}",bg=BG3,fg=TEXT_BRIGHT,font=("Segoe UI",10)).pack()
            tk.Label(rdlg,text=f"Skipped:    {skipped:,}  (thumbnail already exists — use Overwrite to regenerate)",
                     bg=BG3,fg=TEXT_DIM,font=("Segoe UI",9)).pack()
            tk.Label(rdlg,text=f"Errors:     {n_err}",bg=BG3,
                     fg="#cc2222" if n_err else TEXT_BRIGHT,
                     font=("Segoe UI",10,"bold" if n_err else "normal")).pack(pady=(2,8))
            bf2=tk.Frame(rdlg,bg=BG3); bf2.pack(pady=4)

            def show_list(title, items, colour):
                ed=tk.Toplevel(rdlg); ed.title(title)
                ed.configure(bg=BG3); self._centre_window(ed,700,420)
                tk.Label(ed,text=title,bg=BG3,fg=colour,
                         font=("Segoe UI",10,"bold")).pack(pady=(12,4),padx=16,anchor="w")
                fr2=tk.Frame(ed,bg=BG3); fr2.pack(fill="both",expand=True,padx=16,pady=4)
                sb2=tk.Scrollbar(fr2); sb2.pack(side="right",fill="y")
                lb=tk.Listbox(fr2,bg=BG2,fg=TEXT_BRIGHT,font=("Courier New",8),
                              yscrollcommand=sb2.set,selectmode="browse",relief="flat")
                sb2.config(command=lb.yview); lb.pack(fill="both",expand=True)
                for item in items:
                    if isinstance(item, tuple):
                        lb.insert("end", item[0])
                        lb.insert("end", f"  → {item[1]}")
                    else:
                        lb.insert("end", item)
                tk.Button(ed,text="  Close  ",bg=BG2,fg=TEXT_BRIGHT,font=("Segoe UI",9),
                          relief="flat",command=ed.destroy).pack(pady=8)

            if skipped_list:
                tk.Button(bf2,text=f"  Show Skipped ({skipped:,})  ",bg=BG2,fg=TEXT_DIM,
                          font=("Segoe UI",9),relief="flat",padx=8,cursor="hand2",
                          command=lambda: show_list(f"{skipped:,} Skipped — already have thumbnails",
                                                    skipped_list, TEXT_DIM)
                          ).pack(side="left",padx=4)
            if n_err:
                tk.Button(bf2,text=f"  Show Errors ({n_err})  ",bg="#cc2222",fg="white",
                          font=("Segoe UI",10,"bold"),relief="flat",padx=8,cursor="hand2",
                          command=lambda: show_list(f"{n_err} Errors", error_list, "#cc2222")
                          ).pack(side="left",padx=4)
            tk.Button(bf2,text="  Close  ",bg=BG2,fg=TEXT_BRIGHT,font=("Segoe UI",10),
                      relief="flat",padx=10,cursor="hand2",
                      command=rdlg.destroy).pack(side="left",padx=4)
            if gen>0: self._load_folder(self.current_folder)
            self._refresh_tree_stats()
        threading.Thread(target=worker,daemon=True).start()


    # ── Clean orphans ──────────────────────────────────────────────────────────
    def _clean_orphans_dialog(self):
        src_root = self.mode_cfg['root']
        exts=self.mode_cfg['exts']
        if not messagebox.askyesno("Clean Orphans",
            f"Scan the entire {self.mode_cfg['label']} source tree\n"
            "and remove thumbnails whose originals no longer exist?\n\n"
            "Also removes paths from all collection files.", parent=self.win): return
        pw=tk.Toplevel(self.win); pw.title("Cleaning Orphan Thumbnails")
        pw.configure(bg=BG3); self._centre_window(pw,580,220)
        pw.resizable(False,False); pw.grab_set(); pw.transient(self.win)
        pw.protocol("WM_DELETE_WINDOW",lambda:None)
        tk.Label(pw,text="Scanning for orphan thumbnails...",bg=BG3,fg=TEXT_BRIGHT,
                 font=("Segoe UI",12,"bold")).pack(pady=(16,4))
        lbl_cur=tk.Label(pw,text="",bg=BG3,fg=TEXT_DIM,font=("Segoe UI",8),wraplength=540)
        lbl_cur.pack(pady=2,padx=16)
        lbl_counts=tk.Label(pw,text="Scanning...",bg=BG3,fg=ACCENT,font=("Segoe UI",10,"bold"))
        lbl_counts.pack(pady=4)
        bar_outer=tk.Frame(pw,bg="#333",height=16); bar_outer.pack(fill="x",padx=24,pady=4)
        bar_outer.pack_propagate(False)
        bar_fill=tk.Label(bar_outer,bg="#662266",height=16)
        bar_fill.place(x=0,y=0,relheight=1.0,width=0)
        def update_ui(scanned,n_orphans,cur):
            try:
                lbl_cur.config(text=cur)
                lbl_counts.config(text=f"Scanned {scanned:,}  —  {n_orphans} orphan{'s' if n_orphans!=1 else ''} found")
                pw.update_idletasks()
            except: pass
        def worker():
            scanned=0; orphans_found=[]
            # Scan all folders and run gc on each
            for dirpath, dirs, files in os.walk(src_root):
                dirs.sort()
                scanned += 1
                kept, removed = thumb_gc(dirpath)
                if removed:
                    orphans_found.append(f'{removed} orphan(s) removed from {os.path.basename(dirpath)}')
                if scanned % 5 == 0:
                    self.win.after(0, update_ui, scanned, len(orphans_found), os.path.basename(dirpath))
            self.win.after(0,finish_scan,scanned,orphans_found)
        def finish_scan(scanned,orphans):
            try: pw.grab_release(); pw.destroy()
            except: pass
            if not orphans:
                messagebox.showinfo("Clean Orphans",f"Scanned {scanned:,} folders.\n\nNo orphans found.", parent=self.win); return
            if not messagebox.askyesno("Clean Orphans",
                f"Scanned {scanned:,} folders.\n\n"
                f"Found {len(orphans):,} orphan entry group{'s' if len(orphans)!=1 else ''}.\n\n"
                "Remove from collections?", parent=self.win): return
            coll_updated=0
            if _db_conn is not None:
                try:
                    rows = _db_conn.execute("SELECT DISTINCT path FROM collection_items").fetchall()
                    dead = [r[0] for r in rows if not os.path.exists(r[0])]
                    if dead:
                        _db_conn.executemany("DELETE FROM collection_items WHERE path=?", [(p,) for p in dead])
                        _db_conn.commit()
                        coll_updated = len(dead)
                except Exception as e:
                    print(f"Orphan cleanup failed: {e}")
            if coll_updated and self.collection:
                data=_read_collection(self.collection,src_root)
                self.tagged=set(data.keys()); self.tagged_at=dict(data)
                self._update_coll_info()
            self._refresh_tree_stats(); self._load_folder(self.current_folder)
            msg=f"Orphan cleanup complete — {len(orphans)} blob(s) compacted."
            if coll_updated: msg+=f"\n{coll_updated} collection file(s) updated."
            messagebox.showinfo("Clean Orphans Complete",msg, parent=self.win)
        threading.Thread(target=worker,daemon=True).start()

    # ── Export ─────────────────────────────────────────────────────────────────
    def _copy_move_collection(self):
        """Copy/Move from the Output toolbar — operates on the full tagged collection."""
        if not self.tagged:
            messagebox.showinfo("Empty collection",
                f"No files tagged in '{self.collection}'.\nTag some files first.",
                parent=self.win); return
        files = list(self.tagged_order) if self.tagged_order else sorted(self.tagged)
        self._copy_move_dialog(files=files)

    def _copy_move_dialog(self, files=None):
        """Twin-panel copy/move.
        Left = source folder files to choose from.
        Centre = destination folder tree.
        Right = queue of files to copy/move (built by user selecting left and clicking Move to Right).
        Mirrors the Sort window interaction pattern exactly.
        files param pre-selects those paths on open."""

        if getattr(self, '_copy_move_win', None):
            try:
                if self._copy_move_win.winfo_exists():
                    self._copy_move_win.lift(); return
            except: pass

        root = self.mode_cfg['root']
        exts = self.mode_cfg['exts']
        sz   = min(self._disp_size, 120)

        # ── Window ────────────────────────────────────────────────────────
        rw = tk.Toplevel(self.win)
        rw.title("Copy / Move")
        rw.configure(bg=BG)
        rw.transient(self.win)
        self.win.update_idletasks()
        try:
            cx = self.canvas.winfo_rootx(); cy = self.canvas.winfo_rooty()
            cw = self.canvas.winfo_width(); ch = self.canvas.winfo_height()
        except:
            cx = self.win.winfo_rootx(); cy = self.win.winfo_rooty()
            cw = self.win.winfo_width() - 100; ch = self.win.winfo_height() - 60
        rw.geometry(f"{cw}x{ch}+{cx}+{cy}")
        rw.resizable(True, True)
        self._copy_move_win = rw

        def _on_close():
            self._copy_move_win = None
            try: rw.destroy()
            except: pass

        # ── State ─────────────────────────────────────────────────────────
        source_folder   = [self.current_folder]
        source_files    = [None]      # all files in current source folder
        dest_folder     = [None]      # selected destination folder
        queued          = []          # ordered list of files queued for copy/move
        queued_set      = set()       # fast membership
        selected        = set()       # selected in LEFT panel
        right_sel       = set()       # selected in RIGHT panel (to move back)
        last_click      = [None]      # left panel shift-click anchor (index into source_files)
        right_last_click= [None]
        photo_cache     = {}          # fpath -> PhotoImage, persists across folder changes
        load_gen        = [0]
        op_mode         = tk.StringVar(value="copy")
        add_suffix      = tk.BooleanVar(value=True)

        CELL_PAD = 4
        CELL_W   = sz + 8
        CELL_H   = sz + 20
        SEL_COL  = "#cc2222"
        NRM_COL  = UNTAGGED_BD

        # ── Cell builder (Canvas-based, same as Sort window) ───────────────
        def _make_cell(parent, fpath, sel=False, dimmed=False, seq=None):
            bd = SEL_COL if sel else ("#555555" if dimmed else NRM_COL)
            cv = tk.Canvas(parent, width=CELL_W, height=CELL_H,
                           bg=bd, highlightthickness=0,
                           cursor="arrow" if dimmed else "hand2")
            cv.create_rectangle(2, 2, CELL_W-2, CELL_H-2, fill=BG, outline="")
            photo = photo_cache.get(fpath)
            if photo:
                cv.create_image(CELL_W//2, 2 + sz//2, anchor="center", image=photo)
            else:
                cv.create_text(CELL_W//2, 2 + sz//2, anchor="center",
                               text=os.path.basename(fpath)[:16],
                               fill=TEXT_DIM, font=("Segoe UI", 8), width=sz-4)
            if dimmed:
                cv.create_rectangle(2, 2, CELL_W-2, CELL_H-2,
                                    fill="#000000", stipple="gray25", outline="")
                cv.create_text(CELL_W//2, 2 + sz//2, anchor="center",
                               text="QUEUED", fill="#ffee00",
                               font=("Segoe UI", 11, "bold"))
            if seq is not None:
                cv.create_rectangle(2, 2, 22, 16, fill="#000000", outline="")
                cv.create_text(4, 9, anchor="w", text=str(seq),
                               fill="white", font=("Segoe UI", 8, "bold"))
            cv.create_text(4, CELL_H-4, anchor="sw",
                           text=os.path.basename(fpath),
                           fill="#888888" if dimmed else TEXT_BRIGHT,
                           font=("Segoe UI", 8), width=CELL_W-8)
            return cv

        # ── Toolbar ───────────────────────────────────────────────────────
        tb = tk.Frame(rw, bg=BG2, pady=4); tb.pack(fill="x")

        src_lbl = tk.Label(tb, text="", bg=BG2, fg=TEXT_BRIGHT,
                           font=("Segoe UI", 10, "bold"))
        src_lbl.pack(side="left", padx=8)

        msg_lbl = tk.Label(tb, text="", bg=BG2, fg=TEXT_DIM,
                           font=("Segoe UI", 10, "italic"))
        msg_lbl.pack(side="left", padx=4)

        def _set_msg(t, colour=None):
            msg_lbl.config(text=t, fg=colour or TEXT_DIM)

        tb_right = tk.Frame(tb, bg=BG2); tb_right.pack(side="right", padx=4)

        def _do_execute():
            if not dest_folder[0]:
                _set_msg("Select a destination folder first", "#cc4444"); return
            if not queued:
                _set_msg("Move files to the right panel first", "#cc4444"); return
            import hashlib, shutil as _sh
            op  = op_mode.get()
            sfx = add_suffix.get()
            errors = []; done = 0
            for fp in list(queued):
                stem, ext = os.path.splitext(os.path.basename(fp))
                if sfx:
                    h = hashlib.md5(fp.encode()).hexdigest()[:4]
                    new_name = f"{stem}_{h}{ext}"
                else:
                    new_name = stem + ext
                dest_path = os.path.join(dest_folder[0], new_name)
                counter = 1
                while os.path.exists(_longpath(dest_path)):
                    new_name = f"{stem}_{h}_{counter}{ext}" if sfx else f"{stem}_{counter}{ext}"
                    dest_path = os.path.join(dest_folder[0], new_name)
                    counter += 1
                try:
                    if op == "copy":
                        _sh.copy2(_longpath(fp), _longpath(dest_path))
                    else:
                        import os as _os
                        _os.rename(_longpath(fp), _longpath(dest_path))
                        if fp in self.tagged:
                            self.tagged.discard(fp); self.tagged.add(dest_path)
                            if fp in self.tagged_at:
                                self.tagged_at[dest_path] = self.tagged_at.pop(fp)
                            if fp in self.tagged_order:
                                self.tagged_order[self.tagged_order.index(fp)] = dest_path
                        if fp in self._all_files:
                            self._all_files[self._all_files.index(fp)] = dest_path
                        try:
                            thumb = thumb_get(fp)
                            if thumb: thumb_put_many([(dest_path, thumb)])
                        except: pass
                    done += 1
                except Exception as e:
                    errors.append(f"{os.path.basename(fp)}: {e}")
            if op == "move":
                self._save_current_collection()
                self._schedule_tree_refresh()
            queued.clear(); queued_set.clear()
            selected.clear(); right_sel.clear()
            last_click[0] = None; right_last_click[0] = None
            _load_source(source_folder[0])
            if errors:
                _set_msg(f"✓ {done} {op}d, {len(errors)} errors", "#cc8800")
                messagebox.showerror("Errors", "\n".join(errors[:10]), parent=rw)
            else:
                _set_msg(f"✓ {done} files {op}d to {os.path.basename(dest_folder[0])}", "#44cc44")
            _update_status()

        tk.Button(tb_right, text="▶ Execute", bg="#1a5276", fg="white",
                  font=("Segoe UI", 10, "bold"), relief="flat", padx=10,
                  cursor="hand2", command=_do_execute).pack(side="left", padx=4)
        tk.Button(tb_right, text="✕ Close", bg=BG2, fg=TEXT_BRIGHT,
                  font=("Segoe UI", 10), relief="flat", padx=8,
                  cursor="hand2", command=_on_close).pack(side="left", padx=(0,12))
        tk.Frame(tb_right, bg=HOVER_BD, width=2).pack(side="left", fill="y", pady=4)
        tk.Checkbutton(tb_right, text="_xxxx suffix", variable=add_suffix,
                       bg=BG2, fg=TEXT_BRIGHT, selectcolor=BG,
                       activebackground=BG2, font=("Segoe UI", 10)).pack(side="left", padx=(8,4))
        tk.Label(tb_right, text="Op:", bg=BG2, fg=TEXT_BRIGHT,
                 font=("Segoe UI", 10)).pack(side="left", padx=(8,2))
        tk.Radiobutton(tb_right, text="Copy", variable=op_mode, value="copy",
                       bg=BG2, fg=TEXT_BRIGHT, selectcolor=BG,
                       activebackground=BG2, font=("Segoe UI", 10)).pack(side="left", padx=2)
        tk.Radiobutton(tb_right, text="Move", variable=op_mode, value="move",
                       bg=BG2, fg=TEXT_BRIGHT, selectcolor=BG,
                       activebackground=BG2, font=("Segoe UI", 10)).pack(side="left", padx=2)

        # ── Action bar ────────────────────────────────────────────────────
        act = tk.Frame(rw, bg="#1a2a3a", pady=5); act.pack(fill="x")
        centre = tk.Frame(act, bg="#1a2a3a"); centre.pack(anchor="center")

        status_lbl = tk.Label(centre, text="", bg="#1a2a3a", fg="#ffaaaa",
                              font=("Segoe UI", 9, "bold"))
        status_lbl.pack(side="left", padx=(0,16))

        move_right_btn = tk.Button(centre, text="▶  Move to Right",
                                   bg="#444444", fg="white",
                                   font=("Segoe UI", 9, "bold"), relief="flat",
                                   padx=12, pady=2, cursor="hand2", state="disabled")
        move_right_btn.pack(side="left", padx=(0,8))

        move_left_btn = tk.Button(centre, text="◀  Move Back",
                                  bg="#444444", fg="white",
                                  font=("Segoe UI", 9, "bold"), relief="flat",
                                  padx=12, pady=2, cursor="hand2", state="disabled")
        move_left_btn.pack(side="left", padx=(0,8))

        hint_lbl = tk.Label(centre, text="Click · Ctrl · Shift to select on left, then ▶ Move to Right",
                            bg="#1a2a3a", fg=TEXT_DIM, font=("Segoe UI", 8, "italic"))
        hint_lbl.pack(side="left", padx=8)

        def _do_move_right():
            if not selected: return
            sf = source_files[0] or []
            sel_ordered = [p for p in sf if p in selected and p not in queued_set]
            for p in sel_ordered:
                queued.append(p); queued_set.add(p)
            selected.clear(); last_click[0] = None
            _build_left(); _build_right(); _update_status()

        def _do_move_left():
            if not right_sel: return
            for p in list(right_sel):
                if p in queued: queued.remove(p)
                queued_set.discard(p)
            right_sel.clear(); right_last_click[0] = None
            _build_left(); _build_right(); _update_status()

        move_right_btn.config(command=_do_move_right)
        move_left_btn.config(command=_do_move_left)

        def _update_status():
            n_left  = len(selected)
            n_right = len(right_sel)
            parts = []
            if n_left:  parts.append(f"{n_left} selected on left")
            if n_right: parts.append(f"{n_right} selected on right")
            if queued:  parts.append(f"{len(queued)} queued")
            status_lbl.config(text="  |  ".join(parts))
            move_right_btn.config(state="normal" if n_left  else "disabled",
                                  bg="#2255aa"   if n_left  else "#444444")
            move_left_btn.config(state="normal"  if n_right else "disabled",
                                 bg="#226655"    if n_right else "#444444")

        # ── Panels ────────────────────────────────────────────────────────
        panes = tk.Frame(rw, bg=BG); panes.pack(fill="both", expand=True)

        def _make_panel(parent, title, bg_col):
            frm = tk.Frame(parent, bg=BG)
            frm.pack(side="left", fill="both", expand=True, padx=2, pady=2)
            hdr = tk.Frame(frm, bg=bg_col, pady=3); hdr.pack(fill="x")
            tk.Label(hdr, text=title, bg=bg_col, fg="white",
                     font=("Segoe UI", 9, "bold")).pack(side="left", padx=8)
            cnt = tk.Label(hdr, text="", bg=bg_col, fg="white",
                           font=("Segoe UI", 9)); cnt.pack(side="right", padx=8)
            outer = tk.Frame(frm, bg=BG); outer.pack(fill="both", expand=True)
            cv = tk.Canvas(outer, bg=BG, highlightthickness=0)
            sb = tk.Scrollbar(outer, orient="vertical", command=cv.yview)
            cv.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y"); cv.pack(side="left", fill="both", expand=True)
            gf = tk.Frame(cv, bg=BG)
            cv.create_window((0,0), window=gf, anchor="nw")
            gf.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
            cv.bind("<MouseWheel>", lambda e: cv.yview_scroll(-1 if e.delta>0 else 1, "units"))
            tk.Frame(parent, bg=HOVER_BD, width=2).pack(side="left", fill="y")
            return gf, cv, cnt

        left_gf,  left_cv,  left_cnt  = _make_panel(panes,
            "  Source folder  —  select files, then ▶ Move to Right", "#334466")
        left_cell_map = {}

        # ── Centre: destination tree ───────────────────────────────────────
        tree_frame = tk.Frame(panes, bg=BG, width=330)
        tree_frame.pack(side="left", fill="y")
        tree_frame.pack_propagate(False)
        tree_hdr = tk.Frame(tree_frame, bg="#446644", pady=3); tree_hdr.pack(fill="x")
        tk.Label(tree_hdr, text="  Destination folder", bg="#446644", fg="white",
                 font=("Segoe UI", 9, "bold")).pack(side="left", padx=8)
        tree_btn_row = tk.Frame(tree_frame, bg=BG2); tree_btn_row.pack(fill="x")

        def _new_folder():
            parent_dir = dest_folder[0] if dest_folder[0] else root
            dlg = tk.Toplevel(rw); dlg.title("New Folder")
            dlg.configure(bg=BG3); dlg.grab_set(); dlg.transient(rw)
            dlg.geometry(f"320x130+{rw.winfo_rootx()+rw.winfo_width()//2-160}+{rw.winfo_rooty()+200}")
            tk.Label(dlg, text="New folder under:", bg=BG3, fg=TEXT_DIM,
                     font=("Segoe UI", 8)).pack(pady=(10,0))
            tk.Label(dlg, text=os.path.basename(parent_dir) or parent_dir,
                     bg=BG3, fg=TEXT_BRIGHT, font=("Segoe UI", 9, "bold")).pack()
            nv = tk.StringVar()
            ent = tk.Entry(dlg, textvariable=nv, font=("Segoe UI", 10),
                           bg=BG2, fg=TEXT_BRIGHT, insertbackground=TEXT_BRIGHT,
                           relief="flat", highlightthickness=1, highlightbackground="#555")
            ent.pack(padx=16, fill="x", pady=6); ent.focus_set()
            def _ok():
                name = nv.get().strip()
                for ch in r'\/:*?"<>|': name = name.replace(ch, "")
                if not name: return
                new_path = os.path.join(parent_dir, name)
                try:
                    os.makedirs(new_path, exist_ok=True)
                    dest_folder[0] = new_path
                    dlg.destroy()
                    _refresh_dest_tree()
                    _set_msg(f"Created and selected: {name}")
                except Exception as e:
                    messagebox.showerror("Failed", str(e), parent=dlg)
            ent.bind("<Return>", lambda e: _ok())
            tk.Button(dlg, text="Create", bg=GREEN, fg="white",
                      font=("Segoe UI", 9, "bold"), relief="flat", padx=10,
                      command=_ok).pack(pady=(0,8))

        tk.Button(tree_btn_row, text="+ New Folder", bg="#226644", fg="white",
                  font=("Segoe UI", 8, "bold"), relief="flat", padx=6, pady=3,
                  cursor="hand2", command=_new_folder).pack(side="left", padx=4, pady=2)

        dest_tree_sb = tk.Scrollbar(tree_frame, orient="vertical")
        dest_tree = ttk.Treeview(tree_frame, show="tree", selectmode="browse",
                                  yscrollcommand=dest_tree_sb.set)
        dest_tree_sb.configure(command=dest_tree.yview)
        dest_tree_sb.pack(side="right", fill="y")
        dest_tree.pack(side="left", fill="both", expand=True)

        def _refresh_dest_tree():
            dest_tree.delete(*dest_tree.get_children())
            root_name = os.path.basename(root) or root
            dest_tree.insert("", "end", iid=root, text=f"  {root_name}", open=True)
            _insert_dest_subdirs(root, root)
            if dest_folder[0] and dest_tree.exists(dest_folder[0]):
                dest_tree.selection_set(dest_folder[0])
                dest_tree.see(dest_folder[0])

        def _insert_dest_subdirs(parent_iid, path):
            try:
                dirs = sorted([e for e in os.scandir(path) if e.is_dir()],
                              key=lambda e: e.name.lower())
            except: return
            for d in dirs:
                dp = os.path.normpath(d.path)
                if dest_tree.exists(dp): continue
                dest_tree.insert(parent_iid, "end", iid=dp, text=f"  {d.name}")
                try:
                    if any(e.is_dir() for e in os.scandir(dp)):
                        dest_tree.insert(dp, "end", iid=dp+"/__ph__", text="")
                except: pass

        def _dest_tree_expand(event):
            iid = dest_tree.focus()
            if not iid or "__ph__" in iid: return
            ph = iid + "/__ph__"
            if dest_tree.exists(ph):
                dest_tree.delete(ph)
                _insert_dest_subdirs(iid, iid)

        def _dest_tree_select(event):
            sel = dest_tree.selection()
            if not sel or "__ph__" in sel[0]: return
            dest_folder[0] = sel[0]
            _set_msg(f"Destination: {os.path.basename(dest_folder[0]) or dest_folder[0]}")

        dest_tree.bind("<<TreeviewOpen>>",   _dest_tree_expand)
        dest_tree.bind("<<TreeviewSelect>>", _dest_tree_select)

        tk.Frame(panes, bg=HOVER_BD, width=2).pack(side="left", fill="y")

        right_gf, right_cv, right_cnt = _make_panel(panes,
            "  Queue  —  files to be copied/moved  |  select to move back", "#446633")
        right_cell_map = {}

        # ── Left panel build ──────────────────────────────────────────────
        def _build_left():
            for w in left_gf.winfo_children(): w.destroy()
            left_cell_map.clear()
            sf   = source_files[0] or []
            cols = max(1, (left_cv.winfo_width() or 400) // (CELL_W + CELL_PAD))
            for i, fp in enumerate(sf):
                is_queued = fp in queued_set
                is_sel    = fp in selected
                cv = _make_cell(left_gf, fp, sel=is_sel, dimmed=is_queued)
                cv.grid(row=i//cols, column=i%cols, padx=CELL_PAD//2, pady=CELL_PAD//2)
                left_cell_map[fp] = cv
                if not is_queued:
                    def _click(e, f=fp, idx=i, c=cv):
                        ctrl  = (e.state & 0x4) != 0
                        shift = (e.state & 0x1) != 0
                        sf2   = source_files[0] or []
                        if ctrl:
                            if f in selected:
                                selected.discard(f); c.configure(bg=NRM_COL)
                            else:
                                selected.add(f); c.configure(bg=SEL_COL)
                            last_click[0] = idx
                        elif shift and last_click[0] is not None:
                            lo = min(last_click[0], idx)
                            hi = max(last_click[0], idx)
                            for j in range(lo, hi+1):
                                if j < len(sf2) and sf2[j] not in queued_set:
                                    fp2 = sf2[j]
                                    selected.add(fp2)
                                    c2 = left_cell_map.get(fp2)
                                    if c2: c2.configure(bg=SEL_COL)
                            last_click[0] = idx
                        else:
                            for p in list(selected):
                                c2 = left_cell_map.get(p)
                                if c2: c2.configure(bg=NRM_COL)
                            selected.clear()
                            selected.add(f); c.configure(bg=SEL_COL)
                            last_click[0] = idx
                        _update_status()
                    cv.bind("<Button-1>", _click)
                cv.bind("<MouseWheel>", lambda e: left_cv.yview_scroll(-1 if e.delta>0 else 1, "units"))
            left_cnt.config(text=f"{len(sf)} files  |  {len(queued_set)} queued")

        # ── Right panel build ─────────────────────────────────────────────
        def _build_right():
            for w in right_gf.winfo_children(): w.destroy()
            right_cell_map.clear()
            cols = max(1, (right_cv.winfo_width() or 400) // (CELL_W + CELL_PAD))
            for i, fp in enumerate(queued):
                is_sel = fp in right_sel
                cv = _make_cell(right_gf, fp, sel=is_sel, seq=i+1)
                cv.grid(row=i//cols, column=i%cols, padx=CELL_PAD//2, pady=CELL_PAD//2)
                right_cell_map[fp] = cv
                def _rclick(e, f=fp, idx=i, c=cv):
                    ctrl  = (e.state & 0x4) != 0
                    shift = (e.state & 0x1) != 0
                    if ctrl:
                        if f in right_sel:
                            right_sel.discard(f); c.configure(bg=NRM_COL)
                        else:
                            right_sel.add(f); c.configure(bg=SEL_COL)
                        right_last_click[0] = idx
                    elif shift and right_last_click[0] is not None:
                        lo = min(right_last_click[0], idx)
                        hi = max(right_last_click[0], idx)
                        for j in range(lo, hi+1):
                            if j < len(queued):
                                fp2 = queued[j]
                                right_sel.add(fp2)
                                c2 = right_cell_map.get(fp2)
                                if c2: c2.configure(bg=SEL_COL)
                        right_last_click[0] = idx
                    else:
                        if f in right_sel:
                            right_sel.discard(f); c.configure(bg=NRM_COL)
                        else:
                            right_sel.add(f); c.configure(bg=SEL_COL)
                        right_last_click[0] = idx
                    _update_status()
                cv.bind("<Button-1>", _rclick)
                cv.bind("<MouseWheel>", lambda e: right_cv.yview_scroll(-1 if e.delta>0 else 1, "units"))
            right_cnt.config(text=f"{len(queued)} queued")

        # ── Load source folder ────────────────────────────────────────────
        def _load_source(folder, pre_select=None):
            source_folder[0] = folder
            selected.clear(); last_click[0] = None
            try:
                sf = sorted([os.path.join(folder, e.name)
                             for e in os.scandir(folder)
                             if e.is_file() and os.path.splitext(e.name)[1].lower() in exts])
            except: sf = []
            source_files[0] = sf
            if pre_select:
                selected.update(p for p in pre_select if p in set(sf))
            src_lbl.config(text=f"Source: {os.path.basename(folder) or folder}  ({len(sf)} files)")
            # Load thumbnails in background
            load_gen[0] += 1
            gen = load_gen[0]
            missing = [p for p in sf if p not in photo_cache]
            def _bg(paths=missing, g=gen):
                jmap = thumb_get_many(paths)
                decoded = []
                for fpath in paths:
                    jpeg = jmap.get(fpath)
                    if jpeg:
                        try:
                            img = Image.open(_io.BytesIO(jpeg))
                            img.thumbnail((sz, int(sz * THUMB_IMG_H / THUMB_SIZE)), Image.BILINEAR)
                            decoded.append((fpath, img))
                        except: pass
                def _apply(dec=decoded, g=g):
                    if not rw.winfo_exists() or load_gen[0] != g: return
                    for fpath, img in dec:
                        photo_cache[fpath] = ImageTk.PhotoImage(img)
                    _build_left()
                rw.after(0, _apply)
            _build_left()
            if missing:
                threading.Thread(target=_bg, daemon=True).start()
            _update_status()

        # ── Hook main tree navigation ─────────────────────────────────────
        orig_load_folder = self._load_folder.__func__

        def _patched_load_folder(self_inner, folder):
            orig_load_folder(self_inner, folder)
            if rw.winfo_exists():
                _load_source(folder)

        self._load_folder = lambda folder, _f=_patched_load_folder: _f(self, folder)

        def _restore_on_close():
            self._load_folder = lambda folder: orig_load_folder(self, folder)
            _on_close()
        rw.protocol("WM_DELETE_WINDOW", _restore_on_close)

        # ── Initial load ──────────────────────────────────────────────────
        rw.after(200, _refresh_dest_tree)
        rw.after(250, lambda: _load_source(self.current_folder,
                                           pre_select=set(files) if files else None))


    def _export_list(self):
        """Export selected files (or tagged collection if no selection) to a folder."""
        # Use selection if available, otherwise fall back to tagged collection
        if self._selected:
            files = [f for f in self._all_files if f in self._selected]
            if not files:
                files = sorted(self._selected)
            source_label = f"{len(files)} selected files"
        elif self.tagged:
            files = list(self.tagged_order) if self.tagged_order else sorted(self.tagged)
            source_label = f"collection '{self.collection}'"
        else:
            messagebox.showinfo("Nothing to export",
                "Select files or tag a collection first.", parent=self.win)
            return

        n = len(files)
        import tkinter.filedialog as fd, hashlib, shutil as _sh

        dlg = tk.Toplevel(self.win)
        dlg.title(f"Export — {source_label}")
        dlg.configure(bg=BG3); dlg.grab_set(); dlg.transient(self.win)
        self._centre_window(dlg, 700, 600)

        tk.Label(dlg, text=f"Export  {n}  files from {source_label}",
                 bg=BG3, fg=TEXT_BRIGHT, font=("Segoe UI",11,"bold")).pack(pady=(14,4))

        # Options row
        opt_row = tk.Frame(dlg, bg=BG3); opt_row.pack(fill="x", padx=16, pady=(0,6))
        add_suffix = tk.BooleanVar(value=True)
        tk.Checkbutton(opt_row, text="Add unique _xxxx suffix to filenames  (prevents collisions)",
                       variable=add_suffix, bg=BG3, fg=TEXT_BRIGHT,
                       selectcolor=BG2, activebackground=BG3,
                       font=("Segoe UI",9)).pack(side="left")

        tk.Label(dlg, text="Files will be copied with a sequential numeric prefix (0010, 0020 ...)",
                 bg=BG3, fg=TEXT_DIM, font=("Segoe UI",9,"italic")).pack(pady=(0,6))

        # Preview list
        list_frame = tk.Frame(dlg, bg=BG3); list_frame.pack(fill="both", expand=True, padx=16, pady=4)
        lb = tk.Listbox(list_frame, bg=BG2, fg=TEXT_BRIGHT, font=("Segoe UI",8),
                        selectmode="none", relief="flat", bd=0,
                        highlightthickness=1, highlightbackground=HOVER_BD)
        vsb = tk.Scrollbar(list_frame, command=lb.yview)
        lb.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y"); lb.pack(side="left", fill="both", expand=True)

        def _refresh_preview(*_):
            lb.delete(0, "end")
            for i, fp in enumerate(files):
                seq  = (i + 1) * 10
                stem, ext = os.path.splitext(os.path.basename(fp))
                if add_suffix.get():
                    h = hashlib.md5(fp.encode()).hexdigest()[:4]
                    new_name = f"{seq:04d}_{stem}_{h}{ext}"
                else:
                    new_name = f"{seq:04d}_{stem}{ext}"
                lb.insert("end", f"  {new_name}   ←   {os.path.basename(fp)}")

        add_suffix.trace_add("write", _refresh_preview)
        _refresh_preview()

        # Destination
        dest_frame = tk.Frame(dlg, bg=BG3); dest_frame.pack(fill="x", padx=16, pady=6)
        tk.Label(dest_frame, text="Destination:", bg=BG3, fg=TEXT_BRIGHT,
                 font=("Segoe UI",9)).pack(side="left", padx=(0,6))
        dest_var = tk.StringVar()
        tk.Entry(dest_frame, textvariable=dest_var, bg=BG2, fg=TEXT_BRIGHT,
                 insertbackground=TEXT_BRIGHT, font=("Segoe UI",9),
                 relief="flat", highlightthickness=1,
                 highlightbackground=HOVER_BD, width=44).pack(side="left", padx=(0,6))

        def _browse():
            p = fd.askdirectory(title="Choose export folder", parent=dlg)
            if p: dest_var.set(os.path.normpath(p))
        tk.Button(dest_frame, text="Browse…", bg="#335577", fg="white",
                  font=("Segoe UI",8,"bold"), relief="flat", padx=6,
                  cursor="hand2", command=_browse).pack(side="left")

        status_lbl = tk.Label(dlg, text="", bg=BG3, fg=TEXT_DIM, font=("Segoe UI",8))
        status_lbl.pack(pady=(0,4))

        def _do_export():
            dest = dest_var.get().strip()
            if not dest:
                status_lbl.config(text="⚠  Please choose a destination folder", fg="#cc4444")
                return
            try:
                os.makedirs(dest, exist_ok=True)
            except Exception as e:
                status_lbl.config(text=f"⚠  Cannot create folder: {e}", fg="#cc4444")
                return
            btn_export.config(state="disabled", text="Exporting…")
            dlg.update_idletasks()
            errors = []; copied = 0
            for i, fp in enumerate(files):
                seq  = (i + 1) * 10
                stem, ext = os.path.splitext(os.path.basename(fp))
                if add_suffix.get():
                    h = hashlib.md5(fp.encode()).hexdigest()[:4]
                    new_name = f"{seq:04d}_{stem}_{h}{ext}"
                else:
                    new_name = f"{seq:04d}_{stem}{ext}"
                dest_path = os.path.join(dest, new_name)
                try:
                    _sh.copy2(fp, dest_path)
                    copied += 1
                    lb.itemconfig(i, fg="#44cc44")
                    if i % 5 == 0:
                        status_lbl.config(text=f"Copying {i+1} of {n}…")
                        dlg.update_idletasks()
                except Exception as e:
                    lb.itemconfig(i, fg="#cc4444")
                    errors.append(f"{os.path.basename(fp)}: {e}")
            if errors:
                status_lbl.config(text=f"✓ {copied} copied, {len(errors)} errors", fg="#cc8800")
                btn_export.config(state="normal", text="Export")
            else:
                status_lbl.config(text=f"✓ {copied} files exported to {dest}", fg="#44cc44")
                btn_export.config(state="normal", text="Export Again")

        bf = tk.Frame(dlg, bg=BG3); bf.pack(pady=10)
        btn_export = tk.Button(bf, text="Export", bg=GREEN, fg="white",
                               font=("Segoe UI",10,"bold"), relief="flat",
                               padx=16, pady=6, cursor="hand2", command=_do_export)
        btn_export.pack(side="left", padx=8)
        tk.Button(bf, text="Close", bg=BG2, fg=TEXT_BRIGHT,
                  font=("Segoe UI",10), relief="flat", padx=16, pady=6,
                  cursor="hand2", command=dlg.destroy).pack(side="left", padx=8)

    def _export_collection_csv(self):
        """Export current collection as a CSV file: Filename, Size, Path."""
        if not self.collection:
            messagebox.showinfo("No Collection", "No active collection selected.", parent=self.win)
            return
        if not self.tagged:
            messagebox.showinfo("Empty Collection",
                f"Collection '{self.collection}' has no files.", parent=self.win)
            return
        import csv as _csv
        from tkinter import filedialog as _fd
        # Default filename based on collection name
        safe_name = "".join(c for c in self.collection if c not in r'\/:*?"<>|')
        default_file = f"{safe_name}.csv"
        out_path = _fd.asksaveasfilename(
            parent=self.win,
            title="Save Collection CSV",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialdir=_reports_dir(),
            initialfile=default_file)
        if not out_path: return
        try:
            files = self.tagged_order if self.tagged_order else sorted(self.tagged)
            with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = _csv.writer(f)
                writer.writerow(["Filename", "Size", "Path"])
                for path in files:
                    fname = os.path.basename(path)
                    try:
                        size = os.path.getsize(_longpath(path))
                    except Exception:
                        size = 0
                    writer.writerow([fname, size, path])
            messagebox.showinfo("CSV Exported",
                f"Exported {len(files)} files to:\n{out_path}", parent=self.win)
        except Exception as e:
            messagebox.showerror("Export Failed", str(e), parent=self.win)

    def _contact_sheet_sort_key(self):
        """Return the correct sort key function for contact sheet file ordering."""
        if self.mode == "photos":
            return lambda path: os.path.basename(path).lower()
        else:
            import re as _re
            _date_pat = _re.compile(r'^\d{4}-\d{2}-\d{2}')
            def _key(path):
                fname = os.path.basename(path)
                if fname.lower().startswith('scan'):
                    return (0, fname.lower())
                m = _date_pat.match(fname)
                if m:
                    return (1, tuple(~ord(c) for c in m.group(0)))
                try:    mtime = os.path.getmtime(path)
                except: mtime = 0.0
                return (2, -mtime)
            return _key

    def _contact_sheet_dialog(self):
        """Contact sheet settings dialog with preview of first page layout."""
        if not self.tagged:
            messagebox.showinfo("Contact Sheet",
                "No files are tagged.", parent=self.win)
            return

        files    = sorted(self.tagged, key=self._contact_sheet_sort_key())
        n_files  = len(files)
        cols_var = tk.IntVar(value=6)
        rows_var = tk.IntVar(value=0)   # 0 = auto
        orient_var = tk.StringVar(value="Portrait")

        from datetime import datetime as _dt
        safe = self.collection.replace(' ', '_').encode('ascii', errors='replace').decode('ascii')
        default_name = f"{safe}_{_dt.now().strftime('%y%m%d_%H-%M')}.pdf"
        contact_dir  = _sheets_dir(self.mode_cfg['root'])
        out_var   = tk.StringVar(value=os.path.join(contact_dir, default_name))
        title_var = tk.StringVar(value=self.collection)

        # ── Inner functions — defined first so widgets can reference them ──────

        def _calc(landscape, cols, rows):
            pw = 277.0 if landscape else 190.0
            ph = 190.0 if landscape else 277.0
            cell_w_mm = pw / cols
            # Each cell: seq/GPS line + image + filename
            # seq line ~4mm, filename ~4mm, image proportional to thumb
            line_h  = 4.0
            img_ratio = 0.75   # typical landscape photo
            img_h_mm  = cell_w_mm * img_ratio
            cell_h_mm = line_h + img_h_mm + line_h
            avail_h   = ph - 12.0   # header
            rows_auto = max(1, int(avail_h / cell_h_mm))
            rows_pp   = rows if rows > 0 else rows_auto
            cells_pp  = cols * rows_pp
            n_pages   = max(1, -(-n_files // cells_pp))
            return n_pages, rows_pp, cell_w_mm, cell_h_mm

        def _update(*_):
            landscape = orient_var.get() == "Landscape"
            cols = max(1, cols_var.get())
            rows = max(0, rows_var.get())
            n_pages, rpp, cw, ch = _calc(landscape, cols, rows)
            info_lbl.config(text=
                f"{n_files} images  —  {cols} cols × {rpp} rows  —  {n_pages} page{'s' if n_pages!=1 else ''}")
            _draw_preview(landscape, cols, rpp, cw, ch, n_pages)

        def _draw_preview(landscape, cols, rows_pp, cell_w_mm, cell_h_mm, n_pages):
            """Draw first-page grid layout on the preview canvas."""
            prev_canvas.update_idletasks()
            cw = prev_canvas.winfo_width()
            ch = prev_canvas.winfo_height()
            if cw < 10 or ch < 10: return
            prev_canvas.delete("all")

            # Page proportions
            if landscape:
                ratio = 297.0 / 210.0
            else:
                ratio = 210.0 / 297.0

            # Fit page into canvas with margin
            pad = 8
            if cw / ch > ratio:
                ph = ch - pad * 2
                pw = ph * ratio
            else:
                pw = cw - pad * 2
                ph = pw / ratio
            px = (cw - pw) / 2
            py = (ch - ph) / 2

            # Page bg
            prev_canvas.create_rectangle(px, py, px+pw, py+ph,
                                         fill="white", outline="#aaaaaa", width=1)

            # Header band
            hdr_h = ph * 0.04
            prev_canvas.create_rectangle(px, py, px+pw, py+hdr_h,
                                         fill="#eeeeee", outline="")
            prev_canvas.create_text(px + pw/2, py + hdr_h/2,
                                    text=f"{title_var.get()}  —  Page 1 of {n_pages}",
                                    fill="#333", font=("Segoe UI", 7), anchor="center")

            # Grid cells
            margin_frac = 0.03
            mx = pw * margin_frac
            my = hdr_h + ph * 0.01
            grid_w = pw - mx * 2
            grid_h = ph - my - ph * margin_frac
            cell_w = grid_w / cols
            cell_h = grid_h / rows_pp if rows_pp else grid_h

            for r in range(rows_pp):
                for c in range(cols):
                    x0 = px + mx + c * cell_w
                    y0 = py + my + r * cell_h
                    x1 = x0 + cell_w - 1
                    y1 = y0 + cell_h - 1
                    # Cell border
                    prev_canvas.create_rectangle(x0, y0, x1, y1,
                                                 fill="#f8f8f8", outline="#cccccc")
                    # Seq line at top
                    seq_h = cell_h * 0.12
                    prev_canvas.create_rectangle(x0, y0, x1, y0+seq_h,
                                                 fill="#e0e0e0", outline="")
                    # Image area
                    img_h = cell_h * 0.72
                    prev_canvas.create_rectangle(x0+1, y0+seq_h, x1-1, y0+seq_h+img_h,
                                                 fill="#cccccc", outline="")
                    # Filename line at bottom
                    prev_canvas.create_rectangle(x0, y0+seq_h+img_h, x1, y1,
                                                 fill="#eeeeee", outline="")

        # ── Dialog ────────────────────────────────────────────────────────────
        dlg = tk.Toplevel(self.win)
        dlg.title("Contact Sheet")
        dlg.configure(bg=BG3)
        dlg.resizable(True, True)
        dlg.transient(self.win)
        self.win.update_idletasks()
        dlg_w, dlg_h = 700, 700
        x = self.win.winfo_rootx() + (self.win.winfo_width()  - dlg_w) // 2
        y = self.win.winfo_rooty() + (self.win.winfo_height() - dlg_h) // 2
        dlg.geometry(f"{dlg_w}x{dlg_h}+{x}+{y}")

        tk.Label(dlg, text="Generate Contact Sheet", bg=BG3, fg=TEXT_BRIGHT,
                 font=("Segoe UI",12,"bold")).pack(pady=(12,4))

        # ── Controls ─────────────────────────────────────────────────────────
        ctrl = tk.Frame(dlg, bg=BG3); ctrl.pack(fill="x", padx=16, pady=4)

        tk.Label(ctrl, text="Title:", bg=BG3, fg=TEXT_BRIGHT,
                 font=("Segoe UI",9)).grid(row=0, column=0, sticky="e", padx=(0,4), pady=3)
        tk.Entry(ctrl, textvariable=title_var, bg=BG2, fg=TEXT_BRIGHT,
                 font=("Segoe UI",9), relief="flat",
                 highlightthickness=1, highlightbackground=HOVER_BD,
                 width=32).grid(row=0, column=1, columnspan=5, sticky="ew", pady=3)

        tk.Label(ctrl, text="Columns:", bg=BG3, fg=TEXT_BRIGHT,
                 font=("Segoe UI",9)).grid(row=1, column=0, sticky="e", padx=(0,4), pady=3)
        tk.Spinbox(ctrl, from_=1, to=20, textvariable=cols_var, width=4,
                   bg=BG2, fg=TEXT_BRIGHT, buttonbackground=BG2,
                   font=("Segoe UI",9), command=_update).grid(
                       row=1, column=1, sticky="w", pady=3)

        tk.Label(ctrl, text="Rows:", bg=BG3, fg=TEXT_BRIGHT,
                 font=("Segoe UI",9)).grid(row=1, column=2, sticky="e", padx=(12,4), pady=3)
        tk.Spinbox(ctrl, from_=0, to=30, textvariable=rows_var, width=4,
                   bg=BG2, fg=TEXT_BRIGHT, buttonbackground=BG2,
                   font=("Segoe UI",9), command=_update).grid(
                       row=1, column=3, sticky="w", pady=3)
        tk.Label(ctrl, text="(0=auto)", bg=BG3, fg=TEXT_DIM,
                 font=("Segoe UI",8)).grid(row=1, column=4, sticky="w", padx=2)

        tk.Label(ctrl, text="Orientation:", bg=BG3, fg=TEXT_BRIGHT,
                 font=("Segoe UI",9)).grid(row=2, column=0, sticky="e", padx=(0,4), pady=3)
        or_fr = tk.Frame(ctrl, bg=BG3)
        or_fr.grid(row=2, column=1, columnspan=5, sticky="w")
        for txt in ("Portrait", "Landscape"):
            tk.Radiobutton(or_fr, text=txt, variable=orient_var, value=txt,
                           bg=BG3, fg=TEXT_BRIGHT, selectcolor=BG2,
                           activebackground=BG3, font=("Segoe UI",9),
                           command=_update).pack(side="left", padx=(0,12))
        ctrl.columnconfigure(1, weight=1)

        # Info line
        info_lbl = tk.Label(dlg, text="", bg=BG3, fg=TEXT_BRIGHT,
                            font=("Segoe UI",9))
        info_lbl.pack()

        # ── Preview canvas ────────────────────────────────────────────────────
        prev_frame = tk.Frame(dlg, bg="#333", highlightthickness=1,
                              highlightbackground="#555")
        prev_frame.pack(fill="both", expand=True, padx=16, pady=4)
        prev_canvas = tk.Canvas(prev_frame, bg="#444", highlightthickness=0)
        prev_canvas.pack(fill="both", expand=True)
        prev_canvas.bind("<Configure>", lambda e: _update())

        # ── Save As row ───────────────────────────────────────────────────────
        save_fr = tk.Frame(dlg, bg=BG3); save_fr.pack(fill="x", padx=16, pady=(4,2))
        tk.Label(save_fr, text="Save to:", bg=BG3, fg=TEXT_BRIGHT,
                 font=("Segoe UI",9)).pack(side="left", padx=(0,4))
        tk.Entry(save_fr, textvariable=out_var, bg=BG2, fg=TEXT_BRIGHT,
                 font=("Segoe UI",8), relief="flat",
                 highlightthickness=1, highlightbackground=HOVER_BD).pack(
                     side="left", fill="x", expand=True, padx=(0,4))
        def _browse_save():
            import tkinter.filedialog as _fd
            p = _fd.asksaveasfilename(parent=dlg,
                defaultextension=".pdf", filetypes=[("PDF","*.pdf")],
                initialdir=os.path.dirname(out_var.get()),
                initialfile=os.path.basename(out_var.get()))
            if p: out_var.set(p)
        tk.Button(save_fr, text="...", bg=ACCENT, fg="white",
                  font=("Segoe UI",9,"bold"), relief="flat", padx=6,
                  cursor="hand2", command=_browse_save).pack(side="left")

        # ── Buttons ───────────────────────────────────────────────────────────
        btn_fr = tk.Frame(dlg, bg=BG3); btn_fr.pack(pady=(6,10))
        tk.Button(btn_fr, text="Cancel", bg="#442222", fg="white",
                  font=("Segoe UI",10,"bold"), relief="flat", padx=14, pady=6,
                  cursor="hand2", command=dlg.destroy).pack(side="left", padx=8)
        tk.Button(btn_fr, text="Generate", bg="#226633", fg="white",
                  font=("Segoe UI",10,"bold"), relief="flat", padx=14, pady=6,
                  cursor="hand2",
                  command=lambda: self._run_contact_sheet(
                      files, max(1, cols_var.get()),
                      max(0, rows_var.get()),
                      orient_var.get() == "Landscape",
                      title_var.get().strip(),
                      out_var.get().strip(), dlg)
                  ).pack(side="left", padx=8)

        # Bind variable changes to update
        cols_var.trace_add("write", _update)
        rows_var.trace_add("write", _update)
        orient_var.trace_add("write", _update)
        title_var.trace_add("write", _update)

        dlg.after(200, _update)

    def _run_contact_sheet(self, files, cols, rows, landscape, title, out_path, dlg):
        """Generate the contact sheet PDF."""
        if not out_path:
            messagebox.showwarning("No path", "Please specify an output file.", parent=dlg)
            return
        if os.path.exists(out_path):
            if not messagebox.askyesno("File exists",
                    f"Overwrite:\n{os.path.basename(out_path)}?", parent=dlg):
                return
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        dlg.destroy()

        prog = tk.Toplevel(self.win)
        prog.title("Generating..."); prog.configure(bg=BG3)
        prog.resizable(False, False); prog.transient(self.win)
        self.win.update_idletasks()
        x = self.win.winfo_rootx() + (self.win.winfo_width()  - 320) // 2
        y = self.win.winfo_rooty() + (self.win.winfo_height() - 80)  // 2
        prog.geometry(f"320x80+{x}+{y}")
        prog_lbl = tk.Label(prog, text="Starting...", bg=BG3, fg=TEXT_BRIGHT,
                            font=("Segoe UI",10)); prog_lbl.pack(pady=16)
        prog.update()

        def do_generate():
            try:
                from fpdf import FPDF
                from PIL import ImageOps as _IOS
                import tempfile, io
                from datetime import datetime as _DT

                margin = 10.0
                hdr_h  = 8.0
                gap    = 1.5

                if landscape:
                    page_w, page_h = 297.0, 210.0
                    pdf = FPDF(orientation="L", unit="mm", format=[210.0, 297.0])
                else:
                    page_w, page_h = 210.0, 297.0
                    pdf = FPDF(orientation="P", unit="mm", format="A4")
                pdf.set_auto_page_break(False)
                pdf.set_margins(margin, margin, margin)

                pw = page_w - 2 * margin
                ph = page_h - 2 * margin

                cell_w = pw / cols

                # Cell layout: seq/GPS line + image + filename
                line_h    = 3.5    # mm for seq line and filename line
                img_ratio = 0.68   # height/width ratio for image area
                img_h     = cell_w * img_ratio
                cell_h    = line_h + img_h + line_h

                avail_h   = ph - hdr_h - gap
                rows_auto = max(1, int(avail_h / cell_h))
                rows_pp   = rows if rows > 0 else rows_auto

                # Recalculate cell_h to fill page exactly if rows specified
                if rows > 0:
                    cell_h = avail_h / rows_pp
                    img_h  = cell_h - 2 * line_h

                cells_pp  = cols * rows_pp
                total     = len(files)
                n_pages   = max(1, -(-total // cells_pp))
                grid_top  = margin + hdr_h + gap

                title_enc = title.encode("latin-1", errors="replace").decode("latin-1")
                stamp     = _DT.now().strftime("%d %b %Y  %H:%M")
                fname_pt  = max(4, min(7, int(cell_w * 1.8)))
                seq_pt    = fname_pt

                def _render_img(fpath):
                    """Return PIL Image for fpath (JPG or PDF first page)."""
                    ext = os.path.splitext(fpath)[1].lower()
                    if ext == ".pdf":
                        try:
                            import fitz
                            doc  = fitz.open(_longpath(fpath))
                            page = doc[0]
                            mat  = fitz.Matrix(2, 2)
                            pix  = page.get_pixmap(matrix=mat)
                            doc.close()
                            return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                        except Exception:
                            # Fall back to cached thumbnail
                            jpeg = thumb_get(fpath)
                            if jpeg:
                                return Image.open(_io.BytesIO(jpeg))
                            return None
                    else:
                        try:
                            img = Image.open(_longpath(fpath))
                            return _IOS.exif_transpose(img)
                        except Exception:
                            jpeg = thumb_get(fpath)
                            if jpeg:
                                return Image.open(_io.BytesIO(jpeg))
                            return None

                for page_idx in range(n_pages):
                    pdf.add_page()
                    page_files = files[page_idx * cells_pp : (page_idx+1) * cells_pp]

                    # ── Header ───────────────────────────────────────────────
                    pdf.set_fill_color(240, 240, 240)
                    pdf.rect(margin, margin, pw, hdr_h, "F")
                    pdf.set_draw_color(160, 160, 160)
                    pdf.set_line_width(0.3)
                    pdf.line(margin, margin + hdr_h, margin + pw, margin + hdr_h)

                    pdf.set_font("Helvetica", "B", size=10)
                    pdf.set_text_color(0, 0, 0)
                    pdf.text(margin + 1, margin + hdr_h * 0.72, title_enc)

                    pg_str = f"Page {page_idx+1} of {n_pages}"
                    pdf.set_font("Helvetica", "", size=8)
                    pdf.set_text_color(80, 80, 80)
                    pg_w = pdf.get_string_width(pg_str)
                    pdf.text(margin + pw - pg_w - 1, margin + hdr_h * 0.72, pg_str)

                    pdf.set_font("Helvetica", "", size=7)
                    pdf.text(margin + pw * 0.55, margin + hdr_h * 0.72, stamp)

                    # ── Cells ────────────────────────────────────────────────
                    for i, fpath in enumerate(page_files):
                        row = i // cols
                        col = i %  cols
                        cx  = margin + col * cell_w
                        cy  = grid_top + row * cell_h
                        seq = page_idx * cells_pp + i + 1

                        self.win.after(0, prog_lbl.config,
                            {"text": f"Page {page_idx+1}/{n_pages}  —  {seq}/{total}"})
                        self.win.after(0, prog.update)

                        bd = 0.4
                        # Cell border
                        pdf.set_fill_color(200, 200, 200)
                        pdf.rect(cx, cy, cell_w, cell_h, "F")
                        ix = cx + bd; iy = cy + bd
                        iw = cell_w - 2*bd; ih = cell_h - 2*bd

                        # ── Seq/GPS line ──────────────────────────────────
                        pdf.set_fill_color(230, 230, 230)
                        pdf.rect(ix, iy, iw, line_h, "F")
                        pdf.set_font("Helvetica", "B", size=seq_pt)
                        pdf.set_text_color(0, 0, 0)
                        pdf.text(ix + 0.5, iy + line_h * 0.78, str(seq))

                        if self.mode == "photos" and _get_gps_coords(fpath):
                            pdf.set_fill_color(180, 0, 0)
                            gw = pdf.get_string_width("GPS") + 1.5
                            pdf.rect(ix + iw - gw, iy, gw, line_h, "F")
                            pdf.set_text_color(255, 255, 255)
                            pdf.text(ix + iw - gw + 0.5, iy + line_h * 0.78, "GPS")

                        # ── Image ─────────────────────────────────────────
                        img_y = iy + line_h
                        pdf.set_fill_color(180, 180, 180)
                        pdf.rect(ix, img_y, iw, img_h, "F")
                        try:
                            pil = _render_img(fpath)
                            if pil:
                                pil.thumbnail((int(iw*12), int(img_h*12)), Image.BILINEAR)
                                pil = pil.convert("RGB")
                                buf = io.BytesIO()
                                pil.save(buf, "JPEG", quality=82)
                                buf.seek(0)
                                with tempfile.NamedTemporaryFile(
                                        suffix=".jpg", delete=False) as tf:
                                    tf.write(buf.read())
                                    tmp = tf.name
                                iw_px, ih_px = pil.size
                                r = iw_px / ih_px if ih_px else 1
                                if r > iw / img_h:
                                    dw = iw;      dh = iw / r
                                else:
                                    dh = img_h;   dw = img_h * r
                                ox = ix + (iw - dw) / 2
                                oy = img_y + (img_h - dh) / 2
                                pdf.image(tmp, x=ox, y=oy, w=dw, h=dh)
                                os.unlink(tmp)
                        except Exception:
                            pass

                        # ── Filename line ─────────────────────────────────
                        fn_y = img_y + img_h
                        pdf.set_fill_color(245, 245, 245)
                        pdf.rect(ix, fn_y, iw, line_h, "F")
                        fname = os.path.basename(fpath).encode(
                            "latin-1", errors="replace").decode("latin-1")
                        pdf.set_font("Helvetica", "B", size=fname_pt)
                        pdf.set_text_color(0, 0, 0)
                        while len(fname) > 1 and pdf.get_string_width(fname) > iw - 1.0:
                            fname = fname[:-1]
                        pdf.text(ix + 0.5, fn_y + line_h * 0.78, fname)

                try:
                    pdf.output(out_path)
                except PermissionError:
                    self.win.after(0, lambda p=prog: (
                        p.withdraw(),
                        messagebox.showerror("Contact Sheet Failed",
                            f"File is open in another program:\n{os.path.basename(out_path)}",
                            parent=self.win),
                        p.destroy()))
                    return
                self.win.after(0, lambda: self._contact_sheet_done(
                    out_path, total, n_pages, prog))

            except Exception as e:
                import traceback
                err = traceback.format_exc()
                self.win.after(0, lambda msg=f"{e}\n\n{err}", p=prog: (
                    p.withdraw(),
                    messagebox.showerror("Contact Sheet Failed", msg, parent=self.win),
                    p.destroy()))

        threading.Thread(target=do_generate, daemon=True).start()

    def _contact_sheet_done(self, out_path, n_images, n_pages, prog=None):
        """Show completion message with Open in PDF Viewer and Close buttons."""
        try:
            if prog: prog.withdraw()
        except: pass
        if not os.path.exists(out_path):
            try:
                if prog: prog.destroy()
            except: pass
            messagebox.showerror("Contact Sheet",
                f"File not found:\n{out_path}", parent=self.win)
            return
        size_kb = os.path.getsize(out_path) // 1024
        msg = (f"{n_images} images  —  {n_pages} page{'s' if n_pages!=1 else ''}\n"
               f"{size_kb:,} KB\n\n{out_path}")
        dlg = tk.Toplevel(self.win)
        dlg.title("Contact Sheet Ready")
        dlg.configure(bg=BG3)
        dlg.resizable(False, False)
        dlg.transient(self.win)
        self.win.update_idletasks()
        x = self.win.winfo_rootx() + (self.win.winfo_width()  - 380) // 2
        y = self.win.winfo_rooty() + (self.win.winfo_height() - 180) // 2
        dlg.geometry(f"380x180+{x}+{y}")
        try:
            if prog: prog.destroy()
        except: pass
        dlg.lift(); dlg.focus_force()
        tk.Label(dlg, text="Contact Sheet Ready", bg=BG3, fg="#66cc66",
                 font=("Segoe UI",12,"bold")).pack(pady=(12,4))
        tk.Label(dlg, text=msg, bg=BG3, fg=TEXT_BRIGHT,
                 font=("Segoe UI",9), justify="left").pack(padx=16, pady=4)
        btn_fr = tk.Frame(dlg, bg=BG3); btn_fr.pack(pady=(6,10))

        def _open_pdf():
            try:
                import ctypes
                ctypes.windll.shell32.ShellExecuteW(None, "open", out_path, None, None, 1)
            except Exception as e:
                messagebox.showerror("Cannot open", str(e), parent=self.win)
            dlg.destroy()

        tk.Button(btn_fr, text="Open in PDF Viewer", bg="#226633", fg="white",
                  font=("Segoe UI",10,"bold"), relief="flat", padx=14, pady=6,
                  cursor="hand2", command=_open_pdf).pack(side="left", padx=6)
        tk.Button(btn_fr, text="Close", bg="#444", fg="white",
                  font=("Segoe UI",9), relief="flat", padx=10, pady=5,
                  cursor="hand2", command=dlg.destroy).pack(side="left", padx=6)


    # ── About ──────────────────────────────────────────────────────────────────
    def _switch_project(self):
        """Show project selector and switch to selected project seamlessly."""
        def _on_select(name, proj):
            # Flush pending saves
            if getattr(self, '_save_after_id', None):
                try: self.win.after_cancel(self._save_after_id)
                except: pass
                self._save_after_id = None
            self._save_current_collection()
            # Close zoom window
            if self._zoom_win:
                try:
                    if self._zoom_win.winfo_exists():
                        self._zoom_win.destroy()
                except: pass
                self._zoom_win = None
            # Activate project — updates globals
            _activate_project(name, proj)
            # Re-open DB
            db_close()
            db_open(_project_db_path(proj['path']))
            # Migrate any legacy txt files for new roots
            for path, _ in proj.get('photos_roots', []) + proj.get('pdfs_roots', []):
                if path: _migrate_txt_to_db(path)
            # Reload UI
            self._clear_grid()
            self._clear_tree()
            self.mode = "photos"
            self._mode_var.set("Photos")
            self.mode_cfg = _mode_cfg("photos")
            self._update_root_combobox()
            self._init_mode()
            self._status(f"Project: {name}")

        _show_project_selector(self.win, _on_select)

    def _show_settings(self):
        """Open the settings dialog from the toolbar."""
        def _after_save():
            # Reload config and reinitialise
            _load_config()
            self._save_current_collection()
            self.mode_cfg = _mode_cfg(self.mode)
            self._update_root_combobox()
            self._init_mode()
        _settings_dialog(self.win, on_save_cb=_after_save, startup=False)

    def _show_about(self):
        SBORDER="#2E75B6"; SBG="#f0f4f8"; STITLE="#1F3864"; SSUB="#2E75B6"; SDIM="#4a6080"
        dlg=tk.Toplevel(self.win); dlg.title("About FileTagger with SQLite")
        dlg.resizable(False,False); dlg.configure(bg=SBORDER)
        dlg.transient(self.win); dlg.grab_set()
        self._centre_window(dlg,520,470)
        border=tk.Frame(dlg,bg=SBORDER,padx=2,pady=2); border.pack(fill="both",expand=True)
        inner=tk.Frame(border,bg=SBG); inner.pack(fill="both",expand=True)
        tk.Label(inner,text="FileTagger with SQLite",bg=SBG,fg=STITLE,font=("Segoe UI",28,"bold")).pack(pady=(20,0))
        tk.Label(inner,text="📷 Photos  +  📄 PDFs  —  Tag, Browse, Export",
                 bg=SBG,fg=SSUB,font=("Segoe UI",11)).pack(pady=(4,0))
        tk.Label(inner,text=f"Build  {BUILD_DATE}",bg=SBG,fg=SDIM,font=("Segoe UI",13)).pack(pady=(6,0))
        tk.Label(inner,text=f"Photos:  {PHOTOS_ROOT}",bg=SBG,fg=SDIM,font=("Segoe UI",10)).pack(pady=(2,0))
        tk.Label(inner,text=f"PDFs:    {PDFS_ROOT}",bg=SBG,fg=SDIM,font=("Segoe UI",10)).pack(pady=(2,0))
        _db_disp = (_DB_PATH if _DB_PATH else _db_default_path())
        tk.Label(inner,text=f"DB:      {_db_disp}",bg=SBG,fg=SDIM,font=("Segoe UI",10)).pack(pady=(2,0))
        tk.Frame(inner,bg="#cccccc",height=1).pack(fill="x",padx=20,pady=(10,4))
        tk.Label(inner,text="Required libraries:",bg=SBG,fg=STITLE,font=("Segoe UI",9,"bold")).pack(pady=(2,2))
        libs=[("Pillow","pip install Pillow","Image loading, thumbnailing, placeholders"),
              ("PyMuPDF","pip install pymupdf","PDF page rendering  (optional — Photos only without it)"),
              ("tkinter","included with Python","GUI framework")]
        for lib,install,purpose in libs:
            row=tk.Frame(inner,bg=SBG); row.pack(fill="x",padx=24,pady=1)
            tk.Label(row,text=f"{lib:<14}",bg=SBG,fg=STITLE,font=("Courier New",9,"bold"),
                     anchor="w").pack(side="left")
            tk.Label(row,text=purpose,bg=SBG,fg=SDIM,font=("Segoe UI",9),anchor="w").pack(side="left")
        if not HAVE_FITZ:
            tk.Label(inner,text="⚠  PyMuPDF not installed — PDF mode unavailable",
                     bg=SBG,fg="#cc2222",font=("Segoe UI",9,"bold")).pack(pady=(8,0))
        tk.Button(inner,text="  Close  ",bg="#2E75B6",fg="white",font=("Segoe UI",10,"bold"),
                  relief="flat",padx=12,pady=4,cursor="hand2",command=dlg.destroy).pack(pady=14)

    def _show_db_status(self):
        """Show SQLite DB status — path, file size, table record counts."""
        SBORDER="#2d5a2d"; SBG="#f0f4f8"; STITLE="#1a3a1a"; SSUB="#2d5a2d"; SDIM="#4a6060"
        dlg = tk.Toplevel(self.win); dlg.title("Database Status")
        dlg.resizable(False, False); dlg.configure(bg=SBORDER)
        dlg.transient(self.win); dlg.grab_set()
        self._centre_window(dlg, 680, 480)
        border = tk.Frame(dlg, bg=SBORDER, padx=2, pady=2); border.pack(fill="both", expand=True)
        inner  = tk.Frame(border, bg=SBG);  inner.pack(fill="both", expand=True)

        tk.Label(inner, text="🗄  Database Status", bg=SBG, fg=STITLE,
                 font=("Segoe UI", 20, "bold")).pack(pady=(18, 4))

        # DB path and file size
        db_path = _DB_PATH if _DB_PATH else _db_default_path()
        try:    db_size = os.path.getsize(db_path)
        except: db_size = 0
        if db_size < 1_000_000:
            size_str = f"{db_size // 1024} KB"
        else:
            size_str = f"{db_size / 1_000_000:.1f} MB"

        path_frame = tk.Frame(inner, bg=SBG); path_frame.pack(fill="x", padx=20, pady=(0,4))
        tk.Label(path_frame, text=f"Path:  {db_path}", bg=SBG, fg=SDIM,
                 font=("Segoe UI", 11, "bold"), anchor="w", wraplength=630).pack(anchor="w")
        tk.Label(path_frame, text=f"Size:  {size_str}", bg=SBG, fg=SDIM,
                 font=("Segoe UI", 11, "bold"), anchor="w").pack(anchor="w")

        tk.Frame(inner, bg="#cccccc", height=1).pack(fill="x", padx=20, pady=(6, 4))

        # Table counts
        tables = [
            ("thumbnails",       "Cached thumbnails"),
            ("collections",      "Collections"),
            ("collection_items", "Collection items  (file entries)"),
            ("cull_list",        "Files marked for deletion"),
            ("folder_bookmarks", "Folder bookmarks"),
            ("file_metadata",    "File metadata / UUID records"),
            ("settings",         "Settings entries"),
        ]

        tbl_frame = tk.Frame(inner, bg=SBG); tbl_frame.pack(fill="x", padx=24, pady=4)
        if _db_conn is None:
            tk.Label(tbl_frame, text="⚠  Database not connected", bg=SBG, fg="#cc2222",
                     font=("Segoe UI", 11, "bold")).pack(pady=8)
        else:
            for tname, desc in tables:
                try:
                    count = _db_conn.execute(f"SELECT COUNT(*) FROM {tname}").fetchone()[0]
                except Exception:
                    count = "—"
                row = tk.Frame(tbl_frame, bg=SBG); row.pack(fill="x", pady=2)
                tk.Label(row, text=f"{tname}", bg=SBG, fg=STITLE,
                         font=("Courier New", 11, "bold"), width=20, anchor="w").pack(side="left")
                tk.Label(row, text=f"{count:>8}" if isinstance(count, int) else f"{'—':>8}",
                         bg=SBG, fg=SSUB, font=("Courier New", 11, "bold"),
                         width=9, anchor="e").pack(side="left")
                tk.Label(row, text=f"  {desc}", bg=SBG, fg=SDIM,
                         font=("Segoe UI", 11, "bold"), anchor="w").pack(side="left")

        tk.Frame(inner, bg="#cccccc", height=1).pack(fill="x", padx=20, pady=(6, 4))

        # DB connection state
        state = "Connected" if _db_conn is not None else "Not connected"
        state_col = SSUB if _db_conn is not None else "#cc2222"
        tk.Label(inner, text=f"Status:  {state}", bg=SBG, fg=state_col,
                 font=("Segoe UI", 11, "bold")).pack(pady=(0, 4))

        tk.Button(inner, text="  Close  ", bg=SBORDER, fg="white",
                  font=("Segoe UI", 11, "bold"), relief="flat",
                  padx=16, pady=8, cursor="hand2",
                  command=dlg.destroy).pack(pady=12)

    # ── Misc ──────────────────────────────────────────────────────────────────
    def _build_nav_float(self):
        """Navigation bar is now inline in the status bar — this method is a no-op."""
        pass

    def _toggle_theme(self):
        """Show theme selection menu under the Theme button."""
        # Find the Theme button to position the menu under it
        btn = self._toolbars["cache"].named_btns.get("Theme")
        if not btn: return
        menu = tk.Menu(self.win, tearoff=0)
        menu.add_command(label="☀  Light theme",
                         command=lambda: self._set_theme("light"))
        menu.add_command(label="🌙  Dark theme",
                         command=lambda: self._set_theme("dark"))
        try:
            x = btn.winfo_rootx()
            y = btn.winfo_rooty() + btn.winfo_height()
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _set_theme(self, new_theme):
        if new_theme == THEME:
            messagebox.showinfo("Theme", f"Already using {new_theme} theme.",
                                parent=self.win); return
        try:
            ini = os.path.join(os.path.dirname(os.path.abspath(__file__)), "FileTagger.ini")
            if os.path.exists(ini):
                with open(ini, 'r', encoding='utf-8') as f: lines = f.readlines()
            else:
                lines = []
            new_lines = []; in_disp = False
            for line in lines:
                if line.strip().lower() == '[display]': in_disp = True; new_lines.append(line); continue
                if in_disp and line.strip().lower().startswith('theme'):
                    new_lines.append(f'theme = {new_theme}\n'); in_disp = False; continue
                new_lines.append(line)
            # If theme key wasn't found, add it
            if not any('theme' in l.lower() for l in new_lines if '=' in l):
                new_lines.append(f'\n[display]\ntheme = {new_theme}\n')
            with open(ini, 'w', encoding='utf-8') as f: f.writelines(new_lines)
            messagebox.showinfo("Theme changed",
                f"Theme set to '{new_theme}'.\n\nRestart FileTagger to apply.",
                parent=self.win)
        except Exception as e:
            messagebox.showerror("Theme error", str(e), parent=self.win)

    def _toggle_recurse(self):
        """Recurse toggle — removed from UI, kept for compatibility."""
        self._recurse = not self._recurse
        if not self._in_tagged_view:
            self._load_folder(self.current_folder)

    def _toggle_auto_cache(self):
        self._auto_cache = not self._auto_cache
        if self._auto_cache: self.btn_autocache.config(text="Auto ON",  bg="#226622")
        else:                self.btn_autocache.config(text="Auto OFF", bg="#444444")

    def _on_frame_cfg(self, event):
        pass  # scroll region updated in _load_complete to avoid cascade loops

    def _on_canvas_cfg(self, event):
        # Pin grid_frame width to canvas width — prevents thumbnails overflowing right edge
        try:
            self.canvas.itemconfig(self._cw, width=event.width)
        except: pass
        # Only trigger reload when the canvas WIDGET size changes (window resize)
        new_size = (event.width, event.height)
        if new_size == getattr(self, '_last_canvas_size', None):
            return
        self._last_canvas_size = new_size
        if hasattr(self, '_resize_after_id') and self._resize_after_id:
            try: self.win.after_cancel(self._resize_after_id)
            except: pass
        self._resize_after_id = self.win.after(300, self._on_resize_settled)

    def _on_resize_settled(self):
        self._resize_after_id = None
        if getattr(self, '_loading', False): return
        if getattr(self, '_all_files', []):
            self._show_page()

    def _row_height_px(self):
        """Pixel height of one complete row including padding.
        Must match CELL_H = IMG_H + 70 used in _add_cell."""
        sz    = self._disp_size
        img_h = THUMB_IMG_H if sz == THUMB_SIZE else int(sz * THUMB_IMG_H / THUMB_SIZE)
        return img_h + 70 + THUMB_PAD

    def _snap_to_row(self):
        """Snap so the top of the viewport aligns to a row boundary."""
        try:
            bb = self.canvas.bbox("all")
            if not bb or bb[3] <= 0: return
            grid_h  = bb[3]
            row_h   = self._row_height_px()
            top_px  = self.canvas.yview()[0] * grid_h
            # Round to nearest row, accounting for grid top padding
            row_idx = round(max(0, top_px - GRID_TOP_PAD) / row_h)
            snapped = GRID_TOP_PAD + row_idx * row_h
            self.canvas.yview_moveto(snapped / grid_h)
        except: pass

    def _scroll_rows(self, direction):
        """Scroll by exactly one row. Always snaps to row boundary so
        repeated scrolling moves exactly one row each time."""
        try:
            self.canvas.update_idletasks()
            # Get scrollregion height
            sr = self.canvas.cget("scrollregion")
            if sr:
                parts = str(sr).split()
                grid_h = float(parts[3]) if len(parts) >= 4 else 0
            else:
                bb = self.canvas.bbox("all")
                grid_h = bb[3] if bb else 0
            if grid_h <= 0:
                if direction > 0: self._page_next()
                else:             self._page_prev()
                return
            ch    = self.canvas.winfo_height()
            row_h = self._row_height_px()
            ylo, yhi = self.canvas.yview()
            top_px = ylo * grid_h
            bot_px = yhi * grid_h
            # Snap top_px to nearest row boundary
            snapped_row = round(top_px / row_h)
            snapped_top = snapped_row * row_h
            # Now move exactly one row from the snapped position
            new_top = snapped_top + direction * row_h
            if new_top < 0:
                if self._page_num > 0:
                    self._page_num -= 1
                    self._show_page()
                    self.win.after(250, lambda: self.canvas.yview_moveto(1.0))
                return
            if new_top + ch > grid_h + 2:
                # Would scroll past bottom — try next page
                if direction > 0:
                    self._page_next()
                return
            self.canvas.yview_moveto(new_top / grid_h)
            self.win.after(30, self._update_visible_label)
        except Exception as ex:
            print(f"_scroll_rows error: {ex}")

    def _on_scroll(self, event):
        """Mouse wheel fallback — delegates to _scroll_rows."""
        if event.num == 4 or getattr(event, 'delta', 0) > 0:
            self._scroll_rows(-1)
        else:
            self._scroll_rows(1)

    def _check_load_more(self):
        pass  # all images loaded at once — no batch loading needed

    def _status(self, msg):
        try: self.status.config(text=msg)
        except: pass

    def _centre_window(self, win, w, h):
        self.win.update_idletasks()
        mx=self.win.winfo_rootx(); my=self.win.winfo_rooty()
        mw=self.win.winfo_width(); mh=self.win.winfo_height()
        x=mx+(mw-w)//2; y=my+(mh-h)//2
        win.geometry(f"{w}x{h}+{max(0,x)}+{max(0,y)}")

    def _on_close(self):
        # Warn if shadow has unsaved tags
        if self._shadow_active and self._shadow_tagged:
            resp = messagebox.askyesnocancel("Unsaved shadow tags",
                f"◈  Shadow has {len(self._shadow_tagged)} tagged files that have not been "
                f"saved as a collection.\n\nSave as collection before closing?", parent=self.win)
            if resp is None: return   # Cancel — don't close
            if resp:                  # Yes — fork then close
                self._shadow_fork_dialog()
                # After fork dialog, proceed to close
        self._load_gen+=1
        self._save_current_collection()
        # Flush any pending cull save
        if self._save_after_id_cull:
            try: self.win.after_cancel(self._save_after_id_cull)
            except: pass
            _write_cull_list(self.mode_cfg['root'], self._culled, self._culled_at)
        db_close()
        self.win.destroy()

    def run(self):
        self.win.mainloop()

# ── Structured rename dialog ──────────────────────────────────────────────────

def _show_structured_rename_dialog(parent, current_stem, ext, on_apply_cb, zoom_win=None):
    """Structured rename dialog — date/who/category/type/description.
    on_apply_cb(new_stem) called on Save. Dialog stays open after Save, ready for next file.
    Closes automatically when zoom_win is destroyed (if supplied)."""
    import datetime as _dt
    import re as _re

    cats = _load_ft_categories()
    who_list  = cats.get("who", [])
    cat_dict  = cats.get("categories", {})
    cat_names = list(cat_dict.keys())

    today = _dt.date.today()

    # ── Parse existing stem into fields ──────────────────────────────────────
    def _parse_stem(stem):
        parts = stem.split('_')
        date_str = ""; who = ""; cat = ""; typ = ""; desc = ""
        if parts and _re.match(r'^\d{4}-\d{2}-\d{2}$', parts[0]):
            date_str = parts[0]; parts = parts[1:]
        if parts and parts[0] in who_list:
            who = parts[0]; parts = parts[1:]
        if parts and parts[0] in cat_names:
            cat = parts[0]
            types = cat_dict[cat].get('types', []) if cat in cat_dict else []
            parts = parts[1:]
            if parts and parts[0] in types:
                typ = parts[0]; parts = parts[1:]
        desc = ' '.join(parts).replace('_', ' ')
        return date_str, who, cat, typ, desc

    init_date, init_who, init_cat, init_typ, init_desc = _parse_stem(current_stem)

    if init_date:
        try:
            _d = _dt.date.fromisoformat(init_date)
            init_dd = str(_d.day); init_mm = str(_d.month); init_yyyy = str(_d.year)
        except:
            init_dd = str(today.day); init_mm = str(today.month); init_yyyy = str(today.year)
    else:
        init_dd = str(today.day); init_mm = str(today.month); init_yyyy = str(today.year)

    # ── Colours — light dialog, dark text ────────────────────────────────────
    DBG   = "#f0f0f0"   # dialog background
    DFLD  = "#ffffff"   # field background
    DFG   = "#111111"   # primary text
    DDIM  = "#555555"   # label text
    DSAVE = "#1a6b2a"   # save button green
    DCAN  = "#555555"   # cancel button grey

    dlg = tk.Toplevel(parent)
    dlg.title("Structured Rename")
    dlg.configure(bg=DBG)
    dlg.resizable(False, False)
    # Non-modal — user can still navigate the zoom window while this is open
    dlg.lift()
    dlg.focus_force()
    try:
        sw = dlg.winfo_screenwidth(); sh = dlg.winfo_screenheight()
        dlg.geometry(f"440x550+{(sw-440)//2}+{(sh-550)//2}")
    except:
        dlg.geometry("440x550")

    PADX = 16

    def lbl(parent, text):
        tk.Label(parent, text=text, bg=DBG, fg=DDIM,
                 font=("Segoe UI", 8), anchor="w").pack(fill="x", padx=PADX, pady=(6,1))

    def field(parent, var, values=None):
        if values is not None:
            w = ttk.Combobox(parent, textvariable=var, values=values,
                             font=("Segoe UI", 10), width=20)
            w.pack(anchor="w", padx=PADX, pady=(0,2))
        else:
            w = tk.Entry(parent, textvariable=var, bg=DFLD, fg=DFG,
                         insertbackground=DFG, font=("Segoe UI", 10),
                         relief="solid", bd=1)
            w.pack(fill="x", padx=PADX, pady=(0,2))
        return w

    # ── Header ────────────────────────────────────────────────────────────────
    tk.Label(dlg, text="Structured Rename", bg=DBG, fg=DFG,
             font=("Segoe UI", 11, "bold")).pack(padx=PADX, pady=(12,2), anchor="w")

    # ── Before / After display ────────────────────────────────────────────────
    name_frame = tk.Frame(dlg, bg=DBG); name_frame.pack(fill="x", padx=PADX, pady=(0,8))
    tk.Label(name_frame, text="From:", bg=DBG, fg=DDIM,
             font=("Segoe UI", 8)).grid(row=0, column=0, sticky="w")
    from_var = tk.StringVar(value=current_stem + ext)
    tk.Label(name_frame, textvariable=from_var, bg=DBG, fg="#aa2200",
             font=("Segoe UI", 9, "bold"), wraplength=360, justify="left").grid(row=0, column=1, sticky="w", padx=(6,0))
    tk.Label(name_frame, text="To:", bg=DBG, fg=DDIM,
             font=("Segoe UI", 8)).grid(row=1, column=0, sticky="w")
    preview_var = tk.StringVar()
    tk.Label(name_frame, textvariable=preview_var, bg=DBG, fg="#1a6b2a",
             font=("Segoe UI", 9, "bold"), wraplength=360, justify="left").grid(row=1, column=1, sticky="w", padx=(6,0))

    tk.Frame(dlg, bg="#cccccc", height=1).pack(fill="x", padx=PADX, pady=(0,4))

    # ── Date ──────────────────────────────────────────────────────────────────
    lbl(dlg, "Date")
    date_frame = tk.Frame(dlg, bg=DBG); date_frame.pack(fill="x", padx=PADX)
    dd_var = tk.StringVar(value=init_dd)
    mm_var = tk.StringVar(value=init_mm)
    yyyy_var = tk.StringVar(value=init_yyyy)
    for var, label, vals, w in [
        (dd_var,   "Day",   [str(i) for i in range(1,32)], 4),
        (mm_var,   "Month", [str(i) for i in range(1,13)], 4),
        (yyyy_var, "Year",  [str(i) for i in range(2000, today.year+6)], 6),
    ]:
        f = tk.Frame(date_frame, bg=DBG); f.pack(side="left", padx=(0,8))
        tk.Label(f, text=label, bg=DBG, fg=DDIM, font=("Segoe UI",7)).pack(anchor="w")
        ttk.Combobox(f, textvariable=var, values=vals,
                     font=("Segoe UI",9), width=w).pack()
    def _today():
        dd_var.set(str(today.day)); mm_var.set(str(today.month)); yyyy_var.set(str(today.year))
    tk.Button(date_frame, text="Today", bg="#dddddd", fg=DFG,
              font=("Segoe UI", 8), relief="flat", padx=6,
              cursor="hand2", command=_today).pack(side="left", pady=(14,0))

    # ── Who ───────────────────────────────────────────────────────────────────
    lbl(dlg, "Who")
    who_var = tk.StringVar(value=init_who)
    field(dlg, who_var, who_list)

    # ── Category ──────────────────────────────────────────────────────────────
    lbl(dlg, "Category")
    cat_var = tk.StringVar(value=init_cat)
    field(dlg, cat_var, cat_names)

    # ── Type ──────────────────────────────────────────────────────────────────
    lbl(dlg, "Type")
    typ_var = tk.StringVar(value=init_typ)
    typ_cb = field(dlg, typ_var, [])

    def _on_cat_change(*_):
        cat = cat_var.get().strip()
        types = cat_dict.get(cat, {}).get('types', [])
        typ_cb['values'] = types
        if typ_var.get() not in types:
            typ_var.set(types[0] if types else "")
        try: _update_preview()
        except NameError: pass   # called before desc_var exists during init
    cat_var.trace_add("write", _on_cat_change)
    _on_cat_change()  # populate type list — _update_preview silently skipped until desc_var exists

    # ── Description ───────────────────────────────────────────────────────────
    lbl(dlg, "Description")
    desc_var = tk.StringVar(value=init_desc)
    desc_entry = field(dlg, desc_var)

    tk.Frame(dlg, bg="#cccccc", height=1).pack(fill="x", padx=PADX, pady=(10,0))

    # ── Build stem ────────────────────────────────────────────────────────────
    def _clean(s, maxlen=60):
        s = s.strip()[:maxlen]
        s = _re.sub(r'[\s/\\:*?"<>|]+', '_', s)
        return s.strip('_')

    def _build_stem():
        try:
            date_s = f"{int(yyyy_var.get()):04d}-{int(mm_var.get()):02d}-{int(dd_var.get()):02d}"
        except:
            date_s = ""
        parts = [p for p in [
            date_s,
            _clean(who_var.get(), 20),
            _clean(cat_var.get(), 20),
            _clean(typ_var.get(), 20),
            _clean(desc_var.get(), 60),
        ] if p]
        return '_'.join(parts)

    def _update_preview(*_):
        stem = _build_stem()
        preview_var.set((stem + ext) if stem else "(incomplete)")

    for v in (dd_var, mm_var, yyyy_var, who_var, cat_var, typ_var, desc_var):
        v.trace_add("write", _update_preview)
    _update_preview()  # now desc_var exists — safe to call

    # ── Buttons ───────────────────────────────────────────────────────────────
    bf = tk.Frame(dlg, bg=DBG); bf.pack(fill="x", padx=PADX, pady=10)

    def _clear_fields():
        """Reset all fields to today's date and empty strings, ready for next file."""
        dd_var.set(str(today.day)); mm_var.set(str(today.month)); yyyy_var.set(str(today.year))
        who_var.set(""); cat_var.set(""); typ_var.set(""); desc_var.set("")
        preview_var.set("(incomplete)")
        desc_entry.focus_set()

    def _save():
        stem = _build_stem()
        if not stem:
            messagebox.showwarning("Incomplete", "Please fill in at least a date.", parent=dlg)
            return
        on_apply_cb(stem)
        # Update From: to show what was just renamed, then clear for next file
        from_var.set(stem + ext)
        _clear_fields()

    tk.Button(bf, text="Save", bg=DSAVE, fg="white",
              font=("Segoe UI", 9, "bold"), relief="flat", padx=16, pady=4,
              cursor="hand2", command=_save).pack(side="left", padx=(0,8))
    tk.Button(bf, text="Close", bg=DCAN, fg="white",
              font=("Segoe UI", 9), relief="flat", padx=16, pady=4,
              cursor="hand2", command=dlg.destroy).pack(side="left")

    # Auto-close when the zoom window is destroyed
    if zoom_win is not None:
        def _on_zoom_destroy(event=None):
            try:
                if dlg.winfo_exists():
                    dlg.destroy()
            except: pass
        zoom_win.bind("<Destroy>", _on_zoom_destroy, add="+")

    def _update_file(new_path):
        """Called by zoom nav to update the dialog when the user moves to a new file."""
        if not dlg.winfo_exists(): return
        new_ext = os.path.splitext(os.path.basename(new_path))[1]
        new_stem = os.path.splitext(os.path.basename(new_path))[0]
        # Rebind ext so _build_stem / _save use correct extension
        nonlocal ext
        ext = new_ext
        from_var.set(new_stem + new_ext)
        _clear_fields()

    desc_entry.focus_set()
    # Centre on screen after tkinter has sized the window to fit content
    def _centre():
        dlg.update_idletasks()
        w = dlg.winfo_width(); h = dlg.winfo_height()
        sw = dlg.winfo_screenwidth(); sh = dlg.winfo_screenheight()
        dlg.geometry(f"+{(sw-w)//2}+{(sh-h)//2}")
    dlg.after(10, _centre)
    return _update_file


# ── First-run setup dialog ────────────────────────────────────────────────────

def _show_first_run_dialog(parent, on_complete_cb):
    """Shown on very first run — creates Projects.ini, FileTagger.ini and first project."""
    dlg = tk.Toplevel(parent)
    dlg.title("Welcome to FileTagger")
    dlg.configure(bg=BG3)
    dlg.grab_set()
    dlg.transient(parent)
    dlg.resizable(True, False)
    dlg.minsize(520, 0)

    script_dir = os.path.dirname(os.path.abspath(__file__))

    # ── Header ────────────────────────────────────────────────────────────────
    hdr = tk.Frame(dlg, bg="#2E4A7A"); hdr.pack(fill="x")
    tk.Label(hdr, text="🗂  Welcome to FileTagger",
             bg="#2E4A7A", fg="white",
             font=("Segoe UI", 14, "bold")).pack(padx=20, pady=12)
    tk.Label(hdr, text="Let's create your first project to get started.",
             bg="#2E4A7A", fg="#aaccff",
             font=("Segoe UI", 9)).pack(padx=20, pady=(0, 12))

    inner = tk.Frame(dlg, bg=BG3); inner.pack(fill="both", expand=True, padx=20, pady=12)

    def lbl(text, dim=False):
        tk.Label(inner, text=text, bg=BG3,
                 fg=TEXT_DIM if dim else TEXT_BRIGHT,
                 font=("Segoe UI", 9, "bold" if not dim else "normal"),
                 anchor="w").pack(fill="x", pady=(8, 1))

    def entry_row(var, browse_fn=None, browse_label="Browse…"):
        f = tk.Frame(inner, bg=BG3); f.pack(fill="x")
        e = tk.Entry(f, textvariable=var, bg=BG2, fg=TEXT_BRIGHT,
                     insertbackground=TEXT_BRIGHT,
                     font=("Segoe UI", 10), relief="flat",
                     highlightthickness=1, highlightbackground="#555")
        e.pack(side="left", fill="x", expand=True)
        if browse_fn:
            tk.Button(f, text=browse_label, bg=BG2, fg=TEXT_BRIGHT,
                      font=("Segoe UI", 8), relief="flat", padx=6,
                      cursor="hand2", command=browse_fn).pack(side="left", padx=(4, 0))
        return e

    from tkinter import filedialog as _fd3

    # Project name
    lbl("Project name")
    name_var = tk.StringVar(value="My Photos")
    entry_row(name_var)

    # Project location
    lbl("Create project folder in")
    loc_var = tk.StringVar(value=script_dir)
    def _browse_loc():
        d = _fd3.askdirectory(parent=dlg, title="Where to create the FTProj_ folder",
                              initialdir=loc_var.get())
        if d: loc_var.set(d)
    entry_row(loc_var, _browse_loc)

    # Preview
    preview_lbl = tk.Label(inner, text="", bg=BG3, fg="#888888",
                           font=("Segoe UI", 8), anchor="w")
    preview_lbl.pack(fill="x")

    def _update_preview(*_):
        n = name_var.get().strip()
        loc = loc_var.get().strip()
        if n and loc:
            preview_lbl.config(
                text=f"Will create: {os.path.join(loc, 'FTProj_' + n)}")
        else:
            preview_lbl.config(text="")
    name_var.trace_add("write", _update_preview)
    loc_var.trace_add("write", _update_preview)
    _update_preview()

    # Separator
    tk.Frame(inner, bg="#444444", height=1).pack(fill="x", pady=(12, 0))
    lbl("Root folders  (optional — can be added later)", dim=True)

    # Photos root
    lbl("Photos root")
    photos_var = tk.StringVar()
    photos_n_var = tk.StringVar(value="Photos")
    def _browse_photos():
        d = _fd3.askdirectory(parent=dlg, title="Select Photos root folder")
        if d:
            photos_var.set(d)
            photos_n_var.set(os.path.basename(d.rstrip('/\\')) or "Photos")
    entry_row(photos_var, _browse_photos)
    lbl("Photos display name", dim=True)
    entry_row(photos_n_var)

    # PDFs root
    lbl("PDFs root")
    pdfs_var = tk.StringVar()
    pdfs_n_var = tk.StringVar(value="PDFs")
    def _browse_pdfs():
        d = _fd3.askdirectory(parent=dlg, title="Select PDFs root folder")
        if d:
            pdfs_var.set(d)
            pdfs_n_var.set(os.path.basename(d.rstrip('/\\')) or "PDFs")
    entry_row(pdfs_var, _browse_pdfs)
    lbl("PDFs display name", dim=True)
    entry_row(pdfs_n_var)

    # Thumbnail size
    tk.Frame(inner, bg="#444444", height=1).pack(fill="x", pady=(12, 0))
    lbl("Thumbnail display size")
    sz_var = tk.StringVar(value="250")
    sz_frame = tk.Frame(inner, bg=BG3); sz_frame.pack(fill="x")
    for sz in ("150", "200", "250", "300", "350"):
        tk.Radiobutton(sz_frame, text=sz, variable=sz_var, value=sz,
                       bg=BG3, fg=TEXT_BRIGHT, selectcolor=BG2,
                       font=("Segoe UI", 9),
                       activebackground=BG3).pack(side="left", padx=4)

    # Error label
    err_lbl = tk.Label(inner, text="", bg=BG3, fg="#ff6666",
                       font=("Segoe UI", 9), anchor="w")
    err_lbl.pack(fill="x", pady=(8, 0))

    def _create():
        name = name_var.get().strip()
        loc  = loc_var.get().strip()
        if not name:
            err_lbl.config(text="Please enter a project name."); return
        if not loc or not os.path.isdir(loc):
            err_lbl.config(text="Please choose a valid location folder."); return

        photos_roots = []
        if photos_var.get().strip():
            photos_roots = [(photos_var.get().strip(),
                             photos_n_var.get().strip() or "Photos")]
        pdfs_roots = []
        if pdfs_var.get().strip():
            pdfs_roots = [(pdfs_var.get().strip(),
                           pdfs_n_var.get().strip() or "PDFs")]

        # Create project folder structure
        proj = _create_project(name, loc, photos_roots, pdfs_roots)

        # Write FileTagger.ini
        ini_path = os.path.join(script_dir, "FileTagger.ini")
        thumb_size = sz_var.get().strip() or "250"
        ini_content = (
            f"[FileTagger]\n"
            f"active_project = {name}\n\n"
            f"[display]\n"
            f"thumb_size = {thumb_size}\n"
            f"theme = light\n"
        )
        with open(ini_path, 'w', encoding='utf-8') as f:
            f.write(ini_content)

        dlg.destroy()
        on_complete_cb(name, proj)

    bf = tk.Frame(dlg, bg=BG3); bf.pack(pady=14)
    tk.Button(bf, text="  Create Project & Start  ", bg=GREEN, fg="white",
              font=("Segoe UI", 11, "bold"), relief="flat", padx=16, pady=8,
              cursor="hand2", command=_create).pack(side="left", padx=8)

    dlg.wait_window()


# ── Project system UI ─────────────────────────────────────────────────────────

def _show_project_selector(parent, on_select_cb):
    """Show project selector dialog. on_select_cb(name, proj) called on selection."""
    dlg = tk.Toplevel(parent)
    dlg.title("Select Project")
    dlg.configure(bg=BG3)
    dlg.grab_set()
    dlg.transient(parent)
    dlg.resizable(True, True)
    dlg.minsize(500, 300)

    tk.Label(dlg, text="Select a Project", bg=BG3, fg=TEXT_BRIGHT,
             font=("Segoe UI", 13, "bold")).pack(pady=(16, 4))
    tk.Label(dlg, text="Choose an existing project or create a new one.",
             bg=BG3, fg=TEXT_DIM, font=("Segoe UI", 9)).pack(pady=(0, 10))

    frame = tk.Frame(dlg, bg=BG3); frame.pack(fill="both", expand=True, padx=16, pady=4)
    sb = tk.Scrollbar(frame, orient="vertical")
    lb = tk.Listbox(frame, bg=BG2, fg=TEXT_BRIGHT, font=("Segoe UI", 10),
                    selectbackground="#2255aa", activestyle="none",
                    yscrollcommand=sb.set, height=10)
    sb.config(command=lb.yview)
    sb.pack(side="right", fill="y")
    lb.pack(side="left", fill="both", expand=True)

    projects = _load_projects()
    active = _get_active_project_name()
    proj_names = list(projects.keys())

    def _refresh_list():
        lb.delete(0, "end")
        for name in proj_names:
            proj = projects[name]
            path = proj['path']
            accessible = os.path.exists(path)
            db_exists   = os.path.exists(_project_db_path(path))
            status = "" if accessible else "  [OFFLINE]"
            marker = "★ " if name == active else "  "
            lb.insert("end", f"{marker}{name}{status}")
            if not accessible:
                lb.itemconfig("end", fg="#888888")
            elif name == active:
                lb.itemconfig("end", fg="#ffcc44")

    _refresh_list()
    if proj_names:
        # Select active project if present
        idx = proj_names.index(active) if active in proj_names else 0
        lb.selection_set(idx)
        lb.see(idx)

    # Status label for selected project
    lbl_status = tk.Label(dlg, text="", bg=BG3, fg=TEXT_DIM,
                          font=("Segoe UI", 8), wraplength=460, justify="left")
    lbl_status.pack(padx=16, anchor="w")

    def _on_listbox_select(e=None):
        sel = lb.curselection()
        if not sel: return
        name = proj_names[sel[0]]
        proj = projects[name]
        path = proj['path']
        roots_txt = ", ".join(n for _,n in proj.get('photos_roots',[])+proj.get('pdfs_roots',[]))
        db_path = _project_db_path(path)
        accessible = os.path.exists(path)
        lbl_status.config(
            text=f"Path: {path}\nDB: {'exists' if os.path.exists(db_path) else 'new'}  |  "
                 f"{'Online' if accessible else 'OFFLINE'}  |  Roots: {roots_txt or 'none set'}")

    lb.bind("<<ListboxSelect>>", _on_listbox_select)
    if proj_names: _on_listbox_select()

    bf = tk.Frame(dlg, bg=BG3); bf.pack(pady=10)

    def _open():
        sel = lb.curselection()
        if not sel:
            messagebox.showinfo("No selection", "Please select a project.", parent=dlg)
            return
        name = proj_names[sel[0]]
        proj = projects[name]
        if not os.path.exists(proj['path']):
            messagebox.showerror("Offline", f"Project folder not accessible:\n{proj['path']}", parent=dlg)
            return
        _set_active_project_name(name)
        dlg.destroy()
        on_select_cb(name, proj)

    def _new():
        _show_new_project_dialog(dlg, lambda name, proj: (
            projects.update({name: proj}),
            proj_names.append(name),
            _refresh_list(),
        ))

    def _discover():
        found = _discover_ftproj_folders()
        added = 0
        for f in found:
            if f['name'] not in projects:
                projects[f['name']] = {'path': f['path'], 'photos_roots': [], 'pdfs_roots': []}
                proj_names.append(f['name'])
                added += 1
        if added:
            _save_projects(projects)
            _refresh_list()
            messagebox.showinfo("Discover", f"Found {added} new project(s).", parent=dlg)
        else:
            messagebox.showinfo("Discover", "No new FTProj_ folders found.", parent=dlg)

    tk.Button(bf, text="  Open  ", bg=GREEN, fg="white",
              font=("Segoe UI", 10, "bold"), relief="flat", padx=12, pady=6,
              cursor="hand2", command=_open).pack(side="left", padx=6)
    tk.Button(bf, text="  New Project  ", bg="#225588", fg="white",
              font=("Segoe UI", 10), relief="flat", padx=12, pady=6,
              cursor="hand2", command=_new).pack(side="left", padx=6)
    tk.Button(bf, text="  Discover  ", bg="#446644", fg="white",
              font=("Segoe UI", 10), relief="flat", padx=12, pady=6,
              cursor="hand2", command=_discover).pack(side="left", padx=6)

    lb.bind("<Double-Button-1>", lambda e: _open())
    dlg.wait_window()


def _show_new_project_dialog(parent, on_create_cb):
    """Dialog to create a new project."""
    dlg = tk.Toplevel(parent)
    dlg.title("New Project")
    dlg.configure(bg=BG3)
    dlg.grab_set()
    dlg.transient(parent)
    dlg.resizable(True, False)

    def row(label, var, browse_fn=None):
        f = tk.Frame(dlg, bg=BG3); f.pack(fill="x", padx=16, pady=3)
        tk.Label(f, text=label, bg=BG3, fg=TEXT_BRIGHT,
                 font=("Segoe UI", 9), width=18, anchor="w").pack(side="left")
        e = tk.Entry(f, textvariable=var, bg=BG2, fg=TEXT_BRIGHT,
                     insertbackground=TEXT_BRIGHT, font=("Segoe UI", 9),
                     relief="flat", highlightthickness=1, highlightbackground="#555")
        e.pack(side="left", fill="x", expand=True)
        if browse_fn:
            tk.Button(f, text="Browse…", bg=BG2, fg=TEXT_BRIGHT,
                      font=("Segoe UI", 8), relief="flat", padx=6,
                      cursor="hand2", command=browse_fn).pack(side="left", padx=(4,0))
        return e

    tk.Label(dlg, text="Create New Project", bg=BG3, fg=TEXT_BRIGHT,
             font=("Segoe UI", 12, "bold")).pack(pady=(16, 8))

    name_var    = tk.StringVar()
    loc_var     = tk.StringVar(value=os.path.dirname(os.path.abspath(__file__)))
    photos_var  = tk.StringVar()
    photos_n_var= tk.StringVar(value="Photos")
    pdfs_var    = tk.StringVar()
    pdfs_n_var  = tk.StringVar(value="PDFs")

    row("Project name:", name_var)

    from tkinter import filedialog as _fd2
    def _browse_loc():
        d = _fd2.askdirectory(parent=dlg, title="Where to create the FTProj_ folder")
        if d: loc_var.set(d)
    def _browse_photos():
        d = _fd2.askdirectory(parent=dlg, title="Photos root folder")
        if d: photos_var.set(d); photos_n_var.set(os.path.basename(d.rstrip('/\\')) or "Photos")
    def _browse_pdfs():
        d = _fd2.askdirectory(parent=dlg, title="PDFs root folder")
        if d: pdfs_var.set(d); pdfs_n_var.set(os.path.basename(d.rstrip('/\\')) or "PDFs")

    row("Create in folder:", loc_var, _browse_loc)

    tk.Label(dlg, text="Roots (optional — can be added later):",
             bg=BG3, fg=TEXT_DIM, font=("Segoe UI", 8)).pack(anchor="w", padx=16, pady=(8,0))
    row("Photos root:", photos_var, _browse_photos)
    row("Photos name:", photos_n_var)
    row("PDFs root:",   pdfs_var,   _browse_pdfs)
    row("PDFs name:",   pdfs_n_var)

    lbl_preview = tk.Label(dlg, text="", bg=BG3, fg="#888888",
                           font=("Segoe UI", 8), wraplength=440)
    lbl_preview.pack(padx=16, pady=(4,0), anchor="w")

    def _update_preview(*_):
        n = name_var.get().strip()
        loc = loc_var.get().strip()
        if n and loc:
            lbl_preview.config(text=f"Will create: {os.path.join(loc, 'FTProj_' + n)}")
        else:
            lbl_preview.config(text="")
    name_var.trace_add("write", _update_preview)
    loc_var.trace_add("write", _update_preview)

    def _create():
        name = name_var.get().strip()
        loc  = loc_var.get().strip()
        if not name:
            messagebox.showerror("Name required", "Please enter a project name.", parent=dlg); return
        if not loc or not os.path.isdir(loc):
            messagebox.showerror("Invalid location", "Please choose a valid folder.", parent=dlg); return
        photos_roots = [(photos_var.get().strip(), photos_n_var.get().strip())] if photos_var.get().strip() else []
        pdfs_roots   = [(pdfs_var.get().strip(),   pdfs_n_var.get().strip())]   if pdfs_var.get().strip()   else []
        proj = _create_project(name, loc, photos_roots, pdfs_roots)
        messagebox.showinfo("Created",
            f"Project '{name}' created at:\n{proj['path']}", parent=dlg)
        dlg.destroy()
        on_create_cb(name, proj)

    bf = tk.Frame(dlg, bg=BG3); bf.pack(pady=12)
    tk.Button(bf, text="  Create  ", bg=GREEN, fg="white",
              font=("Segoe UI", 10, "bold"), relief="flat", padx=12, pady=6,
              cursor="hand2", command=_create).pack(side="left", padx=6)
    tk.Button(bf, text="  Cancel  ", bg="#444", fg=TEXT_BRIGHT,
              font=("Segoe UI", 10), relief="flat", padx=12, pady=6,
              cursor="hand2", command=dlg.destroy).pack(side="left", padx=6)

    dlg.wait_window()


# ── Entry point ────────────────────────────────────────────────────────────────
def _show_config_dialog(root_win):
    """Show settings dialog on startup when no ini found. Returns True if saved."""
    result = [False]
    _settings_dialog(root_win, on_save_cb=lambda: result.__setitem__(0, True), startup=True)
    return result[0]


def _settings_dialog(parent_win, on_save_cb=None, startup=False):
    """Full settings dialog — root folders, display, processing paths.
    Called at startup (no ini) and from Settings toolbar button.
    """
    import tkinter.filedialog as fd
    import tkinter.ttk as _ttk

    dlg = tk.Toplevel(parent_win)
    dlg.title("FileTagger — Settings")
    dlg.configure(bg=BG3)
    dlg.resizable(True, True)
    sw = parent_win.winfo_screenwidth(); sh = parent_win.winfo_screenheight()
    w, h = 680, 620
    dlg.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")
    if not startup:
        dlg.grab_set()
    dlg.transient(parent_win)
    dlg.lift()
    dlg.focus_force()

    # ── Title ─────────────────────────────────────────────────────────────────
    if startup:
        tk.Label(dlg, text="Welcome to FileTagger — First-Time Setup",
                 bg=BG3, fg=TEXT_BRIGHT, font=("Segoe UI",13,"bold")).pack(pady=(16,2))
        tk.Label(dlg, text="Configure your root folders and preferences below, then click Save.  (Scroll down for all options.)",
                 bg=BG3, fg=TEXT_DIM, font=("Segoe UI",10)).pack(pady=(0,8))
    else:
        tk.Label(dlg, text="⚙  Settings", bg=BG3, fg=TEXT_BRIGHT,
                 font=("Segoe UI",13,"bold")).pack(pady=(16,8))

    # ── Scrollable content ────────────────────────────────────────────────────
    outer = tk.Frame(dlg, bg=BG3); outer.pack(fill="both", expand=True, padx=16)
    canvas_s = tk.Canvas(outer, bg=BG3, highlightthickness=0)
    vsb = tk.Scrollbar(outer, orient="vertical", command=canvas_s.yview)
    canvas_s.configure(yscrollcommand=vsb.set)
    vsb.pack(side="right", fill="y")
    canvas_s.pack(side="left", fill="both", expand=True)
    inner = tk.Frame(canvas_s, bg=BG3)
    canvas_s.create_window((0,0), window=inner, anchor="nw")
    inner.bind("<Configure>", lambda e: canvas_s.configure(
        scrollregion=canvas_s.bbox("all")))

    def section(text):
        tk.Frame(inner, bg=HOVER_BD, height=1).pack(fill="x", pady=(12,4))
        tk.Label(inner, text=text, bg=BG3, fg=ACCENT,
                 font=("Segoe UI",10,"bold")).pack(anchor="w", padx=4, pady=(0,4))

    def path_row(parent, label, var, is_folder=True):
        """A labelled path entry with Browse button. Returns the entry widget."""
        row = tk.Frame(parent, bg=BG3); row.pack(fill="x", pady=3)
        tk.Label(row, text=label, bg=BG3, fg=TEXT_BRIGHT,
                 font=("Segoe UI",9), width=18, anchor="w").pack(side="left")
        ent = tk.Entry(row, textvariable=var, bg=BG2, fg=TEXT_BRIGHT,
                       insertbackground=TEXT_BRIGHT, relief="flat",
                       highlightthickness=1, highlightbackground=HOVER_BD,
                       font=("Segoe UI",9))
        ent.pack(side="left", fill="x", expand=True, padx=(0,4))
        def _browse():
            cur = var.get().strip()
            init = cur if os.path.isdir(cur) else os.path.expanduser("~")
            if is_folder:
                p = fd.askdirectory(title=f"Select {label}", initialdir=init, parent=dlg)
            else:
                p = fd.askopenfilename(title=f"Select {label}", initialdir=init, parent=dlg)
            if p: var.set(os.path.normpath(p))
        tk.Button(row, text="Browse…", bg=BG2, fg=TEXT_BRIGHT, font=("Segoe UI",8),
                  relief="flat", padx=6, cursor="hand2", command=_browse).pack(side="left")
        return ent

    # ── Database ──────────────────────────────────────────────────────────────
    section("🗄  Database")
    db_var = tk.StringVar(value=_DB_PATH if _DB_PATH else _db_default_path())
    db_row = tk.Frame(inner, bg=BG3); db_row.pack(fill="x", pady=3)
    tk.Label(db_row, text="DB path:", bg=BG3, fg=TEXT_BRIGHT,
             font=("Segoe UI",9), width=18, anchor="w").pack(side="left")
    db_ent = tk.Entry(db_row, textvariable=db_var, bg=BG2, fg=TEXT_BRIGHT,
                      insertbackground=TEXT_BRIGHT, relief="flat",
                      highlightthickness=1, highlightbackground=HOVER_BD,
                      font=("Segoe UI",9))
    db_ent.pack(side="left", fill="x", expand=True, padx=(0,4))
    def _browse_db():
        import tkinter.filedialog as _fd2
        cur = db_var.get().strip()
        init_dir = os.path.dirname(cur) if cur else os.path.expanduser("~")
        p = _fd2.asksaveasfilename(
            title="Select or create database file",
            initialdir=init_dir,
            initialfile=os.path.basename(cur) if cur else "filetagger.db",
            defaultextension=".db",
            filetypes=[("SQLite database", "*.db"), ("All files", "*.*")],
            parent=dlg)
        if p: db_var.set(os.path.normpath(p))
    tk.Button(db_row, text="Browse…", bg=BG2, fg=TEXT_BRIGHT, font=("Segoe UI",8),
              relief="flat", padx=6, cursor="hand2", command=_browse_db).pack(side="left")
    tk.Label(inner, text="Change to switch working context (thumbnails, collections, cull list).",
             bg=BG3, fg=TEXT_DIM, font=("Segoe UI",8)).pack(anchor="w", padx=4)

    # ── Photos roots ──────────────────────────────────────────────────────────
    section("📷  Photos Roots  (one per line:  path : Display Name)")
    photo_roots_var = tk.StringVar(value="\n".join(
        f"{p} : {n}" for p, n in PHOTOS_ROOTS) if PHOTOS_ROOTS else "")
    ph_frame = tk.Frame(inner, bg=BG3); ph_frame.pack(fill="x", pady=3)
    ph_text = tk.Text(ph_frame, height=4, bg=BG2, fg=TEXT_BRIGHT,
                      insertbackground=TEXT_BRIGHT, relief="flat",
                      highlightthickness=1, highlightbackground=HOVER_BD,
                      font=("Segoe UI",9), wrap="none")
    ph_text.insert("1.0", photo_roots_var.get())
    ph_text.pack(side="left", fill="x", expand=True, padx=(0,4))
    def _browse_photo():
        p = fd.askdirectory(title="Add Photos root folder", parent=dlg,
                            initialdir=os.path.expanduser("~"))
        if p:
            name = os.path.basename(os.path.normpath(p))
            cur = ph_text.get("1.0","end").strip()
            ph_text.insert("end", ("\n" if cur else "") + f"{os.path.normpath(p)} : {name}")
    tk.Button(ph_frame, text="Add…", bg=BG2, fg=TEXT_BRIGHT, font=("Segoe UI",8),
              relief="flat", padx=6, cursor="hand2", command=_browse_photo).pack(side="left", anchor="n")

    # ── PDFs roots ────────────────────────────────────────────────────────────
    section("📄  PDFs Roots  (one per line:  path : Display Name)")
    pdf_frame = tk.Frame(inner, bg=BG3); pdf_frame.pack(fill="x", pady=3)
    pdf_text = tk.Text(pdf_frame, height=4, bg=BG2, fg=TEXT_BRIGHT,
                       insertbackground=TEXT_BRIGHT, relief="flat",
                       highlightthickness=1, highlightbackground=HOVER_BD,
                       font=("Segoe UI",9), wrap="none")
    pdf_text.insert("1.0", "\n".join(f"{p} : {n}" for p, n in PDFS_ROOTS) if PDFS_ROOTS else "")
    pdf_text.pack(side="left", fill="x", expand=True, padx=(0,4))
    def _browse_pdf():
        p = fd.askdirectory(title="Add PDFs root folder", parent=dlg,
                            initialdir=os.path.expanduser("~"))
        if p:
            name = os.path.basename(os.path.normpath(p))
            cur = pdf_text.get("1.0","end").strip()
            pdf_text.insert("end", ("\n" if cur else "") + f"{os.path.normpath(p)} : {name}")
    tk.Button(pdf_frame, text="Add…", bg=BG2, fg=TEXT_BRIGHT, font=("Segoe UI",8),
              relief="flat", padx=6, cursor="hand2", command=_browse_pdf).pack(side="left", anchor="n")

    # ── MGEN executable ───────────────────────────────────────────────────────
    section("⚙  MGEN Executable  (optional)")
    mgen_var = tk.StringVar(value=PROC_MGEN_EXE)
    path_row(inner, "MGEN executable:", mgen_var, is_folder=False)

    # ── Display ───────────────────────────────────────────────────────────────
    section("🎨  Display")
    disp_row = tk.Frame(inner, bg=BG3); disp_row.pack(fill="x", pady=3)
    tk.Label(disp_row, text="Theme:", bg=BG3, fg=TEXT_BRIGHT,
             font=("Segoe UI",9), width=18, anchor="w").pack(side="left")
    theme_var = tk.StringVar(value=THEME)
    for t in ("dark", "light"):
        tk.Radiobutton(disp_row, text=t.capitalize(), variable=theme_var, value=t,
                       bg=BG3, fg=TEXT_BRIGHT, selectcolor=BG2,
                       activebackground=BG3, font=("Segoe UI",9)
                       ).pack(side="left", padx=8)

    sz_row = tk.Frame(inner, bg=BG3); sz_row.pack(fill="x", pady=3)
    tk.Label(sz_row, text="Thumbnail size:", bg=BG3, fg=TEXT_BRIGHT,
             font=("Segoe UI",9), width=18, anchor="w").pack(side="left")
    sz_var = tk.StringVar(value=str(THUMB_SIZE))
    tk.Spinbox(sz_row, from_=100, to=500, increment=50, textvariable=sz_var,
               width=6, bg=BG2, fg=TEXT_BRIGHT, buttonbackground=BG2,
               font=("Segoe UI",9)).pack(side="left")
    tk.Label(sz_row, text="px", bg=BG3, fg=TEXT_DIM,
             font=("Segoe UI",9)).pack(side="left", padx=4)

    # ── Processing (optional) ─────────────────────────────────────────────────
    section("⚙  Processing  (optional)")
    temp_var = tk.StringVar(value=PROC_TEMP_FOLDER)
    path_row(inner, "Temp folder:", temp_var, is_folder=True)


    # ── Buttons ───────────────────────────────────────────────────────────────
    bf = tk.Frame(dlg, bg=BG3); bf.pack(pady=12)

    def on_save():
        global PHOTOS_ROOT, PDFS_ROOT, PHOTOS_ROOTS, PDFS_ROOTS
        global THUMB_SIZE, PROC_TEMP_FOLDER, PROC_MGEN_EXE

        # Parse roots from text widgets
        new_photo_roots = _parse_roots_from_text(ph_text.get("1.0","end"))
        new_pdf_roots   = _parse_roots_from_text(pdf_text.get("1.0","end"))

        if not new_photo_roots and not new_pdf_roots:
            tk.messagebox.showwarning("No roots set",
                "Please set at least one Photos or PDFs root folder.", parent=dlg)
            return

        PHOTOS_ROOTS = new_photo_roots
        PDFS_ROOTS   = new_pdf_roots
        PHOTOS_ROOT  = PHOTOS_ROOTS[0][0] if PHOTOS_ROOTS else ""
        PDFS_ROOT    = PDFS_ROOTS[0][0]   if PDFS_ROOTS   else ""

        new_theme = theme_var.get()
        try: THUMB_SIZE = max(100, min(500, int(sz_var.get())))
        except: pass
        PROC_TEMP_FOLDER = temp_var.get().strip()
        PROC_MGEN_EXE    = mgen_var.get().strip()

        # Write ini
        ini = os.path.join(os.path.dirname(os.path.abspath(__file__)), "FileTagger.ini")
        try:
            lines = []
            if PHOTOS_ROOTS:
                lines.append("[photos]")
                for p, n in PHOTOS_ROOTS:
                    lines.append(f"root = {p} : {n}")
                lines.append("")
            if PDFS_ROOTS:
                lines.append("[pdfs]")
                for p, n in PDFS_ROOTS:
                    lines.append(f"root = {p} : {n}")
                lines.append("")
            lines += [
                "[display]",
                f"thumb_size = {THUMB_SIZE}",
                f"theme      = {new_theme}",
                "",
            ]
            if PROC_TEMP_FOLDER or PROC_MGEN_EXE:
                lines.append("[processing]")
                if PROC_TEMP_FOLDER: lines.append(f"temp_folder = {PROC_TEMP_FOLDER}")
                if PROC_MGEN_EXE:   lines.append(f"mgen_exe    = {PROC_MGEN_EXE}")
                lines.append("")
            new_db_path = db_var.get().strip() or _db_default_path()
            lines.append("[FileTagger]")
            lines.append(f"database = {new_db_path}")
            lines.append("")
            with open(ini, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        except Exception as e:
            tk.messagebox.showerror("Could not save settings", str(e), parent=dlg)
            return

        global _DB_PATH
        _DB_PATH = db_var.get().strip()
        theme_changed = (new_theme != THEME)
        _apply_theme(new_theme)

        dlg.destroy()
        if on_save_cb: on_save_cb()
        if theme_changed and not startup:
            tk.messagebox.showinfo("Theme changed",
                "Theme will fully apply on next restart.", parent=parent_win)

    tk.Button(bf, text="  Save  ", bg=GREEN, fg="white",
              font=("Segoe UI",10,"bold"), relief="flat", padx=14, pady=6,
              cursor="hand2", command=on_save).pack(side="left", padx=8)
    tk.Button(bf, text="  Cancel  ", bg=BG2, fg=TEXT_BRIGHT,
              font=("Segoe UI",10), relief="flat", padx=14, pady=6,
              cursor="hand2", command=dlg.destroy).pack(side="left", padx=8)
    # wait_window only when called from toolbar (not startup — startup uses callback)


def _parse_roots_from_text(text):
    """Parse multi-line text of 'path : name' entries into list of (path, name) tuples."""
    roots = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'): continue
        # Split on last colon that's not a Windows drive letter
        idx = line.rfind(':')
        while idx > 1:
            candidate_path = line[:idx].strip()
            candidate_name = line[idx+1:].strip()
            if candidate_name and len(candidate_path) > 2:
                roots.append((candidate_path, candidate_name[:30]))
                break
            idx = line.rfind(':', 0, idx)
        else:
            # No name separator — use folder basename
            path = line.strip()
            if path:
                name = os.path.basename(path.rstrip('/\\')) or path
                roots.append((path, name))
    return roots


if __name__ == "__main__":
    _root = tk.Tk()
    _root.withdraw()
    try:
        _splash = _show_startup_splash(_root)
        def _launch():
            try: _splash.destroy()
            except: pass
            _ini = os.path.join(os.path.dirname(os.path.abspath(__file__)), "FileTagger.ini")
            _projects = _load_projects()

            def _do_launch():
                _load_config()
                db_open()
                for _r in (PHOTOS_ROOT, PDFS_ROOT):
                    if _r: _migrate_txt_to_db(_r)
                _root.withdraw()
                try:
                    FileTagger(_root)
                except Exception as _e:
                    import traceback
                    tk.messagebox.showerror("Startup Error", traceback.format_exc(), parent=_root)

            # No Projects.ini or no active project → show project selector
            _active = _get_active_project_name()
            if _projects and _active and _active in _projects:
                # Normal startup with known project
                try:
                    ok, miss_req, miss_opt = _check_libraries()
                    if miss_req or miss_opt:
                        _show_library_warning(_root, miss_req, miss_opt)
                    if not ok: return
                    db_open()
                    for _r in (PHOTOS_ROOT, PDFS_ROOT):
                        if _r: _migrate_txt_to_db(_r)
                    FileTagger(_root)
                except Exception as _e:
                    import traceback
                    tk.messagebox.showerror("Startup Error", traceback.format_exc(), parent=_root)
            elif _projects:
                # Projects exist but none active — show selector
                _root.deiconify()
                _root.geometry("1x1+0+0"); _root.update()
                def _on_proj_select(name, proj):
                    _activate_project(name, proj)
                    _do_launch()
                _show_project_selector(_root, _on_proj_select)
            elif not os.path.exists(_ini):
                # Truly first run — show first-run project creation dialog
                _root.deiconify()
                _root.geometry("1x1+0+0"); _root.update()
                def _on_first_run(name, proj):
                    _activate_project(name, proj)
                    _load_config()
                    _do_launch()
                _show_first_run_dialog(_root, _on_first_run)
            else:
                # Has ini but no projects — legacy mode, launch directly
                try:
                    ok, miss_req, miss_opt = _check_libraries()
                    if miss_req or miss_opt:
                        _show_library_warning(_root, miss_req, miss_opt)
                    if not ok: return
                    db_open()
                    for _r in (PHOTOS_ROOT, PDFS_ROOT):
                        if _r: _migrate_txt_to_db(_r)
                    FileTagger(_root)
                except Exception as _e:
                    import traceback
                    tk.messagebox.showerror("Startup Error", traceback.format_exc(), parent=_root)
        _root.after(1800, _launch)
        _root.mainloop()
    except KeyboardInterrupt:
        pass

# ── Sample FileTagger.ini ──────────────────────────────────────────────────────
# [photos]
# root = S:\Photos : NAS Photos
# root = C:\Users\Ian\Pictures : Local Pictures
# root = C:\Users\Ian\Desktop\Apple Folders : Apple Downloads
#
# [pdfs]
# root = S:\Documents : NAS Documents
# root = C:\Users\Ian\Downloads : Downloaded PDFs
#
# [display]
# thumb_size = 250
# theme      = light        # dark or light
#
# [processing]
# temp_folder = C:\Tools\FT_Temp
# mgen_exe    = C:\Tools\MGEN\mgen.py
