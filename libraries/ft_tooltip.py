"""
ft_tooltip.py — small Tkinter tooltip helper for FileTagger.

Extracted from FT.py in Phase 1.
"""

import tkinter as tk

class _Tooltip:
    """Simple hover tooltip for any tkinter widget."""
    def __init__(self, widget, text, delay=600):
        self.widget = widget
        self.text   = text
        self.delay  = delay
        self._id    = None
        self._win   = None
        widget.bind("<Enter>",   self._on_enter,  "+")
        widget.bind("<Leave>",   self._on_leave,  "+")
        widget.bind("<Button>",  self._on_leave,  "+")

    def _on_enter(self, event=None):
        self._cancel()
        try:
            if self.widget.winfo_exists():
                self._id = self.widget.after(self.delay, self._show)
        except: pass

    def _on_leave(self, event=None):
        self._cancel()
        self._hide()

    def _cancel(self):
        if self._id:
            try: self.widget.after_cancel(self._id)
            except: pass
            self._id = None

    def _show(self):
        if self._win: return
        try:
            if not self.widget.winfo_exists(): return
            x = self.widget.winfo_rootx() + 10
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
            self._win = tk.Toplevel(self.widget)
            self._win.overrideredirect(True)
            try: self._win.transient(self.widget.winfo_toplevel())
            except: pass
            self._win.configure(bg="#ffffcc")
            tk.Label(self._win, text=self.text, bg="#ffffcc", fg="#111111",
                     font=("Segoe UI", 9), padx=6, pady=3,
                     relief="solid", bd=1, wraplength=320, justify="left"
                     ).pack()
            self._win.geometry(f"+{x}+{y}")
        except: pass

    def _hide(self):
        if self._win:
            try: self._win.destroy()
            except: pass
            self._win = None

def _tip(widget, text):
    """Attach a tooltip to a widget. Returns the widget for chaining."""
    _Tooltip(widget, text)
    return widget

