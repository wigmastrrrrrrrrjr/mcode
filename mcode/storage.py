import json
import os
import uuid
from datetime import datetime

MCODE_DIR = os.path.expanduser("~/.mcode")
CONFIG_PATH = os.path.join(MCODE_DIR, "config.json")
SESSIONS_DIR = os.path.join(MCODE_DIR, "sessions")
MCP_PATH = os.path.join(MCODE_DIR, "mcp.json")

DEFAULT_ROUTER_URL = "https://mrouter.csomeone301.workers.dev"


def ensure_dirs():
    os.makedirs(SESSIONS_DIR, exist_ok=True)


def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


def save_config(cfg: dict):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def load_mcp_servers() -> dict:
    if os.path.exists(MCP_PATH):
        try:
            with open(MCP_PATH) as f:
                data = json.load(f)
            return data.get("mcpServers", {})
        except Exception:
            return {}
    return {}
