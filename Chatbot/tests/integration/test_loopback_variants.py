# tests/integration/test_loopback_variants.py
import json, hmac, hashlib
from importlib import import_module
from requests.exceptions import ReadTimeout

def _sig(secret, body_bytes):
    import hmac, hashlib
    mac = hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()
    return f"sha256={mac}"

def _payload_text(text="reservar", wamid="w1"):
    return {"entry":[{"changes":[{"value":{
        "metadata":{"phone_number_id":"PH_1"},
        "messages":[{"from":"600","id":wamid,"timestamp":"1695031200","type":"text","text":{"body":text}}],
    }}]}]}

def test_loopback_ui_variantes(client, monkeypatch):
    secret = "S"
    settings = __import__("Bot_ia_secretaria_peluqueria.settings").settings if hasattr(__import__("Bot_ia_secretaria_peluqueria.settings"), "settings") else __import__("Bot_ia_secretaria_peluqueria.settings")
    settings.WABA_APP_SECRET = secret

    # Simulamos distintas UIs del core en llamadas consecutivas
    import requests
    responses = [
        {"respuesta":"menu","ui":"main_menu"},
        {"respuesta":"elige servicio","ui":"services"},
        {"respuesta":"elige hora","ui":"hours","choices":["10:00","10:30"]},
        {"respuesta":"elige reserva","ui":"res_list","choices":[{"id":"RID_1","title":"Hoy 10:00"}]},
        {"respuesta":"ok"}  # plain text
    ]
    idx={"i":0}
    def post(url, headers=None, json=None, timeout=None):
        if url.endswith("/webhook"):
            i = idx["i"]; idx["i"] += 1
            return type("R", (), {"ok": True, "json": lambda: responses[i]})()
        # mensajes WA â†’ OK silencioso
        return type("R", (), {"ok": True, "json": lambda: {"ok":True}})()
    monkeypatch.setattr(requests, "post", post, raising=True)

    for w in range(5):
        body = json.dumps(_payload_text("duda", wamid=f"x{w}")).encode()
        r = client.post("/webhook/whatsapp", data=body, headers={"X-Hub-Signature-256": _sig(secret, body)})
        assert r.status_code == 200

def test_loopback_timeout_no_revienta(client, monkeypatch):
    secret = "S"; app =  __import__("Bot_ia_secretaria_peluqueria.app")
    settings = __import__("Bot_ia_secretaria_peluqueria.settings").settings if hasattr(__import__("Bot_ia_secretaria_peluqueria.settings"), "settings") else __import__("Bot_ia_secretaria_peluqueria.settings")
    settings.WABA_APP_SECRET = secret

    import requests
    def post(url, headers=None, json=None, timeout=None):
        if url.endswith("/webhook"):
            raise ReadTimeout("slow")
        return type("R", (), {"ok": True, "json": lambda: {"ok":True}})()
    monkeypatch.setattr(requests, "post", post, raising=True)

    body = json.dumps({"entry":[{"changes":[{"value":{
        "metadata":{"phone_number_id":"PH_1"},
        "messages":[{"from":"600","id":"wamid","timestamp":"1695031200","type":"text","text":{"body":"reservar"}}],
    }}]}]}).encode()
    r = client.post("/webhook/whatsapp", data=body, headers={"X-Hub-Signature-256": _sig(secret, body)})
    assert r.status_code == 200  # ACK vacÃ­o
