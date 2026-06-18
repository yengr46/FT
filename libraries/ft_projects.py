"""
ft_projects.py — project and configuration helpers for FileTagger.

Root folder model (new, from June 2026):
  Each project has a flat, ordered list of root paths stored as root_0, root_1, ...
  in Projects.ini.  There are no longer typed keys (photos / videos / pdfs).
  Mode (Photos / Videos / Documents / All) is a display filter applied after
  the tree has loaded — it does not determine which root is browsed.

Migration:
  On first open of a file that still has the old photos/videos/pdfs keys,
  migrate_project_file() converts it in-place to root_N format.

Public API:
  load_projects(script_file)          -> {name: {path, roots: [(path,label)]}}
  save_projects(script_file, projects)
  migrate_project_file(script_file)   -> True if migration was performed
  create_project(script_file, name, proj_path, roots=None)
  get_active_project_name(script_file)
  set_active_project_name(script_file, name)
  project_db_path(proj_path)
  project_sheets_dir(proj_path)
  project_reports_dir(proj_path)
  discover_ftproj_folders(script_file)
  parse_root_line(text)               -> (path, label) | None
  parse_roots_from_text(text)         -> [(path, label)]
"""

from __future__ import annotations

import configparser
import os
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _script_dir(script_file=None):
    if script_file:
        return os.path.dirname(os.path.abspath(script_file))
    return os.getcwd()


def projects_ini_path(script_file=None):
    return os.path.join(_script_dir(script_file), "Projects.ini")


def filetagger_ini_path(script_file=None):
    return os.path.join(_script_dir(script_file), "FileTagger.ini")


# ---------------------------------------------------------------------------
# Root-line parsing (shared)
# ---------------------------------------------------------------------------

def parse_root_line(text):
    """Parse 'path : label' -> (path, label), or just 'path' -> (path, basename).
    Returns None for empty/blank input.
    """
    text = (text or "").strip()
    if not text:
        return None
    # Walk backward through colons to find a valid path:label split.
    # Skip drive-letter colons (position 1).
    idx = text.rfind(":")
    while idx > 1:
        candidate_path = text[:idx].strip()
        candidate_label = text[idx + 1:].strip()
        if candidate_path and candidate_label and len(candidate_path) > 2:
            return (candidate_path, candidate_label[:60])
        idx = text.rfind(":", 0, idx)
    # No label found — use folder name as label
    return (text, os.path.basename(text.rstrip("/\\")) or text)


def parse_roots_from_text(text):
    """Parse one or more 'path : label' lines into a list of (path, label) tuples."""
    roots = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parsed = parse_root_line(line)
        if parsed:
            roots.append(parsed)
    return roots


# ---------------------------------------------------------------------------
# Low-level file reader
# ---------------------------------------------------------------------------

def _read_ini_raw(ini_path):
    """Read Projects.ini, return {section: {key: [values]}} preserving multi-values."""
    result = {}
    current = None
    if not os.path.exists(ini_path):
        return result
    try:
        with open(ini_path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or line.startswith(";"):
                    continue
                if line.startswith("[") and line.endswith("]"):
                    current = line[1:-1].strip()
                    result.setdefault(current, {})
                    continue
                if current is None or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip().lower()
                val = val.strip()
                result[current].setdefault(key, []).append(val)
    except Exception as e:
        print(f"ft_projects._read_ini_raw error: {e}")
    return result


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def needs_migration(script_file=None):
    """Return True if Projects.ini contains any old-format typed root keys."""
    raw = _read_ini_raw(projects_ini_path(script_file))
    for keys in raw.values():
        if any(k in keys for k in ("photos", "videos", "pdfs")):
            return True
    return False


def migrate_project_file(script_file=None):
    """Convert old photos/videos/pdfs keys to root_N keys in Projects.ini.

    Returns True if migration was performed, False if already up to date.
    """
    p = projects_ini_path(script_file)
    if not os.path.exists(p):
        return False
    if not needs_migration(script_file):
        return False
    # load_projects() reads both formats; save_projects() writes root_N only
    projects = load_projects(script_file)
    save_projects(script_file, projects)
    print(f"ft_projects: migrated Projects.ini to root_N format ({p})")
    return True


# ---------------------------------------------------------------------------
# load / save
# ---------------------------------------------------------------------------

def load_projects(script_file=None):
    """Parse Projects.ini. Understands both root_N and legacy photos/videos/pdfs formats.

    Returns {name: {path: str, roots: [(path, label), ...]}}
    """
    p = projects_ini_path(script_file)
    projects = {}
    if not os.path.exists(p):
        return projects

    raw = _read_ini_raw(p)

    for section, keys in raw.items():
        proj_path = (keys.get("path", [""])[0] or "").strip()
        if not proj_path:
            continue

        roots = []

        # New format: root_0, root_1, ...
        idx = 0
        while True:
            key = f"root_{idx}"
            if key not in keys:
                break
            for val in keys[key]:
                parsed = parse_root_line(val)
                if parsed:
                    roots.append(parsed)
            idx += 1

        # Legacy format fallback: photos / videos / pdfs
        if not roots:
            for legacy_key in ("photos", "videos", "pdfs"):
                for val in keys.get(legacy_key, []):
                    for entry in parse_roots_from_text(val):
                        if entry not in roots:
                            roots.append(entry)

        projects[section] = {
            "path":  proj_path,
            "roots": roots,
        }

    return projects


def save_projects(script_file=None, projects=None):
    """Write projects dict to Projects.ini using the new root_N format."""
    if projects is None:
        projects = {}
    p = projects_ini_path(script_file)
    lines = []
    for name, proj in projects.items():
        lines.append(f"[{name}]")
        lines.append(f"path    = {proj['path']}")
        for i, (root_path, label) in enumerate(proj.get("roots", [])):
            lines.append(f"root_{i} = {root_path} : {label}")
        lines.append("")
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception as e:
        print(f"ft_projects.save_projects error: {e}")


# ---------------------------------------------------------------------------
# Project folder helpers
# ---------------------------------------------------------------------------

def project_db_path(proj_path):
    return os.path.join(proj_path, "FileTagger.db")


def project_sheets_dir(proj_path):
    d = os.path.join(proj_path, "ContactSheets")
    os.makedirs(d, exist_ok=True)
    return d


def project_reports_dir(proj_path):
    d = os.path.join(proj_path, "Reports")
    os.makedirs(d, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Active project (FileTagger.ini)
# ---------------------------------------------------------------------------

def get_active_project_name(script_file=None):
    ini = filetagger_ini_path(script_file)
    if not os.path.exists(ini):
        return ""
    cfg = configparser.ConfigParser(strict=False)
    cfg.read(ini, encoding="utf-8")
    return cfg.get("FileTagger", "active_project", fallback="").strip()


def set_active_project_name(script_file=None, name=""):
    ini = filetagger_ini_path(script_file)
    cfg = configparser.ConfigParser(strict=False)
    if os.path.exists(ini):
        cfg.read(ini, encoding="utf-8")
    if not cfg.has_section("FileTagger"):
        cfg.add_section("FileTagger")
    cfg.set("FileTagger", "active_project", name)
    try:
        with open(ini, "w", encoding="utf-8") as f:
            cfg.write(f)
    except Exception as e:
        print(f"ft_projects.set_active_project_name error: {e}")


# ---------------------------------------------------------------------------
# Project creation
# ---------------------------------------------------------------------------

def create_project(script_file=None, name="", proj_path="", roots=None):
    """Create a new FTProj_ folder structure and register it in Projects.ini."""
    folder_name = f"FTProj_{name}"
    full_path = os.path.join(proj_path, folder_name)
    os.makedirs(full_path, exist_ok=True)
    os.makedirs(os.path.join(full_path, "ContactSheets"), exist_ok=True)
    os.makedirs(os.path.join(full_path, "Reports"), exist_ok=True)
    proj = {
        "path":  full_path,
        "roots": roots or [],
    }
    projects = load_projects(script_file)
    projects[name] = proj
    save_projects(script_file, projects)
    return proj


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover_ftproj_folders(script_file=None):
    """Scan drives and common locations for FTProj_ folders."""
    found = []
    script_dir = _script_dir(script_file)
    search_roots = [script_dir, os.path.dirname(script_dir)]
    if os.name == "nt":
        import string
        for letter in string.ascii_uppercase:
            d = f"{letter}:\\"
            if os.path.exists(d):
                search_roots.append(d)
    seen = set()
    for search_root in search_roots:
        if not search_root or search_root in seen:
            continue
        seen.add(search_root)
        try:
            for entry in os.scandir(search_root):
                if entry.is_dir() and entry.name.startswith("FTProj_"):
                    db = os.path.join(entry.path, "FileTagger.db")
                    found.append({
                        "name":   entry.name[7:],
                        "path":   entry.path,
                        "has_db": os.path.exists(db),
                    })
        except (PermissionError, OSError):
            pass
    return found
