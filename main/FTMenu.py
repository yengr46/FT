"""
FTMenu.py — small launcher menu for the FileTagger app family.

Reads FTMenu.ini from the same folder as this script.
Launches Python apps from the configured HomeFolder.

Usage:
    python FTMenu.py
"""

from __future__ import annotations
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import configparser
import os
import subprocess
import sys
import tkinter as tk
from tkinter import messagebox, filedialog

APP_TITLE = "FTMenu — FileTagger Launcher"
INI_NAME = "FTMenu.ini"

BG = "#dddddd"
PANEL_BG = "#eeeeee"
TEXT = "#111111"
DIM = "#555555"
ACCENT = "#1a5276"
GREEN = "#1e8449"
RED = "#922b21"
BAR_FONT = ("Segoe UI", 11, "bold")
BAR_PAD_Y = 2


def _script_dir() -> str:
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except Exception:
        return os.getcwd()


def _ini_path() -> str:
    return os.path.join(_script_dir(), INI_NAME)


def _normalise_script_name(name: str) -> str:
    name = str(name or "").strip()
    if not name:
        return name
    root, ext = os.path.splitext(name)
    if not ext:
        name += ".py"
    return name


def _default_ini_text() -> str:
    # Default to the root folder (parent of main/) so relative script paths work.
    home = os.path.dirname(_script_dir())
    return (
        "[General]\n"
        f"HomeFolder = {home}\n"
        "PythonExe = \n"
        "\n"
        "[Image/PDF Organisation and Manipulation]\n"
        "1 = main/FTmod.py | FileTagger full system\n"
        "\n"
        "[FileTagger FT Image/PDF Standalone Utilities]\n"
        "2 = helpers/FTView.py | Viewer\n"
        "3 = helpers/FTImgedit.py | Editor\n"
        "4 = helpers/FTCompare.py | Compare Folders\n"
        "5 = helpers/FTFiler.py | Organisation and Renaming\n"
        "6 = helpers/FTMap.py | Display on Map\n"
    )


def ensure_ini_exists() -> None:
    path = _ini_path()
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(_default_ini_text())


class MenuItem:
    def __init__(self, number: str, script: str, description: str):
        self.number = str(number).strip()
        self.script = _normalise_script_name(script)
        self.description = str(description or "").strip()

    @property
    def label(self) -> str:
        return f"{self.number}. {self.script}: {self.description}"


class FTMenu(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.configure(bg=BG)
        self.minsize(360, 420)
        self.home_folder = ""
        self.python_exe = ""
        self.groups: list[tuple[str, list[MenuItem]]] = []
        self._status_var = tk.StringVar(value="Ready")
        self._build_ui()
        self.load_config()
        self._populate_menu()
        self._centre()

    def _centre(self):
        self.update_idletasks()
        w = max(370, self.winfo_width())
        h = max(480, self.winfo_height())
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 2)
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _build_ui(self):
        header = tk.Frame(self, bg=ACCENT)
        header.pack(fill="x")
        tk.Label(
            header,
            text="FTMenu",
            bg=ACCENT,
            fg="white",
            font=("Segoe UI", 18, "bold"),
            padx=12,
            pady=8,
        ).pack(side="left")
        tk.Label(
            header,
            text="FileTagger launcher",
            bg=ACCENT,
            fg="white",
            font=("Segoe UI", 15, "bold"),
            padx=8,
        ).pack(side="left", anchor="s", pady=(0, 10))

        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=10, pady=(10, 4))
        self.home_label = tk.Label(
            top,
            text="Home folder: ",
            bg=BG,
            fg=TEXT,
            anchor="w",
            font=("Segoe UI", 9),
            wraplength=460,
            justify="left",
        )
        self.home_label.pack(fill="x")

        home_buttons = tk.Frame(top, bg=BG)
        home_buttons.pack(fill="x", pady=(6, 0))
        for text, cmd in (
            ("Change Home", self.change_home_folder),
            ("Reload", self.reload_config),
            ("Open INI", self.open_ini),
        ):
            tk.Button(
                home_buttons,
                text=text,
                command=cmd,
                bg=ACCENT,
                fg="white",
                activebackground=ACCENT,
                activeforeground="white",
                font=("Segoe UI", 9, "bold"),
                relief="flat",
                padx=10,
                pady=4,
            ).pack(side="left", padx=(0, 6))

        self.menu_frame = tk.Frame(self, bg=BG)
        self.menu_frame.pack(fill="both", expand=True, padx=10, pady=6)

        footer = tk.Frame(self, bg=BG)
        footer.pack(fill="x", padx=10, pady=(0, 10))
        tk.Label(footer, textvariable=self._status_var, bg=BG, fg=DIM, anchor="w", font=("Segoe UI", 9)).pack(side="left", fill="x", expand=True)
        tk.Button(footer, text="Close", command=self.destroy, bg=RED, fg="white", padx=12).pack(side="right")

    def load_config(self):
        ensure_ini_exists()
        cfg = configparser.ConfigParser()
        cfg.optionxform = str
        cfg.read(_ini_path(), encoding="utf-8")

        self.home_folder = cfg.get("General", "HomeFolder", fallback="").strip()
        self.python_exe = cfg.get("General", "PythonExe", fallback="").strip() or sys.executable

        # Robustness: if HomeFolder is stale but the apps are beside FTMenu.py,
        # use the launcher folder. This covers deleted/rebuilt INIs and moved
        # test folders without falsely marking apps missing.
        script_folder = _script_dir()
        root_folder = os.path.dirname(script_folder)
        if self.home_folder and os.path.isdir(root_folder):
            known_apps = (
                os.path.join("main", "FTmod.py"),
                os.path.join("helpers", "FTView.py"),
                os.path.join("helpers", "FTImgedit.py"),
                os.path.join("helpers", "FTCompare.py"),
                os.path.join("helpers", "FTFiler.py"),
                os.path.join("helpers", "FTMap.py"),
            )
            home_has_any = any(os.path.isfile(os.path.join(self.home_folder, app)) for app in known_apps)
            root_has_any = any(os.path.isfile(os.path.join(root_folder, app)) for app in known_apps)
            if root_has_any and not home_has_any:
                self.home_folder = root_folder
                try:
                    cfg.set("General", "HomeFolder", self.home_folder)
                    with open(_ini_path(), "w", encoding="utf-8") as f:
                        cfg.write(f)
                except Exception:
                    pass

        groups: list[tuple[str, list[MenuItem]]] = []
        for section in cfg.sections():
            if section.lower() == "general":
                continue
            items: list[MenuItem] = []
            for key, value in cfg.items(section):
                value = str(value or "").strip()
                if not value:
                    continue
                if "|" in value:
                    script, desc = value.split("|", 1)
                elif ":" in value:
                    script, desc = value.split(":", 1)
                else:
                    script, desc = value, ""
                items.append(MenuItem(key, script.strip(), desc.strip()))
            items.sort(key=lambda item: int(item.number) if item.number.isdigit() else item.number)
            groups.append((section, items))
        self.groups = groups

    def reload_config(self):
        self.load_config()
        self._populate_menu()
        self._status_var.set("Reloaded FTMenu.ini")

    def _populate_menu(self):
        for child in self.menu_frame.winfo_children():
            child.destroy()

        self.home_label.config(text=f"Home folder: {self.home_folder or '(not set)'}")

        if not self.groups:
            tk.Label(
                self.menu_frame,
                text="No menu groups found in FTMenu.ini",
                bg=BG,
                fg=TEXT,
                font=("Segoe UI", 11),
            ).pack(anchor="w", pady=20)
            return

        for group_name, items in self.groups:
            panel = tk.Frame(self.menu_frame, bg=PANEL_BG, bd=0, relief="flat")
            panel.pack(fill="x", pady=(0, 8))
            tk.Label(
                panel,
                text=group_name,
                bg=PANEL_BG,
                fg=TEXT,
                font=("Segoe UI", 11, "bold"),
                anchor="w",
                padx=10,
                pady=2,
            ).pack(fill="x")

            for item in items:
                row = tk.Frame(panel, bg=PANEL_BG)
                row.pack(fill="x", padx=10, pady=(0, 3))
                exists = self._script_exists(item.script)
                btn_bg = ACCENT if exists else "#999999"
                btn = tk.Button(
                    row,
                    text=item.label,
                    command=lambda i=item: self.launch(i),
                    bg=btn_bg,
                    fg="white",
                    anchor="w",
                    relief="flat",
                    padx=10,
                    pady=BAR_PAD_Y,
                    font=BAR_FONT,
                )
                btn.pack(side="left", fill="x", expand=True)
                if not exists:
                    tk.Label(row, text="missing", bg=PANEL_BG, fg=RED, font=("Segoe UI", 9, "bold")).pack(side="right", padx=(8, 0))

    def _script_path(self, script: str) -> str:
        if os.path.isabs(script):
            return script
        return os.path.join(self.home_folder, script)

    def _script_exists(self, script: str) -> bool:
        return os.path.isfile(self._script_path(script))

    def launch(self, item: MenuItem):
        if not self.home_folder:
            messagebox.showerror("FTMenu", "HomeFolder is not set in FTMenu.ini", parent=self)
            return
        script_path = self._script_path(item.script)
        if not os.path.isfile(script_path):
            messagebox.showerror(
                "FTMenu",
                f"Cannot find:\n\n{script_path}\n\nCheck HomeFolder and script name in FTMenu.ini.",
                parent=self,
            )
            return
        py = self.python_exe or sys.executable
        try:
            subprocess.Popen([py, script_path], cwd=os.path.dirname(script_path))
            self._status_var.set(f"Launched {item.script}")
        except Exception as e:
            messagebox.showerror("FTMenu", f"Could not launch {item.script}:\n\n{e}", parent=self)

    def open_ini(self):
        ensure_ini_exists()
        path = _ini_path()
        try:
            if os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            messagebox.showerror("FTMenu", f"Could not open FTMenu.ini:\n\n{e}", parent=self)

    def change_home_folder(self):
        initial = self.home_folder if os.path.isdir(self.home_folder) else os.path.expanduser("~")
        folder = filedialog.askdirectory(title="Select FileTagger home folder", initialdir=initial, parent=self)
        if not folder:
            return
        self.home_folder = os.path.normpath(folder)
        self._save_home_folder()
        self.reload_config()

    def _save_home_folder(self):
        ensure_ini_exists()
        cfg = configparser.ConfigParser()
        cfg.optionxform = str
        cfg.read(_ini_path(), encoding="utf-8")
        if not cfg.has_section("General"):
            cfg.add_section("General")
        cfg.set("General", "HomeFolder", self.home_folder)
        if not cfg.has_option("General", "PythonExe"):
            cfg.set("General", "PythonExe", "")
        with open(_ini_path(), "w", encoding="utf-8") as f:
            cfg.write(f)


def main():
    app = FTMenu()
    app.mainloop()


if __name__ == "__main__":
    main()
