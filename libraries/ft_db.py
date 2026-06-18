"""
ft_db.py — consolidated FileTagger database layer.

FT41 finalises the DB refactor:
- one stable module name instead of ft_dbXX version sprawl
- schema/open/close owned here
- collections, cull list, migration, and small maintenance DB operations
  live here instead of in FT.py

Thumbnail storage was removed in the global-cache refactor.  Thumbnails now
live in the per-PC cache managed by libraries.ft_thumb_cache.  The thumb_*
functions below are compatibility shims so callers need no immediate changes.
"""

from __future__ import annotations

import os


def _normalise_ui_path(p):
    if not p:
        return p
    p = str(p)
    if p.startswith("\\\\?\\"):
        p = p[4:]
    return os.path.normpath(p)


# ── Thumbnail shims — delegate to global per-PC cache ────────────────────────
# These preserve the existing call signatures so FTMod and FTVideo continue
# to work without changes.  db_conn is accepted but ignored.

def _tc():
    """Lazy import of ft_thumb_cache to avoid circular imports at module load."""
    from libraries import ft_thumb_cache
    return ft_thumb_cache


def delete_thumbs_for_folder(db_conn, folder):
    """Remove all global-cache entries whose path starts with folder."""
    if not folder:
        return 0
    try:
        import sqlite3
        db = _tc()._db()
        folder_norm = _normalise_ui_path(folder).rstrip("\\/")
        total = 0
        for sep in ("\\", "/"):
            cur = db.execute(
                "DELETE FROM thumbnails WHERE full_path=? OR full_path LIKE ?",
                (folder_norm, folder_norm + sep + "%"),
            )
            total += cur.rowcount or 0
        db.commit()
        return total
    except Exception as e:
        print(f"ft_db.delete_thumbs_for_folder error: {e}")
        return 0


def thumb_get(db_conn, source_path, *, ui_path_func=None, longpath_func=None):
    """Return cached JPEG bytes from the global cache, or None."""
    return _tc().get_thumb(source_path)


def thumb_get_many(db_conn, source_paths, *, ui_path_func=None, longpath_func=None):
    """Return {path: jpeg_bytes} for all globally-cached paths."""
    return _tc().get_thumb_many(list(source_paths))


def thumb_put(db_conn, source_path, jpeg_bytes, *, ui_path_func=None, longpath_func=None):
    """Store a thumbnail in the global cache."""
    _tc().put_thumb(source_path, jpeg_bytes)


def thumb_put_many(db_conn, items, *, ui_path_func=None, longpath_func=None):
    """Store multiple thumbnails in the global cache.

    Accepts both (path, jpeg) and (path, jpeg, width, height) tuples.
    """
    _tc().put_thumb_many(items)


def thumb_gc(db_conn, source_folder, *, ui_path_func=None, longpath_func=None):
    """No-op: global cache uses mtime-based invalidation; no explicit GC needed.

    Returns (0, 0) to satisfy callers that unpack the result.
    """
    return 0, 0


def thumb_move(db_conn, source_paths, dest_folder, *, ui_path_func=None):
    """Update global-cache keys when files are moved to dest_folder."""
    tc = _tc()
    ui = ui_path_func or _normalise_ui_path
    dest = ui(dest_folder)
    for old in (source_paths or []):
        old_key = ui(old)
        new_key = ui(os.path.join(dest, os.path.basename(old_key)))
        tc.move_thumb(old_key, new_key)


# ── Collection helpers moved from FT30 ────────────────────────────────────────

def list_collections(db_conn, root=None):
    """Return sorted list of collection names from DB."""
    if db_conn is None:
        return []
    try:
        rows = db_conn.execute("SELECT name FROM collections ORDER BY name").fetchall()
        return [r[0] for r in rows]
    except Exception:
        return []


def read_collection(db_conn, name, root=None):
    """Return {path: timestamp} dict for named collection."""
    if db_conn is None:
        return {}
    try:
        row = db_conn.execute("SELECT id FROM collections WHERE name=?", (name,)).fetchone()
        if not row:
            return {}
        rows = db_conn.execute(
            "SELECT path, added_at FROM collection_items WHERE collection_id=? ORDER BY sort_order",
            (row[0],)
        ).fetchall()
        return {r[0]: (r[1] or "") for r in rows}
    except Exception:
        return {}


def write_collection(db_conn, name, root=None, tagged=None, tagged_at=None, order=None):
    """Write collection to DB. Creates collection if it does not exist."""
    if db_conn is None:
        return
    if tagged is None:
        tagged = set()
    if tagged_at is None:
        tagged_at = {}
    try:
        import datetime as _dt2
        now = _dt2.datetime.now().isoformat(timespec="seconds")
        cur = db_conn.execute("SELECT id FROM collections WHERE name=?", (name,))
        row = cur.fetchone()
        if row:
            cid = row[0]
            db_conn.execute("UPDATE collections SET modified_at=? WHERE id=?", (now, cid))
        else:
            db_conn.execute(
                "INSERT INTO collections (name, created_at, modified_at) VALUES (?,?,?)",
                (name, now, now)
            )
            cid = db_conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        db_conn.execute("DELETE FROM collection_items WHERE collection_id=?", (cid,))
        paths = order if order else sorted(tagged)
        rows = [
            (cid, p, tagged_at.get(p, now), i)
            for i, p in enumerate(paths)
        ]
        db_conn.executemany(
            "INSERT INTO collection_items (collection_id, path, added_at, sort_order) VALUES (?,?,?,?)",
            rows
        )
        db_conn.commit()
    except Exception as e:
        print(f"Could not write collection {name}: {e}")


def append_collection_items(db_conn, name, paths, root=None):
    """Append paths to a collection in order, preserving existing sort_order.

    Existing paths are not duplicated. Returns the number of newly-added items.
    """
    if db_conn is None:
        return 0
    paths = [p for p in (paths or []) if p]
    if not paths:
        return 0
    try:
        import datetime as _dt2
        now = _dt2.datetime.now().isoformat(timespec="seconds")
        row = db_conn.execute("SELECT id FROM collections WHERE name=?", (name,)).fetchone()
        if row:
            cid = row[0]
            db_conn.execute("UPDATE collections SET modified_at=? WHERE id=?", (now, cid))
        else:
            db_conn.execute(
                "INSERT INTO collections (name, created_at, modified_at) VALUES (?,?,?)",
                (name, now, now)
            )
            cid = db_conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        existing_rows = db_conn.execute(
            "SELECT path, sort_order FROM collection_items WHERE collection_id=? ORDER BY sort_order",
            (cid,)
        ).fetchall()
        existing = {r[0] for r in existing_rows}
        next_order = 0
        if existing_rows:
            next_order = max(int(r[1] or 0) for r in existing_rows) + 1

        rows = []
        for p in paths:
            if p in existing:
                continue
            rows.append((cid, p, now, next_order))
            existing.add(p)
            next_order += 1

        if rows:
            db_conn.executemany(
                "INSERT INTO collection_items (collection_id, path, added_at, sort_order) VALUES (?,?,?,?)",
                rows
            )
        db_conn.commit()
        return len(rows)
    except Exception as e:
        print(f"Could not append to collection {name}: {e}")
        return 0


def delete_collection(db_conn, name, root=None):
    """Delete a collection from DB."""
    if db_conn is None:
        return
    try:
        db_conn.execute("DELETE FROM collections WHERE name=?", (name,))
        db_conn.commit()
    except Exception as e:
        print(f"Could not delete collection {name}: {e}")


# ── Cull list helpers moved from FT32 ─────────────────────────────────────────

def read_cull_list(db_conn, root=None, *, exists_func=None):
    """Return {path: timestamp} for culled files that still exist on disk."""
    if db_conn is None:
        return {}
    try:
        exists = exists_func or os.path.exists
        rows = db_conn.execute("SELECT path, marked_at FROM cull_list").fetchall()
        return {r[0]: (r[1] or "") for r in rows if exists(r[0])}
    except Exception:
        return {}


def write_cull_list(db_conn, root=None, culled=None, culled_at=None):
    """Replace cull list in DB with current state."""
    if db_conn is None:
        return
    if culled is None:
        culled = set()
    if culled_at is None:
        culled_at = {}
    try:
        import datetime as _dt2
        now = _dt2.datetime.now().isoformat(timespec="seconds")
        db_conn.execute("DELETE FROM cull_list")
        rows = [(p, culled_at.get(p, now)) for p in culled]
        db_conn.executemany("INSERT INTO cull_list (path, marked_at) VALUES (?,?)", rows)
        db_conn.commit()
    except Exception as e:
        print(f"Could not write cull list: {e}")


# ── First-run migration helper moved from FT35 ────────────────────────────────

def migrate_txt_to_db(
    db_conn,
    root,
    *,
    ft_system_dir="_FileTagger",
    coll_subdir="_Collections",
    coll_prefix="_tags_",
):
    """Import legacy .txt collections into DB if DB collections table is empty.

    FT38: this no longer calls back into FT.py. The migration writes collections
    through this module's own write_collection() helper so migration is fully
    owned by ft_db.py.
    """
    if db_conn is None:
        return
    try:
        existing = db_conn.execute("SELECT COUNT(*) FROM collections").fetchone()[0]
        if existing > 0:
            return
    except Exception:
        return

    import datetime as _dt2
    now = _dt2.datetime.now().isoformat(timespec="seconds")

    cdir = os.path.join(root, ft_system_dir, coll_subdir)
    if os.path.isdir(cdir):
        for fname in sorted(os.listdir(cdir)):
            if not (fname.startswith(coll_prefix) and fname.endswith(".txt")):
                continue
            name = fname[len(coll_prefix):-4]
            data = {}
            try:
                with open(os.path.join(cdir, fname), "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        parts = line.split("\\t")
                        fp = parts[0]
                        ts = parts[1] if len(parts) > 1 else now
                        data[fp] = ts
            except Exception:
                continue

            if data:
                write_collection(db_conn, name, root, set(data.keys()), data)
                print(f"Migrated collection: {name} ({len(data)} files)")


# ── Connection lifecycle and schema owned by ft_db.py ───────────────────────
import sqlite3 as _sqlite3

_db_conn = None

DB_SCHEMA = """
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


def default_db_path(script_file=None):
    """Return default DB path: <script_dir>\\Database\\FileTagger.db."""
    script_dir = os.path.dirname(os.path.abspath(script_file or __file__))
    folder = os.path.join(script_dir, "Database")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "FileTagger.db")


def _write_database_to_ini(ini_path, db_path):
    """Update [FileTagger] database= while preserving FileTagger's custom root lines."""
    if not ini_path:
        return
    try:
        if os.path.exists(ini_path):
            with open(ini_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        else:
            lines = []

        in_ft = False
        found_current = False
        for line in lines:
            stripped = line.strip()
            if stripped.lower() == "[filetagger]":
                in_ft = True
            elif stripped.startswith("["):
                in_ft = False
            elif in_ft and stripped.lower().startswith("database"):
                val = stripped.split("=", 1)[1].strip() if "=" in stripped else ""
                if val == db_path:
                    found_current = True
                break
        if found_current:
            return

        new_lines = []
        skip = False
        for line in lines:
            if line.strip().lower() == "[filetagger]":
                skip = True
                continue
            if skip and line.strip().startswith("["):
                skip = False
            if not skip:
                new_lines.append(line)
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines.append("\n")
        new_lines.append("[FileTagger]\n")
        new_lines.append(f"database = {db_path}\n")
        os.makedirs(os.path.dirname(os.path.abspath(ini_path)), exist_ok=True)
        with open(ini_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
    except Exception as e:
        print(f"ft_db: could not write ini: {e}")



def open_database(path=None, *, default_path=None, ini_path=None):
    """Open/create the SQLite database, apply schema, and return the connection."""
    global _db_conn
    if path is None:
        path = default_path or default_db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if _db_conn is not None:
        try:
            _db_conn.close()
        except Exception:
            pass
    _db_conn = _sqlite3.connect(path, check_same_thread=False)
    _db_conn.execute("PRAGMA journal_mode=WAL")
    _db_conn.execute("PRAGMA foreign_keys=ON")
    _db_conn.executescript(DB_SCHEMA)
    _db_conn.commit()
    if ini_path:
        _write_database_to_ini(ini_path, path)
    return _db_conn


def get_connection():
    return _db_conn


def close_database():
    global _db_conn
    if _db_conn is not None:
        try:
            _db_conn.close()
        finally:
            _db_conn = None


# ── Small DB operations externalised from FT.py in FT37 ──────────────────────
def rename_collection(db_conn, old_name, new_name):
    if db_conn is None:
        return
    db_conn.execute("UPDATE collections SET name=? WHERE name=?", (new_name, old_name))
    db_conn.commit()


def thumb_count_under_folder(db_conn, folder, *, ui_path_func=None):
    """Return cached thumbnail count for files anywhere under folder.

    Queries the global cache (db_conn is accepted for API compatibility but ignored).
    """
    try:
        ui = ui_path_func or _normalise_ui_path
        base = os.path.normpath(ui(folder)).rstrip("\\/")
        if not base:
            return 0
        db = _tc()._db()
        total = 0
        for sep in ("\\", "/"):
            row = db.execute(
                "SELECT COUNT(*) FROM thumbnails WHERE full_path LIKE ?",
                (base + sep + "%",),
            ).fetchone()
            total += int(row[0]) if row else 0
        return total
    except Exception as e:
        print(f"ft_db.thumb_count_under_folder error: {e}")
        return 0


def thumb_count_in_folder(db_conn, folder, *, ui_path_func=None):
    """Return cached thumbnail count for files directly in folder only.

    Queries the global cache (db_conn is accepted for API compatibility but ignored).
    """
    try:
        ui = ui_path_func or _normalise_ui_path
        base = os.path.normpath(ui(folder)).rstrip("\\/")
        if not base:
            return 0
        db = _tc()._db()
        total = 0
        for sep in ("\\", "/"):
            # Match exactly one level below base: base\<name> but not base\sub\<name>
            row = db.execute(
                "SELECT COUNT(*) FROM thumbnails"
                " WHERE full_path LIKE ? AND full_path NOT LIKE ?",
                (base + sep + "%", base + sep + "%" + sep + "%"),
            ).fetchone()
            total += int(row[0]) if row else 0
        return total
    except Exception as e:
        print(f"ft_db.thumb_count_in_folder error: {e}")
        return 0


def rekey_file_paths(db_conn, old_path, new_path):
    if db_conn is None:
        return
    db_conn.execute("UPDATE cull_list SET path=? WHERE path=?", (new_path, old_path))
    db_conn.commit()
    # Update global thumbnail cache key too
    _tc().move_thumb(old_path, new_path)


def remove_collection_items(db_conn, paths):
    if db_conn is None or not paths:
        return 0
    rows = [(p,) for p in set(paths)]
    cur = db_conn.executemany("DELETE FROM collection_items WHERE path=?", rows)
    db_conn.commit()
    return cur.rowcount if getattr(cur, 'rowcount', -1) and cur.rowcount > 0 else len(rows)


def cleanup_missing_collection_items(db_conn, *, exists_func=None):
    if db_conn is None:
        return 0
    exists = exists_func or os.path.exists
    rows = db_conn.execute("SELECT DISTINCT path FROM collection_items").fetchall()
    dead = [r[0] for r in rows if not exists(r[0])]
    if dead:
        db_conn.executemany("DELETE FROM collection_items WHERE path=?", [(p,) for p in dead])
        db_conn.commit()
    return len(dead)


def table_count(db_conn, table_name):
    if db_conn is None:
        return None
    allowed = {
        "collections", "collection_items", "cull_list",
        "folder_bookmarks", "file_metadata", "settings"
    }
    if table_name not in allowed:
        raise ValueError(f"Unsupported table name: {table_name}")
    row = db_conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()
    return int(row[0]) if row else 0
