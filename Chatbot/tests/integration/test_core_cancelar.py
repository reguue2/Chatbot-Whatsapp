# tests/integration/test_core_cancelar.py
from importlib import import_module

def test_cancelar_flujo_minimo(client, monkeypatch):
    app =  __import__("Bot_ia_secretaria_peluqueria.app")
    # simula que existe reserva RID_1 y cancelar_reserva_db devuelve True
    monkeypatch.setattr(app, "cancelar_reserva_db", lambda rid: True, raising=True)

    # Usuario arranca cancelar y elige RID_1
    client.post("/webhook", json={"session_id":"wa_600","mensaje":"cancelar"})
    client.post("/webhook", json={"session_id":"wa_600","mensaje":"RID_1"})
    r = client.post("/webhook", json={"session_id":"wa_600","mensaje":"sÃ­"})
    assert r.status_code == 200
    assert "cancelada" in r.json()["respuesta"].lower()
