"""
FFNx setup of FF8 Audio Monitor: switches FFNx's sound logging on inside the running game, so the main window shows
the game's sounds with nothing to press. FFNx.toml is never changed.

FFNx writes the sounds it plays into FFNx.log only when these are on:
    trace_music, trace_sfx, trace_voice, trace_ambient, trace_movies
and it takes over the music and the sound effects (plays them from .ogg files, which is what puts their names in the
log) only when these are on:
    use_external_music, use_external_sfx
It reads them from FFNx.toml as the game starts, into variables of its own (one byte each). This sets those bytes
inside the running game instead (when they're off):
- The five traces at any time: FFNx looks at them again for every sound, so the logging starts at once, even in a
  game started before the app.
- The two external ones only as the game starts: FFNx looks at them once, as it opens the game window (its Sound
  menu) and sets up its sound a moment later. The app watches for the game; once FFNx has read FFNx.toml (right
  after, it keeps the game's start address; or a trace the app had switched on went back to off; or the window is
  there), it pauses the game for a few milliseconds and, if the window isn't open yet, switches them on and lets the
  game go on. With the window already open: use_external_sfx still, if FFNx.log shows FFNx hasn't started its sound
  setup ("Max texture size" comes just before it); use_external_music too with an FFNx that has no Sound menu.
  Too late (the game was already running): the window says to start it again.
  With them on, a music or sound effect with no .ogg is silent: FFNx has no fallback to the game's own sounds. Mods
  that bring their own .ogg files set them anyway.
Nothing is written on disk, and it's all gone when the game closes.

Finding the bytes: FFNx doesn't export them, so they're found in its code, two ways that must agree:
1. Its settings code: trace_music = config["trace_music"].value_or(false) stores a byte shortly after it pushes the
   text "trace_music".
2. The code that uses them: if (trace_all || trace_music) ffnx_trace("<a music line>") reads two bytes just before it
   pushes the line's text (trace_all at every such place, so the other byte is trace_music). use_external_sfx is read
   just before the texts "wm_footsteps_%d_%d_%d" and "sfx_highwind_%d" are pushed; use_external_music is set to 1
   just after "Detected Remastered edition." or "Detected eStore edition." is logged.
Only a byte found both ways, inside FFNx's own writable data, holding 0 or 1, is ever written. Anything else is left
alone, and the window says so.

FFNx is eax.dll (FF8 2000, and the Remaster through Junction VIII) or AF3DN.P (the 2013 Steam release) inside the
game, ff8.exe or ff8_<language>.exe (32-bit). Its FFNx.log is in the game's working folder.

Blitzball FF8 (the mod, BlitzballFF8.dll, test 80 and newer) logs its own sounds into BlitzballFF8.log when its three
switches are on (Junction VIII's Configure Mod: Log Blitzball's music, sound effects, ambience). The app keeps them on
inside the game the same way: they're three ints one after another, which Blitzball's settings code fills from three
settings one after another just before it logs its "Settings ... letters" line. Its settings file isn't changed.

Windows only for the game's memory (ctypes); the finding runs anywhere (the tests run it on a real FFNx build).
"""
import json
import os
import re
import struct
import threading
import time
import queue

GAME_EXE_RE = re.compile(r'^ff8(_[a-z]{2})?\.exe$', re.I)
REMASTER_EXE = "ffviii.exe"                 # the Remaster's own exe (no FFNx in it)
LOG_NAME = "FFNx.log"
FFNX_FILES = ("eax.dll", "af3dn.p", "ffnx.dll")   # FFNx's file name in the game (FF8 2000 / Remaster, 2013 Steam)

TRACE_KEYS = ("trace_music", "trace_sfx", "trace_voice", "trace_ambient", "trace_movies")
EXT_KEYS = ("use_external_music", "use_external_sfx")
KEYS = TRACE_KEYS + EXT_KEYS

# Each trace's own lines in FFNx's code: "if (trace_all || trace_x) ffnx_trace(<one of these>)"
TRACE_LINES = {
    "trace_sfx": ("NxAudioEngine::%s: filename=%s,loop=%d\n",
                  "NxAudioEngine::%s: name=%s,id=%d,channel=%d,panning=%f,volume=%f\n",
                  "%s: channel=%d, id=%d, volume=%d, panning=%d, pitch=%d\n"),
    "trace_music": ("NxAudioEngine::%s: %s (%d) on channel #%d\n",
                    "%s: music_id=%u, name=%s, channel=%d, volume=%d\n",
                    "NxAudioEngine::%s: midi %d, time %f, sync %d\n"),
    "trace_voice": ("NxAudioEngine::%s: slot[%d] %s exists=%d\n",
                    "NxAudioEngine::%s: slot=%d time=%lf handle=%X\n"),
    "trace_ambient": ("NxAudioEngine::%s: %s exists=%d handle=%X\n",
                      "NxAudioEngine::%s: time=%f ( overridden through config.toml )\n"),
    "trace_movies": ("prepare_movie %s\n", "start_movie\n", "stop_movie\n", "NxAudioEngine::%s: %s exists=%d\n"),
}
SFX_TEXTS = ("wm_footsteps_%d_%d_%d", "sfx_highwind_%d")   # pushed only when use_external_sfx is on
# logged just before use_external_music = true: FF8's Remaster (FFNx canary), FF7's eStore release (every FFNx)
MUSIC_TEXTS = ("Detected Remastered edition.\n", "Detected eStore edition.\n")
VERSION_RE = re.compile(rb"FFNx driver version ([\x21-\x7e]{1,40})")     # (1.24.3.522, or "devel" for an own build)
SETUP_FMT = "Max texture size: %ix%i\n"                    # FFNx's line just before it sets up its sound
SETUP_LINE = b"Max texture size: "
WINDOW_TITLE = "final fantasy viii"                        # FFNx names the game window so
SOUND_SCREEN = "Volume of the music"                       # FFNx's Config > Sound screen (Oct 2026): it reads
#                                                            use_external_music as the game window opens
# right after reading FFNx.toml, FFNx keeps the game's start address (one of these, by language and patch)
FF8_STARTS = (0x55AC07, 0x55ADB7, 0x55B137, 0x55B2E7, 0x55B237, 0x55B3E7, 0x55B227, 0x55B3D7, 0x55B1B7, 0x55B367,
              0x55AB87, 0x55AD37, 0x55F487, 0x55F6E7)

SCN_EXEC, SCN_WRITE = 0x20000000, 0x80000000
BUILDS_FILE = "ffnx_builds.json"            # where each FFNx build keeps them, found once (the app's settings folder)


# ---------------------------------------------------------------- the running games
def process_names():
    """{pid: process name} of every running process. Windows: one Toolhelp snapshot (fast, needs nothing else);
    elsewhere, or if that fails: psutil. {} if neither works."""
    if os.name == "nt":
        try:
            return _api_toolhelp_names()
        except Exception:
            pass
    try:
        import psutil
    except ImportError:
        return {}
    out = {}
    for pr in psutil.process_iter(["name"]):
        try:
            out[pr.pid] = pr.info.get("name") or ""
        except Exception:
            continue
    return out


def game_info(pid, name):
    """{pid, name, exe, folder (its working folder: FFNx opens FFNx.log there), created} of a running game, or None.
    psutil gives the working folder; without it (or if it can't), the exe's folder, which is the same when Junction
    VIII or Steam starts the game."""
    exe = folder = None
    created = 0
    try:
        import psutil
        pr = psutil.Process(pid)
        try:
            exe = pr.exe() or None
        except Exception:
            pass
        try:
            cwd = pr.cwd()
            if cwd and os.path.isdir(cwd):
                folder = cwd
        except Exception:
            pass
        try:
            created = pr.create_time()
        except Exception:
            pass
    except Exception:
        pass
    if os.name == "nt" and (not exe or not created):
        try:
            exe2, created2 = _api_exe_and_start(pid)
            exe, created = exe or exe2, created or created2
        except Exception:
            pass
    if not exe:
        return None
    return {"pid": pid, "name": name, "exe": exe, "folder": folder or os.path.dirname(exe), "created": created}


def running_games():
    """Every running FF8 that can have FFNx: [{pid, name, exe, folder, created}], the newest first"""
    out = []
    for pid, name in process_names().items():
        if GAME_EXE_RE.match(name):
            g = game_info(pid, name)
            if g:
                out.append(g)
    out.sort(key=lambda g: g["created"], reverse=True)
    return out


def kind_of(folder):
    """Which FF8 a folder holds, from its files."""
    try:
        names = {n.lower() for n in os.listdir(folder)}
    except OSError:
        return "FF8"
    if REMASTER_EXE in names:
        return "Remastered (GOG)" if "goggame-1086370078.info" in names else "Remastered"
    if "ff8_launcher.exe" in names or any(GAME_EXE_RE.match(n) and n != "ff8.exe" for n in names):
        return "2013 Steam"
    if "ff8.exe" in names:
        return "FF8 2000"
    return "FF8"


# ---------------------------------------------------------------- FFNx's flags, found in its code
def pe_sections(head):
    """[(name, rva, size, flags)] from a module's first page, or None if it isn't a 32-bit Windows module."""
    try:
        if len(head) < 0x200 or bytes(head[:2]) != b"MZ":
            return None
        pe = struct.unpack_from("<I", head, 0x3C)[0]
        if pe + 24 > len(head) or bytes(head[pe:pe + 4]) != b"PE\0\0":
            return None
        machine, count = struct.unpack_from("<HH", head, pe + 4)
        opt = struct.unpack_from("<H", head, pe + 20)[0]
        if machine != 0x14C:
            return None
        out = []
        for i in range(count):
            o = pe + 24 + opt + 40 * i
            if o + 40 > len(head):
                return None
            name = bytes(head[o:o + 8]).rstrip(b"\0").decode("ascii", "replace")
            size, rva = struct.unpack_from("<II", head, o + 8)
            out.append((name, rva, size, struct.unpack_from("<I", head, o + 36)[0]))
        return out
    except struct.error:
        return None


class _Code:
    """FFNx's code and read-only data as loaded in the game (img: its bytes from its base, as far as they were read)."""

    def __init__(self, img, base, secs):
        self.img, self.base = img, base
        n = len(img)
        self.code = [(r, min(r + s, n)) for _, r, s, f in secs if f & SCN_EXEC]
        self.rodata = [(r, min(r + s, n)) for _, r, s, f in secs if not f & (SCN_EXEC | SCN_WRITE)]
        self.data = [(base + r, base + r + s) for _, r, s, f in secs if f & SCN_WRITE and not f & SCN_EXEC]
        self._pushes = {}

    def in_data(self, a):
        return any(lo <= a < hi for lo, hi in self.data)

    def texts(self, s):
        """Each place a whole text (0 before and after it) is, as an address in the game."""
        b = s.encode("ascii") + b"\0"
        out = []
        for lo, hi in self.rodata:
            i = self.img.find(b, lo, hi)
            while i >= 0:
                if i > lo and self.img[i - 1] == 0:
                    out.append(self.base + i)
                i = self.img.find(b, i + 1, hi)
        return out

    def prepare(self, strings):
        """Each "push offset <text>" of these texts in the code, found in one pass over it (it's big)."""
        vas = {}
        for s in strings:
            self._pushes.setdefault(s, [])
            for va in self.texts(s):
                vas[va] = s
        if not vas:
            return
        rx = re.compile(b"|".join(re.escape(b"\x68" + struct.pack("<I", va)) for va in sorted(vas)))
        for lo, hi in self.code:
            for m in rx.finditer(self.img, lo, hi):
                self._pushes[vas[struct.unpack_from("<I", self.img, m.start() + 1)[0]]].append(m.start())

    def pushes(self, s):
        """Each "push offset <text>" in the code (where it is in img)."""
        if s not in self._pushes:
            self.prepare([s])
        return self._pushes[s]

    def _addr(self, p, at):
        a = struct.unpack_from("<I", self.img, p + at)[0]
        return a if self.in_data(a) else None

    def reads_before(self, pos, span=110):
        """The bytes of FFNx's data read by the instructions before pos (cmp / test / mov / movzx / or ... [a])."""
        img, got = self.img, set()
        for p in range(max(0, pos - span), pos):
            if p + 7 > len(img):
                break
            b0, b1 = img[p], img[p + 1]
            a = None
            if b0 == 0x80 and b1 == 0x3D and img[p + 6] == 0:                      # cmp byte ptr [a], 0
                a = self._addr(p, 2)
            elif b0 in (0x38, 0x3A, 0x8A, 0x0A, 0x84) and b1 & 0xC7 == 0x05:        # cmp / mov / or / test reg8, [a]
                a = self._addr(p, 2)
            elif b0 == 0xA0:                                                       # mov al, [a]
                a = self._addr(p, 1)
            elif b0 == 0x0F and b1 == 0xB6 and img[p + 2] & 0xC7 == 0x05:           # movzx reg, byte ptr [a]
                a = self._addr(p, 3)
            elif b0 == 0xF6 and b1 == 0x05:                                        # test byte ptr [a], n
                a = self._addr(p, 2)
            if a is not None:
                got.add(a)
        return got

    def stores_after(self, pos, span=800, ones=False, first=None):
        """The bytes of FFNx's data written by the instructions after pos (mov byte ptr [a], al / reg8 / n; ones:
        only "mov byte ptr [a], 1"; first: only the first few such writes)."""
        img, got, n = self.img, set(), 0
        for p in range(pos, min(len(img) - 7, pos + span)):
            if first is not None and n >= first:
                break
            b0, b1 = img[p], img[p + 1]
            a = None
            if b0 == 0xC6 and b1 == 0x05 and (not ones or img[p + 6] == 1):       # mov byte ptr [a], n
                a = self._addr(p, 2)
            elif not ones and b0 == 0xA2:                                          # mov byte ptr [a], al
                a = self._addr(p, 1)
            elif not ones and b0 == 0x88 and b1 & 0xC7 == 0x05:                    # mov byte ptr [a], reg8
                a = self._addr(p, 2)
            if a is not None:
                got.add(a)
                n += 1
        return got

    def setting(self, key):
        """The bytes FFNx's settings code may keep a key in: x = config["x"].value_or(false) stores it in the first
        or the second byte written after the push of the text "x" (the key before's value is often stored only then,
        and this one's after the next key's push)."""
        got = set()
        for p in self.pushes(key):
            got |= self.stores_after(p, first=2)
        return got


def _common(sets):
    sets = [s for s in sets if s]
    return set.intersection(*sets) if sets else set()


def find_flags(img, base):
    """Where FFNx keeps its trace and external flags in the game. img: FFNx's bytes from its base (its code and
    read-only data at least), base: where it is loaded.
    Returns {"addr": {key: address}, "missing": {key: why}, "version": FFNx's version or None, "setup_line": FFNx
    logs SETUP_LINE (so its sound setup can be told apart), "read_mark": where it keeps the game's start address
    once it has read FFNx.toml (or None), "sound_screen": it has the Config > Sound screen}. "addr" may hold
    trace_all too (only read)."""
    res = {"addr": {}, "missing": {}, "version": None, "setup_line": False, "read_mark": None,
           "sound_screen": False}
    secs = pe_sections(img[:0x1000])
    if not secs:
        res["missing"] = dict.fromkeys(KEYS, "not a 32-bit Windows module")
        return res
    c = _Code(img, base, secs)
    for lo, hi in c.rodata:
        m = VERSION_RE.search(img, lo, hi)
        if m:
            res["version"] = m.group(1).decode("ascii", "replace").rstrip(".")
            break
    if not res["version"]:
        res["missing"] = dict.fromkeys(KEYS, "not FFNx")
        return res
    res["setup_line"] = bool(c.texts(SETUP_FMT))
    res["sound_screen"] = bool(c.texts(SOUND_SCREEN))
    # "mov dword ptr [a], <the game's start>": one place for every language and patch (FFNx's DllMain, after read_cfg)
    rx = re.compile(b"\xC7\x05(.{4})(?:" + b"|".join(re.escape(struct.pack("<I", v)) for v in FF8_STARTS) + b")", re.S)
    hits = {}
    for lo, hi in c.code:
        for m in rx.finditer(img, lo, hi):
            a = struct.unpack("<I", m.group(1))[0]
            if c.in_data(a):
                hits[a] = hits.get(a, 0) + 1
    if hits:
        a, n = max(hits.items(), key=lambda kv: kv[1])
        if n >= 8 and sum(hits.values()) == n:
            res["read_mark"] = a
    c.prepare(("trace_all",) + KEYS + SFX_TEXTS + MUSIC_TEXTS + tuple(x for v in TRACE_LINES.values() for x in v))
    # the traces: the bytes read before each of their own lines; trace_all is the one read before all of them
    read = {k: _common(c.reads_before(p) for s in lines for p in c.pushes(s)) for k, lines in TRACE_LINES.items()}
    every = _common(read.values()) if sum(1 for v in read.values() if v) >= 2 else set()
    every &= c.setting("trace_all")
    if len(every) == 1:
        all_at = every.pop()
        res["addr"]["trace_all"] = all_at
        for k in TRACE_KEYS:
            got = (read[k] - {all_at}) & c.setting(k)
            if len(got) == 1:
                res["addr"][k] = got.pop()
            else:
                res["missing"][k] = "its log lines don't show it" if not read[k] else "not one clear place"
    else:
        for k in TRACE_KEYS:
            res["missing"][k] = "trace_all, read before every sound line, wasn't found"
    # use_external_sfx: read before the texts it guards; use_external_music: set to 1 after the Remaster's line
    got = _common(c.reads_before(p) for s in SFX_TEXTS for p in c.pushes(s)) & c.setting("use_external_sfx")
    if len(got) == 1:
        res["addr"]["use_external_sfx"] = got.pop()
    else:
        res["missing"]["use_external_sfx"] = "not one clear place"
    got = set()
    for s in MUSIC_TEXTS:
        for p in c.pushes(s):
            got |= c.stores_after(p, 300, ones=True)
    got &= c.setting("use_external_music")
    if len(got) == 1:
        res["addr"]["use_external_music"] = got.pop()
    else:
        res["missing"]["use_external_music"] = "not one clear place"
    # never two keys on one byte
    seen = {}
    for k, a in list(res["addr"].items()):
        seen.setdefault(a, []).append(k)
    for a, ks in seen.items():
        if len(ks) > 1:
            for k in ks:
                res["addr"].pop(k, None)
                if k != "trace_all":
                    res["missing"][k] = "shares a place with " + ", ".join(x for x in ks if x != k)
    return res


BLITZ_FILE = "blitzballff8.dll"
BLITZ_SETTINGS_LINE = b"Settings%s (%s): letters"
BLITZ_BUILD_RE = re.compile(rb"(20\d\d\.\d{4} test \d+[a-z]?)")


def find_blitz_trace(img, base):
    """Blitzball's three sound-log switches (music, sound effects, ambience) in BlitzballFF8.dll as loaded: in its
    settings code, before the "Settings ... letters" line, three registers loaded from three settings one after another
    (mov reg, [y], [y + 4], [y + 8]) are stored to three ints one after another (mov [x], reg, [x + 4], [x + 8]); the
    settings are those the code just read in a loop (mov [i * 4 + base], reg, with y a little after base). And the
    code that logs a sound reads them by layer (mov reg, [layer * 4 + x]): both ways must agree.
    Returns {"addr": x or None, "build": "2026.1067 test 80" or None}."""
    res = {"addr": None, "build": None}
    secs = pe_sections(img[:0x1000])
    if not secs:
        return res
    c = _Code(img, base, secs)
    for lo, hi in c.rodata:
        m = BLITZ_BUILD_RE.search(img, lo, hi)
        if m:
            res["build"] = m.group(1).decode("ascii")
            break
    found = set()
    sv = []
    for lo, hi in c.rodata:
        i = img.find(BLITZ_SETTINGS_LINE, lo, hi)
        while i >= 0:
            sv.append(base + i)
            i = img.find(BLITZ_SETTINGS_LINE, i + 1, hi)
    refs = []
    for va in sv:
        pat = struct.pack("<I", va)
        for lo, hi in c.code:
            i = img.find(pat, lo, hi)
            while i >= 0:
                refs.append(i)
                i = img.find(pat, i + 1, hi)
    for r in refs:
        loads, stores, loops = {}, {}, set()
        for q in range(max(0, r - 1600), r):
            b0, b1 = img[q], img[q + 1]
            if b0 == 0x8B and b1 & 0xC7 == 0x05:                                   # mov reg, [a]
                loads.setdefault((b1 >> 3) & 7, []).append((q, struct.unpack_from("<I", img, q + 2)[0]))
            elif b0 == 0xA1:                                                       # mov eax, [a]
                loads.setdefault(0, []).append((q, struct.unpack_from("<I", img, q + 1)[0]))
            elif b0 == 0x89 and b1 & 0xC7 == 0x05:                                 # mov [a], reg
                stores.setdefault((b1 >> 3) & 7, []).append((q, struct.unpack_from("<I", img, q + 2)[0]))
            elif b0 == 0xA3:                                                       # mov [a], eax
                stores.setdefault(0, []).append((q, struct.unpack_from("<I", img, q + 1)[0]))
            elif b0 == 0x89 and b1 & 0xC7 == 0x04 and img[q + 2] & 0xC7 == 0x85:  # mov [i * 4 + a], reg
                loops.add(struct.unpack_from("<I", img, q + 3)[0])
        pairs = {}
        for reg, sts in stores.items():
            for sp, x in sts:
                prev = [(lp, y) for lp, y in loads.get(reg, []) if lp < sp]
                if prev and c.in_data(x):
                    pairs[x] = max(prev)[1]
        for x, y in pairs.items():
            if pairs.get(x + 4) == y + 4 and pairs.get(x + 8) == y + 8 and \
                    any(0 <= y - b < 0x100 and (y - b) % 4 == 0 for b in loops):
                found.add(x)
    if len(found) == 1:
        x = found.pop()
        read = b"\x8B" + struct.pack("<I", x)              # (mov reg, [i * 4 + x]: 8B, modrm, SIB, x)
        for lo, hi in c.code:
            i = img.find(struct.pack("<I", x), lo, hi)
            while i >= 0:
                if i >= 3 and img[i - 3] == read[0] and img[i - 2] & 0xC7 == 0x04 and img[i - 1] & 0xC7 == 0x85:
                    res["addr"] = x
                    return res
                i = img.find(struct.pack("<I", x), i + 1, hi)
    return res


FFNX_EXPORT = b"nxRegisterKeyListener\0"      # one of FFNx's own exports (the game's EAX.DLL and the rest don't have it)


def export_block(head):
    """(rva, size) of a module's export table, from its first page, or None."""
    try:
        pe = struct.unpack_from("<I", head, 0x3C)[0]
        if bytes(head[pe:pe + 4]) != b"PE\0\0" or struct.unpack_from("<H", head, pe + 24)[0] != 0x10B:
            return None
        rva, size = struct.unpack_from("<II", head, pe + 24 + 96)        # (data directory 0 of a 32-bit module)
        return (rva, size) if rva and 0 < size < (1 << 20) else None
    except struct.error:
        return None


def build_key(head):
    """One FFNx build, from its first page: its link time, size, checksum and entry point (or None)."""
    try:
        pe = struct.unpack_from("<I", head, 0x3C)[0]
        ts = struct.unpack_from("<I", head, pe + 8)[0]
        opt = pe + 24
        return "%08x-%08x-%08x-%08x" % (ts, struct.unpack_from("<I", head, opt + 56)[0],
                                         struct.unpack_from("<I", head, opt + 64)[0],
                                         struct.unpack_from("<I", head, opt + 16)[0])
    except struct.error:
        return None


def flags_to_build(res, base):
    """find_flags' result as kept for its build (offsets from FFNx's start, so any load address fits)."""
    return {"version": res["version"], "setup_line": res["setup_line"], "sound_screen": res["sound_screen"],
            "rva": {k: a - base for k, a in res["addr"].items()},
            "read_mark": res["read_mark"] - base if res["read_mark"] else None}


def flags_from_build(b, base):
    """A kept build's flags, for FFNx loaded at base (None if the entry isn't one this app wrote)."""
    try:
        rva = b["rva"]
        if not isinstance(rva, dict) or not all(k in rva for k in KEYS):
            return None
        return {"addr": {k: base + int(v) for k, v in rva.items()}, "missing": {}, "version": str(b["version"]),
                "setup_line": bool(b["setup_line"]), "sound_screen": bool(b["sound_screen"]),
                "read_mark": base + int(b["read_mark"]) if b.get("read_mark") is not None else None}
    except (KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------- the game's memory (Windows)
_API = None


def _api():
    """kernel32 / user32 / ntdll functions, set up once (Windows only)."""
    global _API
    if _API is not None:
        return _API
    import ctypes
    from ctypes import wintypes

    class MODULEENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("th32ModuleID", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("GlblcntUsage", wintypes.DWORD), ("ProccntUsage", wintypes.DWORD),
                    ("modBaseAddr", ctypes.c_void_p), ("modBaseSize", wintypes.DWORD), ("hModule", wintypes.HMODULE),
                    ("szModule", ctypes.c_wchar * 256), ("szExePath", ctypes.c_wchar * 260)]

    class MODULEINFO(ctypes.Structure):
        _fields_ = [("lpBaseOfDll", ctypes.c_void_p), ("SizeOfImage", wintypes.DWORD), ("EntryPoint", ctypes.c_void_p)]

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    nt = ctypes.WinDLL("ntdll")

    class A(object):
        pass

    a = A()

    def fn(dll, name, res, *args):
        f = getattr(dll, name)
        f.restype = res
        f.argtypes = list(args)
        setattr(a, name, f)

    H, D, B, P = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, ctypes.c_void_p
    fn(k32, "OpenProcess", H, D, B, D)
    fn(k32, "CloseHandle", B, H)
    fn(k32, "ReadProcessMemory", B, H, P, P, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t))
    fn(k32, "WriteProcessMemory", B, H, P, P, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t))
    fn(k32, "GetExitCodeProcess", B, H, ctypes.POINTER(D))
    fn(k32, "CreateToolhelp32Snapshot", H, D, D)
    fn(k32, "Process32FirstW", B, H, ctypes.POINTER(PROCESSENTRY32W))
    fn(k32, "Process32NextW", B, H, ctypes.POINTER(PROCESSENTRY32W))
    fn(k32, "QueryFullProcessImageNameW", B, H, D, wintypes.LPWSTR, ctypes.POINTER(D))
    fn(k32, "GetProcessTimes", B, H, *([ctypes.POINTER(wintypes.FILETIME)] * 4))
    fn(k32, "Module32FirstW", B, H, ctypes.POINTER(MODULEENTRY32W))
    fn(k32, "Module32NextW", B, H, ctypes.POINTER(MODULEENTRY32W))
    fn(k32, "K32EnumProcessModulesEx", B, H, ctypes.POINTER(wintypes.HMODULE), D, ctypes.POINTER(D), D)
    fn(k32, "K32GetModuleBaseNameW", D, H, wintypes.HMODULE, wintypes.LPWSTR, D)
    fn(k32, "K32GetModuleInformation", B, H, wintypes.HMODULE, ctypes.POINTER(MODULEINFO), D)
    fn(nt, "NtSuspendProcess", ctypes.c_long, H)
    fn(nt, "NtResumeProcess", ctypes.c_long, H)
    a.WNDENUMPROC = ctypes.WINFUNCTYPE(B, wintypes.HWND, wintypes.LPARAM)
    fn(u32, "EnumWindows", B, a.WNDENUMPROC, wintypes.LPARAM)
    fn(u32, "GetWindowThreadProcessId", D, wintypes.HWND, ctypes.POINTER(D))
    fn(u32, "GetWindowTextW", ctypes.c_int, wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    a.ctypes, a.wintypes, a.MODULEENTRY32W, a.MODULEINFO = ctypes, wintypes, MODULEENTRY32W, MODULEINFO
    a.PROCESSENTRY32W = PROCESSENTRY32W
    a.INVALID = ctypes.c_void_p(-1).value
    _API = a
    return a


def _api_toolhelp_names():
    """{pid: exe file name} from one Toolhelp process snapshot (Windows)."""
    a = _api()
    snap = a.CreateToolhelp32Snapshot(0x2, 0)                          # TH32CS_SNAPPROCESS
    if not snap or snap == a.INVALID:
        raise _fail("CreateToolhelp32Snapshot")
    try:
        e = a.PROCESSENTRY32W()
        e.dwSize = a.ctypes.sizeof(e)
        out = {}
        ok = a.Process32FirstW(snap, a.ctypes.byref(e))
        while ok:
            out[e.th32ProcessID] = e.szExeFile.rsplit("\\", 1)[-1]
            ok = a.Process32NextW(snap, a.ctypes.byref(e))
        return out
    finally:
        a.CloseHandle(snap)


def _api_exe_and_start(pid):
    """(the exe's full path, when it started as a Unix time) of a process (Windows), or (None, 0)."""
    a = _api()
    h = a.OpenProcess(0x1000, False, pid)                               # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None, 0
    try:
        buf = a.ctypes.create_unicode_buffer(1024)
        n = a.wintypes.DWORD(1024)
        exe = buf.value if a.QueryFullProcessImageNameW(h, 0, buf, a.ctypes.byref(n)) else None
        t = [a.wintypes.FILETIME() for _ in range(4)]
        created = 0
        if a.GetProcessTimes(h, *[a.ctypes.byref(x) for x in t]):
            created = ((t[0].dwHighDateTime << 32) | t[0].dwLowDateTime) / 1e7 - 11644473600
        return exe, created
    finally:
        a.CloseHandle(h)


class WinError(OSError):
    pass


def _fail(what):
    import ctypes
    err = ctypes.get_last_error()
    return WinError(err, "%s failed (Windows error %d)" % (what, err))


class GameProcess(object):
    """A running game seen from outside: its modules, its memory, its windows. Windows only."""

    # PROCESS_QUERY_INFORMATION, PROCESS_VM_READ, PROCESS_VM_WRITE, PROCESS_VM_OPERATION, PROCESS_SUSPEND_RESUME
    ACCESS = 0x0400 | 0x0010 | 0x0020 | 0x0008 | 0x0800

    def __init__(self, pid):
        a = _api()
        self.pid = pid
        self.paused = 0
        self.h = a.OpenProcess(self.ACCESS, False, pid)
        if not self.h:
            raise _fail("OpenProcess")

    def close(self):
        if self.h:
            while self.paused > 0:              # (never left paused)
                self.resume()
            _api().CloseHandle(self.h)
            self.h = None

    def alive(self):
        a = _api()
        code = a.wintypes.DWORD(0)
        return bool(self.h) and bool(a.GetExitCodeProcess(self.h, a.ctypes.byref(code))) and code.value == 259

    def modules(self):
        """[(file name in lower case, base, size)]; [] while Windows can't list them (the game just started)."""
        a = _api()
        snap = a.CreateToolhelp32Snapshot(0x8 | 0x10, self.pid)       # TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32
        if snap and snap != a.INVALID:
            try:
                me = a.MODULEENTRY32W()
                me.dwSize = a.ctypes.sizeof(me)
                out = []
                ok = a.Module32FirstW(snap, a.ctypes.byref(me))
                while ok:
                    out.append((me.szModule.lower(), me.modBaseAddr or 0, me.modBaseSize))
                    ok = a.Module32NextW(snap, a.ctypes.byref(me))
                if out:
                    return out
            finally:
                a.CloseHandle(snap)
        need = a.wintypes.DWORD(0)                                       # (the other way to list them)
        arr = (a.wintypes.HMODULE * 1024)()
        if not a.K32EnumProcessModulesEx(self.h, arr, a.ctypes.sizeof(arr), a.ctypes.byref(need), 3):
            return []
        out = []
        for i in range(min(need.value // a.ctypes.sizeof(a.wintypes.HMODULE), 1024)):
            name = a.ctypes.create_unicode_buffer(260)
            info = a.MODULEINFO()
            if not a.K32GetModuleBaseNameW(self.h, arr[i], name, 260):
                continue
            if not a.K32GetModuleInformation(self.h, arr[i], a.ctypes.byref(info), a.ctypes.sizeof(info)):
                continue
            out.append((name.value.lower(), info.lpBaseOfDll or 0, info.SizeOfImage))
        return out

    def read(self, addr, n):
        a = _api()
        buf = a.ctypes.create_string_buffer(n)
        got = a.ctypes.c_size_t(0)
        if not a.ReadProcessMemory(self.h, addr, buf, n, a.ctypes.byref(got)) or got.value != n:
            raise _fail("ReadProcessMemory")
        return buf.raw

    def read_into(self, img, at, addr, n, piece=1 << 20):
        """img[at:at + n] = the game's memory at addr, a piece at a time (a piece it can't read stays 0)."""
        a = _api()
        got = a.ctypes.c_size_t(0)
        done = 0
        while done < n:
            k = min(piece, n - done)
            dst = (a.ctypes.c_char * k).from_buffer(img, at + done)
            a.ReadProcessMemory(self.h, addr + done, dst, k, a.ctypes.byref(got))
            done += k

    def write(self, addr, data):
        a = _api()
        data = bytes(data)
        buf = a.ctypes.create_string_buffer(data, len(data))
        n = a.ctypes.c_size_t(0)
        if not a.WriteProcessMemory(self.h, addr, buf, len(data), a.ctypes.byref(n)) or n.value != len(data):
            raise _fail("WriteProcessMemory")

    def suspend(self):
        st = _api().NtSuspendProcess(self.h)
        if st != 0:
            raise WinError(st, "NtSuspendProcess failed (0x%08X)" % (st & 0xFFFFFFFF))
        self.paused += 1

    def resume(self):
        if self.paused > 0:
            self.paused -= 1
            _api().NtResumeProcess(self.h)

    def window_titles(self):
        """The titles of the game's own top-level windows."""
        a = _api()
        pid, titles = self.pid, []

        def each(hwnd, _lp):
            owner = a.wintypes.DWORD(0)
            a.GetWindowThreadProcessId(hwnd, a.ctypes.byref(owner))
            if owner.value == pid:
                buf = a.ctypes.create_unicode_buffer(256)
                a.GetWindowTextW(hwnd, buf, 256)        # (another program's window: no message is sent to it)
                titles.append(buf.value)
            return True

        cb = a.WNDENUMPROC(each)
        a.EnumWindows(cb, 0)
        return titles

    def read_image(self, base, size):
        """FFNx as loaded: its first page, its code and its read-only data (zeros elsewhere)."""
        img = bytearray(size)
        self.read_into(img, 0, base, min(0x1000, size))
        for _name, rva, vsize, flags in pe_sections(img[:0x1000]) or []:
            if (flags & SCN_EXEC or not flags & SCN_WRITE) and rva < size:
                self.read_into(img, rva, base + rva, min(vsize, size - rva))
        return img


# ---------------------------------------------------------------- the watcher
def _log_has(path, text, not_before, head=1 << 20):
    """This run's FFNx.log (written since the game started) has the text near its start: True / False, or None if it
    can't tell. Only its first MB is read (the game is paused meanwhile): a log longer than that is long past it."""
    try:
        if os.path.getmtime(path) < not_before - 2:
            return None                         # (the run before's log: FFNx hasn't started it afresh)
        with open(path, "rb") as f:
            start = f.read(head)
        return text in start or len(start) >= head
    except OSError:
        return None


def _and(keys):
    return " and ".join(keys)


def _what(keys, its=""):
    """What those external flags bring: "the music", "the sound effects" or both."""
    w = [x for k, x in (("use_external_music", "music"), ("use_external_sfx", "sound effects")) if k in keys]
    return (its or "the ") + " and ".join(w)


class Setup(threading.Thread):
    """Runs beside the app: notices each FF8 that starts (or runs already) and switches FFNx's sound logging on inside
    it. Talks to the window through `events`: ("game", info) a game to follow, ("gone", pid), ("note", text)."""

    LOOK = 0.1              # new games looked for this often
    FAST = 0.03             # a starting game looked at this often (FFNx reads its settings, then opens its window)
    MODULES = 0.05          # its modules listed this often while FFNx isn't in the list yet
    SLOW = 1.0              # then this often (a trace switched off again is switched on again)
    START_LIMIT = 60.0      # a game still without its window after this long: its start isn't watched any more
    NO_FFNX = 20.0          # this long with no FFNx in the game's modules (or 3 s after its window): it has none
    TRIES = 3               # FFNx's settings looked for this many times more if some aren't found (0.3 s apart)

    def __init__(self, events, opener=None, lister=None, clock=time.time, settings_dir=None):
        super().__init__(daemon=True)
        self.events = events
        self.dir = settings_dir                 # (theme's settings folder unless the tests give one)
        self.builds = self._load_builds()       # FFNx builds already looked through: their flags at once
        self.cmds = queue.Queue()               # ("stop",)
        self.opener = opener or GameProcess     # (the tests give a pretend game)
        self.lister = lister                    # (the tests too: () -> [{pid, name, exe, folder, created}])
        self.clock = clock
        self.games = {}                         # pid -> what is known of each running game
        self.stop = False
        self.said = set()

    def _builds_path(self):
        if self.dir is None:
            import theme
            self.dir = str(theme.settings_dir())
        return os.path.join(self.dir, BUILDS_FILE)

    def _load_builds(self):
        try:
            with open(self._builds_path(), encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_builds(self):
        try:
            os.makedirs(self.dir, exist_ok=True)
            keep = dict(list(self.builds.items())[-20:])
            tmp = self._builds_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(keep, f, indent=1)
            os.replace(tmp, self._builds_path())
        except OSError:
            pass

    def _once(self, key, text):
        if key not in self.said:
            self.said.add(key)
            self.events.put(("note", text))

    def _safe(self, fn, *args):
        try:
            fn(*args)
        except Exception as e:                  # (never let a surprise stop the watching; each one said once)
            self._once(("error", fn.__name__, type(e).__name__, str(e)), "FFNx setup: %s: %s" % (type(e).__name__, e))
            for g in args:                      # (that game: not looked at again for a moment)
                if isinstance(g, dict) and "next" in g:
                    g["next"] = self.clock() + self.SLOW

    def run(self):
        if self.lister is None and os.name != "nt":
            try:
                import psutil  # noqa: F401
            except ImportError:
                self._once("psutil", "psutil isn't installed, so the app can't see the game start (pip install "
                                     "psutil). Locate FFNx.log still works.")
                return
        last = -1e9
        while not self.stop:
            if self.clock() - last >= self.LOOK:
                last = self.clock()
                self._safe(self._look)
            for g in list(self.games.values()):
                if self._active(g) and g["next"] <= self.clock():
                    self._safe(self._tend, g)
            soon = [g["next"] for g in self.games.values() if self._active(g)]
            wait = max(0.005, min([last + self.LOOK] + soon) - self.clock())
            try:
                cmd = self.cmds.get(timeout=wait)
            except queue.Empty:
                cmd = None
            if cmd and cmd[0] == "stop":
                self.stop = True
        for g in self.games.values():
            self._close(g)

    # ---- which games run
    def _list(self):
        if self.lister is not None:
            return self.lister()
        out = []
        for pid, name in process_names().items():
            if pid in self.games:
                out.append(self.games[pid]["info"])
            elif GAME_EXE_RE.match(name):
                g = game_info(pid, name)
                if g:
                    out.append(g)
            elif name.lower() == REMASTER_EXE:
                self._once(("remaster", pid), "FFVIII.exe is running: the Remaster's own exe, without FFNx (nothing in "
                                              "FFNx.log). Start the game through Junction VIII to see its sounds here.")
        return out

    def _look(self):
        now_games = {g["pid"]: g for g in self._list()}
        for pid, info in now_games.items():
            if pid in self.games:
                continue
            info = dict(info, kind=kind_of(info["folder"]), log=os.path.join(info["folder"], LOG_NAME))
            self.games[pid] = {"pid": pid, "info": info, "seen": self.clock(), "next": 0.0, "proc": None,
                               "done": False, "ffnx": None, "flags": None, "tries": 0, "starting": True,
                               "set_early": set(), "window_at": None, "said_on": False, "said": set(),
                               "bz": {"done": False, "next": 0.0, "mod": None, "addr": None, "build": None,
                                      "said_on": False}}
            self.events.put(("game", info))
        for pid in [p for p in self.games if p not in now_games]:
            self._close(self.games.pop(pid))
            self.events.put(("gone", pid))

    def _close(self, g):
        if g["proc"] is not None:
            try:
                g["proc"].close()
            except Exception:
                pass
            g["proc"] = None

    def _say(self, g, key, text):
        if key not in g["said"]:
            g["said"].add(key)
            self.events.put(("note", text))

    def _give_up(self, g, key, text):
        """FFNx's part ends (said once); Blitzball's goes on. The game's handle closes once neither has more to do."""
        self._say(g, key, text)
        g["done"] = True
        if not self._active(g):
            self._close(g)

    @staticmethod
    def _active(g):
        return not g["done"] or not g["bz"]["done"]

    # ---- one game
    def _tend(self, g):
        try:
            self._tend_now(g)
        except OSError:
            p = g["proc"]
            alive = True
            try:
                alive = p is None or p.alive()
            except Exception:
                pass
            if alive:
                raise
            g["done"] = g["bz"]["done"] = True  # (the game has just closed: "gone" comes with the next look)
            self._close(g)

    def _tend_now(self, g):
        now = self.clock()
        g["next"] = now + (self.FAST if g["starting"] and not g["done"] else self.SLOW)
        if g["proc"] is None:
            try:
                g["proc"] = self.opener(g["pid"])
            except OSError as e:
                g["bz"]["done"] = True
                if getattr(e, "winerror", None) == 5 or getattr(e, "errno", None) == 5:
                    self._give_up(g, "open", "Windows doesn't let this app into the game, so its sound logging can't "
                                             "be switched on (is the game running as administrator? Then start this "
                                             "app as administrator too).")
                else:
                    self._give_up(g, "open", "Couldn't reach the game to switch its sound logging on (%s)." % e)
                return
        if not g["done"]:
            self._ffnx_step(g, g["proc"], now)
        if g["proc"] is not None and not g["bz"]["done"] and (g["done"] or not g["starting"]):
            self._blitz_step(g, g["proc"], now)     # (after FFNx's start: Blitzball logs nothing before its menu)
        g["next"] = min(g["next"], g["bz"]["next"]) if not g["bz"]["done"] else g["next"]
        if g["proc"] is not None and not self._active(g):
            self._close(g)

    def _ffnx_step(self, g, p, now):
        """FFNx's flags (see the top of this file)."""
        if g["ffnx"] is None and not self._find_ffnx(g, p, now):
            return
        if g["flags"] is None and not self._find_flags(g, p, now):
            return
        vals = self._values(g)
        if vals is None:
            return
        addr = g["flags"]["addr"]
        off = [k for k in TRACE_KEYS if k in addr and vals[k] == 0]
        if g["starting"]:
            if g["window_at"] is None and self._has_window(p):
                g["window_at"] = now
            # FFNx has read FFNx.toml: it has kept the game's start address, or a trace this app had switched on went
            # back to off, or the game window is there
            if self._read_done(g) or g["set_early"] & set(off) or g["window_at"] is not None:
                self._start(g)
                return
            if now - g["seen"] > self.START_LIMIT:
                g["starting"] = False
        if off:
            if not self._write(g, off):
                return
            if g["starting"]:
                g["set_early"] |= set(off)
        if not g["starting"]:
            self._said_on(g, off or g["set_early"])

    BLITZ_EVERY = 1.0       # Blitzball's switches looked at this often (its settings can switch them off again)
    BLITZ_LOOK = 2.0        # its DLL looked for this often until it's there (Junction VIII loads it with the game)

    def _blitz_step(self, g, p, now):
        """Blitzball FF8's own sound logging (test 80 and newer), kept on while the game runs."""
        bz = g["bz"]
        if now < bz["next"]:
            return
        bz["next"] = now + self.BLITZ_EVERY
        if bz["mod"] is None:
            m = [x for x in p.modules() if x[0] == BLITZ_FILE and x[1]]
            if not m:
                bz["next"] = now + self.BLITZ_LOOK
                return
            bz["mod"] = m[0]
        if bz["addr"] is None:
            name, base, size = bz["mod"]
            res = find_blitz_trace(p.read_image(base, size), base)
            bz["build"] = res["build"]
            if not res["addr"]:
                bz["done"] = True
                self._say(g, "bz", "Blitzball %s: the app couldn't find its sound logging switches, so it's as Junction "
                                   "VIII's Configure Mod has it (Log Blitzball's music, sound effects, ambience)."
                          % (res["build"] or "(an older one)"))
                return
            bz["addr"] = res["addr"]
        vals = struct.unpack("<3i", p.read(bz["addr"], 12))
        if any(v not in (0, 1) for v in vals):
            bz["done"] = True
            self._say(g, "bz", "Blitzball %s: its sound logging switches hold %s, not 0 or 1, so the app leaves them "
                               "alone." % (bz["build"], ", ".join(str(v) for v in vals)))
            return
        off = [i for i, v in enumerate(vals) if v == 0]
        try:
            for i in off:
                p.write(bz["addr"] + 4 * i, b"\x01\0\0\0")
        except OSError as e:
            if not p.alive():
                raise
            bz["done"] = True
            self._say(g, "bz", "Windows didn't let the app switch Blitzball's sound logging on (%s)." % e)
            return
        if not bz["said_on"]:
            bz["said_on"] = True
            self._say(g, "bz", "Blitzball %s: its sound logging %s inside the game (music, sound effects, ambience: "
                               "the Blitzball window shows them). Its settings aren't changed."
                      % (bz["build"], "on" if off else "was already on"))

    def _find_ffnx(self, g, p, now):
        """FFNx among the game's modules (it loads with the game; with the 2013 Steam release the game's own EAX.DLL
        is there too): True once it's there."""
        mods = p.modules()
        ff = sorted((m for m in mods if m[0] in FFNX_FILES and m[1]), key=lambda m: FFNX_FILES.index(m[0]))
        for m in ff:
            if self._is_ffnx(p, m[1]):
                g["ffnx"] = m
                return True
        g["next"] = now + self.MODULES
        if g["window_at"] is None and self._has_window(p):
            g["window_at"] = now
        waited = now - g["seen"]
        if mods and (waited > self.NO_FFNX or g["window_at"] is not None and now - g["window_at"] > 3):
            self._give_up(g, "noffnx", "No FFNx in this game (%s), so FFNx.log won't have its sounds."
                          % ("its %s isn't FFNx" % " and ".join(m[0] for m in ff) if ff else "no eax.dll or AF3DN.P in it"))
        elif waited > self.START_LIMIT:
            self._give_up(g, "noffnx", "Couldn't see what's loaded in the game, so its sound logging can't be switched "
                                       "on.")
        return False

    @staticmethod
    def _is_ffnx(p, base):
        """The module at base exports nxRegisterKeyListener (FFNx does), read from the game."""
        try:
            blk = export_block(p.read(base, 0x400))
            return bool(blk) and FFNX_EXPORT in p.read(base + blk[0], blk[1])
        except OSError:
            return False

    def _find_flags(self, g, p, now):
        """FFNx's settings, found in its code (or kept from the last time this FFNx build was seen: no time lost while
        the game starts): True once known (some may be missing)."""
        name, base, size = g["ffnx"]
        key = build_key(p.read(base, 0x400))
        res = flags_from_build(self.builds[key], base) if key in self.builds else None
        g["kept"] = key if res is not None else None
        if res is None:
            res = find_flags(p.read_image(base, size), base)
            if res["version"] and not res["addr"] and g["tries"] < self.TRIES:
                g["tries"] += 1                 # (Windows may still be setting FFNx up in the game: once more soon)
                g["next"] = now + 0.3
                return False
            if key and res["version"] and all(k in res["addr"] for k in KEYS):
                self.builds[key] = flags_to_build(res, base)
                self._save_builds()
        g["flags"] = res
        ver = res["version"]
        if not ver:
            self._give_up(g, "missing", "FFNx (%s): the app couldn't find its version or its sound settings in it, so "
                                        "nothing was changed. Turn on %s in FFNx.toml to see its sounds."
                          % (name, ", ".join(TRACE_KEYS)))
            return False
        miss = [k for k in KEYS if k not in res["addr"]]
        if not any(k in res["addr"] for k in TRACE_KEYS):
            self._give_up(g, "missing", "FFNx %s: the app couldn't find its sound settings in it (a newer FFNx?), so "
                                        "nothing was changed. Turn on %s in FFNx.toml to see its sounds."
                          % (ver, ", ".join(TRACE_KEYS)))
            return False
        if miss:
            self._say(g, "missing", "FFNx %s: the app couldn't find %s in it (a newer FFNx?), so FFNx.toml decides %s."
                      % (ver, " and ".join(miss), "that one" if len(miss) == 1 else "those"))
        return True

    def _write(self, g, keys):
        """Each key's byte set to 1. False (and the game left alone) if Windows refuses."""
        try:
            for k in keys:
                g["proc"].write(g["flags"]["addr"][k], b"\x01")
            return True
        except OSError as e:
            self._give_up(g, "write", "Windows didn't let the app change the game's memory (%s), so its sound logging "
                                      "can't be switched on (an antivirus can block this)." % e)
            return False

    def _read_done(self, g):
        """FFNx has read FFNx.toml: right after, it keeps the game's start address (0 until then)."""
        mark = g["flags"]["read_mark"]
        return bool(mark) and struct.unpack("<I", g["proc"].read(mark, 4))[0] in FF8_STARTS

    def _has_window(self, p):
        return any(t.strip().lower().startswith(WINDOW_TITLE) for t in p.window_titles())

    def _read_values(self, g):
        """{key: its byte now} (one read when they're close together, as they are)."""
        addr = g["flags"]["addr"]
        lo, hi = min(addr.values()), max(addr.values())
        if hi - lo < 4096:
            block = g["proc"].read(lo, hi - lo + 1)
            return {k: block[a - lo] for k, a in addr.items()}
        return {k: g["proc"].read(a, 1)[0] for k, a in addr.items()}

    def _values(self, g):
        """_read_values, or None (and the game left alone) if a byte isn't 0 or 1: not FFNx's flag after all."""
        vals = self._read_values(g)
        bad = [k for k in vals if vals[k] not in (0, 1)]
        if bad and g.get("kept"):               # (kept from before but wrong now: looked for afresh next time)
            self.builds.pop(g["kept"], None)
            self._save_builds()
        if bad:
            self._give_up(g, "bad", "FFNx %s: %s holds %d, not 0 or 1, so the app leaves this game alone."
                          % (g["flags"]["version"], bad[0], vals[bad[0]]))
            return None
        return vals

    def _start(self, g):
        """FFNx has read FFNx.toml: the external flags that are off, with the game paused a moment (FFNx reads them
        as it opens the game window: its Sound menu, then its sound a moment later), and the traces."""
        g["starting"] = False
        p, flags, info = g["proc"], g["flags"], g["info"]
        addr = flags["addr"]
        vals = self._values(g)
        if vals is None:
            return
        ext_off = [k for k in EXT_KEYS if k in addr and vals[k] == 0]
        if not ext_off:                         # (nothing that needs the moment: no pause)
            traces = [k for k in TRACE_KEYS if k in addr and vals[k] == 0]
            if traces and not self._write(g, traces):
                return
            self._said_on(g, traces or g["set_early"])
            return
        done, late, unsure, traces, err = [], [], [], [], None
        try:
            p.suspend()
            paused = True
        except OSError as e:
            paused = False
            self._say(g, "pause", "Couldn't pause the game a moment (%s), so FFNx.toml decides %s."
                      % (e, _and(ext_off)))
        try:                                    # (nothing in here closes the game's handle: it's resumed first)
            vals = self._read_values(g)
            good = all(v in (0, 1) for v in vals.values())
            ext_off = [k for k in EXT_KEYS if k in addr and vals[k] == 0] if good and paused else []
            if ext_off:
                if not self._has_window(p):     # (paused: nothing can open it now) before all of it
                    done = ext_off
                else:
                    started = _log_has(info["log"], SETUP_LINE, info["created"] or 0) if flags["setup_line"] else None
                    for k in ext_off:
                        if started is None:
                            unsure.append(k)
                        elif started or k == "use_external_music" and flags["sound_screen"]:
                            late.append(k)      # (its Sound menu read use_external_music as the window opened)
                        else:
                            done.append(k)
                for k in done:
                    p.write(addr[k], b"\x01")
            if good:
                traces = [k for k in TRACE_KEYS if k in addr and vals[k] == 0]
                for k in traces:
                    p.write(addr[k], b"\x01")
        except OSError as e:
            err = e
        finally:
            if paused:
                p.resume()
        if err is not None:
            if not p.alive():                   # (the game has just closed)
                g["done"] = g["bz"]["done"] = True
                self._close(g)
                return
            self._give_up(g, "write", "Windows didn't let the app change the game's memory (%s), so its sound logging "
                                      "can't be switched on (an antivirus can block this)." % err)
            return
        if self._values(g) is None:
            return
        self._said_on(g, traces or g["set_early"])
        ver = flags["version"]
        if done:
            self._say(g, "ext", "FFNx %s: %s switched on for this run. FFNx now plays %s from .ogg files and logs each "
                                "one it looks for: any without an .ogg is silent." % (ver, _and(done), _what(done)))
        if late:
            self._say(g, "late", "This game was already too far into its start when the app found it, so %s stayed "
                                 "off: %s won't show here. To see %s, close the game and start it again while the app "
                                 "is open." % (_and(late), _what(late, "its "), "them" if len(late) > 1 or
                                               late == ["use_external_sfx"] else "it"))
        if unsure:
            one = unsure == ["use_external_music"]
            self._say(g, "unsure", "FFNx %s: the app can't tell if this FFNx has set up its sound yet, so %s stayed "
                                   "off: %s %s only if a mod or FFNx.toml turns %s on."
                      % (ver, _and(unsure), _what(unsure, "its "), "shows" if one else "show", "it" if one else "them"))

    def _said_on(self, g, switched):
        if g["said_on"]:
            return
        g["said_on"] = True
        on = ", ".join(k for k in TRACE_KEYS if k in g["flags"]["addr"])
        if switched:
            self._say(g, "on", "FFNx %s: sound logging on inside the game (%s). FFNx.toml isn't changed."
                      % (g["flags"]["version"], on))
        else:
            self._say(g, "on", "FFNx %s: its sound logging was already on (%s)." % (g["flags"]["version"], on))
