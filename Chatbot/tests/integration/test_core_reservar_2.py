# tests/integration/test_core_reservar_2.py
from datetime import date

def _mk_payload_reservar(fecha, hora, servicio_id, nombre="Diego", telefono="600000000"):
    return {
        "fecha": fecha,
        "hora": hora,
        "servicio_id": servicio_id,
        "nombre": nombre,
        "telefono": telefono,
    }

def test_confirm_bd_then_calendar_idempotent(appm, monkeycalendar, monkeycache):
    """
    - Confirma: BD OK -> Calendar se llama con forced_event_id = res{reserva_id}
    - Guarda event_id en BD
    - Purga cache del dÃ­a
    """
    fecha = date.today().strftime("%Y-%m-%d")
    datos = _mk_payload_reservar(fecha, "11:00", servicio_id=1)

    class PeluStub:
        id = 1
        nombre = "Pelu Test"
        servicios = []

    # 1) Guardar en BD
    res = appm.guardar_reserva_db(PeluStub.id, datos["servicio_id"], datos["nombre"],
                                  datos["telefono"], datos["fecha"], datos["hora"], event_id=None)
    assert isinstance(res, int), f"BD no devolviÃ³ ID: {res}"
    reserva_id = int(res)

    # 2) Calendar idempotente por reserva
    forced_event_id = f"res{reserva_id}"
    gcal_key = f"{PeluStub.id}:{datos['fecha']}:{datos['hora']}:{reserva_id}"
    servicio_sel = getattr(appm, "get_servicio_from_datos", None)
    if callable(servicio_sel):
        servicio_sel = servicio_sel(PeluStub, datos)
    else:
        servicio_sel = type("Srv", (), {"id": datos["servicio_id"], "nombre": "Srv", "duracion_min": 30})()

    gcal = appm.crear_reserva_google_idempotente(
        peluqueria=PeluStub,
        datos={"fecha": datos["fecha"], "hora": datos["hora"], "servicio": servicio_sel,
               "nombre": datos["nombre"], "telefono": datos["telefono"]},
        forced_event_id=forced_event_id,
        private_key=gcal_key
    )
    assert gcal and gcal.get("success") is True
    assert gcal.get("event_id") == forced_event_id
    assert any(c[2] == forced_event_id for c in monkeycalendar["created"])

    # 3) set_event_id_db
    ok = appm.set_event_id_db(reserva_id, forced_event_id)
    assert ok is True

    # 4) purge cache
    appm.purge_horas_cache(PeluStub, datos["fecha"])
    assert monkeycache["purge"] and monkeycache["purge"][-1] == (PeluStub.id, datos["fecha"])
