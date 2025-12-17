# tests/unit/test_utiles_core.py
from importlib import import_module

def test_wa_outbound_allow_incr_ok(fake_storage, monkeypatch):
    settings = __import__("Bot_ia_secretaria_peluqueria.settings").settings if hasattr(__import__("Bot_ia_secretaria_peluqueria.settings"), "settings") else __import__("Bot_ia_secretaria_peluqueria.settings")
    # limite 2/minuto
    try:
        settings.RATE_LIMITS["OUTBOUND_WA_PER_PELU"] = 2
    except Exception:
        pass

    app =  __import__("Bot_ia_secretaria_peluqueria.app")

    # Finge peluquerÃ­a id
    class Pelu: id = 7
    monkeypatch.setattr(app, "get_peluqueria_by_wa_phone_number_id", lambda _: Pelu(), raising=True)

    phone_id = "PH_1"
    assert app._wa_outbound_allow(phone_id) is True
    assert app._wa_outbound_allow(phone_id) is True
    # 3Âª excede
    res = app._wa_outbound_allow(phone_id)
    assert res is False or res == {"ok": False, "error": "wa_outbound_rate_limited"}
