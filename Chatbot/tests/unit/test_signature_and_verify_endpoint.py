# tests/unit/test_signature_and_verify_endpoint.py
import json, hmac, hashlib
from importlib import import_module

def test_whatsapp_verify_bad_token(client):
    settings = __import__("Bot_ia_secretaria_peluqueria.settings").settings if hasattr(__import__("Bot_ia_secretaria_peluqueria.settings"), "settings") else __import__("Bot_ia_secretaria_peluqueria.settings")
    settings.WABA_VERIFY_TOKEN = "correct"
    r = client.get("/webhook/whatsapp?hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=42")
    assert r.status_code == 403 and r.data.decode() == ""

def test_signature_missing_forbidden(client):
    r = client.post("/webhook/whatsapp", data=b"{}", headers={})
    assert r.status_code == 403

def test_signature_prefix_wrong(client):
    from Bot_ia_secretaria_peluqueria.app import verify_waba_signature
    assert verify_waba_signature("S", b"{}", "sha1=000") is False
