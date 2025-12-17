# tests/unit/test_purge_horas_cache.py
import sys
import types
from importlib import import_module


def _make_pelu(servicios_ids):
    class Servicio:
        def __init__(self, sid):
            self.id = sid

    class Pelu:
        id = 42
        servicios = [Servicio(sid) for sid in servicios_ids]

    return Pelu()


def test_purge_horas_cache_removes_keys_memory_storage(monkeypatch):
    app =  __import__("Bot_ia_secretaria_peluqueria.app")
    storage_mod = import_module("storage")

    memoria = storage_mod.MemoryStorage()
    monkeypatch.setattr(app, "storage", memoria, raising=False)

    pelu = _make_pelu([1, 2])
    fecha = "2024-01-01"

    keys = [
        app.get_horas_cache_key(pelu.id, servicio.id, fecha)
        for servicio in pelu.servicios
    ]
    keys.append(app.get_horas_cache_key(pelu.id, None, fecha))

    for key in keys:
        memoria.setex(key, "cached", ttl=60)
        assert memoria.get(key) == "cached"

    app.purge_horas_cache(pelu, fecha)

    for key in keys:
        assert memoria.get(key) is None


def test_purge_horas_cache_invoca_delete_en_backend_redis(monkeypatch):
    app =  __import__("Bot_ia_secretaria_peluqueria.app")
    storage_mod = import_module("storage")

    deleted = []

    class FakeRedisClient:
        def __init__(self):
            self.deleted = deleted

        def get(self, key):
            return None

        def setex(self, key, ttl, value):
            pass

        def delete(self, key):
            self.deleted.append(key)

        def pipeline(self):
            class _Pipe:
                def incr(self, key):
                    return self

                def expire(self, key, ttl):
                    return self

                def execute(self):
                    return [1, True]

            return _Pipe()

    fake_client = FakeRedisClient()

    fake_redis_module = types.SimpleNamespace()
    fake_redis_module.Redis = type(
        "Redis",
        (),
        {"from_url": staticmethod(lambda url, decode_responses=True: fake_client)},
    )

    monkeypatch.setitem(sys.modules, "redis", fake_redis_module)

    class FakeSettings:
        STORAGE_BACKEND = "redis"
        REDIS_URL = "redis://example"

    redis_storage = storage_mod.get_storage(FakeSettings)
    monkeypatch.setattr(app, "storage", redis_storage, raising=False)

    pelu = _make_pelu([3])
    fecha = "2024-02-02"

    keys = [
        app.get_horas_cache_key(pelu.id, pelu.servicios[0].id, fecha),
        app.get_horas_cache_key(pelu.id, None, fecha),
    ]

    app.purge_horas_cache(pelu, fecha)

    assert deleted == keys
