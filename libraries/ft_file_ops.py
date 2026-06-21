"""
ft_file_ops.py — shared filesystem operations for the FileTagger suite.

This module deliberately contains no tkinter, no thumbnail code, no database
code, and no application-specific state.  It only performs filesystem work and
returns structured results so FT, FTView, FTCompare, and other apps can handle
UI/state refresh in their own layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import shutil
import uuid
from typing import Iterable, List, Tuple


@dataclass
class FileOpResult:
    operation: str
    requested: List[str] = field(default_factory=list)
    copied: List[Tuple[str, str]] = field(default_factory=list)
    moved: List[Tuple[str, str]] = field(default_factory=list)
    deleted: List[str] = field(default_factory=list)
    skipped_existing: List[Tuple[str, str]] = field(default_factory=list)
    skipped_missing: List[str] = field(default_factory=list)
    errors: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def ok_count(self) -> int:
        return len(self.copied) + len(self.moved) + len(self.deleted)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped_existing) + len(self.skipped_missing)

    def affected_folders(self) -> List[str]:
        folders = set()
        for src, dst in self.copied + self.moved:
            if src:
                folders.add(os.path.normpath(os.path.dirname(src)))
            if dst:
                folders.add(os.path.normpath(os.path.dirname(dst)))
        for src in self.deleted:
            folders.add(os.path.normpath(os.path.dirname(src)))
        return sorted(folders, key=str.lower)


def _norm(p: str) -> str:
    return os.path.normpath(str(p))


def _longpath(p: str) -> str:
    p = _norm(p)
    if os.name == "nt" and not p.startswith("\\\\?\\"):
        return "\\\\?\\" + os.path.abspath(p)
    return p


def _unique_existing_files(paths: Iterable[str]) -> List[str]:
    seen = set()
    result = []
    for p in paths or []:
        if not p:
            continue
        np = _norm(p)
        key = os.path.normcase(np)
        if key in seen:
            continue
        seen.add(key)
        result.append(np)
    return result


def validate_destination(dest_folder: str) -> str:
    dest = _norm(dest_folder)
    if not os.path.isdir(_longpath(dest)):
        raise ValueError(f"Destination folder does not exist: {dest}")
    return dest


def copy_files(paths: Iterable[str], dest_folder: str, *, overwrite: bool = False) -> FileOpResult:
    """Copy files to dest_folder.

    Existing destination filenames are skipped by default.  The function never
    silently renames a duplicate file, because that makes UI counts and user
    expectations ambiguous.
    """
    files = _unique_existing_files(paths)
    dest = validate_destination(dest_folder)
    result = FileOpResult(operation="copy", requested=list(files))

    for src in files:
        try:
            if not os.path.isfile(_longpath(src)):
                result.skipped_missing.append(src)
                continue
            dst = _norm(os.path.join(dest, os.path.basename(src)))
            if os.path.exists(_longpath(dst)) and not overwrite:
                result.skipped_existing.append((src, dst))
                continue
            shutil.copy2(_longpath(src), _longpath(dst))
            result.copied.append((src, dst))
        except Exception as ex:
            result.errors.append((src, str(ex)))
    return result


def move_files(paths: Iterable[str], dest_folder: str, *, overwrite: bool = False) -> FileOpResult:
    """Move files to dest_folder.

    Existing destination filenames are skipped by default.  A file is only
    reported as moved after the source path no longer exists and the destination
    path exists.

    Important Windows/cross-drive detail:
    ``shutil.move`` may copy the file and then fail while deleting the source,
    leaving the same file in both folders.  That is disastrous for FTView
    because the UI then thinks a move succeeded even though a real duplicate
    was created.  This implementation avoids that by using an atomic rename
    where possible, and for cross-drive moves it copies to a temporary file,
    deletes the source, then promotes the temporary file to the final name.  If
    the source cannot be deleted, the temporary copy is removed and the move is
    reported as an error rather than a success.
    """
    files = _unique_existing_files(paths)
    dest = validate_destination(dest_folder)
    result = FileOpResult(operation="move", requested=list(files))

    for src in files:
        try:
            if not os.path.isfile(_longpath(src)):
                result.skipped_missing.append(src)
                continue

            dst = _norm(os.path.join(dest, os.path.basename(src)))

            if os.path.normcase(src) == os.path.normcase(dst):
                result.skipped_existing.append((src, dst))
                continue

            if os.path.exists(_longpath(dst)) and not overwrite:
                result.skipped_existing.append((src, dst))
                continue

            # First try a normal rename.  On the same drive this is atomic and
            # cannot leave a source+destination duplicate.
            try:
                if overwrite:
                    os.replace(_longpath(src), _longpath(dst))
                else:
                    os.rename(_longpath(src), _longpath(dst))
            except OSError:
                # Cross-drive or other non-atomic move path.  Do NOT copy
                # straight to the final destination name, because a later
                # source-delete failure would leave the user with the same file
                # in both folders.
                tmp = _norm(os.path.join(
                    dest,
                    f".ftmove-{uuid.uuid4().hex}-{os.path.basename(src)}.tmp"
                ))
                try:
                    shutil.copy2(_longpath(src), _longpath(tmp))
                    try:
                        os.remove(_longpath(src))
                    except Exception as remove_ex:
                        try:
                            if os.path.exists(_longpath(tmp)):
                                os.remove(_longpath(tmp))
                        except Exception:
                            pass
                        raise OSError(f"Copied temporary file but could not remove source; move cancelled: {remove_ex}")

                    if overwrite:
                        os.replace(_longpath(tmp), _longpath(dst))
                    else:
                        os.rename(_longpath(tmp), _longpath(dst))
                except Exception:
                    # Best effort cleanup of temp file.  Do not hide the
                    # original exception; it is the important user-visible fact.
                    try:
                        if 'tmp' in locals() and os.path.exists(_longpath(tmp)):
                            os.remove(_longpath(tmp))
                    except Exception:
                        pass
                    raise

            # Postcondition check: only report success if the move really moved.
            if os.path.exists(_longpath(dst)) and not os.path.exists(_longpath(src)):
                result.moved.append((src, dst))
            elif os.path.exists(_longpath(dst)) and os.path.exists(_longpath(src)):
                result.errors.append((src, "Move left the file in both source and destination; treating as failed."))
            elif not os.path.exists(_longpath(dst)):
                result.errors.append((src, "Move failed: destination file was not created."))
            else:
                result.errors.append((src, "Move failed: source file still exists."))

        except Exception as ex:
            result.errors.append((src, str(ex)))
    return result


def sort_files(paths: Iterable[str], column: str = "name",
               reverse: bool = False) -> List[str]:
    """Return a sorted copy of paths.

    column:
        "name"       — case-insensitive filename (always instant)
        "date_taken" — creation_time from the app-wide metadata cache;
                       files with no cached date are placed at the end
                       regardless of direction (not first/last by date)

    For "date_taken", the cache is checked in bulk (one SQLite query).  Files
    whose dates are not yet in the cache sort after dated files.  The cache is
    populated as a side-effect of thumbnail generation via ft_movie._ffprobe_info,
    so re-sorting after browsing a folder will produce a fully-ordered result.
    """
    paths_list = list(paths)

    if column == "date_taken":
        try:
            try:
                from libraries.ft_metadata_cache import get_many_creation_times
            except ImportError:
                from ft_metadata_cache import get_many_creation_times
            dates = get_many_creation_times(paths_list)
        except Exception:
            dates = {}

        dated   = [(p, dates[p]) for p in paths_list if dates.get(p)]
        undated = [p for p in paths_list if not dates.get(p)]

        # Sort dated files by ISO string (lexicographic == chronological)
        dated.sort(key=lambda x: x[1], reverse=reverse)
        # Undated files always at end, in filename order
        undated.sort(key=lambda p: os.path.basename(p).lower())
        return [p for p, _ in dated] + undated

    # Default: sort by filename
    return sorted(paths_list,
                  key=lambda p: os.path.basename(p).lower(),
                  reverse=reverse)


def delete_files(paths: Iterable[str]) -> FileOpResult:
    """Delete files from disk.  Directories are never deleted by this function.

    Also removes each successfully deleted file from the shared thumbnail cache
    so standalone apps (FTView, FTVideo, etc.) don't leave orphaned cache entries
    without needing any knowledge of projects or collections.
    """
    files = _unique_existing_files(paths)
    result = FileOpResult(operation="delete", requested=list(files))

    for src in files:
        try:
            if not os.path.isfile(_longpath(src)):
                result.skipped_missing.append(src)
                continue
            os.remove(_longpath(src))
            result.deleted.append(src)
            # Keep thumbnail cache consistent -- remove stale entry immediately.
            # Best-effort: never let a cache error prevent the deletion completing.
            try:
                from libraries import ft_thumb_cache as _ftc
                _ftc.delete_thumb(src)
            except Exception:
                try:
                    import ft_thumb_cache as _ftc2  # type: ignore
                    _ftc2.delete_thumb(src)
                except Exception:
                    pass
        except Exception as ex:
            result.errors.append((src, str(ex)))
    return result
