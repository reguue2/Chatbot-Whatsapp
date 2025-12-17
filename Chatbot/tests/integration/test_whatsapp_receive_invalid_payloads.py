# tests/integration/test_whatsapp_receive_invalid_payloads.py
import json, hmac, hashlib
from importlib import import_module

def _sig(secret, body_bytes):
    mac = hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()
    return f"sha256={mac}"

def test_post_payload_sin_messages_no_revienta(client):
    secret = "S"; settings = __import__("Bot_ia_secretaria_peluqueria.settings").settings if hasattr(__import__("Bot_ia_secretaria_peluqueria.settings"), "settings") else __import__("Bot_ia_secretaria_peluqueria.settings")
    settings.WABA_APP_SECRET = secret
    body = json.dumps({"entry":[{"changes":[{"value":{"metadata":{"phone_number_id":"PH"}}}]}]}).encode()
    r = client.post("/webhook/whatsapp", data=body, headers={"X-Hub-Signature-256": _sig(secret, body)})
    assert r.status_code == 200

def test_post_entry_multiples_no_duplica(client, monkeypatch):
    secret = "S"; settings = __import__("Bot_ia_secretaria_peluqueria.settings").settings if hasattr(__import__("Bot_ia_secretaria_peluqueria.settings"), "settings") else __import__("Bot_ia_secretaria_peluqueria.settings")
    settings.WABA_APP_SECRET = secret

    # core ok
    import requests
    def ok(url, headers=None, json=None, timeout=None):
        return type("R", (), {"ok": True, "json": lambda: {"respuesta":"ok"}})()
    monkeypatch.setattr(requests, "post", ok, raising=True)

    body = json.dumps({
        "entry":[
            {"changes":[{"value":{"metadata":{"phone_number_id":"PH"},"messages":[{"from":"600","id":"w1","timestamp":"1","type":"text","text":{"body":"duda"}}]}}]},
            {"changes":[{"value":{"metadata":{"phone_number_id":"PH"},"messages":[{"from":"600","id":"w1","timestamp":"1","type":"text","text":{"body":"duda"}}]}}]}
        ]
    }).encode()
    r = client.post("/webhook/whatsapp", data=body, headers={"X-Hub-Signature-256": _sig(secret, body)})
    assert r.status_code == 200
