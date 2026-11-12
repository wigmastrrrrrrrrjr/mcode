import functools
import json
import time
import queue as _queue

from textual.app import App, ComposeResult
from textual import events, on
from textual.widgets import (Header, Footer, Input, Button, Static,
                            OptionList, ListView, ListItem, Label, Markdown)
from textual.widgets.option_list import Option
from textual.containers import Vertical, Horizontal, VerticalScroll, Container
from textual.screen import ModalScreen

from .api import (MRouterClient, CustomProviderClient, DispatchClient,
                  ApiError, Unauthorized)
from . import health as mhealth
from .agent import run_turn
from .mcp import MCPManager
from . import sessions as store
from .storage import (ensure_dirs, load_config, save_config,
                      DEFAULT_ROUTER_URL, CONFIG_PATH, MCP_PATH)


def ok(msg: str) -> str:
    return f"[green]{msg}[/]"


def err(msg: str) -> str:
    return f"[red]{msg}[/]"


# ------------------------------------------------------------------ login ---

class LoginScreen(ModalScreen):
    BINDINGS = [("escape", "app.quit", "Quit")]

    def __init__(self, router_url: str = ""):
        super().__init__()
        self.router_url = router_url or DEFAULT_ROUTER_URL
        self.busy = False

    def compose(self) -> ComposeResult:
        with Container(id="login-box"):
            yield Static("[bold blue]mcode[/] [dim]— powered by mrouter[/]")
            yield Static("Log in to generate your API key (or register a new account)", id="login-sub")
            yield Label("Router URL", classes="field-label")
            yield Input(value=self.router_url, id="login-url")
            yield Label("Email", classes="field-label")
            yield Input(placeholder="you@example.com", id="login-email")
            yield Label("Password", classes="field-label")
            yield Input(password=True, placeholder="min 6 chars", id="login-password")
            with Horizontal(id="login-buttons"):
                yield Button("Log in", variant="primary", id="btn-login")
                yield Button("Register", id="btn-register")
            yield Static("", id="login-status")

    @on(Button.Pressed, "#btn-login")
    @on(Button.Pressed, "#btn-register")
    def do_auth(self, event: Button.Pressed) -> None:
        if self.busy:
            return
        url = self.query_one("#login-url", Input).value.strip()
        email = self.query_one("#login-email", Input).value.strip()
        password = self.query_one("#login-password", Input).value
        if not email or not password:
            self.query_one("#login-status", Static).update(err("Fill in email and password"))
            return
        self.busy = True
        self.query_one("#login-status", Static).update("[dim]Connecting...[/]")
        register = event.button.id == "btn-register"
        self.app.run_worker(functools.partial(self._auth_worker, url, email, password, register),
                            thread=True, exclusive=True)

    def _auth_worker(self, url: str, email: str, password: str, register: bool):
        import httpx
        try:
            probe = httpx.Client(timeout=20)
            if register:
                r = probe.post(f"{url.rstrip('/')}/v1/auth/register",
                               json={"email": email, "password": password})
                data = r.json()
                if not data.get("ok"):
                    self.app.call_from_thread(self._auth_fail, data.get("error", "Register failed"))
                    return
            client = MRouterClient(url, "")
            data = client.login(email, password)
            key = data.get("api_key", "")
            if not key:
                self.app.call_from_thread(self._auth_fail, "No key returned")
                return
            me = MRouterClient(url, key).me()
            self.app.call_from_thread(self._auth_ok, url, key, me.get("email", email))
        except Unauthorized as e:
            self.app.call_from_thread(self._auth_fail, str(e))
        except Exception as e:
            self.app.call_from_thread(self._auth_fail, f"Connection failed: {e}")

    def _auth_fail(self, message: str):
        self.busy = False
        self.query_one("#login-status", Static).update(err(message))

    def _auth_ok(self, url: str, key: str, email: str):
        cfg = load_config()
        cfg["router_url"] = url
        cfg["api_key"] = key
        cfg["email"] = email
        save_config(cfg)
        self.app.dismiss_login(email)


# ------------------------------------------------------------- rename modal ---

class RenameScreen(ModalScreen):
    def __init__(self, current: str):
        super().__init__()
        self.current = current

    def compose(self) -> ComposeResult:
        with Container(id="rename-box"):
            yield Label("Session name", classes="field-label")
            yield Input(value=self.current, id="rename-input")

    def on_mount(self):
        self.query_one("#rename-input", Input).focus()

    @on(Input.Submitted, "#rename-input")
    def rename(self, event: Input.Submitted) -> None:
        self.dismiss()
        self.app.rename_session(event.value.strip() or self.current)


# ------------------------------------------------------------- sessions UI ---

class SessionsScreen(ModalScreen):
    BINDINGS = [
        ("escape", "dismiss", "Close"),
        ("d", "delete_selected", "Delete"),
        ("r", "rename_selected", "Rename"),
    ]

    def compose(self) -> ComposeResult:
        with Container(id="sessions-box"):
            yield Static("[bold]Sessions[/] [dim](saved on this device · enter=open · r=rename · d=delete · esc=close)[/]",
                         id="sessions-title")
            yield ListView(id="sessions-list")

    def on_mount(self):
        self.refresh_list()
        self.query_one("#sessions-list", ListView).focus()

    def _placeholder(self, lv):
        lv.append(ListItem(Label("[dim]…[/]")))

    def refresh_list(self):
        lv = self.query_one("#sessions-list", ListView)
        lv.clear()
        self._rows = store.list_sessions()
        if not self._rows:
            lv.append(ListItem(Label("[dim]No sessions yet — send your first message[/]")))
        for s in self._rows:
            dt = time.strftime("%b %d %H:%M", time.localtime(s["updated"]))
            lv.append(ListItem(Label(
                f"[bold]{s['name']}[/]  [dim]· {s['messages']} msgs · {s['model'] or 'no model'} · {dt}[/]")))

    @on(ListView.Selected, "#sessions-list")
    def open_selected(self, event: ListView.Selected) -> None:
        if self._rows:
            self.app.open_session(self._rows[event.index]["id"])

    def action_delete_selected(self):
        lv = self.query_one("#sessions-list", ListView)
        if 0 <= lv.index < len(self._rows):
            store.delete_session(self._rows[lv.index]["id"])
            if self._rows[lv.index]["id"] == self.app.session_id:
                self.app.reset_session()
            self.refresh_list()

    def action_rename_selected(self):
        lv = self.query_one("#sessions-list", ListView)
        if 0 <= lv.index < len(self._rows):
            s = self._rows[lv.index]
            self.app.push_screen(RenameScreen(s["name"]), None)


# ------------------------------------------------------------ providers UI ---

class ProvidersScreen(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        with Container(id="providers-box"):
            yield Static("[bold]Providers[/] [dim]· connect your own API keys (OpenAI-compatible) — or manage MCP servers[/]")
            ol = OptionList(id="provider-list")
            ol.add_option(Option("[dim]…[/]", id="_"))
            yield ol
            yield Label("Name", classes="field-label")
            yield Input(placeholder="my-provider", id="prov-name")
            yield Label("Base URL", classes="field-label")
            yield Input(placeholder="https://api.example.com/v1", id="prov-url")
            yield Label("API key", classes="field-label")
            yield Input(password=True, placeholder="sk-...", id="prov-key")
            with Horizontal():
                yield Button("Add provider", variant="primary", id="btn-add-provider")
                yield Button("Delete selected", id="btn-del-provider")
                yield Button("Done", id="btn-prov-done")
            yield Static("", id="prov-status")
            yield Static(f"[dim]MCP servers: {MCP_PATH}[/]")

    def on_mount(self):
        self.refresh_providers()

    def refresh_providers(self):
        ol = self.query_one("#provider-list", OptionList)
        ol.clear_options()
        ol.add_option(Option("mrouter (default, key managed by login)", id="mrouter"))
        cfg = load_config()
        for p in cfg.get("providers", []):
            key_shown = (p.get("api_key") or "")[:6] + "..."
            ol.add_option(Option(f"{p['name']} — {p['base_url']} [{key_shown}]",
                                 id=f"prov:{p['name']}"))

    @on(Button.Pressed, "#btn-prov-done")
    def done(self):
        self.dismiss()

    @on(Button.Pressed, "#btn-del-provider")
    def delete_provider(self):
        cfg = load_config()
        provs = cfg.get("providers", [])
        if provs:
            provs.pop(0)
            cfg["providers"] = provs
            save_config(cfg)
            self.refresh_providers()
            self.query_one("#prov-status", Static).update(ok("Deleted first custom provider"))

    @on(Button.Pressed, "#btn-add-provider")
    def add_provider(self):
        name = self.query_one("#prov-name", Input).value.strip()
        url = self.query_one("#prov-url", Input).value.strip().rstrip("/")
        key = self.query_one("#prov-key", Input).value.strip()
        status = self.query_one("#prov-status", Static)
        if not name or not url:
            status.update(err("Need at least name and base URL"))
            return
        status.update("[dim]Fetching models...[/]")
        self.app.run_worker(functools.partial(self._add_worker, name, url, key), thread=True, exclusive=True)

    def _add_worker(self, name: str, url: str, key: str):
        try:
            client = CustomProviderClient(name, url, key)
            models = client.list_models(force=True)
            cfg = load_config()
            providers = [p for p in cfg.get("providers", []) if p["name"] != name]
            providers.append({"name": name, "base_url": url, "api_key": key})
            cfg["providers"] = providers
            save_config(cfg)
            self.app.invalidate_model_cache()
            self.app.call_from_thread(self._add_ok, name, len(models))
        except Exception as e:
            self.app.call_from_thread(self._add_fail, f"{e}")

    def _add_ok(self, name: str, count: int):
        self.refresh_providers()
        self.query_one("#prov-status", Static).update(ok(f"Added {name} ({count} models)"))

    def _add_fail(self, message: str):
        self.query_one("#prov-status", Static).update(err(message))


# ------------------------------------------------------- thinking widget ---

def _shimmer(text: str, offset: float, active: bool) -> str:
    if not active:
        return f"[dim]✻ {text}[/]"
    base = "  38  76 166 208 255 208 166 76"
    n = len(text)
    out = []
    for i, ch in enumerate(text):
        idx = int((i + offset) * 2.2) % len(base.split())
        g = base.split()[idx]
        out.append(f"[rgb(38,{g},255)]{ch}[/]")
    return "✻ " + "".join(out)


class ThinkingWidget(Static):
    """Shimmering 'Thinking...' line; click to expand/collapse the model's reasoning."""

    def __init__(self):
        super().__init__("✻ Thinking…")
        self.active = True
        self.expanded = False
        self.reasoning = ""
        self.started = time.time()
        self._offset = 0.0

    def on_mount(self):
        self.set_interval(0.08, self._tick)

    def _tick(self):
        self._offset += 1
        if self.active or self.expanded:
            self._render()

    def finish(self, reasoning: str = ""):
        self.active = False
        if reasoning:
            self.reasoning = reasoning

    def _render(self):
        elapsed = int(time.time() - self.started)
        head = _shimmer("Thinking", self._offset, self.active)
        if not self.active:
            head = f"[dim]✻ Thought for {elapsed}s"
            head += " — click to expand[/]" if self.reasoning else "[/]"
            if self.reasoning:
                head = f"✻ Thought for {elapsed}s [dim]— click to expand[/]"
        if self.expanded and self.reasoning:
            text = head + "\n" + self.reasoning
        else:
            text = head
        self.update(text)

    def on_click(self) -> None:
        if self.reasoning:
            self.expanded = not self.expanded
            self._render()


# ------------------------------------------------------------------ app ---

class MCodeApp(App):
    TITLE = "mcode"
    CSS = """
    Screen { background: black; }
    #topbar { height: 3; dock: top; background: black; }
    #beta { width: 8; text-align: center; color: #9e9e9e; padding-top: 1; }
    #session-name { color: #cfcfcf; margin-left: 2; padding-top: 1; width: 1fr; }
    #chat { background: black; padding: 1 2; }
    .msg-user { text-align: right; color: #9ecbff; margin-bottom: 1; }
    .msg-you { text-align: right; color: #4d6a8f; margin-bottom: 0; }
    .msg-assistant { color: #e8e8e8; margin-bottom: 1; }
    .msg-label { color: #666; margin-bottom: 0; }
    .tool-line { color: #7ec8a0; margin-left: 2; }
    .tool-out { color: #888; margin-left: 4; margin-bottom: 1; }
    .sys-line { color: #666; margin-bottom: 1; }
    #input { dock: bottom; height: 3; background: #0d47a1; border: heavy #42a5f5;
            color: white; padding: 0 1; }
    #model-dropdown { display: none; dock: top; margin-top: 3; height: 12;
                      layer: overlay; background: #101018; border: solid #4d6a8f; }
    #model-dropdown.visible { display: block; }
    #statusbar { dock: bottom; height: 1; color: #555; }
    #login-box, #sessions-box, #providers-box, #rename-box {
        background: #0a0a12; border: solid #2a4a7a; padding: 1 2; width: 60; margin: 4 0 0 20;
    }
    #login-sub { color: #888; margin-bottom: 1; }
    .field-label { color: #888; margin-top: 1; }
    #login-buttons { height: 3; margin-top: 1; }
    #login-status, #prov-status { margin-top: 1; min-height: 1; }
    #sessions-title, #providers-title { margin-bottom: 1; }
    #sessions-list { height: 12; }
    #provider-list { height: 8; margin-bottom: 1; }
    """
    BINDINGS = [
        ("ctrl+s", "sessions", "Sessions"),
        ("ctrl+m", "providers", "Providers"),
        ("ctrl+n", "new_session", "New session"),
        ("f2", "rename_session", "Rename session"),
        ("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self):
        super().__init__()
        ensure_dirs()
        cfg = load_config()
        self.router_url = cfg.get("router_url", DEFAULT_ROUTER_URL)
        self.api_key = cfg.get("api_key", "")
        self.model_id = cfg.get("model", "")
        self.models_cache: list[dict] = []
        self.health: dict[str, bool] = {}
        self.session: dict | None = None
        self._turn_queue: _queue.Queue = _queue.Queue()
        self.mcp = MCPManager()

    # ----- session helpers
    def fresh_session(self) -> dict:
        return {"id": store.new_session_id(), "name": "Untitled",
                "model": self.model_id, "messages": []}

    def reset_session(self):
        self.session = self.fresh_session()
        self.query_one("#chat", VerticalScroll).remove_children()
        self.refresh_name()

    def refresh_name(self):
        name = self.session["name"]
        model = self.session.get("model") or "no model selected"
        self.query_one("#session-name", Static).update(
            f"[bold]{name}[/]  [dim]· {model}[/]")

    # ----- login flow
    def on_mount(self):
        self.set_interval(0.15, self._drain_queue)
        if self.api_key:
            self._enter_main()
        else:
            self.push_screen(LoginScreen(self.router_url))

    def dismiss_login(self, email: str):
        for screen in list(self.screen_stack):
            if isinstance(screen, LoginScreen):
                self.pop_screen()
        self._enter_main()
        self.notify(f"Logged in as {email}", title="mcode")

    def _enter_main(self):
        cfg = load_config()
        self.model_id = cfg.get("model", "")
        self.session = self.fresh_session()
        self.refresh_name()
        self.mcp.start_all()
        status = self.mcp.status_line()
        sb = self.query_one("#statusbar", Static)
        sb.update(f"[dim]{self.router_url}" +
                  (f" · MCP: {status}[/]" if status else " · add MCP servers via ~/.mcode/mcp.json[/]"))
        self.refresh_models()

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Button("＋ New session", id="new-session", variant="primary")
            yield Static("[bold]Untitled[/]  [dim]· no model selected[/]", id="session-name")
            yield Static("beta", id="beta")
            yield Button("no model ▾", id="model-btn")
        yield VerticalScroll(id="chat")
        ol = OptionList(id="model-dropdown")
        ol.add_option(Option("[dim]Loading models…[/]", id="_"))
        yield ol
        yield Static("", id="statusbar")
        yield Input(placeholder="Type your prompt… (enter to send)", id="input")
        yield Footer()

    # ----- models
    def refresh_models(self, worker: bool = False):
        clients = self.all_clients()
        self.run_worker(functools.partial(self._models_worker, clients), thread=True, exclusive=False)

    def all_clients(self) -> dict:
        clients = {"mrouter": MRouterClient(self.router_url, self.api_key)}
        for p in load_config().get("providers", []):
            clients[p["name"]] = CustomProviderClient(p["name"], p["base_url"], p["api_key"])
        return clients

    def invalidate_model_cache(self):
        self.models_cache = []

    def _models_worker(self, clients: dict):
        seen = {}
        for name, c in clients.items():
            try:
                models = c.list_models(force=True)
                seen[name] = models
            except Exception:
                seen[name] = []
        self.app.call_from_thread(self._models_done, seen)

    def _models_done(self, seen: dict):
        self.models_cache = []
        for prov, models in seen.items():
            for m in models:
                if prov != "mrouter":
                    m = {**m, "id": f"custom:{prov}:{m['id']}"}
                else:
                    m = {**m}
                m["provider"] = prov
                self.models_cache.append(m)
        self._update_model_button()

        # apply cached health, re-probe if stale
        hdata = mhealth.load_health()
        self.health = hdata.get("results", {})
        self._apply_health()
        if mhealth.is_stale(hdata) and self.models_cache:
            self._health_status("[dim]Checking which models work…[/]")
            self.run_worker(
                functools.partial(self._health_worker, self.models_cache),
                thread=True, exclusive=False)

    def _apply_health(self):
        if not self.health:
            return
        working = [m for m in self.models_cache if self.health.get(m["id"])]
        dead = [m for m in self.models_cache if m["id"] in self.health and not self.health.get(m["id"])]
        unknown = [m for m in self.models_cache if m["id"] not in self.health]
        self.models_cache = working + unknown + dead
        if self.session and (not self.session.get("model") or not self._model_working(self.session["model"])):
            if working:
                self.session["model"] = working[0]["id"]
                self.refresh_name()
                cfg = load_config()
                cfg["model"] = self.session["model"]
                save_config(cfg)

    def _model_working(self, mid: str) -> bool:
        return self.health.get(mid, True) is True

    def _health_worker(self, models: list):
        results = mhealth.probe(models, self.all_clients())
        data = {"checked_at": time.time(), "results": results}
        mhealth.save_health(data)
        self.app.call_from_thread(self._health_done, results)

    def _health_done(self, results: dict):
        self.health = results
        self._apply_health()
        n_ok = sum(1 for v in results.values() if v)
        self._health_status(
            ok(f"Model check done: {n_ok}/{len(results)} models working"))
        self._update_model_button()

    def _health_status(self, text: str):
        try:
            self.query_one("#statusbar", Static).update(text)
        except Exception:
            pass

    def _update_model_button(self):
        btn = self.query_one("#model-btn", Button)
        current = self.session.get("model") if self.session else None
        label = current if current else (f"{len(self.models_cache)} models")
        btn.label = f"{label} ▾"

    @on(Button.Pressed, "#model-btn")
    def toggle_model_dropdown(self):
        dd = self.query_one("#model-dropdown", OptionList)
        if dd.has_class("visible"):
            dd.remove_class("visible")
            return
        dd.clear_options()
        if not self.models_cache:
            dd.add_option(Option("[dim]No models found[/]", id="_"))
        for m in self.models_cache:
            mid = m["id"]
            if mid not in self.health:
                mark = "[dim]·[/]"
            elif self.health[mid]:
                mark = "[green]✓[/]"
            else:
                mark = "[red]✗[/] [dim](dead)[/]"
            label = f"{mark} {mid}  [dim]({m.get('provider', '')})[/]"
            dd.add_option(Option(label, id=mid))
        dd.add_class("visible")
        dd.focus()

    @on(OptionList.OptionSelected, "#model-dropdown")
    def model_selected(self, event: OptionList.OptionSelected):
        self.query_one("#model-dropdown", OptionList).remove_class("visible")
        if event.option.id and event.option.id != "_":
            self.session["model"] = str(event.option.id)
            cfg = load_config()
            cfg["model"] = self.session["model"]
            save_config(cfg)
            self.refresh_name()

    # ----- new session / rename
    @on(Button.Pressed, "#new-session")
    def action_new_session(self):
        if self.session and self.session["messages"]:
            store.save_session(self.session)
        self.reset_session()
        self.notify("New session started", title="mcode")

    def action_rename_session(self):
        self.push_screen(RenameScreen(self.session["name"]), None)

    def rename_session(self, new_name: str):
        self.session["name"] = new_name
        store.save_session(self.session)
        self.refresh_name()

    # ----- sessions
    def action_sessions(self):
        self.push_screen(SessionsScreen(), None)

    def open_session(self, sid: str):
        s = store.load_session(sid)
        if not s:
            return
        if self.session and self.session["messages"]:
            store.save_session(self.session)
        self.session = s
        self.model_id = s.get("model", self.model_id)
        chat = self.query_one("#chat", VerticalScroll)
        chat.remove_children()
        for m in s["messages"]:
            if m["role"] == "user":
                self._add_user_msg(m["content"])
            elif m["role"] == "assistant" and m.get("content"):
                self._add_assistant_msg(m["content"])
        self.refresh_name()
        self.pop_screen()
        self.notify(f"Opened “{s['name']}”", title="mcode")

    # ----- providers (ctrl+m)
    def action_providers(self):
        dd = self.query_one("#model-dropdown", OptionList)
        dd.remove_class("visible")
        self.push_screen(ProvidersScreen(), None)

    # ----- input & turn loop
    @on(Input.Submitted, "#input")
    def send(self, event: Input.Submitted):
        text = event.value.strip()
        if not text or not self.session:
            return
        self.query_one("#input", Input).value = ""
        if not self.session.get("model"):
            self.notify("Select a model first (button top-right)", severity="error")
            return
        self.run_worker(functools.partial(self._turn_worker, text), thread=True, exclusive=True)

    def _turn_worker(self, text: str):
        try:
            result = run_turn(self.turn_client(), self.mcp, self.session,
                              self.session["model"], text)
            for ev in result.events:
                self._turn_queue.put(ev)
        except Exception as e:
            self._turn_queue.put({"type": "error", "content": str(e)})
        self._turn_queue.put({"type": "__done__"})

    def turn_client(self):
        return DispatchClient(self.all_clients())

    def _drain_queue(self):
        try:
            while True:
                ev = self._turn_queue.get_nowait()
                if ev["type"] == "__done__":
                    store.save_session(self.session)
                    self._update_model_button()
                    self.query_one("#chat", VerticalScroll).scroll_end()
                    continue
                self._handle_event(ev)
        except _queue.Empty:
            pass

    def _handle_event(self, ev: dict):
        chat = self.query_one("#chat", VerticalScroll)
        t = ev["type"]
        if t == "user_message":
            self._add_user_msg(ev["content"])
        elif t == "thinking":
            chat.mount(ThinkingWidget())
            chat.scroll_end()
        elif t == "reasoning":
            tws = [w for w in chat.children if isinstance(w, ThinkingWidget)]
            if tws:
                tws[-1].reasoning += ("" if not tws[-1].reasoning else "\n") + ev["content"]
        elif t == "thinking_done":
            tws = [w for w in chat.children if isinstance(w, ThinkingWidget)]
            if tws:
                tws[-1].finish()
        elif t == "assistant_message":
            self._add_assistant_msg(ev["content"])
        elif t == "tool_call":
            chat.mount(Static(f"› {ev['name']} {ev['args']}", classes="tool-line"))
            chat.scroll_end()
        elif t == "tool_result":
            out = ev["content"].replace("\n", " ⏎ ")[:200]
            chat.mount(Static(f"[dim]{out}[/]", classes="tool-out"))
            chat.scroll_end()
        elif t == "error":
            chat.mount(Static(err(f"✗ {ev['content']}"), classes="sys-line"))
            chat.scroll_end()
        store.save_session(self.session)

    def _add_user_msg(self, content: str):
        chat = self.query_one("#chat", VerticalScroll)
        label = Static("you ›", classes="msg-you")
        msg = Static(content.replace("[", "\\["), classes="msg-user")
        chat.mount(label)
        chat.mount(msg)
        chat.scroll_end()

    def _add_assistant_msg(self, content: str):
        chat = self.query_one("#chat", VerticalScroll)
        label = Static("mcode", classes="msg-label")
        msg = Static(content.replace("[", "\\["), classes="msg-assistant")
        chat.mount(label)
        chat.mount(msg)
        chat.scroll_end()


def main():
    MCodeApp().run()
