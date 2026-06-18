# FTAPPS – Session Handover
**Date:** 2026-06-18  
**Project:** FTAPPS suite (Python 3.11 tkinter, Windows)  
**Git repo:** https://github.com/yengr46/FT

---

## File edit rules — READ FIRST

| File | Size | Rule |
|------|------|------|
| `helpers/FTVideo.py` | ~1861 lines | **Edit tool ONLY** — bash scripts read stale OneDrive cache and truncate the file |
| `main/FTmod.py` | ~13748 lines | **Bash patch scripts only** — too large for Edit tool |
| `libraries/ft_movie.py` | large | **Bash patch scripts only** |
| `libraries/ft_combine_strip.py` | large | **Bash patch scripts only** |
| All other files | any | Either tool OK |

**OneDrive sync lag:** The bash sandbox mounts the OneDrive folder but may see a cached (stale) version seconds to minutes after the Edit tool writes. For read operations (syntax checks etc.) on FTVideo.py, treat bash results as possibly stale.

---

## Current file versions

| File | Version | Location |
|------|---------|----------|
| `ft_combine_strip.py` | 1.06 | `libraries/` |
| `ft_movie.py` | 1.22 | `libraries/` |
| `FTVideo.py` | 1.73 | `helpers/` |

**Version bump rule:** Every file changed in a session gets +0.01 to its `__version__` string.

---

## System overview

FTAPPS is a suite of Python/tkinter desktop apps for photo and video file management, all sharing a common libraries folder.

| App | File | Purpose |
|-----|------|---------|
| FileTagger (main) | `main/FTmod.py` | Photo tagging/culling/organising with canvas-based thumbnails |
| FTView | `helpers/FTView.py` | Photo browser/viewer with folder tree |
| FTVideo | `helpers/FTVideo.py` | Video browser + timeline editor (embedded MoviePlayerPanel) |
| FTMap | `helpers/FTMap.py` | GPS photo map viewer |
| FTCompare | `helpers/FTCompare.py` | Side-by-side photo comparison |

### Shared libraries

- `libraries/ft_movie.py` — `MoviePlayerPanel` + `_PlaybackEngine` (VLC/cv2/ffmpeg)
- `libraries/ft_combine_strip.py` — `CombineStrip` timeline widget
- `libraries/ft_file_ops.py` — filesystem operations (copy/move/delete), no tkinter
- `libraries/ft_thumb_cache.py` — thumbnail disk cache
- `libraries/ft_movie_edit.py` — EDL/marker edit list model
- `libraries/ft_db.py` — SQLite tag database
- `libraries/ft_metadata_cache.py` — date/metadata cache

---

## What has been built (complete features)

### CombineStrip timeline (`ft_combine_strip.py`)

CapCut-style video timeline at the bottom of `MoviePlayerPanel`. Users drag clips from thumbnail grid into strip; clips play sequentially with yellow playhead.

- `ClipEntry` dataclass: `in_point` / `out_point` (source frame numbers). `duration_s` is a `@property`.
- Splits create two `ClipEntry` objects referencing same source with adjusted in/out.
- Undo/redo: `_snapshot_clips()` + `_push_history()`, capped at 50 entries.
- `split_at_playhead()` → `split_clip(idx, source_frame)`.
- `save_edit_list(index, edit_list)` / `get_entry_edit_list(index)` — marker bar state per clip.
- Right-click menu: ✂ Split, 🗑 Delete, ↩ Undo, ↪ Redo, ▶ Play all, ⬆ Export all, ✕ Clear all.
- Key bindings: `<Delete>` delete, `<Control-z/Z>` undo, `<Control-y/Y>` redo.

### MoviePlayerPanel (`ft_movie.py`)

- `_PlaybackEngine`: generation-based; stop() increments `_generation`; worker checks `_is_current(gen)`.
- `start(path, start_frame, fps, total, edit_list=None, end_frame=None, canvas_size=None)`.
- EOS: worker delivers `(None, -1)` → advances to next strip clip.
- `_clip_end_frame` tracks `out_point`; `None` = play to file EOF.
- Canvas scrub bar: `_scrub_press`, `_scrub_drag`, `_scrub_release`, `_scrub_jump`.
- `_grab_frame()` — exports frame to `<video_folder>/FrameGrabs/` as date-prefixed PNG.
- MarkerBar: red vertical lines, draggable, drawn on CombineStrip filmstrip canvas.

### FTVideo (`helpers/FTVideo.py`)

- **Files-thumb sash:** draggable divider between file list and thumb panel (`FILES_MIN_W = 120`). Drag handlers: `_start_files_divider_drag`, `_drag_files_divider`, `_end_files_divider_drag`.
- **Right-click menu:** Add to timeline, Copy selected, Move selected, Delete selected.
- **Selection watermark:** Small (74×16) "SELECTED" label near bottom of thumbnail image. Managed via `thumb_watermarks` dict and `_set_thumb_watermark(idx, selected)`.
- **File ops:** `_copy_selected_files`, `_move_selected_files`, `_delete_selected_files`.

### Selection logic (all apps — FTVideo, FTView, FTmod)

Consistent behaviour:
- **Left click:** preview only, no selection change
- **Ctrl+click:** toggle selected/deselected, update preview, set shift anchor (`_thumb_sel_anchor`)
- **Shift+click:** apply last Ctrl-click op (select/deselect) to range from anchor, update preview
- **Right-click:** popup only, no selection change; if nothing selected, implicitly targets right-clicked item (count=1) without selecting it; if multiple ctrl-selected, operates on all
- `_last_ctrl_op` ("select"/"deselect"): tracks what last Ctrl-click did, used by Shift-click range

### SELECTED watermark (FTView, FTVideo, FTmod)

- Size: 74×16 px
- Font: `("Segoe UI", 8, "bold")`
- Position: near bottom of thumbnail image area (4px from bottom edge)
- FTView/FTVideo: tkinter Label overlay (`place()`/`place_forget()`)
- FTmod: Canvas `create_rectangle` + `create_text` with tags `sel_watermark_bg` / `sel_watermark`
- FTVideo: watermark IS shown (was previously hidden — fixed)

### FTmod columns control

Replaced `tk.Entry` with `tk.Spinbox(from_=1, to=30, width=3)` to match FTVideo style.

### FTMap sash

Fixed blank space when dragging file list / preview sash right. Changed `stretch="never"` to `stretch="always"` on both the file list pane inside `_left_paned` (standalone mode) and `left_outer` in `_paned` (embedded mode).

### `ft_file_ops.py`

Added `delete_files(paths)` function (was missing — FTView was calling it but it didn't exist). Uses `skipped_missing` tracking. `FileOpResult` dataclass covers copy/move/delete with `ok_count`, `skipped_count`, `affected_folders()`.

---

## Confirmed-working — do not regress

- `_update_timecode()` exists in ft_movie.py
- `_update_scrub_range()` — clamps `scrub_var` only, no `self.scrub.configure`
- `_open_external()` exists
- `_has_played` flag completely removed
- Multi-clip drag order correct (`add_clips_to_strip` single-thread delivery)
- `add_clip_to_strip` delegates to `add_clips_to_strip`
- Play/pause button shows `"▶"` / `"⏸"` via `_update_buttons`
- `bind_all("<space>")` only — direct `w.bind` removed to prevent double-fire

---

## Architecture notes

- `ClipEntry._photos` holds `ImageTk.PhotoImage` — NOT deepcopy-safe. Always set `c._photos = []` before snapshotting.
- `place()` absolute layout in FTVideo — dividers need `lift()` after other frames are placed.
- `_thumb_selected` (set of Ctrl-selected indices) vs `selected_idx` (preview item) are independent in FTVideo.
- `_thumb_sel_anchor`: only updated by Ctrl-click, NOT by plain left-click.
- FTmod canvas cells: `IX = PAD = 8`, `IY = PAD = 8`, image width = `sz`, image height = `IMG_H` (may differ from sz for non-square thumbs).

---

## Backlog — still needs doing

### High priority

- **`_worker_ffmpeg`: wire `end_frame`** — the cv2 playback path respects `end_frame`; the ffmpeg pipe path (`_worker_ffmpeg`) ignores it and plays to EOF. Fix: accept `end_frame` in signature, pass `-frames:v <end_frame - start_frame>` to ffmpeg command.
- **CombineStrip right-click marker menu** — `_combine_marker_menu` injection into CombineStrip right-click still not working properly. User noted "still not correct — will look at this later."
- **FTView `create_contact_sheet_from_selection(files=None)`** — method signature updated to accept optional `files` param but the method body likely still reads `self.selected_files` internally instead of using the param. Needs the body wired up.

### Medium priority

- **Root folder redesign — PARTIALLY DONE:** Core libraries rewritten, FTmod UI updated. Two dialogs still need updating:
  - `_show_new_project_dialog()` (~line 13242 in FTmod.py) — still uses old `photos_roots`/`pdfs_roots` fields
  - `_show_config_dialog()` (~line 13346 in FTmod.py) — same issue; won't crash but won't save new `root_N` format correctly
  - End-to-end test needed: filter buttons (All/Photos/Videos/Docs), root switching, collection switching, tagging
  - FTView/FTVideo need verification they work with new `ft_project_roots.py` output
  - Last known good backup: `FTAPPS_backup_20260615_004650.zip`

- **FTEditImg:** click-to-white eyedropper white balance
- **Icon-based controls:** tooltips for marker/cut operations in MoviePlayerPanel

### Low priority

- End-to-end thumbnail cache testing
- `_worker_ffmpeg` end_frame support (see above)
