"""
ft_metadata_cache.py — app-wide file metadata cache for the FT ecosystem.

Stores per-file metadata (currently: creation_time) keyed on (path, mtime).
If the file is replaced or re-encoded its mtime changes and the entry is
automatically treated as stale, causing a fresh ffprobe fetch on next access.

Cache location:
    Windows:  %APPDATA%\\FTAPPS\\metadata_cache.db
    Other:    ~/.ftapps/metadata_cache.db

The cache is a module-level singleton opened on first use.  All access is
protected by a threading.Lock so it is safe to call from worker threads.

If the cache file is deleted it is silently rebuilt on next access — the only
cost is re-fetching metadata for each file the first time it is seen again.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from typing import Dict, List, Optional

_lock: threading.Lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS file_metadata (
    path          TEXT PRIMARY KEY,
    mtime         REAL NOT NULL,
    creation_time TEXT
);
"""


def _db_path() -> str:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        cache_dir = os.path.join(base, "FTAPPS")
    else:
        cache_dir = os.path.expanduser("~/.ftapps")
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, "metadata_cache.db")


def _get_conn() -> sqlite3.Connection:
    """Return the module-level connection, opening it on first call."""
    global _conn
    if _conn is None:
        path = _db_path()
        _conn = sqlite3.connect(path, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(_SCHEMA)
        _conn.commit()
    return _conn


def get_creation_time(path: str) -> Optional[str]:
    """Return cached creation_time ISO string for path, or None if not cached / stale.

    A None return means either the entry is not in the cache yet, or the file
    has no creation_time metadata (also stored as NULL so we don't re-probe).
    The caller can distinguish these two cases if needed by calling
    is_cached(path) first, but for sorting purposes None→sort-last is fine.
    """
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    try:
        with _lock:
            row = _get_conn().execute(
                "SELECT mtime, creation_time FROM file_metadata WHERE path=?", (path,)
            ).fetchone()
        if row is not None and abs(row[0] - mtime) < 1.0:
            return row[1]  # may be None if file has no creation_time metadata
        return None
    except Exception:
        return None


def is_cached(path: str) -> bool:
    """Return True if path has a non-stale entry (even if creation_time is NULL)."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return False
    try:
        with _lock:
            row = _get_conn().execute(
                "SELECT mtime FROM file_metadata WHERE path=?", (path,)
            ).fetchone()
        return row is not None and abs(row[0] - mtime) < 1.0
    except Exception:
        return False


def put_creation_time(path: str, creation_time: Optional[str]) -> None:
    """Store creation_time for path, keyed on its current mtime.

    Pass creation_time=None to record that the file has no metadata (avoids
    repeated ffprobe calls for files that genuinely have no creation_time tag).
    """
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return
    try:
        with _lock:
            conn = _get_conn()
            conn.execute(
                "INSERT OR REPLACE INTO file_metadata (path, mtime, creation_time)"
                " VALUES (?,?,?)",
                (path, mtime, creation_time),
            )
            conn.commit()
    except Exception:
        pass


def get_many_creation_times(paths: List[str]) -> Dict[str, Optional[str]]:
    """Batch lookup.  Returns {path: creation_time} for all cached, non-stale entries.

    Paths not in the result are not yet cached (or are stale).
    Values may be None for files that have been probed but have no creation_time tag.
    """
    if not paths:
        return {}

    # Gather mtimes (outside lock — disk IO)
    mtimes: Dict[str, float] = {}
    for p in paths:
        try:
            mtimes[p] = os.path.getmtime(p)
        except OSError:
            mtimes[p] = -1.0

    result: Dict[str, Optional[str]] = {}
    try:
        placeholders = ",".join("?" * len(paths))
        with _lock:
            rows = _get_conn().execute(
                f"SELECT path, mtime, creation_time FROM file_metadata"
                f" WHERE path IN ({placeholders})",
                paths,
            ).fetchall()
        for db_path, db_mtime, creation_time in rows:
            file_mtime = mtimes.get(db_path, -1.0)
            if file_mtime >= 0 and abs(file_mtime - db_mtime) < 1.0:
                result[db_path] = creation_time
    except Exception:
        pass
    return result
