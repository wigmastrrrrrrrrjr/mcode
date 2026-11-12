import json
import os
import subprocess
import threading

from .storage import load_mcp_servers


class MCPServer:
    """Minimal MCP client over stdio JSON-RPC 2.0."""

    def __init__(self, name: str, command: str, args: list, env: dict | None):
        self.name = name
        self.command = command
        self.args = args or []
        self.env = env or {}
        self.proc: subprocess.Popen | None = None
        self._next_id = 0
        self.tools: list[dict] = []
        self.error: str | None = None

    def start(self):
        try:
            full_env = {**os.environ, **self.env}
            self.proc = subprocess.Popen(
                [self.command, *self.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                env=full_env,
            )
            self._request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "mcode", "version": "0.1.0"},
            })
            self._notify("notifications/initialized", {})
            result = self._request("tools/list", {})
            self.tools = result.get("tools", [])
        except Exception as e:
            self.error = str(e)

    def _readline(self):
        if self.proc and self.proc.stdout:
            line = self.proc.stdout.readline()
            if not line:
                raise ConnectionError(f"MCP server '{self.name}' closed its output")
            return line

    def _request(self, method: str, params: dict) -> dict:
        if not self.proc:
            raise ConnectionError(f"MCP server '{self.name}' not running")
        self._next_id += 1
        req = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        deadline_threads = 0
        while deadline_threads < 300:  # ~30s
            line = self._readline().strip()
            if not line:
                deadline_threads += 1
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == self._next_id:
                if "error" in msg:
                    raise RuntimeError(f"MCP error: {msg['error']}")
                return msg.get("result", {})
            deadline_threads += 1
        raise TimeoutError(f"MCP server '{self.name}' did not respond to {method}")

    def _notify(self, method: str, params: dict):
        req = {"jsonrpc": "2.0", "method": method, "params": params}
        if self.proc and self.proc.stdin:
            self.proc.stdin.write(json.dumps(req) + "\n")
            self.proc.stdin.flush()

    def call(self, tool_name: str, args: dict) -> str:
        result = self._request("tools/call", {"name": tool_name, "arguments": args})
        content = result.get("content", [])
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
        text = "\n".join(parts) if parts else json.dumps(result)
        if result.get("isError"):
            text = f"Error: {text}"
        return text[:30_000]

    def stop(self):
        if self.proc:
            try:
                self.proc.stdin.close()
                self.proc.terminate()
            except Exception:
                pass
            self.proc = None


class MCPManager:
    def __init__(self):
        self.servers: dict[str, MCPServer] = {}

    def start_all(self):
        for name, cfg in load_mcp_servers().items():
            cmd = cfg.get("command", "")
            if not cmd:
                continue
            server = MCPServer(name, cmd, cfg.get("args", []), cfg.get("env"))
            server.start()
            self.servers[name] = server

    def tool_schemas(self) -> list[dict]:
        """Return OpenAI-format function schemas prefixed mcp_<server>_<tool>."""
        schemas = []
        for server in self.servers.values():
            if server.error or not server.tools:
                continue
            for tool in server.tools:
                schemas.append({
                    "type": "function",
                    "function": {
                        "name": f"mcp_{server.name}_{tool.get('name', 'tool')}",
                        "description": f"[MCP:{server.name}] {tool.get('description', '')}",
                        "parameters": tool.get("inputSchema", {"type": "object", "properties": {}}),
                    },
                })
        return schemas

    def dispatch(self, prefixed_name: str, args: dict) -> str | None:
        if not prefixed_name.startswith("mcp_"):
            return None
        rest = prefixed_name[len("mcp_"):]
        for server in self.servers.values():
            prefix = f"{server.name}_"
            if rest.startswith(prefix):
                return server.call(rest[len(prefix):], args)
        return f"Error: no MCP server found for tool {prefixed_name}"

    def status_line(self) -> str:
        if not self.servers:
            return ""
        parts = []
        for name, s in self.servers.items():
            if s.error:
                parts.append(f"{name}(error)")
            else:
                parts.append(f"{name}({len(s.tools)} tools)")
        return " | ".join(parts)

    def stop_all(self):
        for s in self.servers.values():
            s.stop()
