# peluqueros_utils.py
from typing import List, Optional, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import and_
from models import Peluqueria, Peluquero, Reserva
from datetime import date, time

def get_active_peluqueros(session: Session, peluqueria_id: int) -> List[Peluquero]:
    return (
        session.query(Peluquero)
        .filter(Peluquero.peluqueria_id == peluqueria_id, Peluquero.activo == True)
        .order_by(Peluquero.orden.asc(), Peluquero.id.asc())
        .all()
    )

def exists_peluquero(session: Session, peluqueria_id: int, peluquero_id: int) -> bool:
    return session.query(Peluquero.id).filter(
        Peluquero.peluqueria_id == peluqueria_id,
        Peluquero.id == peluquero_id,
        Peluquero.activo == True
    ).first() is not None

def check_overlap_for_peluquero(session: Session, peluqueria_id: int, peluquero_id: Optional[int],
                                fecha: date, hora: time, duracion_min: int) -> bool:
    """
    Devuelve True si existe solape parcial o total con otra reserva no cancelada.
    Si peluquero_id es None => solape global (modo antiguo).
    """
    from reserva_utils import _to_min, _overlap

    start = _to_min(hora)
    end = start + duracion_min

    q = session.query(Reserva).filter(
        Reserva.peluqueria_id == peluqueria_id,
        Reserva.fecha == fecha,
        Reserva.estado != "cancelada",
    )
    if peluquero_id is not None:
        q = q.filter(Reserva.peluquero_id == peluquero_id)

    reservas = q.all()
    for r in reservas:
        try:
            dur_exist = getattr(r, "duracion_min", None)
            if dur_exist is None:
                dur_exist = getattr(getattr(r, "servicio", None), "duracion_min", 30)
            if _overlap(hora, duracion_min, r.hora, dur_exist):
                return True
        except Exception:
            continue
    return False

def pick_any_available(session: Session, peluqueria_id: int, fecha: date, hora: time, duracion_min: int) -> Optional[Peluquero]:
    for p in get_active_peluqueros(session, peluqueria_id):
        if not check_overlap_for_peluquero(session, peluqueria_id, p.id, fecha, hora, duracion_min):
            return p
    return None