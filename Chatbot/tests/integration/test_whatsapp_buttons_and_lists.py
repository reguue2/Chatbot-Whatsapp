# tests/integration/test_whatsapp_buttons_and_lists.py
import json, hmac, hashlib
from importlib import import_module

def _sig(secret, body_bytes):
    mac = hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()
    return f"sha256={mac}"

def _payload_button(phone_id="PH_1", from_msisdn="600", btn_id="ACT_RESERVAR", wamid="w1", ts="1695031200"):
    return {
        "entry":[{"changes":[{"value":{
            "metadata":{"phone_number_id":phone_id},
            "messages":[{"from":from_msisdn,"id":wamid,"timestamp":ts,"type":"interactive",
                         "interactive":{"type":"button_reply","button_reply":{"id":btn_id}}}],
        }}]}]
    }

def _payload_list_reply(phone_id="PH_1", from_msisdn="600", list_id="HORA_P1_0", wamid="w2", ts="1695031200"):
    return {
        "entry":[{"changes":[{"value":{
            "metadata":{"phone_number_id":phone_id},
            "messages":[{"from":from_msisdn,"id":wamid,"timestamp":ts,"type":"interactive",
                         "interactive":{"type":"list_reply","list_reply":{"id":list_id}}}],
        }}]}]
    }

def test_button_reservar_dispara_reservar(client, monkeypatch):
    secret = "S3"
    settings = __import__("Bot_ia_secretaria_peluqueria.settings").settings if hasattr(__import__("Bot_ia_secretaria_peluqueria.settings"), "settings") else __import__("Bot_ia_secretaria_peluqueria.settings")
    settings.WABA_APP_SECRET = secret

    # mock core
    import requests
    def fake_core(url, headers=None, json=None, timeout=None):
        # cuando UI=main_menu, el webhook manda menÃºs, etc. aquÃ­ con ok simple
        return type("R", (), {"ok": True, "json": lambda: {"respuesta":"ok","ui":"main_menu"}})()
    monkeypatch.setattr(requests, "post", fake_core, raising=True)

    body = json.dumps(_payload_button(btn_id="ACT_RESERVAR")).encode()
    sig = _sig(secret, body)
    r = client.post("/webhook/whatsapp", data=body, headers={"X-Hub-Signature-256": sig})
    assert r.status_code == 200

def test_list_pagination_next_page_envia_otra_pagina(client, monkeypatch):
    secret = "S3"
    settings = __import__("Bot_ia_secretaria_peluqueria.settings").settings if hasattr(__import__("Bot_ia_secretaria_peluqueria.settings"), "settings") else __import__("Bot_ia_secretaria_peluqueria.settings")
    settings.WABA_APP_SECRET = secret

    # guardamos horas en storage para el session_id
    app =  __import__("Bot_ia_secretaria_peluqueria.app")
    horas = [f"{h:02d}:00" for h in range(1,25)]
    app.storage.setex("hours:wa_600", json.dumps(horas), ttl=300)

    # mock envÃ­o WA list
    sent=[]
    def send_hours(phone_id,to,session_id,horas,page=1,per_page=10):
        sent.append((page, len(horas)))
        return True
    monkeypatch.setattr(app, "wa_send_hours_page", send_hours, raising=True)

    import requests
    def ok_core(url, headers=None, json=None, timeout=None):
        return type("R", (), {"ok": True, "json": lambda: {"respuesta":""}})()
    monkeypatch.setattr(requests, "post", ok_core, raising=True)

    body = json.dumps(_payload_list_reply(list_id="HORA_NEXT_2")).encode()
    sig = _sig(secret, body)
    r = client.post("/webhook/whatsapp", data=body, headers={"X-Hub-Signature-256": sig})
    assert r.status_code == 200
    assert sent and sent[-1][0] == 2  # pidiÃ³ pÃ¡gina 2

def test_list_pick_specific_index_se_ruta_al_core_con_hora(client, monkeypatch):
    secret = "S3"
    settings = __import__("Bot_ia_secretaria_peluqueria.settings").settings if hasattr(__import__("Bot_ia_secretaria_peluqueria.settings"), "settings") else __import__("Bot_ia_secretaria_peluqueria.settings")
    settings.WABA_APP_SECRET = secret

    app =  __import__("Bot_ia_secretaria_peluqueria.app")
    horas = ["09:00","09:30","10:00"]
    app.storage.setex("hours:wa_600", json.dumps(horas), ttl=300)

    captured=[]
    def core(url, headers=None, json=None, timeout=None):
        captured.append(json.get("mensaje"))
        return type("R", (), {"ok": True, "json": lambda: {"respuesta":"ok"}})()
    import requests
    monkeypatch.setattr(requests, "post", core, raising=True)

    body = json.dumps(_payload_list_reply(list_id="HORA_P1_2")).encode()  # idx 2 â†’ "10:00"
    sig = _sig(secret, body)
    r = client.post("/webhook/whatsapp", data=body, headers={"X-Hub-Signature-256": sig})
    assert r.status_code == 200
    assert captured[-1] == "10:00"
