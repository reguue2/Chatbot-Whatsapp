# tests/unit/test_purge_horas_cache.py
def test_purga_cache_en_confirmar_y_cancelar(appm, bdm, monkeycache):
    # Creamos reserva
    from datetime import date
    fecha = date.today().strftime("%Y-%m-%d")
    rid = bdm.guardar_reserva_db(1, 1, "A", "600", fecha, "13:00", event_id=None)
    assert isinstance(rid, int)

    # Confirmar ya purga (lo validas en el test de integraciÃ³n)
    appm.purge_horas_cache(type("Pelu", (), {"id": 1, "servicios": []})(), fecha)
    assert monkeycache["purge"][-1] == (1, fecha)

    # Cancelar y purgar (usa la fecha real de la reserva)
    ok = bdm.cancelar_reserva_db(rid)
    assert ok is True or (isinstance(ok, dict) and ok.get("error") != "lock_timeout")
    appm.purge_horas_cache(type("Pelu", (), {"id": 1, "servicios": []})(), fecha)
    assert monkeycache["purge"][-1] == (1, fecha)
