# tests/unit/test_rate_limits_and_storage.py
import pytest
from importlib import import_module

def test_wa_outbound_allow_ilimitado_si_no_hay_config(monkeypatch):
    app =  __import__("Bot_ia_secretaria_peluqueria.app")
    settings = __import__("Bot_ia_secretaria_peluqueria.settings").settings if hasattr(__import__("Bot_ia_secretaria_peluqueria.settings"), "settings") else __import__("Bot_ia_secretaria_peluqueria.settings")

    # borra la clave para forzar try/except
    monkeypatch.setitem(settings.RATE_LIMITS, "OUTBOUND_WA_PER_PELU", "not_an_int", raising=False)
    assert app._wa_outbound_allow("PH_1") is True

def test_wa_outbound_allow_sin_pelu_devuelve_true(monkeypatch):
    app =  __import__("Bot_ia_secretaria_peluqueria.app")
    monkeypatch.setattr(app, "get_peluqueria_by_wa_phone_number_id", lambda _: None, raising=True)
    assert app._wa_outbound_allow("PH_1") is True

def test_wa_outbound_allow_storage_falla_devuelve_true(monkeypatch):
    app =  __import__("Bot_ia_secretaria_peluqueria.app")
    class Pelu: id = 99
    monkeypatch.setattr(app, "get_peluqueria_by_wa_phone_number_id", lambda _: Pelu(), raising=True)

    class BrokenStorage:
        def incr(self, *a, **k): raise RuntimeError("oops")
    monkeypatch.setattr(app, "storage", BrokenStorage(), raising=False)

    assert app._wa_outbound_allow("PH_1") is True

def test_should_process_by_ts_si_storage_falla_procesa(monkeypatch):
    app =  __import__("Bot_ia_secretaria_peluqueria.app")
    class BrokenStorage:
        def get(self, *a, **k): raise RuntimeError("oops")
        def setex(self, *a, **k): raise RuntimeError("oops")
    monkeypatch.setattr(app, "storage", BrokenStorage(), raising=False)

    assert app.should_process_by_ts("wa_x", 123) is True
    # siguiente igual (aunque â€œfalleâ€ el remember) tambiÃ©n deja pasar
    assert app.should_process_by_ts("wa_x", 122) is True
