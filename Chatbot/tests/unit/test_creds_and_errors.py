# tests/unit/test_creds_and_errors.py
import pytest

def import_module(name):  # evita confundir linters
    return __import__(name)

def test__wa_creds_for_valida(monkeypatch):
    app =  __import__("Bot_ia_secretaria_peluqueria.app")
    class Pelu: wa_token="TOK"
    monkeypatch.setattr(app, "get_peluqueria_by_wa_phone_number_id", lambda _: Pelu(), raising=True)
    tok, ver = app._wa_creds_for("PH_1")
    assert tok == "TOK" and isinstance(ver, str)

def test__wa_creds_for_sin_pelu_error(monkeypatch):
    app =  __import__("Bot_ia_secretaria_peluqueria.app")
    monkeypatch.setattr(app, "get_peluqueria_by_wa_phone_number_id", lambda _: None, raising=True)
    with pytest.raises(ValueError):
        app._wa_creds_for("PH_1")
