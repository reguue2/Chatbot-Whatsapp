# tests/integration/test_core_reservar.py
import pytest
from importlib import import_module

AFF = ["sÃ­","si","ok","confirmo"]
DEN = ["no","cancelar","nop"]

def start(client):
    return client.post("/webhook", json={"session_id":"wa_600","mensaje":"reservar"})

def paso_servicio_fecha_hora_tel(client):
    client.post("/webhook", json={"session_id":"wa_600","mensaje":"corte"})
    client.post("/webhook", json={"session_id":"wa_600","mensaje":"20/09/2025"})
    client.post("/webhook", json={"session_id":"wa_600","mensaje":"10:00"})
    client.post("/webhook", json={"session_id":"wa_600","mensaje":"+34600000000"})

def test_reservar_negacion(client):
    start(client); paso_servicio_fecha_hora_tel(client)
    r = client.post("/webhook", json={"session_id":"wa_600","mensaje":"no"})
    j = r.json()
    assert r.status_code == 200
    assert "no confirmamos" in j["respuesta"].lower()
    assert j.get("ui") == "main_menu"

@pytest.mark.parametrize("pal", AFF)
def test_reservar_ok(client, monkeypatch, pal):
    # Stubs externos
    app =  __import__("Bot_ia_secretaria_peluqueria.app")
    monkeypatch.setattr(app, "crear_reserva_google", lambda *a, **k: {"success": True, "event_id": "evt_1"}, raising=True)
    # Si tu guardar_reserva_db devuelve id int en Ã©xito:
    monkeypatch.setattr(app, "guardar_reserva_db", lambda *a, **k: 1, raising=True)

    start(client); paso_servicio_fecha_hora_tel(client)
    r = client.post("/webhook", json={"session_id":"wa_600","mensaje":pal}, headers={"Idempotency-Key":"k1"})
    j = r.json()
    assert r.status_code == 200
    assert "reserva" in j["respuesta"].lower()
    assert "respuesta2" in j

def test_reservar_bd_falla_sugiere(client, monkeypatch):
    app =  __import__("Bot_ia_secretaria_peluqueria.app")
    monkeypatch.setattr(app, "crear_reserva_google", lambda *a, **k: {"success": True, "event_id": "evt_1"}, raising=True)
    monkeypatch.setattr(app, "guardar_reserva_db", lambda *a, **k: False, raising=True)

    start(client); paso_servicio_fecha_hora_tel(client)
    r = client.post("/webhook", json={"session_id":"wa_600","mensaje":"sÃ­"})
    j = r.json()
    assert r.status_code in (200, 409)
    assert ("elige otra" in j["respuesta"].lower()) or ("no quedan huecos" in j["respuesta"].lower())
