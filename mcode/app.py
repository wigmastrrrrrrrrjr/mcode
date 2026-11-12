"""mcode UI — Claude Code style terminal IDE, built on mtui."""
import sys
import functools
import json
import os
import threading
import time
import queue as _queue

import mcode.mtui as ui
from mcode.api import (MRouterClient, CustomProviderClient, DispatchClient,
                       Unauthorized)
from mcode.agent import run_turn
from mcode.mcp import MCPManager
from mcode import sessions as store
from mcode import health as mhealth
from mcode.storage import (ensure_dirs, load_config, save_config,
                           DEFAULT_ROUTER_URL, CONFIG_PATH, MCP_PATH)

FG = "255"
DIM = "245"
DIMMER = "240"
CYAN = "114"
BLUE = "114"
GREEN = "114"
RED = "203"
PINK = "176"
MAG = ["22", "28", "34", "71", "34", "28"]  # shimmer cycle (green)


def ok(msg):
    return ui.style(msg, fg=GREEN)


def err(msg):
    return ui.style(msg, fg=RED)


def wrap(text: str, width: int) -> list[str]:
    lines = []
    for para in text.split("\n"):
        if not para:
            lines.append("")
            continue
        cur = ""
        for word in para.split(" "):
            if ui.visible_width(cur + (" " if cur else "") + word) <= width:
                cur += (" " if cur else "") + word
            else:
                if cur:
                    lines.append(cur)
                while ui.visible_width(word) > width:
                    cut = width
                    while cut > 1 and ui.char_width(word[cut - 1]) == 0:
                        cut -= 1
                    lines.append(word[:cut])
                    word = word[cut:]
                cur = word
        if cur:
            lines.append(cur)
    return lines


class LineBuilder:
    def __init__(self):
        self.parts = []
        self.tags = []
        self.w = 0

    def add(self, text, style_str="", tag=None):
        w = ui.visible_width(text)
        if tag is not None:
            self.tags.append((self.w, self.w + w, tag))
        self.parts.append(style_str + text + ui.RESET if style_str else text)
        self.w += w
        return self

    def line(self):
        return "".join(self.parts), self.tags


# ------------------------------------------------------------------ app ---

class MCodeApp(ui.App):
    def __init__(self):
        super().__init__()
        ensure_dirs()
        cfg = load_config()
        self.router_url = cfg.get("router_url", DEFAULT_ROUTER_URL)
        self.api_key = cfg.get("api_key", "")
        self.email = cfg.get("email", "")
        self.model_id = cfg.get("model", "")
        self.models_cache: list[dict] = []
        self.health: dict[str, bool] = {}
        self.session: dict | None = None
        self.mcp = MCPManager()
        self.turn_queue: _queue.Queue = _queue.Queue()
        self.busy = False
        self.chat_entries: list[dict] = []   # rendered chat blocks
        self.chat_cache: list = []           # (count, lines, tags)
        self.scroll_from_bottom = 0
        self.input = ui.InputBuffer()
        self.mode = "chat"                   # chat | login
        self.overlay = None                  # dict(kind=...)
        self.notify_text = ""
        self.notify_until = 0.0
        self.shimmer_idx = 0
        self._line_cache: dict[int, tuple] = {}

        self.every(0.1, "tick")

    # ------------------------------------------------------------ helpers
    def all_clients(self) -> dict:
        clients = {"mrouter": MRouterClient(self.router_url, self.api_key)}
        for p in load_config().get("providers", []):
            clients[p["name"]] = CustomProviderClient(p["name"], p["base_url"],
                                                      p["api_key"])
        return clients

    def turn_client(self):
        return DispatchClient(self.all_clients())

    def fresh_session(self):
        return {"id": store.new_session_id(), "name": "Untitled",
                "model": self.model_id, "messages": []}

    def invalidate_chat(self):
        self.chat_cache = []

    def notify(self, text, good=True):
        self.notify_text = (ok(text) if good else err(text))
        self.notify_until = time.time() + 4
        self.redraw()

    # ------------------------------------------------------------ startup
    def on_start(self):
        if self.api_key:
            self.mode = "chat"
            self._enter_main()
        else:
            self.mode = "login"

    def _enter_main(self):
        self.session = self.fresh_session()
        self.mcp.start_all()
        threading.Thread(target=self._refresh_models_worker,
                         daemon=True).start()

    def _refresh_models_worker(self):
        clients = self.all_clients()
        seen = {}
        for name, c in clients.items():
            try:
                seen[name] = c.list_models(force=True)
            except Exception:
                seen[name] = []
        self.turn_queue.put(("models", seen))
        hdata = mhealth.load_health()
        self.turn_queue.put(("health_cached", hdata))
        if mhealth.is_stale(hdata) and any(seen.values()):
            results = mhealth.probe(
                [{**m, "id": (f"custom:{prov}:{m['id']}" if prov != "mrouter"
                              else m["id"])}
                 for prov, models in seen.items() for m in models],
                clients)
            self.turn_queue.put(("health", results))

    def _drain(self):
        try:
            while True:
                item = self.turn_queue.get_nowait()
                kind = item[0]
                payload = item[1] if len(item) > 1 else None
                if kind == "models":
                    self._models_done(payload)
                elif kind == "health":
                    self.health = payload
                    mhealth.save_health({"checked_at": time.time(),
                                         "results": payload})
                    self._apply_health()
                    n_ok = sum(1 for v in payload.values() if v)
                    self.notify(f"Model check: {n_ok}/{len(payload)} working")
                elif kind == "health_cached":
                    if payload and not self.health:
                        self.health = payload.get("results", {})
                        self._apply_health()
                elif kind == "event":
                    self._handle_event(payload)
                elif kind == "turn_done":
                    self.busy = False
                    store.save_session(self.session)
                elif kind == "login_ok":
                    url, key, email = payload
                    cfg = load_config()
                    cfg["router_url"] = url
                    cfg["api_key"] = key
                    cfg["email"] = email
                    save_config(cfg)
                    self.router_url, self.api_key = url, key
                    self.login = None
                    self.mode = "chat"
                    self._enter_main()
                    self.notify(f"Logged in as {email}")
                elif kind == "login_fail":
                    self.login["busy"] = False
                    self.login["status"] = err(payload)
                elif kind == "turn_error":
                    self.busy = False
                    self.add_chat({"kind": "error", "text": payload})
                    store.save_session(self.session)
        except _queue.Empty:
            pass
        self.redraw()

    def _models_done(self, seen: dict):
        self.models_cache = []
        for prov, models in seen.items():
            for m in models:
                mid = m["id"] if prov == "mrouter" else f"custom:{prov}:{m['id']}"
                self.models_cache.append({**m, "id": mid, "provider": prov})
        self._apply_health()
        self.redraw()

    def _apply_health(self):
        if not self.health:
            return
        working = [m for m in self.models_cache if self.health.get(m["id"])]
        dead = [m for m in self.models_cache
                if m["id"] in self.health and not self.health.get(m["id"])]
        unknown = [m for m in self.models_cache if m["id"] not in self.health]
        self.models_cache = working + unknown + dead
        model = self.session.get("model") if self.session else None
        if self.session and (not model or self.health.get(model) is False):
            if working:
                self.session["model"] = working[0]["id"]
                cfg = load_config()
                cfg["model"] = self.session["model"]
                save_config(cfg)

    # ------------------------------------------------------------ chat add
    def add_chat(self, entry: dict):
        self.chat_entries.append(entry)
        self.scroll_from_bottom = 0
        self.invalidate_chat()
        self.redraw()

    def _handle_event(self, ev: dict):
        t = ev["type"]
        if t == "user_message":
            self.add_chat({"kind": "user", "text": ev["content"]})
        elif t == "thinking":
            self.add_chat({"kind": "thinking", "active": True,
                           "started": time.time(), "reasoning": "",
                           "expanded": False})
        elif t == "reasoning":
            th = self._last_thinking()
            if th:
                th["reasoning"] += ev["content"]
                self.invalidate_chat()
        elif t == "thinking_done":
            th = self._last_thinking()
            if th:
                th["active"] = False
                th["done_at"] = time.time()
                self.invalidate_chat()
        elif t == "assistant_message":
            self.add_chat({"kind": "assistant", "text": ev["content"]})
        elif t == "tool_call":
            self.add_chat({"kind": "tool", "name": ev["name"],
                           "args": ev["args"]})
        elif t == "tool_result":
            self.add_chat({"kind": "tool_result", "text": ev["content"]})
        elif t == "error":
            self.add_chat({"kind": "error", "text": ev["content"]})

    def _last_thinking(self):
        for e in reversed(self.chat_entries):
            if e["kind"] == "thinking":
                return e
        return None

    # ------------------------------------------------------------ actions
    def send(self):
        text = self.input.text.strip()
        if not text or self.busy or not self.session:
            return
        if not self.session.get("model"):
            self.notify("Select a model first (model name in the top bar)",
                        good=False)
            return
        self.input.clear()
        self._hist_idx = None
        self.busy = True
        self.redraw()
        model = self.session["model"]

        def worker():
            try:
                result = run_turn(self.turn_client(), self.mcp, self.session,
                                  model, text)
                for ev in result.events:
                    self.turn_queue.put(("event", ev))
            except Exception as e:
                self.turn_queue.put(("turn_error", str(e)))
            self.turn_queue.put(("turn_done", None))

        threading.Thread(target=worker, daemon=True).start()

    def action_new_session(self):
        if self.session and self.session["messages"]:
            store.save_session(self.session)
        self.session = self.fresh_session()
        self.chat_entries = []
        self.invalidate_chat()
        self.overlay = None
        self.notify("New session started")

    def rename_session(self, name: str):
        if not self.session:
            return
        self.session["name"] = name
        store.save_session(self.session)
        self.overlay = None
        self.redraw()

    def open_session(self, sid: str):
        s = store.load_session(sid)
        if not s:
            return
        if self.session and self.session["messages"]:
            store.save_session(self.session)
        self.session = s
        self.chat_entries = []
        for m in s.get("messages", []):
            if m["role"] == "user" and m.get("content"):
                self.add_chat({"kind": "user", "text": m["content"]})
            elif m["role"] == "assistant" and m.get("content"):
                self.add_chat({"kind": "assistant", "text": m["content"]})
        self.overlay = None
        self.notify(f"Opened “{s['name']}”")

    def toggle_thinking(self):
        th = self._last_thinking()
        if th and not th["active"] and th.get("reasoning"):
            th["expanded"] = not th["expanded"]
            self.invalidate_chat()
            self.redraw()

    # ------------------------------------------------------------ keys
    def on_key(self, name, ch=""):
        if ui._dbg:
            ui._dbg.write("APP on_key %s %r mode=%s overlay=%s\n" %
                          (name, ch, self.mode, self.overlay))
        if self.mode == "login":
            self._key_login(name, ch)
            return
        if self.overlay:
            self._key_overlay(name, ch)
            return
        # global
        if name in ("ctrl+q", "ctrl+c"):
            self.quit()
        elif name == "ctrl+n":
            self.action_new_session()
        elif name == "ctrl+s":
            self.overlay = {"kind": "sessions", "idx": 0,
                            "rows": store.list_sessions()}
        elif name == "ctrl+m":
            self.overlay = {"kind": "providers", "idx": 0, "focus": 0,
                            "fields": [ui.InputBuffer(), ui.InputBuffer(),
                                       ui.InputBuffer()],
                            "rows": list(
                                load_config().get("providers", [])),
                            "sel": 0}
            if ui._dbg:
                ui._dbg.write("OPENED providers, overlay=%s\n" %
                              (self.overlay["kind"],))
        elif name == "f2":
            self.overlay = {"kind": "rename",
                            "field": ui.InputBuffer(self.session["name"])}
        elif name == "ctrl+t":
            self.toggle_thinking()
        elif name == "esc":
            th = self._last_thinking()
            if th and th["expanded"]:
                th["expanded"] = False
                self.invalidate_chat()
        elif name == "pgup":
            self.scroll_from_bottom = min(
                len(self._chat_lines()), self.scroll_from_bottom + 10)
        elif name == "pgdown":
            self.scroll_from_bottom = max(0, self.scroll_from_bottom - 10)
        else:
            self._key_input(name, ch)
        self.redraw()

    def _key_input(self, name, ch):
        inp = self.input
        if name == "enter":
            self.send()
        elif name == "backspace" or name == "ctrl+h":
            inp.backspace()
        elif name == "delete":
            inp.delete()
        elif name == "left":
            inp.left()
        elif name == "right":
            inp.right()
        elif name == "home":
            inp.home()
        elif name == "end":
            inp.end()
        elif name == "up":
            inp.history_prev()
        elif name == "down":
            inp.history_next()
        elif name == "ctrl+w":
            inp.delete_word()
        elif name == "ctrl+u":
            inp.clear_line()
        elif name == "char":
            inp.insert(ch)

    def _key_login(self, name, ch):
        ov = getattr(self, "login", None)
        if ov is None:
            self.login = {"focus": 1, "status": "",
                          "fields": [ui.InputBuffer(self.router_url),
                                     ui.InputBuffer(),
                                     ui.InputBuffer()]}
            ov = self.login
        fields = ov["fields"]
        if name == "tab":
            ov["focus"] = (ov["focus"] + 1) % 5
        elif name == "shift+tab":
            ov["focus"] = (ov["focus"] - 1) % 5
        elif name in ("up",):
            ov["focus"] = (ov["focus"] - 1) % 5
        elif name in ("down",):
            ov["focus"] = (ov["focus"] + 1) % 5
        elif name in ("enter",):
            if ov["focus"] == 3:
                self._do_login(ov, False)
            elif ov["focus"] == 4:
                self._do_login(ov, True)
            else:
                ov["focus"] = min(4, ov["focus"] + 1)
        elif name == "ctrl+q":
            self.quit()
        elif ov["focus"] < 3:
            f = fields[ov["focus"]]
            if name == "backspace":
                f.backspace()
            elif name == "char":
                f.insert(ch)
        self.redraw()

    def _do_login(self, ov, register):
        if ov.get("busy"):
            return
        url = ov["fields"][0].text.strip() or DEFAULT_ROUTER_URL
        email = ov["fields"][1].text.strip()
        password = ov["fields"][2].text
        if not email or not password:
            ov["status"] = err("Fill in email and password")
            return
        ov["busy"] = True
        ov["status"] = ui.style("Connecting…", fg=DIM)

        def work():
            try:
                client = MRouterClient(url, "")
                if register:
                    r = client.register(email, password)
                    if not r.get("ok"):
                        self.turn_queue.put(("login_fail",
                                             r.get("error", "Register failed"),
                                             url))
                        return
                data = client.login(email, password)
                key = data.get("api_key", "")
                if not key:
                    self.turn_queue.put(("login_fail", "No key returned", url))
                    return
                me = MRouterClient(url, key).me()
                self.turn_queue.put(("login_ok", (url, key,
                                                  me.get("email", email))))
            except Exception as e:
                self.turn_queue.put(("login_fail", f"Connection failed: {e}",
                                     url))

        threading.Thread(target=work, daemon=True).start()

    def _key_overlay(self, name, ch):
        ov = self.overlay
        kind = ov["kind"]
        if kind == "model":
            if name == "esc":
                self.overlay = None
            elif name == "up":
                ov["idx"] = max(0, ov["idx"] - 1)
                ov["scroll"] = min(ov["idx"], ov.get("scroll", 0))
            elif name == "down":
                ov["idx"] = min(len(self.models_cache) - 1, ov["idx"] + 1)
                ov["scroll"] = max(ov["idx"] - 11, ov.get("scroll", 0))
            elif name == "enter":
                self._select_model(ov["idx"])
            elif name == "ctrl+m":
                self.overlay = {"kind": "providers", "idx": 0, "focus": 0,
                                "fields": [ui.InputBuffer(), ui.InputBuffer(),
                                           ui.InputBuffer()],
                                "rows": list(load_config().get("providers", [])),
                                "sel": 0}
            return
        if kind == "sessions":
            if name == "esc":
                self.overlay = None
            elif name == "up":
                ov["idx"] = max(0, ov["idx"] - 1)
            elif name == "down":
                ov["idx"] = min(len(ov["rows"]) - 1, ov["idx"] + 1)
            elif name == "enter":
                if ov["rows"]:
                    self.open_session(ov["rows"][ov["idx"]]["id"])
            elif name == "char" and ch == "d":
                if ov["rows"]:
                    sid = ov["rows"][ov["idx"]]["id"]
                    store.delete_session(sid)
                    if self.session and self.session["id"] == sid:
                        self.session = self.fresh_session()
                        self.chat_entries = []
                        self.invalidate_chat()
                    ov["rows"] = store.list_sessions()
                    ov["idx"] = min(ov["idx"], max(0, len(ov["rows"]) - 1))
            elif name == "char" and ch == "r":
                if ov["rows"]:
                    self.overlay = {"kind": "rename",
                                    "field": ui.InputBuffer(
                                        ov["rows"][ov["idx"]]["name"])}
            return
        if kind == "rename":
            f = ov["field"]
            if name == "esc":
                self.overlay = None
            elif name == "enter":
                self.rename_session(f.text.strip() or
                                    self.session["name"])
            elif name == "backspace":
                f.backspace()
            elif name == "char":
                f.insert(ch)
            return
        if kind == "providers":
            if name == "esc":
                self.overlay = None
            elif name == "tab":
                ov["focus"] = (ov["focus"] + 1) % 5
            elif name == "shift+tab":
                ov["focus"] = (ov["focus"] - 1) % 5
            elif name == "down":
                ov["focus"] = (ov["focus"] + 1) % 5
            elif name == "up":
                ov["focus"] = (ov["focus"] - 1) % 5
            elif name == "enter":
                if ov["focus"] == 3:
                    self._provider_add(ov)
                elif ov["focus"] == 4:
                    self.overlay = None
                else:
                    ov["focus"] = min(4, ov["focus"] + 1)
            elif name == "char" and ch == "d" and ov["focus"] == 5:
                pass
            elif ov["focus"] < 3:
                f = ov["fields"][ov["focus"]]
                if name == "backspace":
                    f.backspace()
                elif name == "char":
                    f.insert(ch)
            return

    def _select_model(self, idx):
        if 0 <= idx < len(self.models_cache):
            self.session["model"] = self.models_cache[idx]["id"]
            cfg = load_config()
            cfg["model"] = self.session["model"]
            save_config(cfg)
        self.overlay = None
        self.redraw()

    def _provider_add(self, ov):
        name = ov["fields"][0].text.strip()
        url = ov["fields"][1].text.strip().rstrip("/")
        key = ov["fields"][2].text.strip()
        if not name or not url:
            ov["status"] = err("Need at least name and base URL")
            self.redraw()
            return
        ov["status"] = ui.style("Fetching models…", fg=DIM)
        self.redraw()

        def work():
            try:
                c = CustomProviderClient(name, url, key)
                n = len(c.list_models(force=True))
                cfg = load_config()
                provs = [p for p in cfg.get("providers", [])
                         if p["name"] != name]
                provs.append({"name": name, "base_url": url, "api_key": key})
                cfg["providers"] = provs
                save_config(cfg)
                self.turn_queue.put(("prov_added", (name, n)))
            except Exception as e:
                self.turn_queue.put(("prov_failed", str(e)))

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ clicks
    def on_click(self, tag, x, y):
        if tag == "new_session":
            self.action_new_session()
        elif tag == "model_btn":
            if not self.overlay:
                self.overlay = {"kind": "model", "idx": 0, "scroll": 0}
        elif tag == "think":
            self.toggle_thinking()
        elif tag == "login_btn":
            self._do_login(self.login, False)
        elif tag == "register_btn":
            self._do_login(self.login, True)
        elif tag == "prov_add":
            self._provider_add(self.overlay)
        elif tag == "prov_done":
            self.overlay = None
        elif tag.startswith("model:"):
            mid = tag[6:]
            for i, m in enumerate(self.models_cache):
                if m["id"] == mid:
                    self._select_model(i)
                    break
        elif tag.startswith("session:"):
            self.open_session(tag[8:])
        elif tag.startswith("focus:"):
            self.login["focus"] = int(tag[6:])
        self.redraw()

    def on_wheel(self, delta):
        if self.overlay:
            ov = self.overlay
            if ov["kind"] == "model":
                ov["scroll"] = max(0, min(max(0, len(self.models_cache) - 10),
                                          ov.get("scroll", 0) - delta))
            elif ov["kind"] == "sessions":
                ov["idx"] = max(0, min(len(ov["rows"]) - 1, ov["idx"] + delta))
            return
        total = len(self._chat_lines())
        max_off = max(0, total - (self.rows - 3))
        self.scroll_from_bottom = max(0, min(max_off,
                                             self.scroll_from_bottom + delta))
        self.redraw()

    def on_timer(self, name):
        if name == "tick":
            self.shimmer_idx = (self.shimmer_idx + 1) % len(MAG)
            self._drain()
            if self.mode == "chat":
                th = self._last_thinking()
                if th and th["active"]:
                    self.invalidate_chat()
                if self.notify_text and time.time() > self.notify_until:
                    self.notify_text = ""
            self.redraw()

    # ============================================================ render ==
    def render(self):
        if self.mode == "login":
            return self._render_login()
        rows = []
        rows.append(self._render_topbar())
        chat = self._chat_lines()
        visible = self.rows - 3
        start = max(0, len(chat) - visible - self.scroll_from_bottom)
        shown = chat[start:start + visible]
        rows.extend(shown)
        while len(rows) < self.rows - 2:
            rows.append(("", []))
        if self.overlay:
            return self._render_overlay(rows)
        rows.append(self._render_hintline())
        rows.append(self._render_input())
        return rows

    # --- topbar
    def _render_topbar(self):
        lb = LineBuilder()
        lb.add(" ")
        lb.add("＋ new session ", ui.style("", fg=CYAN), tag="new_session")
        name = (self.session["name"] if self.session else "—")
        lb.add(name + " ", ui.style("", fg=FG), tag="new_session")
        model = self.session.get("model") if self.session else None
        label = model if model else f"{len(self.models_cache)} models"
        mark = ""
        if model and self.health:
            mark = ui.style("✓", fg=GREEN) if self.health.get(model) \
                else ui.style("✗", fg=RED)
        lb.add(mark + " ")
        lb.add(label + " ▾", ui.style("", fg=BLUE), tag="model_btn")
        beta = "beta"
        w = self.cols
        cur = lb.w
        pad = max(1, (w - cur - 4) // 2)
        lb.add(" " * pad + ui.style(beta, fg=DIMMER))
        lb.add(" ")
        return lb.line()

    # --- chat
    def _chat_lines(self):
        entries = self.chat_entries
        key = tuple((e["kind"], e.get("active"), e.get("expanded"),
                     e.get("reasoning", "")) for e in entries)
        if self.chat_cache and self.chat_cache[0] == len(entries) \
                and self.chat_cache[3] == key:
            return self.chat_cache[1]
        lines = []
        for e in entries:
            block, _tags = self._render_entry(e)
            lines.extend(block)
            lines.append(("", []))
        self.chat_cache = (len(entries), lines, [], key)
        return lines

    def _render_entry(self, e):
        width = self.cols - 6
        lines = []
        if e["kind"] == "user":
            lb = LineBuilder()
            lb.add("  ❯ ", ui.style("", fg=CYAN, bold=True))
            body = lb.w
            wrapped = wrap(e["text"], width - 2)
            first = True
            for wl in wrapped:
                if first:
                    lines.append((lb.parts[0] + ui.style(wl, fg=FG), []))
                    first = False
                else:
                    lines.append(("    " + ui.style(wl, fg=FG), []))
        elif e["kind"] == "assistant":
            for plain, kw in ui.md_to_lines(e["text"], {"fg": FG})[:200]:
                for wl in wrap(plain, width) or [""]:
                    lines.append(("  " + ui.style(wl, **kw), []))
        elif e["kind"] == "thinking":
            lines.extend(self._render_thinking(e))
        elif e["kind"] == "tool":
            args = e.get("args", {})
            brief = ""
            for k in ("path", "command"):
                if k in args:
                    brief = str(args[k])[:60]
                    break
            lb = LineBuilder()
            lb.add("  ● ", ui.style("", fg=GREEN))
            lb.add(e["name"], ui.style("", fg=GREEN))
            if brief:
                lb.add(" " + ui.style(brief, fg=DIM))
            lines.append(lb.line())
        elif e["kind"] == "tool_result":
            first = e["text"].replace("\n", " ⏎ ")[:width]
            lines.append(("    " + ui.style(first, fg=DIMMER), []))
        elif e["kind"] == "error":
            for wl in wrap(e["text"], width):
                lines.append(("  " + ui.style("✗ " + wl, fg=RED), []))
        return lines, []

    def _render_thinking(self, e):
        lines = []
        if e["active"]:
            color = MAG[self.shimmer_idx]
            label = "Thinking…"
        else:
            secs = int(e.get("done_at", e.get("started", time.time()))
                       - e["started"])
            label = f"Thought for {max(1, secs)}s"
            if e.get("reasoning"):
                label += " — click to expand" if not e["expanded"] \
                    else " — click to close"
        head = ui.style("✻ ", fg=color if e["active"] else PINK) + \
            ui.style(label, fg=color if e["active"] else DIM)
        if e["expanded"] and e.get("reasoning"):
            lines.append(("  " + head + " " + ui.style("▾", fg=DIMMER),
                          [(0, self.cols, "think")]))
            for plain, kw in ui.md_to_lines(e["reasoning"], {"fg": DIM})[:100]:
                for wl in wrap(plain, self.cols - 8) or [""]:
                    lines.append(("    " + ui.style(wl, fg=DIMMER), []))
        else:
            lines.append(("  " + head, [(0, self.cols, "think")]))
        return lines

    # --- bottom
    def _render_hintline(self):
        lb = LineBuilder()
        if self.notify_text:
            lb.add(" " + self.notify_text)
            return lb.line()
        hints = [("ctrl+s", "sessions"), ("ctrl+m", "providers"),
                 ("ctrl+n", "new"), ("f2", "rename"), ("ctrl+t", "thinking"),
                 ("ctrl+q", "quit")]
        lb.add(" ")
        for i, (k, v) in enumerate(hints):
            lb.add(k, ui.style("", fg=DIMMER))
            lb.add(" " + v + ("   " if i < len(hints) - 1 else " "),
                   ui.style("", fg=DIMMER))
        return lb.line()

    def _render_input(self):
        lb = LineBuilder()
        text, col = self.input.view(self.cols - 6)
        content = " ❯ " + text
        pad = self.cols - ui.visible_width(content) - 1
        bar = ui.style(content, fg="15", bg="22", bold=True) + \
            ui.style(" " * max(0, pad), bg="22")
        lb.add(bar, tag="input")
        self._cursor_cache = (3 + col)
        return lb.line()

    # --- overlays
    def _render_overlay(self, rows):
        kind = self.overlay["kind"]
        if kind == "model":
            return self._render_model_dropdown(rows)
        if kind == "sessions":
            return self._render_box_overlay(rows, "Sessions",
                                            self._sessions_rows(),
                                            self.overlay["idx"],
                                            width=70)
        if kind == "rename":
            return self._render_rename(rows)
        if kind == "providers":
            return self._render_providers(rows)
        return rows

    def _render_model_dropdown(self, rows):
        ov = self.overlay
        items = self.models_cache
        vis = 12
        scroll = ov.get("scroll", 0)
        scroll = max(0, min(scroll, max(0, len(items) - vis)))
        ov["scroll"] = scroll
        box_lines = []
        for i in range(scroll, min(len(items), scroll + vis)):
            m = items[i]
            mark = ""
            if m["id"] in self.health:
                mark = ui.style("✓", fg=GREEN) if self.health[m["id"]] \
                    else ui.style("✗", fg=RED)
            cur = " › " if self.session and m["id"] == self.session.get("model") \
                else "   "
            label = cur + mark + " " + m["id"] + \
                ui.style(f"  ({m.get('provider')})", fg=DIMMER)
            if i == ov["idx"]:
                label = ui.style(label, fg="16", bg="114")
            box_lines.append((label, [(0, 70, "model:" + m["id"])]))
        if not box_lines:
            box_lines.append((ui.style("  loading models…", fg=DIM), []))
        start_x = max(0, self.cols - 72)
        out = list(rows)
        for j, (line, tags) in enumerate(box_lines[:vis]):
            row = 1 + j
            padded = ui.pad_to(line, 70)
            shifted = [(s + start_x, e + start_x, t) for (s, e, t) in tags]
            out[row] = (" " * start_x + ui.style(padded, bg="234"), shifted)
        return out

    def _sessions_rows(self):
        ov = self.overlay
        out = []
        if not ov["rows"]:
            out.append((ui.style("  No sessions yet", fg=DIM), []))
        for i, s in enumerate(ov["rows"]):
            dt = time.strftime("%b %d %H:%M", time.localtime(s["updated"]))
            label = (" › " if i == ov["idx"] else "   ") + \
                ui.style(s["name"], fg=FG) + \
                ui.style(f"  · {s['messages']} msgs · {s['model'][:24]} · {dt}",
                         fg=DIMMER)
            if i == ov["idx"]:
                label = ui.style(label, fg="16", bg="114")
            out.append((label, [(0, self.cols, "session:" + s["id"])]))
        return out

    def _box_overlay(self, rows, lines, top=3, width=70):
        x = max(1, (self.cols - width) // 2)
        n = len(lines)
        out = list(rows)
        out[top - 1] = (" " * x + ui.style("╭" + "─" * (width - 2) + "╮",
                                           fg="240"), [])
        for j, (line, tags) in enumerate(lines):
            row = top + j
            shifted = [(max(0, s + x + 1), min(self.cols, e + x + 1), t)
                       for (s, e, t) in tags]
            out[row] = (" " * x + ui.style("│", fg="240") +
                        ui.pad_to(line, width - 2) +
                        ui.style("│", fg="240"), shifted)
        out[top + n] = (" " * x + ui.style("╰" + "─" * (width - 2) + "╯",
                                           fg="240"), [])
        return out

    def _render_box_overlay(self, rows, title, lines, idx, width=70):
        ov = self.overlay
        head = [(ui.style(" " + title + ui.style(
            "  enter=open · r=rename · d=delete · esc=close", fg=DIMMER),
            fg=FG, bold=True), [])]
        return self._box_overlay(rows, head + lines, top=3, width=width)

    def _render_rename(self, rows):
        f = self.overlay["field"]
        lines = [(ui.style(" Session name", fg=FG, bold=True), []),
                 ("", []),
                 ("   " + ui.style(f.text, fg=FG, bg="236") +
                  ui.style(" ", bg="236"), [])]
        out = self._box_overlay(rows, lines, top=self.rows // 2 - 2, width=50)
        return out

    def _render_providers(self, rows):
        ov = self.overlay
        status = getattr(ov, "status", "") if False else ov.get("status", "")
        lines = []
        lines.append((ui.style(" Providers", fg=FG, bold=True) +
                      ui.style("  · connect your own API keys (OpenAI-compatible)",
                               fg=DIMMER), []))
        lines.append(("  " + ui.style("mrouter (default, key managed by login)",
                                      fg=DIM), []))
        for p in ov["rows"]:
            key_shown = (p.get("api_key") or "")[:6] + "…"
            lines.append(("  " + ui.style(f"{p['name']} — {p['base_url']} [{key_shown}]",
                                          fg=DIM), []))
        lines.append(("  " + status, []))
        labels = ["Name", "Base URL", "API key"]
        for i in range(3):
            f = ov["fields"][i]
            focus = ov["focus"] == i
            text = f.text if i != 2 else ("*" * len(f.text))
            cursor = ui.style(" ", bg="114") if focus else ""
            lines.append(("  " + ui.style(f"{labels[i]:<9}", fg=DIMMER) +
                          ui.style(text, fg=FG, bg="236" if not focus else "236") +
                          cursor, []))
        lines.append(("  " + self._btn("Add provider", "prov_add", ov["focus"] == 3)
                      + " " + self._btn("Done", "prov_done", ov["focus"] == 4)
                      + ui.style("   esc=close", fg=DIMMER), []))
        return self._box_overlay(rows, lines, top=max(2, self.rows // 2 - 8),
                                 width=72)

    def _btn(self, label, tag, focused):
        bg = "117" if focused else "236"
        fg = "16" if focused else FG
        return ui.style(f"[ {label} ]", fg=fg, bg=bg)

    # --- login
    def _login_box_top(self):
        return self.rows // 2 - 5

    def _render_login(self):
        ov = getattr(self, "login", None)
        if ov is None:
            self.login = {"focus": 1, "status": "",
                          "fields": [ui.InputBuffer(self.router_url),
                                     ui.InputBuffer(),
                                     ui.InputBuffer()]}
            ov = self.login
        rows = [("", [])] * self.rows
        lines = []
        lines.append((ui.style(" mcode", fg=BLUE, bold=True) +
                      ui.style(" — powered by mrouter", fg=DIMMER), []))
        lines.append((ui.style(" Log in to get your API key (or register)",
                               fg=DIM), []))
        lines.append(("", []))
        labels = ["Router URL", "Email", "Password"]
        for i in range(3):
            f = ov["fields"][i]
            focus = ov["focus"] == i
            text = f.text if i != 2 else ("*" * len(f.text))
            cursor = ui.style(" ", bg="114") if focus else ""
            tag = f"focus:{i}"
            lines.append(("  " + ui.style(f"{labels[i]:<11}", fg=DIMMER) +
                          ui.style(text, fg=FG, bg="236") + cursor,
                          [(0, self.cols, tag)]))
        lines.append(("  " + self._btn("Log in", "login_btn", ov["focus"] == 3)
                      + " " + self._btn("Register", "register_btn",
                                        ov["focus"] == 4), []))
        lines.append(("  " + (ov.get("status") or ""), []))
        out = self._box_overlay(list(rows), lines,
                                top=self.rows // 2 - 5, width=64)
        while len(out) < self.rows:
            out.append(("", []))
        return out

    def cursor_pos(self):  # noqa: F811 — login/rename/providers cursors
        if self.mode == "login":
            ov = getattr(self, "login", None)
            if not ov:
                return None
            if ov["focus"] < 3:
                f = ov["fields"][ov["focus"]]
                row = self.rows // 2 - 5 + 3 + ov["focus"]
                col = 13 + ui.visible_width(f.text[:f.cursor]) + 1
                return (row, min(col, self.cols - 2))
            return None
        if self.overlay:
            kind = self.overlay["kind"]
            if kind == "rename":
                f = self.overlay["field"]
                row = self.rows // 2 - 2 + 2
                col = 3 + ui.visible_width(f.text[:f.cursor]) + 1
                return (row, min(col, self.cols - 2))
            if kind == "providers" and self.overlay["focus"] < 3:
                f = self.overlay["fields"][self.overlay["focus"]]
                n = len(self.overlay["rows"])
                row = max(2, self.rows // 2 - 8) + 3 + n + self.overlay["focus"]
                col = 11 + ui.visible_width(f.text[:f.cursor]) + 1
                return (row, min(col, self.cols - 2))
            return None
        if self.mode == "chat":
            _, col = self.input.view(self.cols - 6)
            return (self.rows - 1, 3 + col)
        return None


def main():
    global _app
    _app = MCodeApp()
    app = _app
    sys_exit = app.run()
    raise SystemExit(sys_exit)
