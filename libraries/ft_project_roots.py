"""ft_project_roots.py — lightweight project root reader for standalone FT apps.

Returns the active project's root list so apps like FTView, FTVideo, and
FTFiler can populate their root selector on startup without importing the
full ft_projects module.

Supports both the new root_N format and the legacy photos/videos/pdfs format
(read-only; migration is left to FTMod which owns Projects.ini writes).

Public API
----------
read_project_roots(base_file=None)
    Returns:
        {
          "project": str,            # active project name
          "roots":  [(path, label)], # ordered flat list (new format)
          # backward-compat keys — still populated for apps not yet updated:
          "photos": str,             # first root path, or ""
          "pdfs":   str,             # "" (no longer typed)
          "videos": str,             # "" (no longer typed)
        }
"""

from __future__ import annotations

import configparser
import os
from typing import Dict, List, Tuple


def _script_dir(base_file=None):
    if base_file:
        try:
            return os.path.dirname(os.path.abspath(base_file))
        except Exception:
            pass
    return os.getcwd()


def _active_project_name(folder):
    ini = os.path.join(folder, "FileTagger.ini")
    if not os.path.exists(ini):
        return ""
    cfg = configparser.ConfigParser(strict=False)
    try:
        cfg.read(ini, encoding="utf-8")
        return cfg.get("FileTagger", "active_project", fallback="").strip()
    except Exception:
        return ""


def _parse_root_line(text):
    """'path : label' -> (path, label) or just path -> (path, basename)."""
    text = (text or "").strip()
    if not text:
        return None
    idx = text.rfind(":")
    while idx > 1:
        path = text[:idx].strip()
        name = text[idx + 1:].strip()
        if path and name and len(path) > 2:
            return (os.path.normpath(path), name[:60])
        idx = text.rfind(":", 0, idx)
    return (os.path.normpath(text), os.path.basename(text.rstrip("/\\")) or text)


def _read_section(ini_path, section):
    """Read all key=value pairs for one section, returning {key: [values]}."""
    result = {}
    in_sec = False
    if not os.path.exists(ini_path):
        return result
    try:
        with open(ini_path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith(("#", ";")):
                    continue
                if line.startswith("[") and line.endswith("]"):
                    in_sec = (line[1:-1].strip().lower() == section.lower())
                    continue
                if not in_sec or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                result.setdefault(key.strip().lower(), []).append(val.strip())
    except Exception:
        pass
    return result


def read_project_roots(base_file=None):
    """Return active project roots.

    Returns a dict with:
      project  – project name (str)
      roots    – ordered [(path, label)] list
      photos   – first root path str (backward compat)
      pdfs     – "" (no longer a distinct type)
      videos   – "" (no longer a distinct type)
    """
    folder = _script_dir(base_file)
    projects_ini = os.path.join(folder, "Projects.ini")
    out = {"project": "", "roots": [], "photos": "", "pdfs": "", "videos": ""}

    if not os.path.exists(projects_ini):
        return out

    # Determine active project name
    active = _active_project_name(folder)
    if not active:
        # Fall back to first section in Projects.ini
        try:
            with open(projects_ini, "r", encoding="utf-8", errors="replace") as f:
                for raw in f:
                    line = raw.strip()
                    if line.startswith("[") and line.endswith("]"):
                        active = line[1:-1].strip()
                        break
        except Exception:
            pass

    if not active:
        return out

    out["project"] = active
    keys = _read_section(projects_ini, active)

    roots = []

    # New format: root_0, root_1, ...
    idx = 0
    while True:
        key = f"root_{idx}"
        if key not in keys:
            break
        for val in keys[key]:
            parsed = _parse_root_line(val)
            if parsed:
                roots.append(parsed)
        idx += 1

    # Legacy format fallback
    if not roots:
        for legacy_key in ("photos", "videos", "pdfs"):
            for val in keys.get(legacy_key, []):
                parsed = _parse_root_line(val)
                if parsed and parsed not in roots:
                    roots.append(parsed)

    out["roots"] = roots
    # Backward-compat: 'photos' = first root path
    out["photos"] = roots[0][0] if roots else ""

    return out
