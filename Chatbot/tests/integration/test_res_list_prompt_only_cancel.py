# tests/integration/test_res_list_prompt_only_cancel.py
import json, hmac, hashlib
from importlib import import_module

def _sig(secret, body_bytes):
    import hmac, hashlib
    mac = hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()
    return f"sha256={mac}"

def _payload_text(text="cancelar", wamid="w1"):
    return {"entry":[{"changes":[{"value":{
        "metadata":{"phone_number_id":"PH_1"},
        "messages":[{"from":"600","id":wamid,"timestamp":"1695031200","type":"text","text":{"body":text}}],
    }}]}]}

def test_prompt_res_list_no_menciona_modificar(client, monkeypatch):
    secret = "S"; app =  __import__("Bot_ia_secretaria_peluqueria.app")
    settings = __import__("Bot_ia_secretaria_peluqueria.settings").settings if hasattr(__import__("Bot_ia_secretaria_peluqueria.settings"), "settings") else __import__("Bot_ia_secretaria_peluqueria.settings")
    settings.WABA_APP_SECRET = secret

    # core devuelve res_list
    import requests
    def post(url, headers=None, json=None, timeout=None):
        if url.endswith("/webhook"):
            return type("R", (), {"ok": True, "json": lambda: {
                "respuesta": "elige reserva",
                "ui": "res_list",
                "choices":[{"id":"RID_1","title":"Hoy 10:00"}]
            }})()
        return type("R", (), {"ok": True, "json": lambda: {"ok":True}})()
    monkeypatch.setattr(requests, "post", post, raising=True)

    captured=[]
    def wa_send_reservas_list(phone_number_id, to, items, prompt_text="Selecciona la reserva:", session_id=None):
        captured.append(prompt_text)
        return True
    monkeypatch.setattr(app, "wa_send_reservas_list", wa_send_reservas_list, raising=True)

    body = json.dumps(_payload_text("cancelar")).encode()
    r = client.post("/webhook/whatsapp", data=body, headers={"X-Hub-Signature-256": _sig(secret, body)})
    assert r.status_code == 200
    assert captured and "cancelar" in captured[-1].lower() and "modific" not in captured[-1].lower()
