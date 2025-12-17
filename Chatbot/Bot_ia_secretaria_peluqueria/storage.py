# storage.py — abstracción de almacenamiento (memoria o Redis)
import time
from typing import Optional
from settings import settings

class Storage:
    def get(self, key: str) -> Optional[str]:
        raise NotImplementedError
    def setex(self, key: str, value: str, ttl: int) -> None:
        raise NotImplementedError
    def incr(self, key: str, ttl: int) -> int:
        raise NotImplementedError
    def delete(self, key: str) -> None:
        raise NotImplementedError

class MemoryStorage(Storage):
    def __init__(self):
        self._data: dict[str, tuple[str, float]] = {}
    def get(self, key: str) -> Optional[str]:
        row = self._data.get(key)
        if not row:
            return None
        value, exp = row
        if exp and exp < time.time():
            self._data.pop(key, None)
            return None
        return value
    def setex(self, key: str, value: str, ttl: int) -> None:
        self._data[key] = (value, time.time() + int(ttl))
    def incr(self, key: str, ttl: int) -> int:
        value = int(self.get(key) or "0") + 1
        self.setex(key, str(value), ttl)
        return value
    def delete(self, key: str) -> None:
        self._data.pop(key, None)

def get_storage(_settings=None) -> Storage:
    st = _settings or settings
    if st.STORAGE_BACKEND.lower() == "redis":
        import redis  # type: ignore
        r = redis.Redis.from_url(st.REDIS_URL, decode_responses=True)
        class RedisStorage(Storage):
            def get(self, key: str) -> Optional[str]:
                return r.get(key)
            def setex(self, key: str, value: str, ttl: int) -> None:
                r.setex(key, ttl, value)
            def incr(self, key: str, ttl: int) -> int:
                pipe = r.pipeline()
                pipe.incr(key)
                pipe.expire(key, ttl)
                res = pipe.execute()
                return int(res[0])
            def delete(self, key: str) -> None:
                r.delete(key)
        return RedisStorage()
    return MemoryStorage()