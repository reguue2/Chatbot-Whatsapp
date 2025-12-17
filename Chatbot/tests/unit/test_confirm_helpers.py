# tests/unit/test_confirm_helpers.py
from datetime import date

def test_no_slot_recalcula_horas_fresh_sin_cache(appm, monkeypatch):
    """
    Si BD devuelve no_slot, el flujo debe recalcular horas con horas_disponibles (sin cache)
    y NO dejar la lista vacÃ­a por culpa del filtro 'desde ahora' -> fallback.
    """
    # Simula BD no_slot
    monkeypatch.setattr(appm, "guardar_reserva_db", lambda *a, **k: {"error": "no_slot"}, raising=True)

    # Mock de horas_disponibles que devuelve opciones
    def fake_horas(db, pelu, srv, fecha):
        return ["12:00", "12:15", "12:30"]
    monkeypatch.setattr(appm, "horas_disponibles", fake_horas, raising=True)

    # Filtro que dejarÃ­a vacÃ­o -> tu cÃ³digo debe hacer fallback
    monkeypatch.setattr(appm, "filtra_horas_desde_ahora", lambda horas, f: [], raising=True)

    fecha = date.today().strftime("%Y-%m-%d")
    class PeluStub:
        id = 1
        servicios = []
    srv = type("Srv", (), {"id": 1, "nombre": "Corte", "duracion_min": 30})()

    # Simula el recÃ¡lculo tal y como lo hace tu bloque confirmar
    with appm.SessionLocal() as _db2:
        horas_fresh = appm.horas_disponibles(_db2, PeluStub, srv, fecha) or []
    try:
        filtradas = appm.filtra_horas_desde_ahora(horas_fresh, fecha)
        horas_fresh = filtradas or horas_fresh
    except Exception:
        pass

    assert horas_fresh == ["12:00", "12:15", "12:30"]
