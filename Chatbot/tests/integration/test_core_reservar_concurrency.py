# tests/integration/test_core_reservar_concurrency.py
"""
Escenario: dos clientes intentan reservar la MISMA franja a la vez.
Objetivo: exactamente UNA confirmaciÃ³n feliz y UNA respuesta de conflicto ("ocupado", "elige otra", etc.)

Nota: en vez de crear hilos (los test clients no son thread-safe),
simulamos la contenciÃ³n con un guard en guardar_reserva_db: el primer
intento para un (pelu, servicio, fecha, hora) "gana", el segundo "pierde".
"""

import pytest
import Bot_ia_secretaria_peluqueria.app as appmod

def _flow_reservar(client, session_id, servicio="corte", fecha="20/09/2025", hora="10:00", tel="+34600000000"):
    client.post("/webhook", json={"session_id": session_id, "mensaje": "reservar"})
    client.post("/webhook", json={"session_id": session_id, "mensaje": servicio})
    client.post("/webhook", json={"session_id": session_id, "mensaje": fecha})
    client.post("/webhook", json={"session_id": session_id, "mensaje": hora})
    client.post("/webhook", json={"session_id": session_id, "mensaje": tel})
    # confirmaciÃ³n
    return client.post("/webhook", json={"session_id": session_id, "mensaje": "sÃ­"}, headers={"Idempotency-Key": f"ik-{session_id}"})


def test_reserva_dos_personas_mismo_slot(client, monkeypatch):
    # --- Stubs externos: Calendar siempre OK ---
    monkeypatch.setattr(appmod, "crear_reserva_google", lambda *a, **k: {"success": True, "event_id": "evt_1"}, raising=True)

    # --- SimulaciÃ³n de contenciÃ³n de capacidad en BD ---
    # La primera confirmaciÃ³n para la clave (pelu_id, servicio_id, fecha, hora) devuelve Ã©xito,
    # la segunda devuelve False (sin hueco).
    state = {"taken": set()}

    def guardar_reserva_db_fake(peluqueria_id, servicio_id, nombre, telefono, fecha_str, hora_str, event_id=None):
        key = (int(peluqueria_id), int(servicio_id), str(fecha_str), str(hora_str))
        if key in state["taken"]:
            return False  # ya ocupado
        state["taken"].add(key)
        return 123  # id de reserva

    monkeypatch.setattr(appmod, "guardar_reserva_db", guardar_reserva_db_fake, raising=True)

    # Si tu core necesita resolver servicio_id a partir del nombre en 'datos',
    # y en la confirmaciÃ³n usa 'servicio_id' del estado, no hace falta stub extra.
    # (Si fuera necesario, aÃ±ade un stub de get_servicio_from_datos aquÃ­.)

    # --- Dos sesiones distintas que apuntan al MISMO slot ---
    r1 = _flow_reservar(client, session_id="wa_601")
    r2 = _flow_reservar(client, session_id="wa_602")

    j1, j2 = r1.json(), r2.json()
    body1 = (j1.get("respuesta") or "").lower()
    body2 = (j2.get("respuesta") or "").lower()

    # Exactamente una debe ser Ã©xito y la otra conflicto
    success_kw = ("reserva confirmada" in body1) or ("Â¡reserva" in body1) or ("reserva realizada" in body1)
    conflict_kw = ("ocupar" in body2) or ("elige otra" in body2) or ("no quedan huecos" in body2) or ("no disponible" in body2)

    # Si por orden de ejecuciÃ³n saliÃ³ al revÃ©s, intercambiamos
    if not (success_kw and conflict_kw):
        success_kw = ("reserva confirmada" in body2) or ("Â¡reserva" in body2) or ("reserva realizada" in body2)
        conflict_kw = ("ocupar" in body1) or ("elige otra" in body1) or ("no quedan huecos" in body1) or ("no disponible" in body1)

    assert success_kw, f"Se esperaba una confirmaciÃ³n de Ã©xito, respuestas: '{body1}' / '{body2}'"
    assert conflict_kw, f"Se esperaba un mensaje de conflicto/ocupado, respuestas: '{body1}' / '{body2}'"
