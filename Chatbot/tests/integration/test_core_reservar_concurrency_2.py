# tests/integration/test_core_reservar_concurrency_2.py
import datetime as dt

def test_capacidad_dos_permite_dos_solapes(bdm):
    """
    Con num_peluqueros=2 y duracion=30, dos reservas a la misma hora deben entrar.
    La tercera debe devolver {"error":"no_slot"}.
    """
    pelu_id = 1
    srv_id = 1
    fecha = dt.date.today().strftime("%Y-%m-%d")
    hora = "10:00"

    r1 = bdm.guardar_reserva_db(pelu_id, srv_id, "A", "600111222", fecha, hora, event_id=None)
    assert isinstance(r1, int)

    r2 = bdm.guardar_reserva_db(pelu_id, srv_id, "B", "600111223", fecha, hora, event_id=None)
    assert isinstance(r2, int)

    r3 = bdm.guardar_reserva_db(pelu_id, srv_id, "C", "600111224", fecha, hora, event_id=None)
    assert isinstance(r3, dict) and r3.get("error") == "no_slot"

def test_solapes_por_duracion_capacidad_uno(bdm):
    """
    Una reserva 10:00-10:30 solapa con otra 10:15-10:45.
    Con capacidad 1, la 2Âª debe rechazar.
    """
    pelu_id = 2   # asegÃºrate de que esta pelu tiene num_peluqueros=1 en test
    srv_id = 2
    fecha = dt.date.today().strftime("%Y-%m-%d")

    r1 = bdm.guardar_reserva_db(pelu_id, srv_id, "A", "600111222", fecha, "10:00", event_id=None)
    assert isinstance(r1, int)

    r2 = bdm.guardar_reserva_db(pelu_id, srv_id, "B", "600111223", fecha, "10:15", event_id=None)
    assert isinstance(r2, dict) and r2.get("error") == "no_slot"
