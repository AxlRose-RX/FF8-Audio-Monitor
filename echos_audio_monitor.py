#!/usr/bin/env python3
"""
FF8 Audio Monitor  -  live view of the .ogg files FF8 (with FFNx) plays.

Open it, then start the game: nothing to press. The app notices the game
(FF8 2000, the 2013 Steam release or the Remaster, with FFNx), switches FFNx's
sound logging on inside it (trace_music, trace_sfx, trace_voice, trace_ambient,
trace_movies; and, as the game starts, use_external_music and use_external_sfx
when they're off), in the game's memory only: FFNx.toml is never changed. Then
it shows each music / sfx / voice / ambient .ogg as it is requested or played,
live, from the FFNx.log the game writes. See ffnx_setup.py. Tick the category
boxes to filter. Turn on the in-game overlay to float the latest lines over the
game.

Battle voice lines (voice/_battle/...) show every name FFNx tried for that one
moment on a single row, in the order it tried them (most specific first):
    voice/_battle/squall/cmd_draw_0007.ogg > cmd_draw > cmd_06_0007 > cmd_06

With use_external_music and use_external_sfx on, FFNx plays music and sound
effects only from .ogg files: one with no .ogg is silent (FFNx has no fallback
to the game's own sounds). Mods that bring their own .ogg files set them anyway.

Note: FFNx writes these audio events only to its log file, and flushes every
line, so tailing the log is real-time. The app does this for you in the
background; you never open the log yourself.

The Blitzball button opens a window of its own for the Blitzball mini game
(the Blitzball FF8 mod), which plays its own sounds and writes them to its own
log: see blitzball_window.py. Nothing in this window changes for it.

Dark mode (on by default) is one choice for both windows, remembered between
runs: see theme.py.

    pip install psutil        (gives the game's working folder, where FFNx.log is)
    python echos_audio_monitor.py
"""
import os
import re
import sys
import threading
import time
import queue
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog

import blitzball_window   # the Blitzball mini game's own window (its own log; kept apart from FFNx's)
import ffnx_setup         # the running game, FFNx's sound logging switched on inside it
import theme              # the app's look: dark (the default) or light, the same in every window

APP_VERSION = "2026.1019"   # release version (YYYY.MMDD); the GitHub build reads it from here
APP_TITLE = f"FF8 Audio Monitor v{APP_VERSION} - by AxlRose"
LOG_NAME  = "FFNx.log"

# The game-relative audio path has no spaces, so match from the category folder.
AUDIO_RE = re.compile(r'((?:data[\\/])?(music|sfx|voice|ambient|movies?)[\\/][^\s"\']*?\.ogg)', re.I)
# a movie is announced by a prepare_movie line ending in a video file
MOVIE_RE = re.compile(r'prepare_movie\s+(.+?\.(?:avi|webm|mp4|mov|bik|bk2|ogv))', re.I)
FRAME_RE = re.compile(r'^\[(\d+)\]')
CATS = ("music", "sfx", "voice", "ambient")
FILTER_CATS = CATS + ("movie",)      # categories that get a Show: checkbox
# battle voices: FFNx tries a chain of names for one action (most specific first)
BATTLE_PREFIX = "voice/_battle/"

# alternating row background + per-category text colour (light; dark mode takes theme.py's dark colours)
BG_A, BG_B = "#ffffff", "#eef1f5"
MOVIE_FG = "#c9770a"
CAT_FG = {"music": "#1f6feb", "sfx": "#1a7f37", "voice": "#8250df",
          "ambient": "#0e7490", "movie": MOVIE_FG}
MARK_FG = "#b0402a"
HINT_FG = "#666"

OVERLAY_LINES = 20
BIG_LOG = 32 * 1024 * 1024      # a game's log bigger than this: only its last part is read when the app finds it
BIG_LOG_TAIL = 2 * 1024 * 1024
NO_SOUND_HINT = 45.0            # seconds following a game with no sound line: the hint about it


def get_exe_dir():
    """Folder of the running .exe (when frozen by PyInstaller) or of this script."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def normalize(full, folder):
    """(matched path, folder) -> (category, clean relative path)."""
    p = full.replace("\\", "/")
    if p.lower().startswith("data/"):
        p = p[5:]
    folder = folder.lower()
    if folder in CATS:
        return folder, p
    # movies/ = FMV audio: the "_va" file is the voice layer, else music layer
    return ("voice" if p[:-4].endswith("_va") else "music"), p


def is_battle_voice(cat, path):
    """True for battle voice lines, where FFNx tries a chain of names in one go."""
    return cat == "voice" and path.lower().startswith(BATTLE_PREFIX)


def chain_text(paths):
    """Battle voice names tried in one frame -> one row: the first full path, then
    each fallback name in the order FFNx tried it. Folder and .ogg are left off
    when the fallback is in the same folder as the first one."""
    folder = paths[0].rsplit("/", 1)[0].lower() + "/"
    parts = [paths[0]]
    for p in paths[1:]:
        rest = p[len(folder):]
        if p.lower().startswith(folder) and "/" not in rest:
            parts.append(rest[:-4])                  # same folder: just the name
        else:
            parts.append(p)                          # other folder: full path
    return " > ".join(parts)


def find_game_log():
    """Return (label, log_path, pid) for a running FF8, or (None, None, None). The log is in the game's working
    folder (where FFNx writes it); the newest game with its FFNx.log there first."""
    first = None
    for g in ffnx_setup.running_games():
        log_path = os.path.join(g["folder"], LOG_NAME)
        found = (f"{g['name']} (PID {g['pid']})", log_path, g["pid"])
        if os.path.exists(log_path):
            return found                # the game, with its FFNx.log
        if first is None:
            first = found               # fallback: the log may not exist yet
    return first if first else (None, None, None)


class Tailer(threading.Thread):
    """Tail FFNx.log, parse audio events, push ('add'|'replace'|'mark', cat, path)."""

    def __init__(self, path, out_queue, from_start=False, big_skip=False, not_before=None):
        super().__init__(daemon=True)
        self.path = path
        self.q = out_queue
        self.from_start = from_start
        self.big_skip = big_skip            # from the start, but a very long log only from its last part
        self.not_before = not_before        # the game's start: an FFNx.log older than that is the session before
        self.stop = False
        self.finish = False                 # the game closed: read what it wrote, then stop
        self.opened = False                 # (the window asks: is the log there yet?)
        self._last = None  # (cat, path, frame)
        self._chain = None  # battle voice names on the current row, or None
        self._last_movie = None  # last prepared movie file, for the play line

    def run(self):
        # wait for the file; for a game just started, until FFNx has started it afresh (it empties it as the game
        # starts: until then it still holds the session before)
        while not self.stop and not self._ready():
            if self.finish:
                return
            time.sleep(0.25)
        if self.stop:
            return
        try:
            fh = open(self.path, "r", encoding="utf-8", errors="replace")
        except OSError:
            self.q.put(("mark", "", "-- could not open FFNx.log --"))
            return
        self.opened = True
        size = self._size()
        if self.from_start and self.big_skip and size > BIG_LOG:
            fh.seek(size - BIG_LOG_TAIL)        # a long session: its last part only
            fh.readline()                       # (from the start of a line)
            self.q.put(("mark", "", "-- a long log: its earlier lines are left out --"))
        elif self.from_start:
            fh.seek(0)                          # read the whole file (this game's session, or a saved log)
        else:
            fh.seek(0, os.SEEK_END)             # live only: skip existing history
        last_size = self._size()
        part, part_t = "", 0.0                  # (a line read before its end was written: the rest comes next)
        while not self.stop:
            line = fh.readline()
            if not line:                        # (Wine / Proton's C runtime keeps saying "end of file" until a seek)
                try:
                    fh.seek(0, os.SEEK_CUR)
                    line = fh.readline()
                except (OSError, ValueError):
                    pass
            if line:
                if not line.endswith("\n") and not self.finish:
                    if not part:
                        part_t = time.time()
                    part += line
                else:
                    self._parse_line(part + line)
                    part = ""
                if not part:
                    continue
            if part and (self.finish or time.time() - part_t > 1.0):
                self._parse_line(part)          # (its end never came: a log cut short)
                part = ""
            if self.finish:                     # (the game closed and everything it wrote is read)
                break
            size = self._size()
            if size < last_size:                # game restarted -> log truncated
                try:
                    fh.close()
                    fh = open(self.path, "r", encoding="utf-8", errors="replace")
                except OSError:
                    break
                self._last = None
                self._chain = None
                self._last_movie = None
                part = ""
                self.q.put(("mark", "", "-- game restarted --"))
            last_size = size
            time.sleep(0.08)
        fh.close()

    def _ready(self):
        try:
            return self.not_before is None or os.path.getmtime(self.path) >= self.not_before - 1
        except OSError:
            return False

    def _size(self):
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    # not named _handle: Python 3.13 uses Thread._handle internally, which hid this method
    def _parse_line(self, line):
        mv = MOVIE_RE.search(line)
        if mv:                                               # prepare_movie: the preload
            p = mv.group(1).replace("\\", "/")
            i = p.lower().rfind("movies/")
            rel = p[i:] if i != -1 else p.rsplit("/", 1)[-1]
            self._last_movie = rel
            self.q.put(("add", "movie", "movie (preload): " + rel))
            return
        if "TRACE: start_movie" in line:                     # start_movie: actual playback
            if self._last_movie:
                self.q.put(("add", "movie", "movie (play): " + self._last_movie))
            return
        found = AUDIO_RE.findall(line)
        if not found:
            return
        fr = FRAME_RE.match(line)
        frame = int(fr.group(1)) if fr else -1
        for full, folder in found:
            cat, path = normalize(full, folder)
            if folder.lower().startswith("movie"):           # movie audio layer
                if "playMovieAudio" not in line:             # skip the fileExists check
                    continue                                 # keep only the real play call
                label = "movie voice: " if cat == "voice" else "movie music: "
                self.q.put(("add", "movie", label + path))   # one line per layer
                continue
            if self._last and self._last[0] == cat and self._last[1] == path:
                continue                                     # exact repeat -> drop
            same_frame = bool(self._last) and self._last[0] == cat and self._last[2] == frame
            if is_battle_voice(cat, path):                   # battle: keep every name tried
                if same_frame and self._chain:
                    if path not in self._chain:              # same name twice -> show once
                        self._chain.append(path)
                        self.q.put(("replace", cat, chain_text(self._chain)))
                else:
                    self._chain = [path]                     # first name -> new row
                    self.q.put(("add", cat, path))
            elif same_frame and not self._chain:
                self.q.put(("replace", cat, path))           # same-frame fallback -> collapse
            else:
                self._chain = None
                self.q.put(("add", cat, path))
            self._last = (cat, path, frame)


class Overlay(tk.Toplevel):
    """Borderless always-on-top window that floats over the game. Drag the top
    bar to move it, drag the bottom-right grip to resize it."""

    MIN_W, MIN_H = 240, 90

    def __init__(self, master):
        super().__init__(master)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        try:
            self.attributes("-alpha", 0.88)
        except tk.TclError:
            pass
        self.configure(bg="#101216")
        self.geometry("560x260+60+60")   # resizable: drag the corner grip to change

        # top bar = move handle
        bar = tk.Frame(self, bg="#2a2f3a", cursor="fleur")
        bar.pack(fill="x")
        tk.Label(bar, text="Audio Monitor  (drag to move)", bg="#2a2f3a", fg="#c8ced9",
                 font=("Segoe UI", 8)).pack(side="left", padx=6)

        # bottom bar holds a resize grip on the right
        grip_bar = tk.Frame(self, bg="#2a2f3a")
        grip_bar.pack(side="bottom", fill="x")
        grip = tk.Label(grip_bar, text="\u25e2", bg="#2a2f3a", fg="#8b93a1",
                        cursor="size_nw_se", font=("Segoe UI", 11))
        grip.pack(side="right", padx=3)

        # text fills the rest; no wrap so names stay on one line (widen to read)
        self.txt = tk.Text(self, bg="#101216", fg="#e6e6e6", bd=0,
                           highlightthickness=0, wrap="none",
                           font=("Consolas", 11), padx=8, pady=6)
        self.txt.pack(fill="both", expand=True)
        self.txt.configure(state="disabled")
        for cat, col in CAT_FG.items():
            self.txt.tag_config(cat, foreground=col)


        for w in (bar,) + tuple(bar.winfo_children()):
            w.bind("<Button-1>", self._move_start)
            w.bind("<B1-Motion>", self._move_drag)
        grip.bind("<Button-1>", self._size_start)
        grip.bind("<B1-Motion>", self._size_drag)
        self.withdraw()

    def _move_start(self, e):
        self._ox, self._oy = e.x_root, e.y_root
        self._wx, self._wy = self.winfo_x(), self.winfo_y()

    def _move_drag(self, e):
        self.geometry(f"+{self._wx + e.x_root - self._ox}+{self._wy + e.y_root - self._oy}")

    def _size_start(self, e):
        self._sx, self._sy = e.x_root, e.y_root
        self._sw, self._sh = self.winfo_width(), self.winfo_height()

    def _size_drag(self, e):
        w = max(self.MIN_W, self._sw + e.x_root - self._sx)
        h = max(self.MIN_H, self._sh + e.y_root - self._sy)
        self.geometry(f"{w}x{h}+{self.winfo_x()}+{self.winfo_y()}")

    def render(self, rows):
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        for cat, path in rows[-OVERLAY_LINES:]:
            self.txt.insert("end", path + "\n", cat)
        self.txt.configure(state="disabled")
        self.txt.see("end")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("720x560")
        self.minsize(560, 420)

        # Set custom icon (icon.ico lives in the _internal bundle folder)
        try:
            base = Path(getattr(sys, "_MEIPASS", get_exe_dir()))
            icon_path = base / "icon.ico"
            if icon_path.exists():
                self.iconbitmap(str(icon_path))
        except Exception:
            pass

        self.q = queue.Queue()
        self.tailer = None
        self.game_pid = None      # PID of the game being followed, or None
        self.live_game = None     # the newest running game (ffnx_setup's info), followed or not
        self.saved_log = False    # a saved log is on show (a new game takes over)
        self.sounds_seen = 0      # sound lines since the game was followed (the hint when none come)
        self.follow_t0 = None
        self.master_events = []   # list of (cat, path) ; marks stored as ("mark", text)
        self.row_cats = []        # category per visible listbox row (for striping/replace)

        self.enabled = {c: tk.BooleanVar(value=True) for c in FILTER_CATS}
        self.live_overlay = tk.BooleanVar(value=False)
        self.dark = tk.BooleanVar(value=theme.is_dark())
        self.row_bg, self.cat_fg, self.mark_fg = (BG_A, BG_B), CAT_FG, MARK_FG

        self._build_ui()
        self.overlay = Overlay(self)
        self._apply_look()
        theme.listen(self._on_look)         # (the Blitzball window's Dark mode box switches this one too)
        self._hint()

        # the running game, its FFNx sound logging switched on inside it: all by itself (ffnx_setup.py)
        self.setup_q = queue.Queue()
        self.setup = ffnx_setup.Setup(self.setup_q)
        self.setup.start()
        self._status()

        self.after(80, self._drain)
        self.after(1000, self._check_following)
        self.protocol("WM_DELETE_WINDOW", self._close)

    # ---------------------------------------------------------------- UI
    def _build_ui(self):
        try:
            ttk.Style(self).theme_use("clam")
        except tk.TclError:
            pass

        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        ttk.Button(top, text="Locate FFNx.log", command=self._locate).pack(side="right")
        ttk.Button(top, text="Blitzball", command=self._blitzball).pack(side="right", padx=(0, 8))
        self.back_btn = ttk.Button(top, text="Back to the game", command=self._back_to_game)
        self.status = tk.StringVar(value="Looking for the game...")
        self.status_lbl = ttk.Label(top, textvariable=self.status)    # (after the buttons: a long one is cut, not them)
        self.status_lbl.pack(side="left", fill="x", expand=True)

        row = ttk.Frame(self, padding=(8, 0))
        row.pack(fill="x")
        ttk.Label(row, text="Show:").pack(side="left")
        for c in FILTER_CATS:
            ttk.Checkbutton(row, text=c, variable=self.enabled[c],
                            command=self._rebuild).pack(side="left", padx=(6, 0))
        ttk.Button(row, text="Clear", width=6, command=self._clear).pack(side="right")
        ttk.Checkbutton(row, text="In-game overlay", variable=self.live_overlay,
                        command=self._toggle_overlay).pack(side="right", padx=8)
        ttk.Checkbutton(row, text="Dark mode", variable=self.dark,
                        command=self._toggle_dark).pack(side="right", padx=(0, 6))

        mid = ttk.Frame(self, padding=8)
        mid.pack(fill="both", expand=True)
        self.listbox = tk.Listbox(mid, activestyle="none", font=("Consolas", 11),
                                  selectmode="extended", exportselection=False)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.listbox.yview)
        self.listbox.config(yscrollcommand=sb.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        # select lines (click, shift-click, ctrl-click) then Ctrl+C to copy
        for seq in ("<Control-c>", "<Control-C>", "<Command-c>"):
            self.listbox.bind(seq, self._copy_selection)
        for seq in ("<Control-a>", "<Control-A>", "<Command-a>"):
            self.listbox.bind(seq, self._select_all)
        self.listbox.bind("<Button-3>", self._popup_menu)
        self._menu = tk.Menu(self, tearoff=0)
        self._menu.add_command(label="Copy", command=self._copy_selection)
        self._menu.add_command(label="Select all", command=self._select_all)

        self.hint = ttk.Label(self, padding=(8, 0),
                              text="Nothing to press: open the app first, then start the game. The app switches on "
                                   "FFNx's sound logging inside the game (FFNx.toml isn't changed).",
                              foreground=HINT_FG)
        self.hint.pack(fill="x")
        # the light look is the window's own (as it always was): kept here for switching back from dark
        lb = ("bg", "fg", "selectbackground", "selectforeground", "highlightbackground", "highlightcolor")
        mn = ("bg", "fg", "activebackground", "activeforeground")
        self._light = {"listbox": {k: self.listbox.cget(k) for k in lb},
                       "menu": {k: self._menu.cget(k) for k in mn}, "bg": self.cget("bg")}

    # ---------------------------------------------------------------- the look (theme.py)
    def _toggle_dark(self):
        theme.set_dark(self.dark.get())     # every open window follows (this one through _on_look)

    def _on_look(self, dark):
        self.dark.set(dark)
        self._apply_look()

    def _apply_look(self):
        """Dark: theme.py's dark colours on everything here. Light: the window as it always looked."""
        dark = self.dark.get()
        if dark:
            P = theme.palette(True)
            theme.style_common(ttk.Style(self), "Main", P)
            self.row_bg, self.cat_fg, self.mark_fg = (P["row_a"], P["row_b"]), {c: P[c] for c in CAT_FG}, P["mark"]
            lb = {"bg": P["row_a"], "fg": P["fg"], "selectbackground": P["sel"], "selectforeground": P["sel_fg"],
                  "highlightbackground": P["border"], "highlightcolor": P["thumb_hover"]}
            mn = {"bg": P["field"], "fg": P["fg"], "activebackground": P["sel"], "activeforeground": P["sel_fg"]}
            bg, hint = P["bg"], P["muted"]
        else:
            self.row_bg, self.cat_fg, self.mark_fg = (BG_A, BG_B), CAT_FG, MARK_FG
            lb, mn, bg, hint = self._light["listbox"], self._light["menu"], self._light["bg"], HINT_FG
        self._restyle(self, dark)
        self.listbox.configure(**lb)
        self._menu.configure(**mn)
        self.configure(bg=bg)
        self.hint.configure(foreground=hint)
        for i, cat in enumerate(self.row_cats):     # the rows already there
            self.listbox.itemconfig(i, bg=self.row_bg[i % 2], fg=self._fg(cat))
        theme.title_bar(self, dark)

    def _restyle(self, w, dark):
        """This window's ttk widgets: the dark styles (Main.*), or their own ones back (light)."""
        for c in w.winfo_children():
            if isinstance(c, tk.Toplevel):
                continue                    # the overlay and the Blitzball window have their own looks
            cls = c.winfo_class()
            if cls in ("TFrame", "TLabel", "TButton", "TCheckbutton"):
                c.configure(style=("Main." + cls) if dark else "")
            elif cls == "TScrollbar":
                c.configure(style="Main.Vertical.TScrollbar" if dark else "")
            self._restyle(c, dark)

    def _fg(self, cat):
        return self.mark_fg if cat == "mark" else self.cat_fg.get(cat, self.listbox.cget("fg"))

    def _hint(self):
        for msg in (
            "Nothing to press: start FF8 (through Junction VIII) and its sounds show here.",
            " ",
            "The app switches FFNx's sound logging on inside the game, in its memory only (FFNx.toml",
            "is never changed, and it's all gone when the game closes):",
            "    trace_music, trace_sfx, trace_voice, trace_ambient, trace_movies",
            "and, if they're off, as the game starts (so open the app first, then the game):",
            "    use_external_music, use_external_sfx",
            "With those two on, FFNx plays music and sound effects only from .ogg files:",
            "one with no .ogg is silent. Mods with their own .ogg files set them anyway.",
            " ",
            "Blitzball mini game (Blitzball FF8 mod): click Blitzball, it has its own window.",
        ):
            self._add_mark(msg)

    # ---------------------------------------------------------------- the game, followed by itself
    def _follow(self, g, from_now=False):
        """A game started (or was running when the app opened): its FFNx.log from this session's first line (a very
        long one from its last part), then live. from_now: back to it after a saved log, from where it is now."""
        self.live_game = g
        self.game_pid = g["pid"]
        self.saved_log = False
        self.back_btn.pack_forget()
        if from_now:
            self._start_tail(g["log"], from_start=False)
            self._add_mark(f"-- back to the game: {g['log']}, from now on --")
        else:
            young = not g.get("created") or time.time() - g["created"] < 120
            self._start_tail(g["log"], from_start=True, big_skip=not young, not_before=g.get("created") or None)
            self._add_mark(f"-- {g['name']} started ({g['kind']}): following {g['log']} --")
        self.sounds_seen = 0
        self.follow_t0 = time.time()
        self._said_no_sound = False
        self._said_no_log = False
        self._status()
        if self.live_overlay.get():
            self.overlay.deiconify()            # re-show overlay for the new session

    def _game_gone(self, pid):
        if self.live_game and self.live_game["pid"] == pid:
            self.live_game = None
            self.back_btn.pack_forget()
        if pid == self.game_pid:
            self.game_pid = None
            if self.tailer:
                self.tailer.finish = True       # (what it wrote last is read, then the reading stops)
            self.overlay.withdraw()             # (the overlay feels part of the game: it goes with it)
            self._add_mark("-- game closed: the lines stay; the next start is followed by itself --")
        self._status()

    def _back_to_game(self):
        if self.live_game:
            self._follow(self.live_game, from_now=True)

    def _check_following(self):
        """Every second: no FFNx.log yet, or no sound line long after the game started: say what to do, once."""
        if self.game_pid is not None and self.tailer and self.follow_t0:
            t = time.time() - self.follow_t0
            if not self.tailer.opened and t > 10 and not self._said_no_log:
                self._said_no_log = True
                self._add_mark("-- No new FFNx.log in the game's folder yet: is FFNx installed there? (Junction VIII "
                               "installs it.) --")
            if self.tailer.opened and not self.sounds_seen and t > NO_SOUND_HINT and not self._said_no_sound:
                self._said_no_sound = True
                self._add_mark("-- No sound in FFNx.log yet: they show as the game plays them (see the lines above "
                               "about FFNx if there's a problem). --")
        self.after(1000, self._check_following)

    def _status(self):
        if self.saved_log:
            return
        if self.game_pid is not None and self.live_game:
            g = self.live_game
            self.status.set(f"Following {g['name']} ({g['kind']}): {g['folder']}")
        elif self.tailer is None:
            self.status.set("Waiting for FF8 to start.")
        else:
            self.status.set("Game closed. Waiting for the next start.")

    def _setup_event(self, ev):
        kind = ev[0]
        if kind == "note":
            self._add_mark("-- " + ev[1] + " --")
        elif kind == "game":
            g = ev[1]
            if self.game_pid is None or not self._attached_alive() or g["created"] >= (self.live_game or {}).get("created", 0):
                self._follow(g)
        elif kind == "gone":
            self._game_gone(ev[1])

    def _locate(self):
        path = filedialog.askopenfilename(
            title="Select FFNx.log",
            filetypes=[("FFNx log", "FFNx.log"), ("Log files", "*.log"), ("All files", "*.*")])
        if path:
            self.game_pid = None                # a picked file may be a closed session
            self.saved_log = True
            self._start_tail(path, from_start=True)    # read the whole file, list every .ogg
            self.status.set(f"Reading a saved log: {path}")
            if self.live_game:
                self.back_btn.pack(side="right", padx=(0, 8), before=self.status_lbl)

    def _start_tail(self, path, from_start=False, big_skip=False, not_before=None):
        if self.tailer:
            self.tailer.stop = True
        self.tailer = Tailer(path, self.q, from_start=from_start, big_skip=big_skip, not_before=not_before)
        self.tailer.start()
        if self.saved_log or not from_start:
            self._add_mark(f"-- reading {path} --")

    # ---------------------------------------------------------------- copy
    def _copy_selection(self, event=None):
        sel = self.listbox.curselection()
        if sel:
            self.clipboard_clear()
            self.clipboard_append("\n".join(self.listbox.get(i) for i in sel))
        return "break"

    def _select_all(self, event=None):
        self.listbox.selection_set(0, "end")
        return "break"

    def _popup_menu(self, event):
        idx = self.listbox.nearest(event.y)
        if idx >= 0 and idx not in self.listbox.curselection():
            self.listbox.selection_clear(0, "end")
            self.listbox.selection_set(idx)
        try:
            self._menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._menu.grab_release()

    # ---------------------------------------------------------------- events
    def _drain(self):
        for _ in range(200):
            try:
                ev = self.setup_q.get_nowait()
            except queue.Empty:
                break
            try:
                self._setup_event(ev)
            except Exception as e:              # (one bad event never stops the window's updates)
                self._add_mark(f"-- {type(e).__name__}: {e} --")
        changed = False
        try:
            while True:
                op, cat, path = self.q.get_nowait()
                changed = True
                if op != "mark":
                    self.sounds_seen += 1
                if op == "add":
                    self.master_events.append((cat, path))
                    if self.enabled.get(cat) and self.enabled[cat].get():
                        self._row_add(cat, path)
                elif op == "replace":
                    if self.master_events and self.master_events[-1][0] == cat:
                        self.master_events[-1] = (cat, path)
                        if self.row_cats and self.row_cats[-1] == cat:
                            self._row_replace_last(cat, path)
                        elif self.enabled.get(cat) and self.enabled[cat].get():
                            self._row_add(cat, path)
                elif op == "mark":
                    self.master_events.append(("mark", path))
                    self._row_add_mark(path)
        except queue.Empty:
            pass
        if changed and self.live_overlay.get():
            self.overlay.render(self._visible_rows())
        self.after(80, self._drain)

    def _visible_rows(self):
        return [(c, p) for c, p in self.master_events
                if c != "mark" and self.enabled[c].get()]

    # ---- listbox row helpers
    def _row_add(self, cat, path):
        i = self.listbox.size()
        self.listbox.insert("end", path)
        self.listbox.itemconfig(i, bg=self.row_bg[i % 2], fg=self._fg(cat))
        self.row_cats.append(cat)
        if self.live_overlay.get():
            self.listbox.see("end")

    def _row_replace_last(self, cat, path):
        i = self.listbox.size() - 1
        if i < 0:
            return self._row_add(cat, path)
        self.listbox.delete(i)
        self.listbox.insert(i, path)
        self.listbox.itemconfig(i, bg=self.row_bg[i % 2], fg=self._fg(cat))
        self.row_cats[-1] = cat
        if self.live_overlay.get():
            self.listbox.see("end")

    def _row_add_mark(self, text):
        i = self.listbox.size()
        self.listbox.insert("end", text)
        self.listbox.itemconfig(i, bg=self.row_bg[i % 2], fg=self.mark_fg)
        self.row_cats.append("mark")
        if self.live_overlay.get():
            self.listbox.see("end")

    # ---- full rebuild (on filter change / clear)
    def _rebuild(self):
        self.listbox.delete(0, "end")
        self.row_cats = []
        for cat, path in self.master_events:
            if cat == "mark":
                self._row_add_mark(path)
            elif self.enabled[cat].get():
                self._row_add(cat, path)
        self.listbox.see("end")
        if self.live_overlay.get():
            self.overlay.render(self._visible_rows())

    def _clear(self):
        self.master_events = []
        self._rebuild()

    def _attached_alive(self):
        # true only while following a live game (game_pid: the game being followed)
        if self.game_pid is None:
            return False
        try:
            import psutil
            return psutil.pid_exists(self.game_pid)
        except Exception:
            return True

    def _toggle_overlay(self):
        if self.live_overlay.get() and self._attached_alive():
            self.overlay.deiconify()
            self.overlay.render(self._visible_rows())
            self.listbox.see("end")
        else:
            self.overlay.withdraw()

    def _add_mark(self, text):
        self.master_events.append(("mark", text))
        self._row_add_mark(text)

    def _blitzball(self):
        # the Blitzball mini game's sounds: a window of its own (blitzball_window.py), same icon
        base = Path(getattr(sys, "_MEIPASS", get_exe_dir()))
        icon = base / "icon.ico"
        blitzball_window.open_window(self, find_game=find_game_log, icon=str(icon) if icon.exists() else None)

    def _close(self):
        theme.forget(self._on_look)
        self.setup.cmds.put(("stop",))
        if self.tailer:
            self.tailer.stop = True
        try:                                    # (the watcher may be pausing the game for a few ms: it lets it go
            self.setup.join(2)                  # on before the app ends)
        except RuntimeError:
            pass
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
