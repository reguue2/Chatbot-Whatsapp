# tests/db/test_db_ops.py
from datetime import datetime
from importlib import import_module

def test_cancelar_reserva_db_updated_at(monkeypatch):
    bd = import_module("bd_utils") if "bd_utils" in globals() else  __import__("Bot_ia_secretaria_peluqueria.app")

    class R:
        id=1; estado="confirmada"; peluqueria_id=1
        fecha=datetime(2025,9,20).date()
        def __init__(self):
            self.updated_at=None
            self.cancelled_at=None
    obj = R()

    class FakeDB:
        def __init__(self): self._rolled=False; self._comm=False
        def query(self, model): return self
        def filter(self, *a, **k): return self
        def filter_by(self, **k): return self
        def one_or_none(self): return obj
        def with_for_update(self): return self
        def commit(self): self._comm=True
        def rollback(self): self._rolled=True
        def close(self): pass
        def execute(self, *a, **k): # no-op locks
            class S:
                def scalar(self_inner): return 1
            return S()
    # parchea SessionLocal y _release_lock
    monkeypatch.setattr(bd, "SessionLocal", lambda: FakeDB(), raising=False)
    monkeypatch.setattr(bd, "_release_lock", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(bd, "_acquire_lock", lambda *a, **k: True, raising=False)
    # Ejecuta
    ok = bd.cancelar_reserva_db(1)
    assert ok is True
    assert obj.estado == "cancelada"
    # si existen, deberÃ­an haberse tocado
    assert obj.updated_at is not None or hasattr(obj,"updated_at")==False
    assert obj.cancelled_at is not None or hasattr(obj,"cancelled_at")==False
