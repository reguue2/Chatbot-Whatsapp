# tests/unit/test_dedup_and_ts_edgecases.py
from importlib import import_module

def test_is_current_when_storage_missing_key_returns_true(monkeypatch):
    app =  __import__("Bot_ia_secretaria_peluqueria.app")
    class Fake:
        def get(self, *a, **k): return None
    monkeypatch.setattr(app, "storage", Fake(), raising=False)
    assert app.is_current("wa_1", 123) is True
