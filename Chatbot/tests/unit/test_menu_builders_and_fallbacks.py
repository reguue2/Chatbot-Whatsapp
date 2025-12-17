# tests/unit/test_menu_builders_and_fallbacks.py
from importlib import import_module

def test_wa_send_main_menu_ok(monkeypatch):
    app =  __import__("Bot_ia_secretaria_peluqueria.app")
    def ok_creds(_): return "T","v21.0"
    monkeypatch.setattr(app, "_wa_creds_for", ok_creds, raising=True)

    # capturamos post
    calls=[]
    def post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "json": json})
        class R: ok=True; status_code=200; text="ok"
        return R()
    import requests
    monkeypatch.setattr(requests, "post", post, raising=True)

    ok = app.wa_send_main_menu("PH_1","600","Pelu",session_id="wa_600")
    assert ok is True
    assert calls and calls[0]["json"]["interactive"]["type"] == "button"

def test_wa_send_main_menu_fallback(monkeypatch):
    app =  __import__("Bot_ia_secretaria_peluqueria.app")
    def ok_creds(_): return "T","v21.0"
    monkeypatch.setattr(app, "_wa_creds_for", ok_creds, raising=True)

    sent=[]
    def bad_post(url, headers=None, json=None, timeout=None):
        if "messages" in url:
            class R: ok=False; status_code=400; text="bad"
            return R()
    def send_text(*a, **k):
        sent.append(True); return True

    import requests
    monkeypatch.setattr(requests, "post", bad_post, raising=True)
    monkeypatch.setattr(app, "wa_send_text", send_text, raising=True)

    ok = app.wa_send_main_menu("PH_1","600","Pelu",session_id="wa_600")
    assert ok is False and sent

def test_wa_send_service_list_ok_y_fallback(monkeypatch):
    app =  __import__("Bot_ia_secretaria_peluqueria.app")
    class Pelu: servicios=[type("S",(),{"nombre":"Corte"})()]
    monkeypatch.setattr(app, "get_peluqueria_by_wa_phone_number_id", lambda _: Pelu(), raising=True)
    monkeypatch.setattr(app, "_wa_creds_for", lambda _:("T","v21.0"), raising=True)

    # ok
    import requests
    def ok_post(url, headers=None, json=None, timeout=None):
        class R: ok=True; status_code=200; text="ok"
        return R()
    monkeypatch.setattr(requests, "post", ok_post, raising=True)
    assert app.wa_send_service_list("PH_1","600",Pelu(),session_id="wa_600") is True

    # fallback
    def bad_post(url, headers=None, json=None, timeout=None):
        class R: ok=False; status_code=400; text="bad"
        return R()
    called=[]
    def send_text(*a, **k): called.append(True); return True
    monkeypatch.setattr(requests, "post", bad_post, raising=True)
    monkeypatch.setattr(app, "wa_send_text", send_text, raising=True)
    assert app.wa_send_service_list("PH_1","600",Pelu(),session_id="wa_600") is False
    assert called

def test_wa_send_hours_page_ok_y_fallback(monkeypatch):
    app =  __import__("Bot_ia_secretaria_peluqueria.app")
    monkeypatch.setattr(app, "_wa_outbound_allow", lambda *a, **k: True, raising=True)
    monkeypatch.setattr(app, "_wa_creds_for", lambda _:("T","v21.0"), raising=True)

    import requests
    sent=[]
    def ok(url, headers=None, json=None, timeout=None):
        class R: ok=True; status_code=200; text="ok"
        return R()
    def bad(url, headers=None, json=None, timeout=None):
        class R: ok=False; status_code=400; text="bad"
        return R()
    def send_text(*a, **k): sent.append(True); return True

    horas = [f"{h:02d}:00" for h in range(1,15)]
    monkeypatch.setattr(requests, "post", ok, raising=True)
    assert app.wa_send_hours_page("PH","600","wa_600",horas,page=1) is True

    monkeypatch.setattr(requests, "post", bad, raising=True)
    monkeypatch.setattr(app, "wa_send_text", send_text, raising=True)
    assert app.wa_send_hours_page("PH","600","wa_600",horas,page=1) is False
    assert sent
