"""ft_startup_check.py — shared startup dependency checks for FileTagger apps.

Each FT app should call check_startup_requirements() before importing its
app-specific helper modules.  This catches missing Python packages and missing
ft_*.py helper files at startup instead of failing later during thumbnail,
PDF, contact-sheet, or tree operations.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass(frozen=True)
class LibraryRequirement:
    display_name: str
    module_name: str
    install_hint: str = ""


@dataclass(frozen=True)
class OptionalRequirement:
    display_name: str
    module_name: str
    note: str = ""


def _module_exists(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except Exception:
        return False


def _normalise_libs(items: Iterable[Sequence[str] | LibraryRequirement]) -> list[LibraryRequirement]:
    out: list[LibraryRequirement] = []
    for item in items or []:
        if isinstance(item, LibraryRequirement):
            out.append(item)
        else:
            vals = list(item)
            display = vals[0] if len(vals) > 0 else ""
            module = vals[1] if len(vals) > 1 else display
            hint = vals[2] if len(vals) > 2 else ""
            if display and module:
                out.append(LibraryRequirement(display, module, hint))
    return out


def _normalise_optional(items: Iterable[Sequence[str] | OptionalRequirement]) -> list[OptionalRequirement]:
    out: list[OptionalRequirement] = []
    for item in items or []:
        if isinstance(item, OptionalRequirement):
            out.append(item)
        else:
            vals = list(item)
            display = vals[0] if len(vals) > 0 else ""
            module = vals[1] if len(vals) > 1 else display
            note = vals[2] if len(vals) > 2 else ""
            if display and module:
                out.append(OptionalRequirement(display, module, note))
    return out


def _show_messagebox(title: str, message: str, *, error: bool) -> bool:
    """Return True if a tkinter message could be shown."""
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        if error:
            messagebox.showerror(title, message, parent=root)
        else:
            messagebox.showwarning(title, message, parent=root)
        root.destroy()
        return True
    except Exception:
        return False


def format_missing_message(app_name: str,
                           missing_libs: Sequence[LibraryRequirement],
                           missing_helpers: Sequence[str],
                           missing_optional: Sequence[OptionalRequirement] = ()) -> str:
    lines: list[str] = []
    if missing_libs or missing_helpers:
        lines.extend([f"{app_name} cannot start because required components are missing.", ""])
    else:
        lines.extend([f"{app_name} can start, but optional components are missing.", ""])

    if missing_libs:
        lines.append("Required Python libraries:")
        for lib in missing_libs:
            hint = f"  (install with: {lib.install_hint})" if lib.install_hint else ""
            lines.append(f"  - {lib.display_name}{hint}")
        lines.append("")

    if missing_helpers:
        lines.append("Required FileTagger helper files:")
        for helper in missing_helpers:
            suffix = ".py" if not helper.endswith(".py") else ""
            lines.append(f"  - {helper}{suffix}")
        lines.append("")

    if missing_optional:
        lines.append("Optional features unavailable:")
        for opt in missing_optional:
            note = f"  ({opt.note})" if opt.note else ""
            lines.append(f"  - {opt.display_name}{note}")
        lines.append("")

    if missing_libs or missing_helpers:
        lines.append("Restore the missing helper files beside this program and install the listed libraries, then restart.")
    else:
        lines.append("The app will continue, but the listed optional features may be disabled.")
    return "\n".join(lines)


def check_startup_requirements(app_name: str,
                               required_libraries: Iterable[Sequence[str] | LibraryRequirement] = (),
                               required_helpers: Iterable[str] = (),
                               optional_libraries: Iterable[Sequence[str] | OptionalRequirement] = (),
                               *,
                               show_optional_warning: bool = False,
                               exit_on_missing: bool = True) -> list[OptionalRequirement]:
    """Check app-specific startup requirements.

    Call this before importing the app's ft_ helpers.

    Parameters
    ----------
    app_name:
        Name/version shown in the startup message.
    required_libraries:
        Iterable of (display_name, module_name, install_hint).
    required_helpers:
        Iterable of helper module names such as "ft_viewer".  The .py suffix
        is optional in messages; import discovery checks the module name.
    optional_libraries:
        Iterable of (display_name, module_name, note). Optional entries do not
        stop startup unless the app chooses to handle the returned list.
    show_optional_warning:
        If True, optional missing libraries are reported in a warning dialog.
        Otherwise they are printed to stdout and returned.
    exit_on_missing:
        If True, sys.exit(1) after showing required-missing diagnostics.

    Returns
    -------
    list[OptionalRequirement]
        Optional libraries that are missing.
    """
    libs = _normalise_libs(required_libraries)
    optional = _normalise_optional(optional_libraries)

    missing_libs = [lib for lib in libs if not _module_exists(lib.module_name)]
    missing_helpers = []
    for helper in required_helpers or []:
        name = str(helper).strip()
        if not name:
            continue
        module_name = name[:-3] if name.endswith(".py") else name
        if not _module_exists(module_name):
            missing_helpers.append(module_name)
    missing_optional = [opt for opt in optional if not _module_exists(opt.module_name)]

    if missing_libs or missing_helpers:
        message = format_missing_message(app_name, missing_libs, missing_helpers, missing_optional)
        if not _show_messagebox(f"{app_name} startup check failed", message, error=True):
            print(message, file=sys.stderr)
        if exit_on_missing:
            sys.exit(1)
    elif missing_optional:
        message = format_missing_message(app_name, [], [], missing_optional)
        if show_optional_warning:
            if not _show_messagebox(f"{app_name} optional components missing", message, error=False):
                print(message, file=sys.stderr)
        else:
            print(message)

    return missing_optional
