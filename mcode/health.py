import json
import os
import time
import concurrent.futures as cf

from .storage import MCODE_DIR

HEALTH_PATH = os.path.join(MCODE_DIR, "model_health.json")
STALE_AFTER = 6 * 3600  # re-probe every 6 hours


def load_health() -> dict:
    if os.path.exists(HEALTH_PATH):
        try:
            with open(HEALTH_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_health(data: dict):
    os.makedirs(MCODE_DIR, exist_ok=True)
    tmp = HEALTH_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, HEALTH_PATH)


def is_stale(data: dict) -> bool:
    return not data or (time.time() - data.get("checked_at", 0)) > STALE_AFTER


def probe(models: list[dict], clients: dict, timeout: float = 45.0,
          max_workers: int = 8) -> dict:
    """Test each model with a minimal chat request.

    Returns {model_id: bool} — True if the model responded.
    """
    by_provider = {}
    for m in models:
        by_provider.setdefault(m.get("provider", "mrouter"), []).append(m)

    results: dict[str, bool] = {}

    def test(mid: str, client) -> bool:
        try:
            r = client.chat({
                "model": mid,
                "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
                "max_tokens": 300,
            })
            msg = r["choices"][0].get("message", {})
            content = msg.get("content") or msg.get("reasoning") or ""
            return bool(str(content).strip())
        except Exception:
            return False

    with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {}
        for prov, prov_models in by_provider.items():
            client = clients.get(prov)
            if client is None:
                for m in prov_models:
                    results[m["id"]] = False
                continue
            for m in prov_models:
                futures[ex.submit(test, m["id"], client)] = m["id"]
        for fut in cf.as_completed(futures, timeout=timeout * 4):
            mid = futures[fut]
            try:
                results[mid] = fut.result()
            except Exception:
                results[mid] = False
    return results
