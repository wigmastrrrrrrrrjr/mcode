# mcode

A terminal coding IDE powered by the [mrouter](https://mrouter.csomeone301.workers.dev/) API.

## Features

- **Login on first start** — mcode registers/logs into the mrouter worker and receives a personal API key (stored at `~/.mcode/config.json`).
- **Model picker** — click the model button top-right for a drop-down of every available model. Press `Ctrl+M` (while it's open) to open Providers and connect your own API keys from any OpenAI-compatible API.
- **Sessions** — saved on your device in `~/.mcode/sessions/`. `Ctrl+S` lists them (open / rename / delete), `Ctrl+N` or the **New session** button starts a fresh one, `F2` renames the current one.
- **Model health check** — on start mcode probes every model with a tiny test request (cached for 6h in `~/.mcode/model_health.json`). The dropdown shows `✓` working models first and marks dead ones `✗ (dead)`, and mcode defaults to the first working model.
- **Thinking** — a shimmering "Thinking" line while the model works; click it to expand the model's reasoning.
- **Tools** — the model can `read` files/dirs, `write` files, and `execute` shell commands.
- **MCP servers** — add [MCP](https://modelcontextprotocol.io) servers (opencode-style config) in `~/.mcode/mcp.json`:

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/some/dir"]
    }
  }
}
```

## Install

```bash
pip install .
mcode
```

## Keys

| Key | Action |
| --- | --- |
| `ctrl+n` | New session |
| `ctrl+s` | Sessions list |
| `f2` | Rename session |
| `ctrl+m` | Providers (own API keys) |
| `ctrl+q` | Quit |
