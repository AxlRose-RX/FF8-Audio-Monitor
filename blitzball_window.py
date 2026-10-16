"""
Blitzball window of FF8 Audio Monitor: the sounds of the Blitzball mini game (the Blitzball FF8 mod for Junction
VIII), live while you play and from saved logs.

Kept apart from the rest of the app on purpose: the main window still reads FFNx.log for the whole game, exactly as
before, and nothing here changes it. Blitzball plays its own music, sound effects and crowd (not through FFNx), so
FFNx.log never has them. When its trace options are on, Blitzball writes every sound it plays into its own log,
BlitzballFF8\\BlitzballFF8.log in the game folder (and keeps the run before as BlitzballFF8.prev.log). This window
reads that log.

Turn the options on in Junction VIII: Configure Mod, Blitzball FF8:
    Log Blitzball's music, Log Blitzball's sound effects, Log Blitzball's ambience
(BlitzballFF8.ini [Trace] TraceMusic, TraceSFX, TraceAmbient). No FFNx.toml settings are needed for them.

The lines it reads (Blitzball FF8 test 78 and later), one a sound:
    Blitzball <music|sfx|ambient>: <file> | <what happened> | <when it plays>
and each time Blitzball opens, the list (all of it the first time, then the ones whose files changed):
    Blitzball sound list: <music|sfx|ambient>: <file> | ready (...) or missing (...) | <when it plays>
From test 79 a sound can have exact ones under it (status_sleep.ogg under status.ogg: the general one plays while
the exact one isn't made yet); their list lines end with "| under <the sound above it>", and "not made yet" says an
exact one is missing (optional). Every name can have takes: name_1.ogg .. name_16.ogg.

Hover any line for the names that sound can have (its takes and the exact ones under it). Dark mode is on by
default: one choice for the whole app, shared with the main window and remembered (theme.py).
"""
import os
import re
import threading
import time
import queue
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog

import theme   # the app's look: dark or light, the same in every window

BLITZ_DIR = "BlitzballFF8"                  # Blitzball's folder in the game folder (its log, saves and settings)
BLITZ_LOG = "BlitzballFF8.log"
BLITZ_PREV = "BlitzballFF8.prev.log"
MAX_TAKES = 16                              # name_1.ogg .. name_16.ogg next to each name.ogg (Blitzball FF8 test 79)

LAYERS = ("music", "sfx", "ambient")
LAYER_LABEL = {"music": "Music", "sfx": "Sound effect", "ambient": "Ambience"}
MAX_ROWS = 5000                             # the Now playing list keeps the last 5000 (trimmed 500 at a time)

# the colours of both looks: theme.PALETTES (the blue band at the top is the same in both)
HEAD_BG = "#0d2a5c"

SOUND_RE = re.compile(r'Blitzball (sound list: )?(music|sfx|ambient): (\S+?\.ogg) \| (.*?) \| (.*?)(?: \| under (\S+?\.ogg))?$')
START_RE = re.compile(r'\]\s+(?:[A-Z]+:\s+)?BlitzballFF8 (.+)$')     # the log's first line: "BlitzballFF8 <build>"
TIME_RE = re.compile(r'^\[(\d\d:\d\d:\d\d)(?:\.\d+)?\]')            # Blitzball's own look: [hh:mm:ss.mmm]
FRAME_RE = re.compile(r'^\[(\d{6,})\]')                             # FFNx's look: [00012345] (a frame number)
VARIATION_RE = re.compile(r'_\d+\.ogg$', re.I)                      # name_1.ogg, name_2.ogg: takes of name.ogg


# ---------------------------------------------------------------- reading the log
def parse_line(line):
    """One line of BlitzballFF8.log -> a dict, or None for lines that are not about sounds.
    kind: "start" (the game started: a new log), "list" (the sound list), "play" (a sound now)."""
    m = START_RE.search(line)
    if m and "BlitzballFF8 " in line[:60]:
        return {"kind": "start", "build": m.group(1).strip()}
    m = SOUND_RE.search(line)
    if not m:
        return None
    tm = TIME_RE.match(line)
    fr = FRAME_RE.match(line) if not tm else None
    what = m.group(4).strip()
    return {
        "kind": "list" if m.group(1) else "play",
        "layer": m.group(2),
        "file": m.group(3).replace("\\", "/"),
        "what": what,
        "when": m.group(5).strip(),
        "under": (m.group(6) or "").replace("\\", "/"),
        "time": tm.group(1) if tm else ("frame " + str(int(fr.group(1))) if fr else ""),
        "state": state_of(what),
    }


def state_of(what):
    """What happened, in one word: missing, optional (an exact sound not made yet), ready, plays, quiet (not played,
    fades out, stops)."""
    w = what.lower()
    if w.startswith("missing") or "cannot be read" in w:
        return "missing"
    if w.startswith("not made yet"):
        return "optional"
    if w.startswith("ready"):
        return "ready"
    if w.startswith("plays") or w.startswith("fades back in"):
        return "plays"
    return "quiet"


def sound_of(file):
    """The sound a file belongs to: shot_1.ogg is a take of shot.ogg."""
    return VARIATION_RE.sub(".ogg", file)


def short(file):
    return file.rsplit("/", 1)[-1]


def takes_lines(name):
    """name.ogg and the first, second and last of its takes, as a little tree (the "..." are the ones between)."""
    base = name[:-4] if name.lower().endswith(".ogg") else name
    return [base + ".ogg", "|______ %s_1.ogg" % base, "|______ %s_2.ogg" % base, "|______ ...",
            "|______ %s_%d.ogg" % (base, MAX_TAKES)]


def tooltip_lines(file, checklist, max_kids=10):
    """What the popup on a line says: the names its sound can have (its takes, the exact ones under it) and what plays
    while it isn't there. -> [(text, kind)], kind: title, tree, note, kid."""
    snd = sound_of(file)
    e = checklist.get(snd)
    folder, name = snd.rsplit("/", 1) if "/" in snd else ("", snd)
    out = [(name + ("    " + e["when"] if e and e.get("when") else ""), "title")]
    out += [(t, "tree") for t in takes_lines(name)[1:]]
    out.append(("Up to %d takes next to it, numbered from 1 with no gaps. A different one plays each time, all of "
                "them before any plays again." % MAX_TAKES, "note"))
    kids = sorted(f for f, k in checklist.items() if k.get("under") == snd)
    if kids:
        out.append(("More exact ones (each optional, each can have its own takes):", "note"))
        wide = max(len(short(k)) for k in kids[:max_kids])
        for k in kids[:max_kids]:
            out.append(("|______ %-*s  %s" % (wide, short(k), checklist[k].get("when", "")), "kid"))
        if len(kids) > max_kids:
            out.append(("... and %d more: open the arrow next to %s in the Sound checklist" % (len(kids) - max_kids, name),
                        "note"))
    if e:
        out.append(("Now: " + e["status"], "note"))
        if e.get("under"):
            out.append(("When this file isn't there, %s plays instead (or the one above that)." % short(e["under"]), "note"))
    if folder:
        out.append(("Folder: %s/ (in your sound mod, or Blitzball's own Common folder)" % folder, "note"))
    return out


def open_shared(path):
    """Open a log for reading without stopping the game from renaming it when it starts again (Windows keeps a file
    open by another program from being renamed unless that program allows it: Blitzball moves the old log aside)."""
    if os.name == "nt":
        try:
            import ctypes
            import msvcrt
            from ctypes import wintypes
            create = ctypes.windll.kernel32.CreateFileW
            create.restype = wintypes.HANDLE
            create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                               wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
            h = create(str(path), 0x80000000, 0x1 | 0x2 | 0x4, None, 3, 0x80, None)   # read; share all; open existing
            if h and h != ctypes.c_void_p(-1).value:
                fd = msvcrt.open_osfhandle(h, os.O_RDONLY)
                return open(fd, "r", encoding="utf-8", errors="replace", newline="")
        except Exception:
            pass
    return open(path, "r", encoding="utf-8", errors="replace", newline="")


def same_file(fh, path):
    """True while the open log is still the file at path (Blitzball starts a new one each time the game starts)."""
    try:
        a, b = os.fstat(fh.fileno()), os.stat(path)
        if b.st_size < fh.tell():                    # emptied and written again from the start
            return False
    except (OSError, ValueError):
        return False
    if a.st_ino and b.st_ino:
        return a.st_ino == b.st_ino
    return True


class BlitzTail(threading.Thread):
    """Reads BlitzballFF8.log from its first line, then follows it as the game writes it. Each line about a sound goes
    into the queue as ("line", dict); ("new", text) when the game starts again (a new log); ("status", text)."""

    def __init__(self, path, out, follow=True):
        super().__init__(daemon=True)
        self.path = str(path)
        self.q = out
        self.follow = follow
        self.stop = False

    def run(self):
        told_wait = False
        while not self.stop:
            if not os.path.exists(self.path):
                if not self.follow:
                    self.q.put(("status", "That file is gone."))
                    return
                if not told_wait:
                    self.q.put(("status", "Waiting for Blitzball's log: open Blitzball once in the game (its menu at a save point)."))
                    told_wait = True
                time.sleep(0.5)
                continue
            try:
                fh = open_shared(self.path)
            except OSError:
                time.sleep(0.5)
                continue
            told_wait = False
            self.q.put(("new", self.path))
            self._read(fh)
            fh.close()
            if not self.follow:
                return

    def _read(self, fh):
        part = ""
        idle = 0.0
        while not self.stop:
            chunk = fh.readline()
            if chunk:
                part += chunk
                if not part.endswith("\n"):          # the game is still writing this line
                    continue
                d = parse_line(part.rstrip("\r\n"))
                part = ""
                if d:
                    self.q.put(("line", d))
                idle = 0.0
                continue
            if not self.follow:
                d = parse_line(part.rstrip("\r\n")) if part else None   # (a last line with no line end)
                if d:
                    self.q.put(("line", d))
                return
            time.sleep(0.1)
            idle += 0.1
            if idle >= 0.5:
                idle = 0.0
                if not same_file(fh, self.path):     # the game started again: a new log
                    return


# ---------------------------------------------------------------- the window
def draw_ball(cv, x, y, r):
    """A simple blitzball: a blue sphere with light rings and a shine (drawn here: no picture file needed)."""
    shades = ("#0b3d91", "#1453b5", "#1e6fd6", "#3a8ce8", "#5aa6f2")
    for i, c in enumerate(shades):
        k = r * (1.0 - i * 0.16)
        dx = -i * r * 0.05
        cv.create_oval(x - k + dx, y - k + dx, x + k + dx, y + k + dx, fill=c, outline="")
    cv.create_arc(x - r * 0.95, y - r * 0.55, x + r * 0.95, y + r * 0.55, start=200, extent=140,
                  style="arc", outline="#e8f4ff", width=3)
    cv.create_arc(x - r * 0.55, y - r * 0.95, x + r * 0.55, y + r * 0.95, start=110, extent=140,
                  style="arc", outline="#e8f4ff", width=3)
    cv.create_oval(x - r * 0.55, y - r * 0.62, x - r * 0.18, y - r * 0.32, fill="#ffffff", outline="")
    cv.create_oval(x - r, y - r, x + r, y + r, outline="#0a2a66", width=2)


class Tip:
    """The popup on a line: shows after the mouse rests on it a moment, goes when it moves off."""

    DELAY = 450

    def __init__(self, win):
        self.win = win
        self.top = None
        self.job = None
        self.key = None

    def schedule(self, key, x, y, lines):
        if key == self.key and (self.top or self.job):
            return
        self.hide()
        self.key = key
        self.job = self.win.after(self.DELAY, lambda: self.show(x, y, lines))

    def show(self, x, y, lines):
        self.job = None
        P = self.win.palette
        top = self.top = tk.Toplevel(self.win)
        top.overrideredirect(True)
        try:
            top.attributes("-topmost", True)
        except tk.TclError:
            pass
        box = tk.Frame(top, bg=P["tip_bg"], highlightthickness=1, highlightbackground=P["tip_border"])
        box.pack()
        mono = ("Consolas", 10) if os.name == "nt" else ("DejaVu Sans Mono", 9)
        for text, kind in lines:
            if kind == "title":
                lab = tk.Label(box, text=text, bg=P["tip_bg"], fg=P["tip_title"], font=("Segoe UI", 10, "bold"),
                               anchor="w", justify="left", wraplength=760)
            elif kind in ("tree", "kid"):
                lab = tk.Label(box, text=text, bg=P["tip_bg"], fg=P["tip_tree"] if kind == "tree" else P["tip_fg"],
                               font=mono, anchor="w", justify="left")
            else:
                lab = tk.Label(box, text=text, bg=P["tip_bg"], fg=P["tip_note"], font=("Segoe UI", 9), anchor="w",
                               justify="left", wraplength=760)
            lab.pack(fill="x", padx=10, pady=(6 if kind == "title" else 0, 0))
        tk.Frame(box, height=6, bg=P["tip_bg"]).pack()
        top.update_idletasks()
        w, h = top.winfo_reqwidth(), top.winfo_reqheight()
        sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
        px, py = x + 18, y + 14
        if px + w > sw - 4:
            px = max(4, x - w - 12)
        if py + h > sh - 4:
            py = max(4, y - h - 10)
        top.geometry("+%d+%d" % (px, py))

    def hide(self, *_):
        if self.job:
            self.win.after_cancel(self.job)
            self.job = None
        if self.top is not None:
            try:
                self.top.destroy()
            except tk.TclError:
                pass
            self.top = None
        self.key = None


class BlitzballWindow(tk.Toplevel):
    """Its own window, next to the main one: what Blitzball plays now, and the checklist of its sounds."""

    def __init__(self, master, find_game=None, icon=None):
        super().__init__(master)
        self.find_game = find_game          # the main window's game finder: (label, FFNx.log path, pid) or Nones
        self.title("Blitzball sounds (FF8 Audio Monitor)")
        self.geometry("1060x660")
        self.minsize(760, 480)
        if icon:
            try:
                self.iconbitmap(icon)
            except Exception:
                pass
        self.dark = tk.BooleanVar(value=theme.is_dark())
        self.palette = theme.palette(self.dark.get())
        self.q = queue.Queue()
        self.tail = None
        self.watching_game = False
        self.rows = []                      # (layer, state, values) of every sound seen, for the filters
        self._live_n = 0                    # rows in the Now playing list (their stripes)
        self.checklist = {}                 # file -> {layer, status, state, when, under}
        self._check_file = {}               # checklist row -> its file
        self.show = {k: tk.BooleanVar(value=True) for k in LAYERS}
        self.only_missing = tk.BooleanVar(value=False)
        self.tip = Tip(self)
        self._drain_job = self._retry_job = None
        self._scroll = False                # (the list scrolls to its end once a batch of lines is in)
        self._build()
        self._apply_theme()
        theme.listen(self._on_look)         # (the main window's Dark mode box switches this one too)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._drain_job = self.after(100, self._drain)
        self._retry_job = self.after(300, self.watch_game)    # starts by itself: nothing to press

    # ---- layout
    def _build(self):
        head = tk.Frame(self, bg=HEAD_BG)
        head.pack(fill="x")
        cv = tk.Canvas(head, width=76, height=76, bg=HEAD_BG, highlightthickness=0)
        cv.pack(side="left", padx=(12, 6), pady=8)
        draw_ball(cv, 38, 38, 32)
        txt = tk.Frame(head, bg=HEAD_BG)
        txt.pack(side="left", fill="x", expand=True)
        tk.Label(txt, text="Blitzball", bg=HEAD_BG, fg="#ffffff", font=("Segoe UI", 22, "bold")).pack(anchor="w")
        tk.Label(txt, text="The sounds of the Blitzball mini game, as they play", bg=HEAD_BG, fg="#c9dcff",
                 font=("Segoe UI", 11)).pack(anchor="w")

        bar = ttk.Frame(self, padding=(10, 8, 10, 2), style="Blitz.TFrame")
        bar.pack(fill="x")
        ttk.Button(bar, text="Watch the game", command=self.watch_game, style="Blitz.TButton").pack(side="left")
        ttk.Button(bar, text="Open a saved log...", command=self.open_log, style="Blitz.TButton").pack(side="left", padx=6)
        ttk.Button(bar, text="Save this list...", command=self.save_list, style="Blitz.TButton").pack(side="left")
        ttk.Button(bar, text="Clear", width=7, command=self.clear, style="Blitz.TButton").pack(side="right")
        ttk.Checkbutton(bar, text="Dark mode", variable=self.dark, command=self._toggle_dark,
                        style="Blitz.TCheckbutton").pack(side="right", padx=(0, 14))
        self.status = tk.StringVar(value="Looking for the game...")
        ttk.Label(self, textvariable=self.status, padding=(12, 2), font=("Segoe UI", 10),
                  style="Blitz.TLabel").pack(fill="x")

        nb = self.tabs = ttk.Notebook(self, style="Blitz.TNotebook")
        nb.pack(fill="both", expand=True, padx=10, pady=6)
        nb.bind("<<NotebookTabChanged>>", self.tip.hide)

        # tab 1: every sound as it plays
        t1 = ttk.Frame(nb, padding=6, style="Blitz.TFrame")
        nb.add(t1, text="  Now playing  ")
        flt = ttk.Frame(t1, style="Blitz.TFrame")
        flt.pack(fill="x", pady=(0, 4))
        ttk.Label(flt, text="Show:", style="Blitz.TLabel").pack(side="left")
        for k in LAYERS:
            ttk.Checkbutton(flt, text=LAYER_LABEL[k], variable=self.show[k], command=self._refill,
                            style="Blitz.TCheckbutton").pack(side="left", padx=(8, 0))
        ttk.Checkbutton(flt, text="Only the missing ones", variable=self.only_missing, command=self._refill,
                        style="Blitz.TCheckbutton").pack(side="left", padx=(18, 0))
        self.live = self._table(t1, (("time", "Time", 70), ("type", "Type", 100), ("file", "Sound file", 300),
                                     ("what", "What happened", 300), ("when", "When it plays", 420)))
        self.live.bind("<Motion>", lambda e: self._hover(self.live, e))

        # tab 2: every sound Blitzball has a place for, ready or still missing, the exact ones under theirs
        t2 = ttk.Frame(nb, padding=6, style="Blitz.TFrame")
        nb.add(t2, text="  Sound checklist  ")
        self.count = tk.StringVar(value="")
        ttk.Label(t2, textvariable=self.count, font=("Segoe UI", 10, "bold"), style="Blitz.TLabel").pack(anchor="w", pady=(0, 4))
        self.check = self._table(t2, (("type", "Type", 100), ("status", "Ready?", 330), ("when", "When it plays", 460)),
                                 tree=("Sound file", 330))
        self.check.bind("<Motion>", lambda e: self._hover(self.check, e))
        for ev in ("<<TreeviewOpen>>", "<<TreeviewClose>>"):
            self.check.bind(ev, lambda e: self.after_idle(self._restripe))

        hint = ttk.Label(self, padding=(12, 0, 12, 8), wraplength=1000, justify="left", style="Blitz.Muted.TLabel",
                         text="To see Blitzball's sounds here: in Junction VIII, Configure Mod, Blitzball FF8, turn on "
                              "\"Log Blitzball's music\", \"Log Blitzball's sound effects\" and \"Log Blitzball's ambience\". "
                              "Then play. No FFNx.toml settings are needed for Blitzball. Red: a sound still missing. "
                              "Yellow: an exact one you can add (optional: the general one plays meanwhile). Rest the mouse "
                              "on a line to see every name that sound can have. New files play the next time Blitzball opens.")
        hint.pack(fill="x")
        hint.bind("<Configure>", lambda e: hint.configure(wraplength=max(200, e.width - 30)))

    def _table(self, parent, cols, tree=None):
        box = ttk.Frame(parent, style="Blitz.TFrame")
        box.pack(fill="both", expand=True)
        tv = ttk.Treeview(box, columns=[c[0] for c in cols], show="tree headings" if tree else "headings",
                          selectmode="extended", style="Blitz.Treeview")
        if tree:
            tv.heading("#0", text=tree[0], anchor="w")
            tv.column("#0", width=tree[1], anchor="w", stretch=False)
        for key, label, width in cols:
            tv.heading(key, text=label, anchor="w")
            tv.column(key, width=width, anchor="w", stretch=key in ("when", "what"))
        sb = ttk.Scrollbar(box, orient="vertical", command=tv.yview, style="Blitz.Vertical.TScrollbar")
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for seq in ("<Control-c>", "<Control-C>"):
            tv.bind(seq, lambda e, t=tv: self._copy(t))
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Copy", command=lambda t=tv: self._copy(t))
        tv.bind("<Button-3>", lambda e, m=menu: (self.tip.hide(), m.tk_popup(e.x_root, e.y_root)))
        tv.bind("<Leave>", self.tip.hide)
        for seq in ("<ButtonPress>", "<MouseWheel>", "<Button-4>", "<Button-5>"):
            tv.bind(seq, self.tip.hide, add="+")
        tv._menu = menu
        return tv

    # ---- the look: dark (the default) or light, one choice for the whole app (theme.py), remembered
    def _toggle_dark(self):
        theme.set_dark(self.dark.get())     # every open window follows (this one through _on_look)

    def _on_look(self, dark):
        self.dark.set(dark)
        self._apply_theme()

    def _apply_theme(self):
        P = self.palette = theme.palette(self.dark.get())
        st = ttk.Style(self)
        theme.style_common(st, "Blitz", P)  # frames, labels, buttons, tick boxes, scroll bars
        st.configure("Blitz.TButton", padding=(10, 4))
        st.configure("Blitz.TNotebook", background=P["bg"], bordercolor=P["border"], lightcolor=P["bg"],
                     darkcolor=P["bg"], tabmargins=(2, 4, 2, 0))
        st.configure("Blitz.TNotebook.Tab", background=P["tab"], foreground=P["muted"], bordercolor=P["border"],
                     lightcolor=P["tab"], darkcolor=P["tab"], padding=(12, 5))
        st.map("Blitz.TNotebook.Tab", background=[("selected", P["tab_sel"]), ("active", P["btn_hover"])],
               foreground=[("selected", P["fg"]), ("active", P["fg"])],
               lightcolor=[("selected", P["tab_sel"])])
        st.configure("Blitz.Treeview", background=P["row_a"], fieldbackground=P["row_a"], foreground=P["fg"],
                     bordercolor=P["border"], lightcolor=P["row_a"], darkcolor=P["row_a"], rowheight=22)
        st.map("Blitz.Treeview", background=[("selected", P["sel"])], foreground=[("selected", P["sel_fg"])])
        st.configure("Blitz.Treeview.Heading", background=P["head"], foreground=P["fg"], bordercolor=P["border"],
                     lightcolor=P["head"], darkcolor=P["head"], relief="flat")
        st.map("Blitz.Treeview.Heading", background=[("active", P["btn_hover"])])
        self.configure(bg=P["bg"])
        for tv in (self.live, self.check):
            for k in LAYERS:
                tv.tag_configure(k, foreground=P[k])
            tv.tag_configure("missing", foreground=P["missing"])
            tv.tag_configure("optional", foreground=P["optional"])
            tv.tag_configure("quiet", foreground=P["quiet"])
            tv.tag_configure("mark", foreground=P["mark"])
            tv.tag_configure("odd", background=P["row_b"])
            tv._menu.configure(bg=P["field"], fg=P["fg"], activebackground=P["sel"], activeforeground=P["sel_fg"])
        theme.title_bar(self, self.dark.get())

    # ---- the popup on a line
    def _hover(self, tv, e):
        row = tv.identify_row(e.y)
        if not row:
            self.tip.hide()
            return
        if tv is self.live:
            vals = tv.item(row, "values")
            file = vals[2] if len(vals) > 2 else ""
        else:
            file = self._check_file.get(row, "")
        if not file.lower().endswith(".ogg"):
            self.tip.hide()
            return
        self.tip.schedule((str(tv), row), e.x_root, e.y_root, tooltip_lines(file, self.checklist))

    # ---- where the log comes from
    def watch_game(self):
        """The running game's BlitzballFF8.log, from its first line (this game's whole session), then live."""
        if self._retry_job:
            self.after_cancel(self._retry_job)
            self._retry_job = None
        found = self.find_game() if self.find_game else (None, None, None)
        path = found[1] if found else None
        if not path:
            self.status.set("Waiting for FF8 to start (this window finds it by itself). Or open a saved log.")
            self.watching_game = True
            self._retry_job = self.after(1500, self._retry_find)
            return
        log = Path(path).parent / BLITZ_DIR / BLITZ_LOG
        self._start(log, follow=True, label=f"Watching the game: {log}")

    def _retry_find(self):
        self._retry_job = None
        if self.watching_game and not self.tail:
            self.watch_game()

    def open_log(self):
        start = None
        if self.tail:
            start = str(Path(self.tail.path).parent)
        path = filedialog.askopenfilename(
            parent=self, title="Open Blitzball's log", initialdir=start,
            filetypes=[("Blitzball log", "BlitzballFF8*.log"), ("Log files", "*.log"), ("All files", "*.*")])
        if path:
            self.watching_game = False
            self._start(Path(path), follow=False, label=f"Reading a saved log: {path}")

    def _start(self, path, follow, label):
        if self.tail:
            self.tail.stop = True
        self.clear()
        self.checklist.clear()
        self._fill_check()
        self.q = queue.Queue()              # (a new one: lines the last log still had waiting stay out)
        self.tail = BlitzTail(path, self.q, follow=follow)
        self.tail.start()
        self.status.set(label)

    # ---- what comes in
    def _drain(self):
        check_changed = False
        try:
            for _ in range(800):
                kind, d = self.q.get_nowait()
                if kind == "line":
                    check_changed |= self._line(d)
                elif kind == "new":
                    if self.rows or self.checklist:
                        self._mark("The game started again: a new log")
                    self.checklist.clear()
                    check_changed = True
                elif kind == "status":
                    self.status.set(d)
        except queue.Empty:
            pass
        if check_changed:
            self._fill_check()
        if self._scroll:
            kids = self.live.get_children()
            if kids:
                self.live.see(kids[-1])
            self._scroll = False
        self._drain_job = self.after(100, self._drain)

    def _line(self, d):
        """One line in; True when the checklist changed."""
        if d["kind"] == "start":
            self._mark("Blitzball FF8 started: " + d["build"])
            return False
        layer = d["layer"]
        if d["kind"] == "list":
            self.checklist[d["file"]] = {"layer": layer, "status": d["what"], "state": d["state"], "when": d["when"],
                                         "under": d["under"]}
            return True
        values = (d["time"], LAYER_LABEL[layer], d["file"], d["what"], d["when"])
        self.rows.append((layer, d["state"], values))
        if len(self.rows) > MAX_ROWS + 500:
            del self.rows[:len(self.rows) - MAX_ROWS]
            self._refill()
        elif self._shown(layer, d["state"]):
            self._add_live(layer, d["state"], values)
        snd = sound_of(d["file"])                        # (shot_1.ogg is a take of shot.ogg: one sound)
        known = self.checklist.get(snd)
        under = known["under"] if known else ""
        if d["state"] in ("missing", "optional") and (not known or known["state"] not in ("missing", "optional")):
            self.checklist[snd] = {"layer": layer, "status": d["what"], "state": d["state"], "when": d["when"], "under": under}
            return True
        if d["state"] == "plays" and not d["what"].startswith("fades") and (not known or known["state"] in ("missing", "optional")):
            self.checklist[snd] = {"layer": layer, "status": "ready", "state": "ready", "when": d["when"], "under": under}
            return True
        return False

    def _shown(self, layer, state):
        if not self.show[layer].get():
            return False
        return state in ("missing", "optional") or not self.only_missing.get()

    def _add_live(self, layer, state, values):
        tag = "mark" if layer == "mark" else (state if state in ("missing", "optional", "quiet") else layer)
        self.live.insert("", "end", values=values, tags=(tag,) + (("odd",) if self._live_n % 2 else ()))
        self._live_n += 1
        self._scroll = True

    def _mark(self, text):
        values = ("", "", text, "", "")
        self.rows.append(("mark", "mark", values))
        self._add_live("mark", "mark", values)

    def _refill(self):
        self.tip.hide()
        self.live.delete(*self.live.get_children())
        self._live_n = 0
        for layer, state, values in self.rows:
            if layer == "mark" or self._shown(layer, state):
                self._add_live(layer, state, values)

    def _tree(self):
        """The checklist as a tree: the general sounds, each one's exact ones under it (by their "under")."""
        kids, tops = {}, []
        for f, e in self.checklist.items():
            u = e.get("under")
            if u and u in self.checklist and u != f:
                kids.setdefault(u, []).append(f)
            else:
                tops.append(f)
        order = {"music": 0, "sfx": 1, "ambient": 2}
        tops.sort(key=lambda f: (order.get(self.checklist[f]["layer"], 3), f))
        for k in kids.values():
            k.sort()
        return tops, kids

    def _fill_check(self):
        tv = self.check
        was_open = {f for iid, f in self._check_file.items() if tv.exists(iid) and tv.item(iid, "open")}
        tv.delete(*tv.get_children())
        self._check_file = {}
        if not self.checklist:
            self.count.set("No list yet: it comes when Blitzball first opens in the game (with its logs on).")
            return
        tops, kids = self._tree()
        general = [f for f, e in self.checklist.items() if not e.get("under")]
        exact = [f for f, e in self.checklist.items() if e.get("under")]
        ready = sum(1 for f in general if self.checklist[f]["state"] != "missing")
        made = sum(1 for f in exact if self.checklist[f]["state"] not in ("missing", "optional"))
        text = f"{len(general)} sounds: {ready} ready, {len(general) - ready} still missing"
        if exact:
            text += f".  {len(exact)} exact ones you can add under them (open the arrows): {made} made"
        self.count.set(text)

        def add(parent, f):
            e = self.checklist[f]
            tag = e["state"] if e["state"] in ("missing", "optional") else e["layer"]
            iid = tv.insert(parent, "end", text=f if not parent else short(f), open=f in was_open,
                            values=(LAYER_LABEL[e["layer"]], e["status"], e["when"]), tags=(tag,))
            self._check_file[iid] = f
            for k in kids.get(f, []):
                add(iid, k)

        for f in tops:
            add("", f)
        self._restripe()

    def _restripe(self):
        """Stripes on the rows that show (the tree opens and closes)."""
        tv = self.check
        n = 0

        def walk(parent):
            nonlocal n
            for iid in tv.get_children(parent):
                tags = [t for t in tv.item(iid, "tags") if t != "odd"]
                if n % 2:
                    tags.append("odd")
                tv.item(iid, tags=tags)
                n += 1
                if tv.item(iid, "open"):
                    walk(iid)

        walk("")

    # ---- buttons
    def clear(self):
        self.tip.hide()
        self.rows = []
        self.live.delete(*self.live.get_children())
        self._live_n = 0

    def save_list(self):
        path = filedialog.asksaveasfilename(parent=self, title="Save the list", defaultextension=".txt",
                                            initialfile="blitzball_sounds.txt",
                                            filetypes=[("Text", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        self.write_list(path)
        self.status.set(f"Saved: {path}")

    def write_list(self, path):
        tops, kids = self._tree()
        with open(path, "w", encoding="utf-8") as f:
            f.write("Blitzball sounds (FF8 Audio Monitor)\n\nSound checklist (the exact ones indented under theirs; every "
                    "name can have takes: name_1.ogg .. name_%d.ogg)\n" % MAX_TAKES)

            def out(fl, depth):
                e = self.checklist[fl]
                name = fl if depth == 0 else short(fl)
                f.write(f"  {'  ' * depth}{LAYER_LABEL[e['layer']]:<13} {name:<48} {e['status']}  ({e['when']})\n")
                for k in kids.get(fl, []):
                    out(k, depth + 1)

            for fl in tops:
                out(fl, 0)
            f.write("\nNow playing\n")
            for layer, state, values in self.rows:
                f.write("  " + "  ".join(v for v in values if v) + "\n")

    def _copy(self, tv):
        sel = tv.selection()
        if sel:
            self.clipboard_clear()
            lines = []
            for i in sel:
                vals = [str(v) for v in tv.item(i, "values") if v]
                if tv is self.check:
                    vals.insert(0, self._check_file.get(i, ""))
                lines.append("  ".join(vals))
            self.clipboard_append("\n".join(lines))
        return "break"

    def _close(self):
        theme.forget(self._on_look)
        self.tip.hide()
        for job in (self._drain_job, self._retry_job):
            if job:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        if self.tail:
            self.tail.stop = True
        self.watching_game = False
        self.destroy()


def open_window(master, find_game=None, icon=None):
    """The main window's Blitzball button: this window (only one; a second press brings it to the front)."""
    w = getattr(master, "_blitzball_window", None)
    if w is not None and w.winfo_exists():
        w.deiconify()
        w.lift()
        w.focus_force()
        return w
    w = BlitzballWindow(master, find_game=find_game, icon=icon)
    master._blitzball_window = w
    return w
