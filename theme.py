"""
FF8 Audio Monitor's look, shared by its windows (the main window and the Blitzball window): dark (the default) or
light. It is one choice for the whole app: the Dark mode box in either window switches both, and the app remembers
it in %APPDATA%\\FF8 Audio Monitor\\settings.json.
"""
import json
import os
from pathlib import Path
import tkinter as tk

# every colour the windows use, per look
PALETTES = {
    "dark": {
        "bg": "#181b20", "fg": "#e6e6e6", "muted": "#9aa4ae", "row_a": "#1f2329", "row_b": "#252a31",
        "head": "#2b3038", "btn": "#2b3038", "btn_hover": "#363c46", "btn_press": "#414854", "border": "#3a404a",
        "field": "#23272e", "tab": "#20242a", "tab_sel": "#2b3038", "sel": "#2f5a8f", "sel_fg": "#ffffff",
        "thumb": "#3a404a", "thumb_hover": "#4a515d", "trough": "#1b1e23",
        "music": "#79c0ff", "sfx": "#7ee787", "voice": "#d2a8ff", "ambient": "#56d4dd", "movie": "#f0a35e",
        "missing": "#ff7b72", "optional": "#e3b341", "quiet": "#8b949e", "mark": "#ffa657",
        "tip_bg": "#262b33", "tip_border": "#4a515d", "tip_fg": "#e6e6e6", "tip_title": "#ffffff", "tip_tree": "#9cdcfe",
        "tip_note": "#b8c0c8",
    },
    "light": {
        "bg": "#eef0f3", "fg": "#1f2328", "muted": "#57606a", "row_a": "#ffffff", "row_b": "#eef1f5",
        "head": "#e1e4e8", "btn": "#e1e4e8", "btn_hover": "#d0d7de", "btn_press": "#c4ccd5", "border": "#c4ccd5",
        "field": "#ffffff", "tab": "#e1e4e8", "tab_sel": "#ffffff", "sel": "#cfe3ff", "sel_fg": "#1f2328",
        "thumb": "#c4ccd5", "thumb_hover": "#afb8c1", "trough": "#eef0f3",
        "music": "#1f6feb", "sfx": "#1a7f37", "voice": "#8250df", "ambient": "#0e7490", "movie": "#c9770a",
        "missing": "#c0392b", "optional": "#9a6700", "quiet": "#7a7f87", "mark": "#b0402a",
        "tip_bg": "#fffbe6", "tip_border": "#d4c27a", "tip_fg": "#1f2328", "tip_title": "#000000", "tip_tree": "#0550ae",
        "tip_note": "#57606a",
    },
}

SETTINGS = "settings.json"
OLD_SETTINGS = "blitzball_window.json"      # (Monitor 2026.1016 kept the Blitzball window's choice there)

_listeners = []                             # each open window's "the look changed" function


def settings_dir():
    base = os.environ.get("APPDATA") or os.path.join(str(Path.home()), ".config")
    return Path(base) / "FF8 Audio Monitor"


def _read(name):
    try:
        with open(settings_dir() / name, encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def load():
    d = _read(SETTINGS)
    if "dark" not in d:                     # the choice made in the Blitzball window before there was one for all
        old = _read(OLD_SETTINGS).get("dark")
        if isinstance(old, bool):
            d["dark"] = old
    return d


def save(d):
    try:
        p = settings_dir()
        p.mkdir(parents=True, exist_ok=True)
        with open(p / SETTINGS, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except OSError:
        pass


def is_dark():
    return bool(load().get("dark", True))


def palette(dark):
    return PALETTES["dark" if dark else "light"]


def set_dark(dark):
    """A Dark mode box was ticked or cleared: remembered, and every open window follows."""
    d = load()
    d["dark"] = bool(dark)
    save(d)
    for fn in list(_listeners):
        try:
            fn(bool(dark))
        except tk.TclError:                 # (a window on its way out)
            pass


def listen(fn):
    if fn not in _listeners:
        _listeners.append(fn)


def forget(fn):
    if fn in _listeners:
        _listeners.remove(fn)


def style_common(st, prefix, P):
    """The ttk styles a window's frames, labels, buttons, tick boxes and scroll bars use: <prefix>.TFrame,
    <prefix>.TLabel, <prefix>.Muted.TLabel, <prefix>.TButton, <prefix>.TCheckbutton, <prefix>.Vertical.TScrollbar."""
    st.configure(prefix + ".TFrame", background=P["bg"])
    st.configure(prefix + ".TLabel", background=P["bg"], foreground=P["fg"])
    st.configure(prefix + ".Muted.TLabel", background=P["bg"], foreground=P["muted"])
    st.configure(prefix + ".TButton", background=P["btn"], foreground=P["fg"], bordercolor=P["border"],
                 lightcolor=P["btn"], darkcolor=P["btn"], focuscolor=P["btn"])
    st.map(prefix + ".TButton", background=[("pressed", P["btn_press"]), ("active", P["btn_hover"])],
           lightcolor=[("pressed", P["btn_press"]), ("active", P["btn_hover"])],
           darkcolor=[("pressed", P["btn_press"]), ("active", P["btn_hover"])])
    st.configure(prefix + ".TCheckbutton", background=P["bg"], foreground=P["fg"], indicatorbackground=P["field"],
                 indicatorforeground=P["fg"], focuscolor=P["bg"], bordercolor=P["border"],
                 lightcolor=P["field"], darkcolor=P["field"])
    st.map(prefix + ".TCheckbutton", background=[("active", P["bg"])], foreground=[("active", P["fg"])],
           indicatorbackground=[("pressed", P["btn_hover"]), ("active", P["field"])])
    st.configure(prefix + ".Vertical.TScrollbar", background=P["thumb"], troughcolor=P["trough"],
                 bordercolor=P["trough"], arrowcolor=P["fg"], lightcolor=P["thumb"], darkcolor=P["thumb"],
                 gripcount=0)
    st.map(prefix + ".Vertical.TScrollbar", background=[("active", P["thumb_hover"]), ("pressed", P["thumb_hover"])])


def _dwm_dark(win):
    try:
        import ctypes
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        val = ctypes.c_int(1 if getattr(win, "_dark_bar", True) else 0)
        for attr in (20, 19):                # DWMWA_USE_IMMERSIVE_DARK_MODE (19 on older Windows 10)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)) == 0:
                break
        ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x27)   # redraw the frame now
    except Exception:
        pass


def title_bar(win, dark):
    """Windows 10 / 11: the window's title bar dark too (nothing to do elsewhere). Once more a moment later, for a
    window that was not on the screen yet."""
    if os.name != "nt":
        return
    win._dark_bar = bool(dark)
    _dwm_dark(win)
    # (on the app's root: a window closed within the 0.3 s takes its own timers with it, the root's stays valid)
    win._root().after(300, lambda: _dwm_dark(win) if win.winfo_exists() else None)
