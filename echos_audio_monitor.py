#!/usr/bin/env python3
"""
FF8 Audio Monitor  -  live view of the .ogg files FF8 (with FFNx) plays.

Click "Attach to FF8". The app finds the running game, locates the FFNx.log it
writes next to the game exe, and shows each music / sfx / voice / ambient .ogg
as it is requested or played, live. Tick the category boxes to filter. Turn on
the in-game overlay to float the latest lines over the game.

Enable these in FFNx.toml so FFNx logs the events:
    trace_music = true
    trace_sfx = true
    trace_voice = true
    trace_ambient = true
    trace_movies = true

For sfx and music you also need (set them in FFNx.toml or in the mod.xml):
    use_external_sfx = true
    use_external_music = true

Note: FFNx writes these audio events only to its log file, and flushes every
line, so tailing the log is real-time. The app does this for you in the
background; you never open the log yourself.

    pip install psutil        (lets the app auto-find the game; optional)
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

APP_VERSION = "2026.1001"   # release version (YYYY.MMDD); the GitHub build reads it from here
APP_TITLE = f"FF8 Audio Monitor v{APP_VERSION} - by AxlRose"
LOG_NAME  = "FFNx.log"

# The game-relative audio path has no spaces, so match from the category folder.
AUDIO_RE = re.compile(r'((?:data[\\/])?(music|sfx|voice|ambient|movies?)[\\/][^\s"\']*?\.ogg)', re.I)
# a movie is announced by a prepare_movie line ending in a video file
MOVIE_RE = re.compile(r'prepare_movie\s+(.+?\.(?:avi|webm|mp4|mov|bik|bk2|ogv))', re.I)
FRAME_RE = re.compile(r'^\[(\d+)\]')
CATS = ("music", "sfx", "voice", "ambient")
FILTER_CATS = CATS + ("movie",)      # categories that get a Show: checkbox

# alternating row background + per-category text colour
BG_A, BG_B = "#ffffff", "#eef1f5"
MOVIE_FG = "#c9770a"
CAT_FG = {"music": "#1f6feb", "sfx": "#1a7f37", "voice": "#8250df",
          "ambient": "#0e7490", "movie": MOVIE_FG}
MARK_FG = "#b0402a"

OVERLAY_LINES = 20


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


# The game's own exe: ff8.exe, or ff8_en.exe (and the other language exes) on the
# 2013 Steam release. Not other tools that happen to be named ff8_*.exe.
GAME_EXE_RE = re.compile(r'^ff8(_[a-z]{2})?\.exe$', re.I)


def find_game_log():
    """Return (label, log_path, pid) for a running FF8, or (None, None, None)."""
    try:
        import psutil
    except ImportError:
        return None, None, None
    first = None
    for pr in psutil.process_iter(["name", "pid", "exe"]):
        try:
            name = pr.info.get("name") or ""
            exe = pr.info.get("exe")
            if not exe or not GAME_EXE_RE.match(name):
                continue
            log_path = os.path.join(os.path.dirname(exe), LOG_NAME)
            found = (f"{name} (PID {pr.info['pid']})", log_path, pr.info["pid"])
            if os.path.exists(log_path):
                return found            # the game, with its FFNx.log next to it
            if first is None:
                first = found           # fallback: the log may not exist yet
        except Exception:
            continue
    return first if first else (None, None, None)


class Tailer(threading.Thread):
    """Tail FFNx.log, parse audio events, push ('add'|'replace'|'mark', cat, path)."""

    def __init__(self, path, out_queue, from_start=False):
        super().__init__(daemon=True)
        self.path = path
        self.q = out_queue
        self.from_start = from_start
        self.stop = False
        self._last = None  # (cat, path, frame)
        self._last_movie = None  # last prepared movie file, for the play line

    def run(self):
        # the user may attach before launching the game; wait for the file
        while not self.stop and not os.path.exists(self.path):
            time.sleep(0.5)
        if self.stop:
            return
        try:
            fh = open(self.path, "r", encoding="utf-8", errors="replace")
        except OSError:
            self.q.put(("mark", "", "-- could not open FFNx.log --"))
            return
        if self.from_start:
            fh.seek(0)                          # read the whole file (e.g. a saved log)
        else:
            fh.seek(0, os.SEEK_END)             # live only: skip existing history
        last_size = self._size()
        while not self.stop:
            line = fh.readline()
            if line:
                self._parse_line(line)
                continue
            size = self._size()
            if size < last_size:                # game restarted -> log truncated
                try:
                    fh.close()
                    fh = open(self.path, "r", encoding="utf-8", errors="replace")
                except OSError:
                    break
                self._last = None
                self._last_movie = None
                self.q.put(("mark", "", "-- game restarted --"))
            last_size = size
            time.sleep(0.08)
        fh.close()

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
            if self._last and self._last[0] == cat and self._last[2] == frame:
                self.q.put(("replace", cat, path))           # same-frame fallback -> collapse
            else:
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
        self.game_pid = None      # PID of the attached game, or None
        self.master_events = []   # list of (cat, path) ; marks stored as ("mark", text)
        self.row_cats = []        # category per visible listbox row (for striping/replace)

        self.enabled = {c: tk.BooleanVar(value=True) for c in FILTER_CATS}
        self.live_overlay = tk.BooleanVar(value=False)

        self._build_ui()
        self.overlay = Overlay(self)
        self._hint()

        self.after(80, self._drain)
        self.after(1000, self._watch_game)
        self.protocol("WM_DELETE_WINDOW", self._close)

    # ---------------------------------------------------------------- UI
    def _build_ui(self):
        try:
            ttk.Style(self).theme_use("clam")
        except tk.TclError:
            pass

        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        self.attach_btn = ttk.Button(top, text="Attach to FF8", command=self._attach)
        self.attach_btn.pack(side="left")
        self.status = tk.StringVar(value="Not attached.")
        ttk.Label(top, textvariable=self.status).pack(side="left", padx=10)
        ttk.Button(top, text="Locate FFNx.log", command=self._locate).pack(side="right")

        row = ttk.Frame(self, padding=(8, 0))
        row.pack(fill="x")
        ttk.Label(row, text="Show:").pack(side="left")
        for c in FILTER_CATS:
            ttk.Checkbutton(row, text=c, variable=self.enabled[c],
                            command=self._rebuild).pack(side="left", padx=(6, 0))
        ttk.Button(row, text="Clear", width=6, command=self._clear).pack(side="right")
        ttk.Checkbutton(row, text="In-game overlay", variable=self.live_overlay,
                        command=self._toggle_overlay).pack(side="right", padx=8)

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

        ttk.Label(self, padding=(8, 0),
                  text="Needs trace_music, trace_sfx, trace_voice, trace_ambient, trace_movies = true, "
                       "plus use_external_sfx and use_external_music = true (FFNx.toml or mod.xml).",
                  foreground="#666").pack(fill="x")

    def _hint(self):
        for msg in (

            "In FFNx.toml set these to true so sounds show here:",
            "    trace_music = true",
            "    trace_sfx = true",
            "    trace_voice = true",
            "    trace_ambient = true",
            "    trace_movies = true",
            " ",
            "For sfx and music you also need (set them in FFNx.toml or in the mod.xml):",
            "    use_external_sfx = true",
            "    use_external_music = true",
            " ",
            "Then launch FF8 with Junction VIII/FFNx and click Attach to FF8.",
        ):
            self._add_mark(msg)

    # ---------------------------------------------------------------- attach
    def _attach(self):
        label, path, pid = find_game_log()
        if not path:
            self.status.set("FF8 not found. Launch it, or use Locate FFNx.log.")
            return
        self.game_pid = pid
        self._start_tail(path, label, from_start=False)
        if self.live_overlay.get():
            self.overlay.deiconify()            # re-show overlay for the new session

    def _locate(self):
        path = filedialog.askopenfilename(
            title="Select FFNx.log",
            filetypes=[("FFNx log", "FFNx.log"), ("Log files", "*.log"), ("All files", "*.*")])
        if path:
            self.game_pid = None                # a picked file may be a closed session
            self._start_tail(path, os.path.basename(os.path.dirname(path)) or path,
                             from_start=True)    # read the whole file, list every .ogg

    def _start_tail(self, path, label, from_start=False):
        if self.tailer:
            self.tailer.stop = True
        self.tailer = Tailer(path, self.q, from_start=from_start)
        self.tailer.start()
        self.status.set(f"Watching: {label}")
        verb = "reading" if from_start else "tailing"
        self._add_mark(f"-- attached, {verb} {os.path.basename(path)} --")

    def _watch_game(self):
        # if the attached game exits, hide the overlay so it feels part of the game
        if self.game_pid is not None:
            try:
                import psutil
                alive = psutil.pid_exists(self.game_pid)
            except Exception:
                alive = True
            if not alive:
                self.game_pid = None
                self.overlay.withdraw()
                self.status.set("Game closed. Captured lines kept.")
        self.after(1000, self._watch_game)

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
        changed = False
        try:
            while True:
                op, cat, path = self.q.get_nowait()
                changed = True
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
        self.listbox.itemconfig(i, bg=(BG_A if i % 2 == 0 else BG_B), fg=CAT_FG.get(cat, "#000"))
        self.row_cats.append(cat)
        if self.live_overlay.get():
            self.listbox.see("end")

    def _row_replace_last(self, cat, path):
        i = self.listbox.size() - 1
        if i < 0:
            return self._row_add(cat, path)
        self.listbox.delete(i)
        self.listbox.insert(i, path)
        self.listbox.itemconfig(i, bg=(BG_A if i % 2 == 0 else BG_B), fg=CAT_FG.get(cat, "#000"))
        self.row_cats[-1] = cat
        if self.live_overlay.get():
            self.listbox.see("end")

    def _row_add_mark(self, text):
        i = self.listbox.size()
        self.listbox.insert("end", text)
        self.listbox.itemconfig(i, bg=(BG_A if i % 2 == 0 else BG_B), fg=MARK_FG)
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
        # true only while attached to a live game (the Attach button sets game_pid)
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

    def _close(self):
        if self.tailer:
            self.tailer.stop = True
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
