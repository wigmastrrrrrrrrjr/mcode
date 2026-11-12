import httpx


class ApiError(Exception):
    pass


class Unauthorized(ApiError):
    pass


class MRouterClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 180.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.client = httpx.Client(timeout=timeout)
        self._model_cache: list[dict] | None = None

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _check(self, resp: httpx.Response):
        if resp.status_code == 401:
            raise Unauthorized("Your session is invalid. Restart mcode to log in again.")
        if resp.status_code >= 400:
            raise ApiError(f"HTTP {resp.status_code}: {resp.text[:200]}")

    def register(self, email: str, password: str) -> dict:
        r = self.client.post(
            f"{self.base_url}/v1/auth/register",
            json={"email": email, "password": password},
        )
        return r.json()

    def login(self, email: str, password: str) -> dict:
        r = self.client.post(
            f"{self.base_url}/v1/auth/login",
            json={"email": email, "password": password},
        )
        if r.status_code == 401:
            raise Unauthorized(r.json().get("error", "Login failed"))
        self._check(r)
        return r.json()

    def me(self) -> dict:
        r = self.client.get(f"{self.base_url}/v1/auth/me", headers=self._headers())
        self._check(r)
        return r.json()

    def list_models(self, force: bool = False) -> list[dict]:
        if self._model_cache is not None and not force:
            return self._model_cache
        r = self.client.get(f"{self.base_url}/v1/models", headers=self._headers())
        self._check(r)
        data = r.json()
        if "error" in data and not data.get("data"):
            raise ApiError(data["error"])
        self._model_cache = data.get("data", [])
        return self._model_cache

    def chat(self, payload: dict) -> dict:
        r = self.client.post(
            f"{self.base_url}/v1/chat/completions",
            headers=self._headers(),
            json=payload,
        )
        self._check(r)
        data = r.json()
        if "error" in data:
            raise ApiError(data["error"])
        return data


class CustomProviderClient:
    """Talks directly to an OpenAI-compatible endpoint with the user's own key."""

    def __init__(self, name: str, base_url: str, api_key: str, timeout: float = 180.0):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.client = httpx.Client(timeout=timeout)
        self.is_custom = True

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _strip(self, model: str) -> str:
        prefix = f"custom:{self.name}:"
        if model.startswith(prefix):
            return model[len(prefix):]
        return model

    def list_models(self, force: bool = False) -> list[dict]:
        r = self.client.get(f"{self.base_url}/models", headers=self._headers())
        if r.status_code >= 400:
            raise ApiError(f"{self.name}: HTTP {r.status_code} listing models")
        data = r.json()
        models = []
        for m in data.get("data", []):
            models.append({
                "id": m.get("id", ""),
                "name": m.get("name", m.get("id", "")),
                "free": bool(m.get("free", False)),
                "context_length": m.get("context_length", 0),
                "pricing": m.get("pricing", {"prompt": 0, "completion": 0}),
            })
        return models

    def chat(self, payload: dict) -> dict:
        payload = dict(payload)
        payload["model"] = self._strip(payload.get("model", ""))
        r = self.client.post(
            f"{self.base_url}/chat/completions",
            headers=self._headers(),
            json=payload,
        )
        if r.status_code == 401:
            raise Unauthorized(f"{self.name}: invalid API key (update it in Providers)")
        if r.status_code >= 400:
            raise ApiError(f"{self.name}: HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        if "error" in data:
            raise ApiError(f"{self.name}: {data['error']}")
        return data


class DispatchClient:
    """Routes chat payloads to the right client based on the model string."""

    def __init__(self, clients: dict):
        self.clients = clients

    def chat(self, payload: dict) -> dict:
        model = payload.get("model", "")
        for name, client in self.clients.items():
            if model.startswith(f"custom:{name}:"):
                return client.chat(payload)
        return self.clients["mrouter"].chat(payload)
