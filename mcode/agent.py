import json
from .api import MRouterClient
from .mcp import MCPManager
from .tools import run_tool, TOOL_SCHEMAS

SYSTEM_PROMPT = """You are mcode, a coding assistant running inside a terminal IDE.
You have tools: read (files/dirs), write (files), execute (shell commands).
Use tools whenever they help: inspect the codebase before editing, write complete files, run commands to verify.
Keep answers concise. Show diffs or key code instead of dumping everything.
Paths are relative to the current working directory unless absolute."""


def _content_str(msg_content) -> str:
    if isinstance(msg_content, str):
        return msg_content
    if isinstance(msg_content, list):
        parts = []
        for p in msg_content:
            if isinstance(p, dict):
                parts.append(p.get("text", ""))
            else:
                parts.append(str(p))
        return "\n".join(parts)
    return str(msg_content or "")


def build_payload(session: dict, model: str) -> dict:
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    for m in session["messages"]:
        clean = {"role": m["role"], "content": m.get("content") or ""}
        if m.get("tool_calls"):
            clean["tool_calls"] = m["tool_calls"]
        if m.get("tool_call_id"):
            clean["tool_call_id"] = m["tool_call_id"]
        if clean["role"] == "assistant" and not clean["content"] and clean.get("tool_calls"):
            clean["content"] = None
        msgs.append(clean)
    return {
        "model": model,
        "messages": msgs,
        "tools": TOOL_SCHEMAS,
        "max_tokens": 8000,
    }


class TurnResult:
    def __init__(self):
        self.events: list[dict] = []  # {type, ...}

    def add(self, ev_type: str, **kw):
        self.events.append({"type": ev_type, **kw})


def run_turn(client: MRouterClient, mcp: MCPManager, session: dict,
             model: str, user_prompt: str, max_steps: int = 12) -> TurnResult:
    result = TurnResult()
    session["messages"].append({"role": "user", "content": user_prompt})
    result.add("user_message", content=user_prompt)

    all_schemas = list(TOOL_SCHEMAS) + mcp.tool_schemas()

    for step in range(max_steps):
        result.add("thinking", step=step)
        payload = build_payload(session, model)
        if all_schemas:
            payload["tools"] = all_schemas
        try:
            resp = client.chat(payload)
        except Exception as e:
            result.add("error", content=str(e))
            return result

        choice = resp.get("choices", [{}])[0]
        message = choice.get("message", {})
        content = _content_str(message.get("content"))
        reasoning = message.get("reasoning") or message.get("reasoning_content") or ""
        if reasoning:
            result.add("reasoning", content=str(reasoning))
        result.add("thinking_done")

        if message.get("tool_calls"):
            session["messages"].append({
                "role": "assistant",
                "content": message.get("content") or None,
                "tool_calls": message["tool_calls"],
            })
            if content:
                result.add("assistant_message", content=content)
            for tc in message["tool_calls"]:
                fn = tc.get("function", {})
                name = fn.get("name", "unknown")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                result.add("tool_call", name=name, args=args)
                out = mcp.dispatch(name, args)
                if out is None:
                    out = run_tool(name, args, cwd=session.get("cwd"))
                result.add("tool_result", name=name, content=str(out)[:4000])
                session["messages"].append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": str(out)[:30_000],
                })
            continue

        session["messages"].append({"role": "assistant", "content": content})
        result.add("assistant_message", content=content)
        return result

    result.add("error", content="Stopped: too many tool steps in one turn.")
    return result
