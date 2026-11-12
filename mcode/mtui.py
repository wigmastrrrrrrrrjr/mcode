"""mtui — a tiny, fast terminal UI framework (raw ANSI, diff rendering).

No dependencies. Provides:
  App          — base app with render loop, key/mouse/timer events
  InputBuffer  — single-line text editor
  ansi helpers — style(), visible_width(), strip_ansi()
  markdown     — lightweight markdown -> styled lines

App subclasses implement:
  render() -> list[(line_str, tags)]   full-screen rows, bottom row last
  on_key(name, ch)                     name like 'enter', 'up', 'ctrl+s'
  on_click(tag)                        mouse click on a tagged region
  on_wheel(delta)                      +1 / -1
  on_resize(w, h)
  on_timer(name)                       from self.every(interval, name)
"""
import os
import re
import sys
import time
import fcntl
import signal

_dbg = None
import struct
import termios
import threading
import unicodedata

# ---------------------------------------------------------------- ansi ---

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b[()][B0]|\x1b[=>]")


def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


def char_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def visible_width(s: str) -> int:
    return sum(char_width(c) for c in strip_ansi(s))


def style(text: str, fg=None, bg=None, bold=False, dim=False, underline=False) -> str:
    codes = []
    if bold:
        codes.append("1")
    if dim:
        codes.append("2")
    if underline:
        codes.append("4")
    if fg is not None:
        codes.append(("38;5;%d" % fg) if isinstance(fg, int) else fg)
    if bg is not None:
        codes.append(("48;5;%d" % bg) if isinstance(bg, int) else bg)
    if not codes:
        return text
    return "\x1b[" + ";".join(codes) + "m" + text + "\x1b[0m"


RESET = "\x1b[0m"
FG_DEFAULT = "39"
BG_DEFAULT = "49"


def pad_to(s: str, width: int) -> str:
    """Truncate an ansi string to visible width, reset at end."""
    out = []
    w = 0
    i = 0
    cur = ""
    while i < len(s):
        m = ANSI_RE.match(s, i)
        if m:
            cur += m.group(0)
            i = m.end()
            continue
        ch = s[i]
        cw = char_width(ch)
        if w + cw > width:
            break
        cur += ch
        w += cw
        i += 1
    out.append(cur)
    return "".join(out) + ("" if w >= width else " " * (width - w))


def truncate(s: str, width: int) -> str:
    out = []
    w = 0
    i = 0
    while i < len(s):
        m = ANSI_RE.match(s, i)
        if m:
            out.append(m.group(0))
            i = m.end()
            continue
        ch = s[i]
        cw = char_width(ch)
        if w + cw > width:
            break
        out.append(ch)
        w += cw
        i += 1
    return "".join(out)


# ---------------------------------------------------------------- input ---

class InputBuffer:
    def __init__(self, text=""):
        self.text = text
        self.cursor = len(text)
        self.history: list[str] = []
        self._hist_idx = None

    def set(self, text):
        self.text = text
        self.cursor = len(text)

    def clear(self):
        if self.text:
            self.history.append(self.text)
        self.text = ""
        self.cursor = 0

    def insert(self, ch: str):
        self.text = self.text[: self.cursor] + ch + self.text[self.cursor:]
        self.cursor += len(ch)

    def backspace(self):
        if self.cursor > 0:
            self.text = self.text[: self.cursor - 1] + self.text[self.cursor:]
            self.cursor -= 1

    def delete(self):
        if self.cursor < len(self.text):
            self.text = self.text[: self.cursor] + self.text[self.cursor + 1:]

    def left(self):
        self.cursor = max(0, self.cursor - 1)

    def right(self):
        self.cursor = min(len(self.text), self.cursor + 1)

    def home(self):
        self.cursor = 0

    def end(self):
        self.cursor = len(self.text)

    def delete_word(self):
        i = self.cursor
        while i > 0 and self.text[i - 1] == " ":
            i -= 1
        while i > 0 and self.text[i - 1] != " ":
            i -= 1
        self.text = self.text[:i] + self.text[self.cursor:]
        self.cursor = i

    def clear_line(self):
        self.text = ""
        self.cursor = 0

    def history_prev(self):
        if not self.history:
            return
        if self._hist_idx is None:
            self._hist_idx = len(self.history)
        self._hist_idx = max(0, self._hist_idx - 1)
        self.set(self.history[self._hist_idx])

    def history_next(self):
        if self._hist_idx is None:
            return
        self._hist_idx += 1
        if self._hist_idx >= len(self.history):
            self._hist_idx = None
            self.set("")
        else:
            self.set(self.history[self._hist_idx])

    # returns (visible_text, cursor_col) given available width
    def view(self, width: int):
        text = self.text
        cur = self.cursor
        if visible_width(text) <= width:
            return text, visible_width(text[:cur])
        # horizontal scroll: keep cursor visible
        left = cur
        while left > 0 and visible_width(text[left - 1:cur]) <= width - 1:
            left -= 1
        right = cur
        while right < len(text) and visible_width(text[cur:right]) <= width - 1:
            right += 1
        while visible_width(text[left:right]) > width and left < cur:
            left += 1
        return text[left:right], visible_width(text[left:cur])


# ---------------------------------------------------------------- keys ---

class Key:
    __slots__ = ("name", "ch", "mouse")

    def __init__(self, name="", ch="", mouse=None):
        self.name = name
        self.ch = ch
        self.mouse = mouse  # ('click'|'release'|'wheel_up'|'wheel_down', x, y)


_CTRL_NAMES = {
    0: "ctrl+@", 3: "ctrl+c", 4: "ctrl+d", 8: "ctrl+h", 9: "tab",
    10: "enter", 11: "ctrl+k", 12: "ctrl+l", 13: "enter", 14: "ctrl+n",
    15: "ctrl+o", 16: "ctrl+p", 17: "ctrl+q", 18: "ctrl+r", 19: "ctrl+s",
    20: "ctrl+t", 21: "ctrl+u", 22: "ctrl+v", 23: "ctrl+w", 24: "ctrl+x",
    25: "ctrl+y", 26: "ctrl+z",
}
_ESCAPE_NAMES = {"A": "up", "B": "down", "C": "right", "D": "left",
                 "H": "home", "F": "end", "Z": "shift+tab"}
_TILDE_NAMES = {"1~": "home", "2~": "insert", "3~": "delete", "4~": "end",
                "5~": "pgup", "6~": "pgdown", "11~": "f1", "12~": "f2",
                "13~": "f3", "14~": "f4", "15~": "f5", "17~": "f6",
                "18~": "f7", "19~": "f8", "20~": "f9", "21~": "f10",
                "23~": "f11", "24~": "f12"}


def parse_keys(buf: bytes):
    """Parse a byte buffer into a list of (Key, consumed_bytes)."""
    out = []
    i = 0
    n = len(buf)
    while i < n:
        b = buf[i]
        if b == 27:  # ESC
            if i + 1 < n and buf[i + 1] == ord("["):
                j = i + 2
                params = bytearray()
                while j < n and buf[j] not in (0x4D, 0x6D, 0x52, 0x7E, 0x41, 0x42, 0x43, 0x44, 0x48, 0x46, 0x50, 0x51, 0x5A, 0x75):
                    params.append(buf[j])
                    j += 1
                if j >= n:
                    break  # incomplete
                final = chr(buf[j])
                p = params.decode("ascii", "ignore")
                if final == "u":  # kitty keyboard: ESC[<code>;<mod>u
                    parts = p.lstrip("<").split(";")
                    try:
                        code = int(parts[0])
                        mod = int(parts[1]) if len(parts) > 1 else 1
                    except Exception:
                        i = j + 1
                        continue
                    name = None
                    if mod >= 5:  # ctrl (1 shift, 2 alt, 4 ctrl)
                        if code == 13:
                            name = "ctrl+m"
                        else:
                            ch = chr(code) if 32 <= code < 127 else ""
                            if ch:
                                name = "ctrl+" + ch.lower()
                    else:
                        if code == 13:
                            name = "enter"
                        elif code == 27:
                            name = "esc"
                        elif code == 9:
                            name = "tab"
                        elif code in (97, 98, 99, 100, 104, 102):  # kitty arrows
                            name = {97: "up", 98: "down", 99: "right",
                                    100: "left", 104: "home", 102: "end"}.get(code)
                        else:
                            ch = chr(code) if 32 <= code < 127 else ""
                            if ch:
                                name = "char"
                                out.append((Key(name=name, ch=ch), j + 1 - i))
                                i = j + 1
                                continue
                    if name:
                        out.append((Key(name=name), j + 1 - i))
                    i = j + 1
                    continue
                if final in ("M", "m"):  # SGR mouse  ESC[<b;x;yM
                    parts = p.lstrip("<").split(";")
                    try:
                        cb, cx, cy = int(parts[0]), int(parts[1]), int(parts[2])
                    except Exception:
                        i = j + 1
                        continue
                    x, y = cx - 1, cy - 1
                    press = final == "M"
                    if cb & 64:
                        name = "wheel_up" if (cb & 1) == 0 else "wheel_down"
                        out.append((Key(name=name, mouse=(name, x, y)), j + 1 - i))
                    elif press:
                        out.append((Key(name="click", mouse=("click", x, y)), j + 1 - i))
                    i = j + 1
                    continue
                if final == "R":
                    i = j + 1
                    continue
                if final == "~":
                    name = _TILDE_NAMES.get(p)
                    if name:
                        out.append((Key(name=name), j + 1 - i))
                elif final in _ESCAPE_NAMES:
                    mod = ""
                    pp = p.split(";")
                    if len(pp) == 3 and pp[1] == "5":
                        mod = "ctrl+"
                    out.append((Key(name=mod + _ESCAPE_NAMES[final]), j + 1 - i))
                i = j + 1
                continue
            if i + 1 < n and buf[i + 1] == ord("O"):
                if i + 2 < n:
                    c = chr(buf[i + 2])
                    name = {"P": "f1", "Q": "f2", "R": "f3", "S": "f4"}.get(c)
                    if name:
                        out.append((Key(name=name), 3))
                    i += 3
                    continue
            # bare esc
            out.append((Key(name="esc"), 1))
            i += 1
            continue
        if b in _CTRL_NAMES:
            out.append((Key(name=_CTRL_NAMES[b]), 1))
            i += 1
            continue
        if b == 127:
            out.append((Key(name="backspace"), 1))
            i += 1
            continue
        # utf8 char
        if b < 0x80:
            out.append((Key(name="char", ch=chr(b)), 1))
            i += 1
            continue
        try:
            ch = buf[i:i + 4].decode("utf-8", "ignore")
            if not ch:
                i += 1
                continue
            out.append((Key(name="char", ch=ch[0]), min(4, n - i)))
            i += len(ch.encode("utf-8")) or 1
        except Exception:
            i += 1
    return out


# --------------------------------------------------------------- terminal ---

class Terminal:
    def __init__(self):
        self.in_fd = sys.stdin.fileno()
        self.out_fd = sys.stdout.fileno()
        self._old_termios = None

    def size(self):
        try:
            res = struct.unpack("HHHH", fcntl.ioctl(self.out_fd,
                                                    termios.TIOCGWINSZ,
                                                    b"\0" * 8))
            return max(20, res[1]), max(4, res[0])  # (cols, rows)
        except Exception:
            return 80, 24

    def enter(self):
        self._old_termios = termios.tcgetattr(self.in_fd)
        attrs = termios.tcgetattr(self.in_fd)
        attrs[0] &= ~(termios.ICRNL | termios.IXON | termios.BRKINT)
        attrs[3] &= ~(termios.ICANON | termios.ECHO | termios.ISIG | termios.IEXTEN)
        attrs[1] &= ~termios.OPOST
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(self.in_fd, termios.TCSANOW, attrs)
        os.write(self.out_fd, b"\x1b[?1049h\x1b[?25l\x1b[2J"
                               b"\x1b[?1000;1002;1006h"
                               b"\x1b[>1u")

    def exit(self):
        try:
            os.write(self.out_fd, b"\x1b[<u"
                                  b"\x1b[?1000;1002;1006l\x1b[?25h"
                                  b"\x1b[?1049l")
            if self._old_termios:
                termios.tcsetattr(self.in_fd, termios.TCSANOW, self._old_termios)
        except Exception:
            pass

    def write(self, s: str):
        os.write(self.out_fd, s.encode("utf-8", "replace"))


# -------------------------------------------------------------------- app ---

def _esc_complete(b: bytes) -> bool:
    """True if a leading escape sequence in b is complete (has final byte)."""
    if len(b) < 2:
        return False
    if b[1:2] == b"[":
        for ch in b[2:]:
            if 0x40 <= ch <= 0x7E:
                return True
        return False
    if b[1:2] in (b"O", b"P"):
        return len(b) >= 3
    return True


class App:
    FPS = 30

    def __init__(self):
        self.term = Terminal()
        self.cols, self.rows = self.term.size()
        self._events: "list" = []
        self._lock = threading.Lock()
        self._timers: list[tuple[float, str, float, float]] = []  # (interval, name, next, last)
        self._dirty = True
        self._prev_frame: list[str] = []
        self._tag_map: dict[int, list] = {}
        self._running = True
        self._exit_code = 0
        self._resize_pending = False

    # --- public helpers
    def every(self, interval: float, name: str):
        self._timers.append((interval, name, time.time() + interval, 0.0))

    def redraw(self):
        self._dirty = True

    def quit(self, code=0):
        self._running = False
        self._exit_code = code

    def bell(self):
        self.term.write("\x07")

    # --- override points
    def on_start(self):
        pass

    def on_key(self, name: str, ch: str = ""):
        pass

    def on_click(self, tag: str, x: int, y: int):
        pass

    def on_wheel(self, delta: int):
        pass

    def on_resize(self, w: int, h: int):
        pass

    def on_timer(self, name: str):
        pass

    def render(self):
        """Return list of (line, tags); len <= rows."""
        return []

    def cursor_pos(self):
        """(row, col) where the block cursor sits, or None."""
        return None

    # --- internals
    def _reader_thread(self):
        pending = b""
        while self._running:
            try:
                import select
                r, _, _ = select.select([self.term.in_fd], [], [], 0.2)
                if not r:
                    if pending == b"\x1b":
                        with self._lock:
                            self._events.append((Key(name="esc"), 1))
                        pending = b""
                    continue
                data = os.read(self.term.in_fd, 4096)
                if not data:
                    continue
                if _dbg:
                    _dbg.write("RAW %r\n" % data)
                if pending:
                    data = pending + data
                    pending = b""
                with self._lock:
                    events = parse_keys(data)
                # leftover after all parsed keys?
                consumed = sum(n for _, n in events)
                if consumed < len(data):
                    rest = data[consumed:]
                    if rest == b"\x1b":
                        # lone escape; wait briefly for a possible sequence
                        pending = rest
                        continue
                    if rest.startswith(b"\x1b") and not _esc_complete(rest):
                        # incomplete escape; wait for more bytes
                        pending = rest
                        continue
                    # else: unknown/alt sequence; parse remainder normally
                    with self._lock:
                        self._events.extend(events)
                        self._events.extend(parse_keys(rest))
                    continue
                with self._lock:
                    self._events.extend(events)
            except Exception:
                if not self._running:
                    break

    def _handle_sigwinch(self, *a):
        self._resize_pending = True

    def _draw(self):
        cols, rows = self.cols, self.rows
        content = self.render()
        content = content[:rows]
        while len(content) < rows:
            content.append(("", []))
        # build frame
        frame = []
        tags = {}
        for i, (line, tl) in enumerate(content):
            frame.append(pad_to(line, cols))
            if tl:
                tags[i] = tl
        # cursor
        cur = self.cursor_pos()
        if cur and 0 <= cur[0] < rows:
            # make room for the cursor block: cap visible width at cols-1
            row_line = pad_to(frame[cur[0]], cols - 1)
            frame[cur[0]] = row_line
            col = cur[1]
            pos = 0
            out_i = 0
            while out_i < len(row_line) and pos < col:
                m = ANSI_RE.match(row_line, out_i)
                if m:
                    out_i = m.end()
                    continue
                out_i += 1
                pos += 1
                # wide char
                if out_i < len(row_line) and not ANSI_RE.match(row_line, out_i):
                    pass
            if out_i <= len(row_line):
                frame[cur[0]] = (row_line[:out_i] + "\x1b[7m \x1b[0m" +
                                 row_line[out_i:])
        buf = []
        prev = self._prev_frame
        first_draw = not prev
        for i, line in enumerate(frame):
            if first_draw or i >= len(prev) or prev[i] != line:
                buf.append("\x1b[%d;1H%s\x1b[K" % (i + 1, line))
        if buf:
            self.term.write("".join(buf))
        self._prev_frame = frame
        self._tag_map = tags

    def run(self):
        global _dbg
        if os.environ.get("MCODE_DEBUG_EVENTS"):
            _dbg = open(os.environ["MCODE_DEBUG_EVENTS"], "a", buffering=1)
        self.term.enter()
        try:
            signal.signal(signal.SIGWINCH, self._handle_sigwinch)
        except ValueError:
            pass  # not main thread (tests)
        try:
            self.on_start()
            reader = threading.Thread(target=self._reader_thread, daemon=True)
            reader.start()
            frame_time = 1.0 / self.FPS
            last_draw = 0.0
            while self._running:
                now = time.time()
                # resize
                if self._resize_pending:
                    self._resize_pending = False
                    self.cols, self.rows = self.term.size()
                    self._prev_frame = []
                    self.on_resize(self.cols, self.rows)
                    self._dirty = True
                # events
                with self._lock:
                    events, self._events = self._events, []
                for key, _ in events:
                    self._dirty = True
                    if _dbg:
                        _dbg.write("KEY %s %r overlay=%s\n" % (
                            key.name, key.ch,
                            getattr(self, "overlay", None)))
                    if key.mouse:
                        kind, x, y = key.mouse
                        if kind == "click":
                            if _dbg:
                                _dbg.write("CLICK x=%d y=%d tags=%s\n" %
                                           (x, y, self._tag_map.get(y)))
                            for start, end, tag in self._tag_map.get(y, []):
                                if start <= x <= end:
                                    self.on_click(tag, x, y)
                                    break
                        elif kind in ("wheel_up", "wheel_down"):
                            self.on_wheel(1 if kind == "wheel_down" else -1)
                    else:
                        self.on_key(key.name, key.ch)
                # timers
                for idx, (interval, name, nxt, _l) in enumerate(self._timers):
                    if now >= nxt:
                        self._timers[idx] = (interval, name,
                                             now + interval, now)
                        self.on_timer(name)
                # draw
                if self._dirty and now - last_draw >= frame_time:
                    self._dirty = False
                    last_draw = now
                    self._draw()
                time.sleep(0.005)
        finally:
            self.term.exit()
        return self._exit_code


# ------------------------------------------------------------- markdown ---

_MD_TOKEN = re.compile(r"(\*\*.+?\*\*|\*.+?\*|`.+?`)")


def _md_inline(text: str, base_kwargs: dict) -> list[tuple[str, dict]]:
    out = []
    for part in _MD_TOKEN.split(text):
        if not part:
            continue
        kw = dict(base_kwargs)
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            kw["bold"] = True
            out.append((part[2:-2], kw))
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            kw["fg"] = "86"
            kw["bg"] = "236"
            out.append((part[1:-1], kw))
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            kw["fg"] = "245"
            out.append((part[1:-1], kw))
        else:
            out.append((part, kw))
    return out


def md_to_lines(text: str, base_kwargs: dict | None = None) -> list[tuple[str, dict]]:
    """Markdown-lite -> list of (plain_text, style_kwargs) lines.

    The caller wraps the plain text and applies the style.
    """
    base = dict(base_kwargs or {"fg": "255"})
    out = []
    in_code = False
    for raw in text.split("\n"):
        stripped = raw.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            out.append(("", {"fg": "245"}))
            continue
        if in_code:
            out.append(("  " + raw, {"fg": "86", "bg": "236"}))
            continue
        for prefix, extra in (("# ", {"bold": True, "underline": True}),
                              ("## ", {"bold": True}),
                              ("### ", {"bold": True})):
            if raw.startswith(prefix):
                out.append((raw[len(prefix):], {**base, **extra}))
                break
        else:
            if stripped.startswith(("- ", "* ")):
                out.append(("• " + stripped[2:], {**base, "fg": "245"}))
            else:
                segs = _md_inline(raw, base)
                if not segs:
                    out.append(("", base))
                elif all(s[1] == segs[0][1] for s in segs):
                    out.append(("".join(s[0] for s in segs), segs[0][1]))
                else:
                    # mixed inline styles: bold if any bold, else code style
                    kw = dict(base)
                    if any(s[1].get("bold") for s in segs):
                        kw["bold"] = True
                    elif any(s[1].get("fg") == "86" for s in segs):
                        kw["fg"] = "86"
                    out.append(("".join(s[0] for s in segs), kw))
    return out


def md_to_segments(text: str, fg=None, dim_fg="240", code_fg="86",
                   code_bg="236") -> list[str]:
    """Convenience: fully styled lines (no wrapping)."""
    base = {"fg": fg} if fg else {}
    lines = []
    for plain, kw in md_to_lines(text, base or None):
        lines.append(style(plain, **kw))
    return lines
