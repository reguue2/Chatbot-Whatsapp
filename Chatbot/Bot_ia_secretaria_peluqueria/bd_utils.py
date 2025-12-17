# bd_utils.py — versión compatible con retornos int/bool
# Concurrencia:
# - Advisory lock por (peluqueria_id, fecha) con GET_LOCK/RELEASE_LOCK (no-op si no existe)
# - Bloqueos row-level con with_for_update() en Peluqueria, Servicio y Reservas del día
# - Transacción controlada explícitamente con commit/rollback (sin context manager)
# - Retornos preservados: int | True | False

from __future__ import annotations

import logging
from datetime import datetime, time
from typing import List, Optional

from sqlalchemy import text
from sqlalchemy.orm import selectinload

from settings import settings
from db import SessionLocal
from models import Reserva, Servicio, Peluqueria
from reserva_utils import _overlap


# -------- utilidades de locking por (peluqueria, fecha) --------
def _lock_key(peluqueria_id: int, fecha_iso: str) -> str:
    return f"resv:{peluqueria_id}:{fecha_iso}"

def _is_mysql(db) -> bool:
    try:
        return (db.bind.dialect.name or "").lower() == "mysql"
    except Exception:
        return False

def _acquire_lock(db, key: str, timeout: int = 5) -> bool:
    """
    Intenta GET_LOCK(key, timeout) solo en MySQL.
    En SQLite (u otros) hace no-op y devuelve True.
    Si GET_LOCK no existe, hace no-op salvo que STRICT_LOCKS=True.
    """
    # No-op en SQLite/otros
    if not _is_mysql(db):
        return True

    try:
        val = db.execute(text("SELECT GET_LOCK(:k, :t)"), {"k": key, "t": timeout}).scalar()
        # MySQL: 1=OK, 0=timeout, NULL=error
        return val == 1
    except Exception:
        # Si STRICT_LOCKS=True (prod duro) → falla; si False (dev) → no-op
        return not getattr(settings, "STRICT_LOCKS", True)

def _release_lock(db, key: str) -> None:
    if not _is_mysql(db):
        return
    try:
        db.execute(text("SELECT RELEASE_LOCK(:k)"), {"k": key})
    except Exception:
        pass

def _contar_solapes(reservas_dia: List[Reserva], start_hora, dur_min: int) -> int:
    """
    Cuenta reservas que solapan con la franja [start_hora, start_hora+dur_min).
    - start_hora: datetime.time o "HH:MM" (lo convierte _overlap)
    - dur_min: minutos de la reserva candidata (int)
    """
    solapadas = 0
    for r in reservas_dia:
        # duración de la reserva existente
        d_exist = getattr(r, "duracion_min", None)
        if d_exist is None:
            d_exist = getattr(getattr(r, "servicio", None), "duracion_min", 0)
        if _overlap(start_hora, int(dur_min or 0), r.hora, int(d_exist or 0)):
            solapadas += 1
    return solapadas

def _to_time(hhmm: str) -> time:
    return datetime.strptime(hhmm, "%H:%M").time()

def _mins(t: time) -> int:
    return t.hour * 60 + t.minute

def _slot_keys(peluqueria_id: int, fecha_str: str, hora_str: str, dur_min: int, step_min: int) -> list[str]:
    """
    Devuelve las claves de lock de TODOS los slots que ocupa la reserva.
    Ej: 10:00 + 30' con step 15' => [600, 615]  (minutos desde 00:00)
    """
    if step_min <= 0:
        step_min = 15
    t0 = _to_time(hora_str)
    start = _mins(t0)
    end = start + int(dur_min or 30)
    keys = []
    m = start
    while m < end:
        keys.append(f"slot:{peluqueria_id}:{fecha_str}:{m:04d}")
        m += step_min
    return keys

def _acquire_locks(db, keys: list[str], timeout_sec: int = 5) -> list[str] | None:
    """
    Intenta adquirir TODOS los locks de la lista.
    Si alguno falla, libera los ya adquiridos y devuelve None.
    """
    acquired = []
    if not keys:
        return []
    # repartimos un pequeño tiempo por lock (p.ej. timeout total ÷ nº locks, mínimo 1s)
    per_lock = max(1, int(timeout_sec / max(1, len(keys))))
    for k in keys:
        if not _acquire_lock(db, k, timeout=per_lock):
            # falló: liberar los que ya tenemos
            for ak in reversed(acquired):
                try:
                    _release_lock(db, ak)
                except Exception:
                    pass
            return None
        acquired.append(k)
    return acquired

def _release_locks(db, keys: list[str]):
    for k in keys or []:
        try:
            _release_lock(db, k)
        except Exception:
            pass

# -------------------- operaciones principales --------------------
def guardar_reserva_db(
    peluqueria_id: int,
    servicio_id: int,
    nombre: str,
    telefono: str,
    fecha_str: str,   # "YYYY-MM-DD"
    hora_str: str,    # "HH:MM"
    event_id: Optional[str] = None,
):
    """
    Devuelve: ID (int) en éxito, False si no hay hueco/lock timeout, o raise en error inesperado.
    """
    db = SessionLocal()
    slot_locks = None
    try:
        # --- INPUTS ya los tienes ---
        fecha = datetime.strptime(fecha_str, "%Y-%m-%d").date()
        hora = datetime.strptime(hora_str, "%H:%M").time()

        # 2) Cargar peluquería y servicio con bloqueo
        pelu = (
            db.query(Peluqueria)
            .filter(Peluqueria.id == peluqueria_id)
            .with_for_update()
            .one()
        )
        servicio = (
            db.query(Servicio)
            .filter(Servicio.id == servicio_id)
            .with_for_update()
            .one()
        )

        duracion = int(getattr(servicio, "duracion_min", 30) or 30)
        capacidad = int(getattr(pelu, "num_peluqueros", 1) or 1)
        step = int(getattr(pelu, "rango_reservas", 15) or 15)

        # 1) 🔒 Lock por FRANJA (todos los slots que ocupa la reserva)
        slot_keys = _slot_keys(peluqueria_id, fecha_str, hora_str, duracion, step)
        slot_locks = _acquire_locks(db, slot_keys, timeout_sec=5)
        if slot_locks is None:
            return {"error": "lock_timeout"}

        # 3) Traer reservas del día con bloqueo (para conteo de solapes)
        reservas_dia = (
            db.query(Reserva)
            .options(selectinload(Reserva.servicio))
            .filter(
                Reserva.peluqueria_id == peluqueria_id,
                Reserva.fecha == fecha,
                Reserva.estado == "confirmada",
            )
            .with_for_update()
            .all()
        )

        # 4) Contar solapes vs capacidad
        if _contar_solapes(reservas_dia, hora, duracion) >= capacidad:
            db.rollback()
            return {"error": "no_slot"}

        # 5) Insert atómico (igual que antes)
        nueva = Reserva(
            peluqueria_id=peluqueria_id,
            servicio_id=servicio_id,
            nombre_cliente=nombre,
            telefono=telefono,
            fecha=fecha,
            hora=hora,
            estado="confirmada",
            event_id=event_id,
        )
        if hasattr(nueva, "duracion_min"):
            setattr(nueva, "duracion_min", duracion)
        if hasattr(nueva, "precio") and hasattr(servicio, "precio"):
            setattr(nueva, "precio", getattr(servicio, "precio"))

        db.add(nueva)
        db.flush()
        db.refresh(nueva)
        db.commit()
        return int(nueva.id)

    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise
    finally:
        # 🔓 liberar SIEMPRE los locks de slots (no del día)
        try:
            _release_locks(db, slot_locks or [])
        except Exception:
            pass
        db.close()

def set_event_id_db(reserva_id: int, event_id: str) -> bool:
    db = SessionLocal()
    lock_key = None
    try:
        base = db.query(Reserva).filter(Reserva.id == reserva_id).one_or_none()
        if not base:
            return False
        lock_key = _lock_key(base.peluqueria_id, base.fecha.strftime("%Y-%m-%d"))
        _acquire_lock(db, lock_key, timeout=4)
        r = (
            db.query(Reserva)
            .filter(Reserva.id == reserva_id)
            .with_for_update()
            .one_or_none()
        )
        if not r:
            db.rollback()
            return False
        r.event_id = event_id
        try:
            if hasattr(r, "updated_at"):
                r.updated_at = datetime.utcnow()
        except Exception:
            pass
        db.commit()
        return True
    except Exception:
        try: db.rollback()
        except Exception: pass
        raise
    finally:
        if lock_key:
            _release_lock(db, lock_key)
        db.close()


def cancelar_reserva_db(reserva_id: int):
    """
    Cancela usando ÚNICAMENTE row-lock (FOR UPDATE) sobre la propia reserva.
    Sin advisory lock de día -> nunca se quedará colgado por GET_LOCK.

    Devuelve:
      { "ok": True }                                  -> cancelada ahora
      { "ok": True, "skipped": "not_found" }          -> no existe
      { "ok": True, "skipped": "already_cancelled" }  -> ya lo estaba
      { "ok": False, "error": "unexpected", "detail": str } -> error no esperado
    """
    db = SessionLocal()
    try:
        r = (
            db.query(Reserva)
            .filter(Reserva.id == reserva_id)
            .with_for_update()            # row-level lock, suficiente para cancelar
            .one_or_none()
        )
        if not r:
            db.rollback()
            return {"ok": True, "skipped": "not_found"}

        if getattr(r, "estado", None) == "cancelada":
            db.rollback()
            return {"ok": True, "skipped": "already_cancelled"}

        r.estado = "cancelada"
        try:
            if hasattr(r, "updated_at"):
                r.updated_at = datetime.utcnow()
        except Exception:
            pass

        db.commit()
        return {"ok": True}

    except Exception as e:
        try: db.rollback()
        except Exception: pass
        logging.exception("[cancelar_reserva_db] unexpected error")
        return {"ok": False, "error": "unexpected", "detail": str(e)}

    finally:
        try: db.close()
        except Exception: pass