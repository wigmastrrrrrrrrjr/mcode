import os
import shlex
import subprocess


MAX_FILE_BYTES = 100_000
MAX_EXEC_SECONDS = 60
MAX_OUTPUT_BYTES = 30_000
FORBIDDEN_EXEC = ["rm -rf /", "mkfs", ":(){:|:&};:", "dd if=/dev/zero of=/dev/"]


def read_file(path: str) -> str:
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        return f"Error: file not found: {path}"
    if os.path.isdir(path):
        try:
            entries = os.listdir(path)
            return "\n".join(sorted(entries))
        except OSError as e:
            return f"Error: {e}"
    try:
        with open(path, "rb") as f:
            data = f.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            return f"Error: file too large ({len(data)} bytes). Read a portion with execute (head/tail/sed)."
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        numbered = [f"{i+1}| {line}" for i, line in enumerate(lines)]
        return "\n".join(numbered) if numbered else "(empty file)"
    except OSError as e:
        return f"Error: {e}"


def write_file(path: str, content: str) -> str:
    path = os.path.expanduser(path)
    directory = os.path.dirname(path)
    try:
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        size = os.path.getsize(path)
        return f"OK. Wrote {size} bytes to {path}"
    except OSError as e:
        return f"Error: {e}"


def execute(command: str, cwd: str | None = None) -> str:
    for bad in FORBIDDEN_EXEC:
        if bad in command:
            return f"Error: command blocked for safety: contains '{bad}'"
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=MAX_EXEC_SECONDS,
            cwd=cwd or os.getcwd(),
        )
        out = (result.stdout or "") + (result.stderr or "")
        if len(out) > MAX_OUTPUT_BYTES:
            out = out[:MAX_OUTPUT_BYTES] + f"\n... (truncated, {len(out)} bytes total)"
        if not out.strip():
            out = "(no output)"
        return f"exit={result.returncode}\n{out}"
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {MAX_EXEC_SECONDS}s"
    except Exception as e:
        return f"Error: {e}"


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Read a file (returns numbered lines) or list a directory's contents.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "File or directory path"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write",
            "description": "Write (create or overwrite) a file with the given content. Directories are created automatically.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute",
            "description": "Execute a shell command and return stdout/stderr/exit code. Timeout 60s.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
]

TOOL_IMPLEMENTATIONS = {
    "read": read_file,
    "write": write_file,
    "execute": execute,
}


def run_tool(name: str, args: dict, cwd: str | None = None) -> str:
    if name in TOOL_IMPLEMENTATIONS:
        try:
            return TOOL_IMPLEMENTATIONS[name](**args)
        except TypeError as e:
            return f"Error: bad arguments for {name}: {e}"
    return f"Error: unknown tool: {name}"
