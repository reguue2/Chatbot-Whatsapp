# tests/db/test_guardar_and_locks.py
from importlib import import_module
from datetime import datetime, date, time

def test_guardar_reserva_db_happy_path(monkeypatch):
    bd = import_module("bd_utils") if "bd_utils" in globals() else  __import__("Bot_ia_secretaria_peluqueria.app")

    # Fakes mÃ­nimos
    class Pelu: id=1; num_peluqueros=2
    class Serv: id=1; duracion_min=30; precio=10
    class R:
        def __init__(self):
            self.id=1; self.peluqueria_id=1; self.servicio_id=1
            self.fecha=date(2025,9,20); self.hora=time(10,0)
            self.estado="confirmada"

    r_obj = R()
    data={"added":False}
    class FakeDB:
        def __init__(self): self.store=[]
        def execute(self, *a, **k):
            class S:
                def scalar(self_inner): return 1
            return S()
        def query(self, model): return self
        def filter(self, *a, **k): return self
        def with_for_update(self): return self
        def one(self):
            return Pelu() if len(self.store)==0 else Serv()
        def one_or_none(self): return None
        def options(self, *a, **k): return self
        def all(self): return []
        def add(self, obj): data["added"]=True
        def flush(self): pass
        def commit(self): pass
        def rollback(self): pass
        def close(self): pass

    monkeypatch.setattr(bd, "SessionLocal", lambda: FakeDB(), raising=False)
    monkeypatch.setattr(bd, "_release_lock", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(bd, "_acquire_lock", lambda *a, **k: True, raising=False)

    rid = bd.guardar_reserva_db(1,1,"Diego","+34600","2025-09-20","10:00",event_id="evt1")
    assert isinstance(rid, int) and data["added"] is True
