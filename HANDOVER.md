# FTAPPS – Session Handover
**Date:** 2026-06-16  
**Project:** FTVideo / FTAPPS (Python 3.11 tkinter, Windows)

---

## Current file versions

| File | Version | Location |
|------|---------|----------|
| `ft_combine_strip.py` | 1.06 | `libraries/` |
| `ft_movie.py` | 1.22 | `libraries/` |
| `FTVideo.py` | 1.73 | `helpers/` |

**Version bump rule:** Every file changed in a session gets +0.01 to its `__version__` string.

---

## What has been built (all working)

### `ft_combine_strip.py` — CombineStrip timeline widget

A CapCut-style video timeline at the bottom of `MoviePlayerPanel`. Users drag clips from a thumbnail grid into the strip; clips play sequentially with a yellow playhead.

**EDL data model (complete):**
- `ClipEntry` is a dataclass with `in_point: int` / `out_point: int` (source frame numbers, inclusive/exclusive). `duration_s` is a `@property` — never stored.
- Splits create two `ClipEntry` objects referencing the same source file with adjusted in/out points.
- `_FallbackEditList` (defined at bottom of file) is used when `ft_movie_edit` can't be imported.

**Undo/redo (complete):**
- `_snapshot_clips()` shallow-copies each entry, clears `_photos`, deepcopies `edit_list`.
- `_push_history()` appends snapshot; capped at 50 entries.
- `undo()` / `redo()` swap stacks and call `on_clip_selected`.

**Split / delete (complete):**
- `split_at_playhead()` → `split_clip(idx, source_frame)` — pushes history, replaces one entry with two.
- `delete_active_clip()` — pushes history, removes active clip.

**Edit list persistence per clip (complete):**
- `save_edit_list(index, edit_list)` — stores marker bar state into the clip's `edit_list`.
- `get_entry_edit_list(index)` — retrieves it.

**Public API:**
```
get_clips() -> list[ClipEntry]
get_active_index() -> int | None
add_clip(path, fps, total_frames, duration_s=None, thumb_data=None, *, in_point=0, out_point=None)
split_clip(idx, source_frame)
delete_active_clip()
undo() / redo()
save_edit_list(index, edit_list)
get_entry_edit_list(index)
```

**Key bindings:** `<Delete>` → delete, `<Control-z/Z>` → undo, `<Control-y/Y>` → redo.  
**Right-click menu:** ✂ Split, 🗑 Delete, ↩ Undo (greyed when empty), ↪ Redo, ▶ Play all, ⬆ Export all, ✕ Clear all.

---

### `ft_movie.py` — MoviePlayerPanel + playback engine

**`_PlaybackEngine` (complete):**
- `start(path, start_frame, fps, total, edit_list=None, end_frame=None, canvas_size=None)` — generation-based; spawns worker thread.
- `_worker` uses `end_frame` to stop: `stop_at = end_frame if end_frame is not None else total`.
- `canvas_size` passed through to worker for pre-resizing frames before delivery.
- EOS: worker delivers `(None, -1)` → `_on_playback_frame` sees `frame_index == -1` → advances to next clip.
- **`_worker_ffmpeg` does NOT yet accept or honour `end_frame`** — it plays to EOF regardless. This is the one known open item (see Backlog).

**`MoviePlayerPanel` clip/strip integration (complete):**
- `_clip_end_frame` tracks `out_point` of the active strip clip; `None` = play to file EOF.
- `_on_strip_clip_selected(idx, entry)` sets `_frame_index = entry.in_point`, `_clip_end_frame = entry.out_point`.
- `_load_strip_clip(idx, auto_play)` loads the clip and optionally auto-plays.
- `_auto_play_timer` prevents double-fire: `_load_strip_clip` cancels any pending timer before setting a new one; `_stop_playback` cancels it so manual pause stays paused.
- `_save_strip_edit_state()` / `_load_strip_edit_state(clip_idx)` — round-trips the marker bar edit list to/from the active strip clip's `edit_list`.

**Scrub bar (complete):**
- `scrub_var = tk.IntVar(value=0)` — stub (Scale widget removed).
- Canvas-based scrub: `_scrub_press`, `_scrub_drag`, `_scrub_release`, `_scrub_jump`.
- `_scrub_load(frame)` loads a single frame without starting playback.
- `_step(delta)` — frame-step forward/back.

**Frame grab (complete):**
- `_grab_frame()` — exports current frame to `<video_folder>/FrameGrabs/` as a date-prefixed PNG. Output folder is resolved via `_output_folder` callable if set.

**Spacebar:** `bind_all("<space>")` only — direct `w.bind` calls removed to prevent double-fire.

---

## CRITICAL rule — never use the Edit tool on large files

`ft_movie.py` and `ft_combine_strip.py` are too large for the Edit tool (truncation). Always use **Python patch scripts via `mcp__workspace__bash`**.

---

## Architecture notes

- `ClipEntry._photos` holds `ImageTk.PhotoImage` objects — NOT deepcopy-safe. Always set `c._photos = []` before snapshotting.
- `_PlaybackEngine` is generation-based: `stop()` increments `_generation`; every worker loop checks `_is_current(gen)`.
- `add_clips_to_strip` resolves metadata in a background thread, delivers all clips in a single `after(0, _add_all)` call — drag order guaranteed.
- `is_playing` property (`bool`) available on `MoviePlayerPanel`.

---

## Confirmed-working — do not regress

- `_update_timecode()` exists
- `_update_scrub_range()` — clamps `scrub_var` only, no `self.scrub.configure`
- `_open_external()` exists
- `_has_played` flag completely removed
- Multi-clip drag order correct (`add_clips_to_strip` single-thread delivery)
- `add_clip_to_strip` delegates to `add_clips_to_strip`
- Play/pause button shows `"▶"` / `"⏸"` via `_update_buttons`

---

## Backlog

- **`_worker_ffmpeg`: wire `end_frame` to `-frames:v`** — the cv2 path respects `end_frame`; the ffmpeg pipe path (`_worker_ffmpeg`) ignores it and plays to EOF. Fix: accept `end_frame` in signature, pass `-frames:v <end_frame - start_frame>` to the ffmpeg command.
- FTEditImg: click-to-white eyedropper white balance
- Root folder redesign (FTVideo browsing UX)
- End-to-end thumbnail cache testing
- Icon-based controls with tooltips for marker/cut operations
