# tests/helpers/fakes.py
from __future__ import annotations
import json
from datetime import datetime, timezone
from types import SimpleNamespace

class FakeStorage:
    """KV simple con incr y TTL opcional (solo guardamos el valor, TTL ignorado en tests)."""
    def __init__(self):
        self._data = {}

    def incr(self, key: str, ttl: int | None = None) -> int:
        self._data[key] = int(self._data.get(key, 0)) + 1
        return self._data[key]

    def get(self, key: str):
        return self._data.get(key)

    def setex(self, key: str, value: str, ttl: int):
        self._data[key] = value

class FakeResp:
    def __init__(self, ok=True, status_code=200, text="{}", json_data=None):
        self.ok = ok
        self.status_code = status_code
        self.text = text
        self._json = json_data

    def json(self):
        if self._json is not None:
            return self._json
        try:
            return json.loads(self.text or "{}")
        except Exception:
            return {}

class PostRecorder:
    """Graba requests.post para aserciones."""
    def __init__(self, always_ok=True):
        self.calls = []
        self.always_ok = always_ok

    def __call__(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers or {}, "json": json or {}, "timeout": timeout})
        if self.always_ok:
            return FakeResp(ok=True, status_code=200, json_data={"messages": ["sent"]})
        return FakeResp(ok=False, status_code=400, text='{"error":"bad"}')

def fixed_utc(ts="2025-09-18T10:00:00Z"):
    dt = datetime.fromisoformat(ts.replace("Z","+00:00"))
    return dt.astimezone(timezone.utc)
