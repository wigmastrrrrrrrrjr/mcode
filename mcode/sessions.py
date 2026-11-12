import json
import os
from .storage import SESSIONS_DIR


def new_session_id() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex()


def session_path(sid: str) -> str:
    safe = "".join(c for c in sid if c.isalnum() or c in "-_")
    return os.path.join(SESSIONS_DIR, safe + ".json")


def list_sessions() -> list[dict]:
    """Return sessions sorted by updated time, newest first."""
    out = []
    if not os.path.isdir(SESSIONS_DIR):
        return out
    for fn in os.listdir(SESSIONS_DIR):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(SESSIONS_DIR, fn)) as f:
                s = json.load(f)
            out.append({
                "id": s.get("id", fn[:-5]),
                "name": s.get("name", "Untitled"),
                "model": s.get("model", ""),
                "updated": s.get("updated", 0),
                "messages": len(s.get("messages", [])),
            })
        except Exception:
            continue
    out.sort(key=lambda x: x.get("updated", 0), reverse=True)
    return out


def load_session(sid: str) -> dict | None:
    path = session_path(sid)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def save_session(session: dict):
    session["updated"] = __import__("time").time()
    path = session_path(session["id"])
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(session, f, indent=2)
    os.replace(tmp, path)


def delete_session(sid: str):
    path = session_path(sid)
    if os.path.exists(path):
        os.remove(path)
