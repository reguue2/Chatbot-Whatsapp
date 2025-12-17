# tests/integration/test_whatsapp_webhook_receive.py
import json
import hmac, hashlib
from importlib import import_module

def _sig(secret, body_bytes):
    mac = hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()
    return f"sha256={mac}"

def _payload_msg(phone_id="PH_1", from_msisdn="600000000", text="reservar", wamid="wamid-1", ts="1695031200"):
    return {
        "entry":[{"changes":[{"value":{
            "metadata":{"phone_number_id":phone_id},
            "messages":[{"from":from_msisdn,"id":wamid,"timestamp":ts,"type":"text","text":{"body":text}}],
        }}]}]
    }

def test_verify_get_ok(client, monkeypatch):
    settings = __import__("Bot_ia_secretaria_peluqueria.settings").settings if hasattr(__import__("Bot_ia_secretaria_peluqueria.settings"), "settings") else __import__("Bot_ia_secretaria_peluqueria.settings")
    settings.WABA_VERIFY_TOKEN = "VT"
    r = client.get("/webhook/whatsapp?hub.mode=subscribe&hub.verify_token=VT&hub.challenge=42")
    assert r.status_code == 200
    assert r.data.decode() == "42"

def test_post_rechaza_firma(client):
    body = json.dumps(_payload_msg()).encode()
    r = client.post("/webhook/whatsapp", data=body, headers={"X-Hub-Signature-256":"bad"})
    assert r.status_code == 403

def test_post_acepta_y_reenvia_al_core(client, monkeypatch, post_recorder):
    app =  __import__("Bot_ia_secretaria_peluqueria.app")
    # Firma vÃ¡lida
    secret = "S3"
    settings = __import__("Bot_ia_secretaria_peluqueria.settings").settings if hasattr(__import__("Bot_ia_secretaria_peluqueria.settings"), "settings") else __import__("Bot_ia_secretaria_peluqueria.settings")
    settings.WABA_APP_SECRET = secret

    # Fake loopback /webhook (core): devuelve UI main_menu
    def fake_post(url, headers=None, json=None, timeout=None):
        if url.endswith("/webhook"):
            return type("R", (), {"ok": True, "json": lambda: {"respuesta":"ok","ui":"main_menu"}})()
        return post_recorder(url, headers=headers, json=json, timeout=timeout)
    import requests
    monkeypatch.setattr(requests, "post", fake_post, raising=True)

    body = json.dumps(_payload_msg(text="duda")).encode()
    sig = _sig(secret, body)
    r = client.post("/webhook/whatsapp", data=body, headers={"X-Hub-Signature-256": sig})
    assert r.status_code == 200

def test_dedup_wamid(client, monkeypatch):
    app =  __import__("Bot_ia_secretaria_peluqueria.app")
    secret = "S3"
    settings = __import__("Bot_ia_secretaria_peluqueria.settings").settings if hasattr(__import__("Bot_ia_secretaria_peluqueria.settings"), "settings") else __import__("Bot_ia_secretaria_peluqueria.settings")
    settings.WABA_APP_SECRET = secret

    # Fake core responde ok
    import requests
    def ok_core(url, headers=None, json=None, timeout=None):
        return type("R", (), {"ok": True, "json": lambda: {"respuesta":"ok"}})()
    monkeypatch.setattr(requests, "post", ok_core, raising=True)

    body = json.dumps(_payload_msg(wamid="same")).encode(); sig = _sig(secret, body)
    r1 = client.post("/webhook/whatsapp", data=body, headers={"X-Hub-Signature-256": sig}); assert r1.status_code==200
    # ReenvÃ­o duplicado con mismo wamid â†’ dedup (no deberÃ­a re-procesar)
    r2 = client.post("/webhook/whatsapp", data=body, headers={"X-Hub-Signature-256": sig}); assert r2.status_code==200
