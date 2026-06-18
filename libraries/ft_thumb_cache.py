"""
ft_thumb_cache.py — Global per-PC thumbnail cache for FTAPPS.

All FT apps (FTMod, FTVideo, FTView, FTMap, FTImgedit) share a single
SQLite database at:

    %APPDATA%\FTAPPS\thumb_cache.db

Cache key is (full_path, mtime) where mtime is integer seconds from
os.path.getmtime().  When a file changes its mtime changes, so the old
entry is simply never found — no explicit invalidation needed.

Usage
-----
    from libraries.ft_thumb_cache import get_thumb, put_thumb, put_thumb_many

    # Read
    jpeg = get_thumb(r"S:\\Photos\\IMG_001.jpg")   # None if not cached

    # Write (single)
    put_thumb(r"S:\\Photos\\IMG_001.jpg", jpeg_bytes)

    # Write (batch — preferred for page loads)
    put_thumb_many([(path, jpeg_bytes), ...])
"""

import os
import sqlite3
import threading
import time

# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------

_DB_PATH = None
_conn: sqlite3.Connection | None = None
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Database bootstrap
# ---------------------------------------------------------------------------

def _resolve_db_path() -> str:
    global _DB_PATH
    if _DB_PATH is None:
        appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
        folder  = os.path.join(appdata, "FTAPPS")
        os.makedirs(folder, exist_ok=True)
        _DB_PATH = os.path.join(folder, "thumb_cache.db")
    return _DB_PATH


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is not None:
        return _conn
    with _lock:
        if _conn is not None:
            return _conn
        conn = sqlite3.connect(_resolve_db_path(), check_same_thread=False)
        # WAL mode: allows concurrent reads while a write is in progress
        conn.execute("PRAGMA journal_mode=WAL")
        # NORMAL sync is safe with WAL and much faster than FULL
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS thumbnails (
                full_path   TEXT    NOT NULL,
                mtime       INTEGER NOT NULL,
                thumb_data  BLOB    NOT NULL,
                width       INTEGER,
                height      INTEGER,
                created_at  INTEGER NOT NULL DEFAULT (strftime('%s','now')),
                PRIMARY KEY (full_path, mtime)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_thumb_mtime
                ON thumbnails (mtime)
        """)
        conn.commit()
        _conn = conn
        return _conn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mtime(path: str) -> int | None:
    """Return file mtime as integer seconds, or None on error."""
    try:
        return int(os.path.getmtime(path))
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_thumb(full_path: str) -> bytes | None:
    """Return cached JPEG bytes for full_path, or None if absent / stale."""
    mt = _mtime(full_path)
    if mt is None:
        return None
    try:
        row = _db().execute(
            "SELECT thumb_data FROM thumbnails WHERE full_path=? AND mtime=?",
            (full_path, mt),
        ).fetchone()
        return bytes(row[0]) if row else None
    except Exception as e:
        print(f"ft_thumb_cache.get_thumb error: {e}")
        return None


def get_thumb_many(full_paths: list[str]) -> dict[str, bytes]:
    """Return {path: jpeg_bytes} for every path that has a current cached entry.

    Uses a single bulk SQL query (WHERE full_path IN (...)) then filters by
    mtime in Python — identical pattern to the existing ft_db.thumb_get_many.
    """
    if not full_paths:
        return {}

    # Build mtime map first so we can validate rows cheaply
    mtime_map: dict[str, int] = {}
    for p in full_paths:
        mt = _mtime(p)
        if mt is not None:
            mtime_map[p] = mt

    if not mtime_map:
        return {}

    result: dict[str, bytes] = {}
    try:
        keys = list(mtime_map.keys())
        placeholders = ",".join("?" * len(keys))
        rows = _db().execute(
            f"SELECT full_path, thumb_data, mtime FROM thumbnails"
            f" WHERE full_path IN ({placeholders})",
            keys,
        ).fetchall()
        for path, data, stored_mt in rows:
            if stored_mt == mtime_map.get(path):
                result[path] = bytes(data)
    except Exception as e:
        print(f"ft_thumb_cache.get_thumb_many error: {e}")

    return result


def put_thumb(
    full_path: str,
    jpeg_bytes: bytes,
    width: int | None = None,
    height: int | None = None,
) -> None:
    """Store a thumbnail for full_path using the file's current mtime as key."""
    mt = _mtime(full_path)
    if mt is None or not jpeg_bytes:
        return
    try:
        with _lock:
            _db().execute(
                """INSERT OR REPLACE INTO thumbnails
                       (full_path, mtime, thumb_data, width, height)
                   VALUES (?, ?, ?, ?, ?)""",
                (full_path, mt, jpeg_bytes, width, height),
            )
            _db().commit()
    except Exception as e:
        print(f"ft_thumb_cache.put_thumb error: {e}")


def put_thumb_many(
    items: list[tuple],
) -> None:
    """Store multiple thumbnails in a single transaction.

    Each item can be:
        (path, jpeg_bytes)
        (path, jpeg_bytes, width, height)
    """
    if not items:
        return

    rows = []
    for item in items:
        path   = item[0]
        jpeg   = item[1]
        width  = item[2] if len(item) > 2 else None
        height = item[3] if len(item) > 3 else None
        mt = _mtime(path)
        if mt is not None and jpeg:
            rows.append((path, mt, jpeg, width, height))

    if not rows:
        return
    try:
        with _lock:
            _db().executemany(
                """INSERT OR REPLACE INTO thumbnails
                       (full_path, mtime, thumb_data, width, height)
                   VALUES (?, ?, ?, ?, ?)""",
                rows,
            )
            _db().commit()
    except Exception as e:
        print(f"ft_thumb_cache.put_thumb_many error: {e}")


def move_thumb(old_path: str, new_path: str) -> None:
    """Update the cache key when a file is moved or renamed.

    Looks up the most recent blob for old_path, re-inserts it under
    new_path with the new file's mtime, then removes the old row.
    """
    mt = _mtime(new_path)
    if mt is None:
        return
    try:
        with _lock:
            db = _db()
            row = db.execute(
                """SELECT thumb_data, width, height FROM thumbnails
                   WHERE full_path=? ORDER BY mtime DESC LIMIT 1""",
                (old_path,),
            ).fetchone()
            if row:
                db.execute(
                    "DELETE FROM thumbnails WHERE full_path=?", (old_path,)
                )
                db.execute(
                    """INSERT OR REPLACE INTO thumbnails
                           (full_path, mtime, thumb_data, width, height)
                       VALUES (?, ?, ?, ?, ?)""",
                    (new_path, mt, row[0], row[1], row[2]),
                )
                db.commit()
    except Exception as e:
        print(f"ft_thumb_cache.move_thumb error: {e}")


def delete_thumb(full_path: str) -> None:
    """Remove all cached entries for full_path (all mtimes)."""
    try:
        with _lock:
            _db().execute(
                "DELETE FROM thumbnails WHERE full_path=?", (full_path,)
            )
            _db().commit()
    except Exception as e:
        print(f"ft_thumb_cache.delete_thumb error: {e}")


def prune(max_age_days: int = 90) -> int:
    """Delete entries not accessed in max_age_days.  Returns row count removed.

    Safe to call periodically (e.g. on app startup, at most once per day).
    Uses created_at as a proxy for last-accessed — sufficient for a dev-stage
    cache where the cost of regeneration is low.
    """
    cutoff = int(time.time()) - max_age_days * 86_400
    try:
        with _lock:
            cur = _db().execute(
                "DELETE FROM thumbnails WHERE created_at < ?", (cutoff,)
            )
            _db().commit()
            return cur.rowcount
    except Exception as e:
        print(f"ft_thumb_cache.prune error: {e}")
        return 0


def db_path() -> str:
    """Return the filesystem path to the thumbnail cache database."""
    return _resolve_db_path()


def stats() -> dict:
    """Return basic cache statistics (row count, db size in bytes)."""
    try:
        row = _db().execute("SELECT COUNT(*) FROM thumbnails").fetchone()
        count = row[0] if row else 0
        size  = os.path.getsize(_resolve_db_path())
        return {"rows": count, "db_bytes": size}
    except Exception:
        return {"rows": 0, "db_bytes": 0}
