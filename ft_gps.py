"""
ft_gps.py — GPS / EXIF helpers for FileTagger.

Extracted from FT.py in Phase 1.
No UI dependencies.
"""

import os

_gps_cache: dict = {}   # path -> (lat, lon) or None


def _get_gps_coords(path):
    """Return (lat, lon) floats or None. Cached after first read."""
    if path in _gps_cache:
        return _gps_cache[path]
    try:
        from PIL import Image as _PILg
        img = _PILg.open(path)
        exif = img.getexif()
        if not exif:
            _gps_cache[path] = None; return None
        gps = exif.get_ifd(34853)
        if not gps or 2 not in gps or 4 not in gps:
            _gps_cache[path] = None; return None
        def _dms(v): return float(v[0]) + float(v[1])/60 + float(v[2])/3600
        lat = _dms(gps[2]); lon = _dms(gps[4])
        if gps.get(1,"N") == "S": lat = -lat
        if gps.get(3,"E") == "W": lon = -lon
        result = None if (lat == 0.0 and lon == 0.0) else (lat, lon)
        _gps_cache[path] = result
        return result
    except Exception:
        _gps_cache[path] = None
        return None


def _scan_folder_for_gps(folder):
    """Return list of JPG paths in folder that have GPS coordinates."""
    try:
        entries = [e.path for e in os.scandir(folder)
                   if e.is_file() and os.path.splitext(e.name)[1].lower() in {'.jpg','.jpeg'}]
        return [p for p in sorted(entries) if _get_gps_coords(p)]
    except Exception:
        return []
