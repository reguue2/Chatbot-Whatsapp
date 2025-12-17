# reserva_utils.py

import hashlib
import logging
from datetime import datetime, time, date, timedelta
from sqlite3 import IntegrityError
from typing import List, Tuple, Optional

from sqlalchemy.orm import selectinload
import sentry_sdk

from db import SessionLocal
from models import Reserva
from time_utils import now_local
from peluqueros_utils import check_overlap_for_peluquero, pick_any_available


# ————— Helpers de tiempo —————
def _to_min(x) -> int:
    """Convierte 'HH:MM' o time -> minutos desde 00:00"""
    if isinstance(x, str):
        h, m = map(int, x.split(":")[:2])
        return h * 60 + m
    if isinstance(x, time):
        return x.hour * 60 + x.minute
    raise ValueError("Formato de hora no soportado")

def _from_min(n: int) -> str:
    n = max(0, n)
    h, m = divmod(n, 60)
    return f"{h:02d}:{m:02d}"

def _overlap(start1: str, dur1: int, start2: str, dur2: int) -> bool:
    s1, e1 = _to_min(start1), _to_min(start1) + dur1
    s2, e2 = _to_min(start2), _to_min(start2) + dur2
    return s1 < e2 and s2 < e1

# ————— Horario “09:00-14:00,16:00-20:00” -> [(inicio, fin), ...] —————
def _parse_horario(horario: str) -> List[Tuple[str, str]]:
    if not horario:
        return [("09:00", "20:00")]
    tramos = []
    for part in horario.split(","):
        part = part.strip()
        if not part:
            continue
        a, b = [p.strip() for p in part.split("-")]
        tramos.append((a, b))
    return tramos or [("09:00", "20:00")]

def _tramos_para_fecha(horario_field, fecha: date) -> List[Tuple[str, str]]:
    """
    Acepta:
      - dict estilo {"mon":["08:00-14:00","16:00-22:00"], "sat":["08:00-14:00"], ...}
      - str JSON equivalente
      - str legacy "08:00-14:00,16:00-22:00"
      - None / vacío => default
    Devuelve lista de tramos [(ini, fin), ...] para el día de 'fecha'.
    """
    def _default():
        return [("09:00", "20:00")]

    if not horario_field:
        return _default()

    try:
        import json
        data = horario_field
        if isinstance(data, str) and data.strip():
            s = data.strip()
            # si parece JSON, lo parseamos; si no, lo tratamos como legacy
            if s[0] in "{[":
                data = json.loads(s)

        if isinstance(data, dict):
            keys = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
            key = keys[fecha.weekday()]
            ranges = (
                data.get(key)
                or data.get(key.upper())
                or data.get(key.capitalize())
            )
            if not ranges:
                return []  # sin tramos para ese día

            tramos: List[Tuple[str, str]] = []
            for r in ranges:
                try:
                    a, b = [p.strip() for p in str(r).split("-", 1)]
                    tramos.append((a, b))
                except Exception:
                    continue
            return tramos or []

        # si no es dict, lo tratamos como string legacy
        return _parse_horario(str(horario_field))
    except Exception:
        # ante cualquier problema, intenta legacy
        return _parse_horario(str(horario_field))

# ————— Cálculo de horas disponibles —————
def horas_disponibles(db, peluqueria, servicio, fecha_str: str) -> list[str]:
    """
    Devuelve horas de inicio 'HH:MM' disponibles en 'fecha_str' leyendo
    la OCUPACIÓN desde Google Calendar (peluqueria.cal_id).

    Reglas:
    - Un slot es válido si las reservas solapadas < num_peluqueros.
    - Paso entre slots = peluqueria.rango_reservas (fallback 30).
    - Respeta antelaciones y tramos horarios de la peluquería.
    """
    try:
        capacidad = int(getattr(peluqueria, "num_peluqueros", 1) or 1)
        step = getattr(peluqueria, "rango_reservas", 30) or 30
        try:
            step = int(step)
        except Exception:
            step = 30

        dur = int(getattr(servicio, "duracion_min", 30) or 30)

        fecha = datetime.strptime(fecha_str, "%Y-%m-%d").date()

        # Antelaciones
        ahora = now_local(peluqueria)
        min_avance = int(getattr(peluqueria, "min_avance_min", 60) or 60)
        max_dias = int(getattr(peluqueria, "max_avance_dias", 150) or 150)

        if fecha > (ahora + timedelta(days=max_dias)).date():
            return []

        cutoff_abs_min = None
        if fecha == ahora.date():
            cutoff_abs_min = (ahora.hour * 60 + ahora.minute) + min_avance

        # Tramos de horario del día
        try:
            tramos = _tramos_para_fecha(getattr(peluqueria, "horario", ""), fecha)
        except Exception:
            # Fallback simple "09:00-14:00,16:00-20:00"
            tramos = _parse_horario(getattr(peluqueria, "horario", ""))

        # Ocupación del día desde Calendar
        from google_calendar_utils import list_event_ranges_for_day
        busy_ranges = list_event_ranges_for_day(peluqueria, fecha)  # [(HH:MM, HH:MM)]
        busy_min = [(_to_min(a), _to_min(b)) for (a, b) in busy_ranges]

        def concurrent_busy(a: int, b: int) -> int:
            """Número de eventos de Calendar que solapan [a,b)."""
            c = 0
            for x1, x2 in busy_min:
                if a < x2 and x1 < b:
                    c += 1
            return c

        slots: list[str] = []
        for ini, fin in tramos:
            start = _to_min(ini)
            end   = _to_min(fin)
            cur   = start
            while cur + dur <= end:
                if cutoff_abs_min is not None and cur < cutoff_abs_min:
                    cur += step
                    continue
                # libre si solapados reales < capacidad
                if concurrent_busy(cur, cur + dur) < capacidad:
                    slots.append(_from_min(cur))
                cur += step

        return slots

    except Exception as e:
        try:
            sentry_sdk.capture_exception(e)
        except Exception:
            pass
        import logging
        logging.error("horas_disponibles (GCal) error", exc_info=True)
        return []

def horas_disponibles_para_peluquero(db, peluqueria, servicio, peluquero_id: int, fecha_str: str) -> list[str]:
    """
    Devuelve horas disponibles SOLO para el peluquero indicado,
    combinando calendario (ocupaciones generales) + reservas de BD de ese peluquero.
    Ahora también respeta min_avance_min y max_avance_dias como horas_disponibles().
    """
    try:
        step = getattr(peluqueria, "rango_reservas", 30) or 30
        dur = int(getattr(servicio, "duracion_min", 30) or 30)
        fecha = datetime.strptime(fecha_str, "%Y-%m-%d").date()

        # --- 🕒 Validaciones de antelación ---
        ahora = now_local(peluqueria)
        min_avance = int(getattr(peluqueria, "min_avance_min", 60) or 60)
        max_dias = int(getattr(peluqueria, "max_avance_dias", 150) or 150)

        # Demasiado en el futuro
        if fecha > (ahora + timedelta(days=max_dias)).date():
            return []

        # Si es hoy, calcula el límite absoluto en minutos
        cutoff_abs_min = None
        if fecha == ahora.date():
            cutoff_abs_min = (ahora.hour * 60 + ahora.minute) + min_avance

        # Horario del día (usa los mismos helpers que horas_disponibles)
        try:
            tramos = _tramos_para_fecha(getattr(peluqueria, "horario", ""), fecha)
        except Exception:
            tramos = _parse_horario(getattr(peluqueria, "horario", ""))

        # Bloqueos desde Google Calendar (no por peluquero, sino del salón)
        from google_calendar_utils import list_event_ranges_for_day
        busy_ranges = list_event_ranges_for_day(peluqueria, fecha)
        busy_min = [(_to_min(a), _to_min(b)) for (a, b) in busy_ranges]

        def concurrent_busy(a: int, b: int) -> bool:
            """True si el rango solapa con Calendar."""
            for x1, x2 in busy_min:
                if a < x2 and x1 < b:
                    return True
            return False

        slots: list[str] = []
        for ini, fin in tramos:
            start = _to_min(ini)
            end = _to_min(fin)
            cur = start
            while cur + dur <= end:
                # ⛔ Saltar si antes del tiempo mínimo permitido
                if cutoff_abs_min is not None and cur < cutoff_abs_min:
                    cur += step
                    continue

                # ⛔ Saltar si solapa con Calendar
                if concurrent_busy(cur, cur + dur):
                    cur += step
                    continue

                hora_str = _from_min(cur)
                # ✅ Verificar solape con otras reservas de ese peluquero
                if not check_overlap_for_peluquero(
                    db, peluqueria.id, peluquero_id, fecha,
                    datetime.strptime(hora_str, "%H:%M").time(), dur
                ):
                    slots.append(hora_str)

                cur += step
        return slots

    except Exception as e:
        sentry_sdk.capture_exception(e)
        logging.error("horas_disponibles_para_peluquero error", exc_info=True)
        return []


def formatea_fecha_es(fecha_in) -> str:
    """Acepta date | datetime | 'YYYY-MM-DD' | 'dd/mm/aaaa' y devuelve '13 de septiembre de 2025'."""
    try:
        if isinstance(fecha_in, datetime):
            f = fecha_in.date()
        elif isinstance(fecha_in, date):
            f = fecha_in
        elif isinstance(fecha_in, str):
            s = fecha_in.strip()
            try:
                f = datetime.strptime(s, "%Y-%m-%d").date()   # ISO
            except ValueError:
                f = datetime.strptime(s, "%d/%m/%Y").date()   # europeo
        else:
            return str(fecha_in)
        return f.strftime("%d/%m/%Y")
    except Exception:
        return str(fecha_in)

def hhmm_str(hora_in) -> str:
    """Acepta time | 'HH:MM' y devuelve 'HH:MM'."""
    try:
        if isinstance(hora_in, time):
            return hora_in.strftime("%H:%M")
        if isinstance(hora_in, str):
            s = hora_in.strip()
            # normaliza 'H:M'/'HH:MM'
            dt = datetime.strptime(s, "%H:%M")
            return dt.strftime("%H:%M")
        return str(hora_in)
    except Exception:
        return str(hora_in)


def obtener_reserva_id_para_calendar(peluqueria, datos: dict) -> Optional[str]:
    """Determina el ``reserva_id`` que se usará en Google Calendar."""

    # 1) Si el identificador ya viene en los datos de la reserva, lo reutilizamos.
    reserva_id = datos.get("reserva_id")
    if reserva_id:
        return str(reserva_id)

    # 2) Intentamos deducir el identificador buscando una reserva existente en BD.
    try:
        pelu_id = getattr(peluqueria, "id", None)
        fecha_str = datos.get("fecha")
        hora_str = datos.get("hora")
        telefono = datos.get("telefono")
        if pelu_id and fecha_str and hora_str and telefono:
            fecha = datetime.strptime(fecha_str, "%Y-%m-%d").date()
            hora = datetime.strptime(hora_str, "%H:%M").time()
            db = SessionLocal()
            try:
                reserva_existente = (
                    db.query(Reserva)
                    .filter(
                        Reserva.peluqueria_id == pelu_id,
                        Reserva.fecha == fecha,
                        Reserva.hora == hora,
                        Reserva.telefono == telefono,
                        Reserva.estado != "cancelada",
                    )
                    .order_by(Reserva.id.desc())
                    .first()
                )
            finally:
                db.close()
            if reserva_existente:
                return str(reserva_existente.id)
    except Exception as e:
        sentry_sdk.capture_exception(e)

    # 3) Como último recurso generamos un hash estable para mantener idempotencia.
    try:
        servicio_id = datos.get("servicio_id") or getattr(datos.get("servicio"), "id", "")
        raw = "|".join(
            [
                str(getattr(peluqueria, "id", "")),
                datos.get("fecha", ""),
                datos.get("hora", ""),
                str(servicio_id or ""),
                datos.get("telefono", ""),
            ]
        )
        if raw.strip("|"):
            return hashlib.sha256(raw.encode("utf-8")).hexdigest()
    except Exception as e:
        sentry_sdk.capture_exception(e)

    # 4) Sin datos suficientes devolvemos None (se omite la propiedad privada).
    return None

def _ensure_date(fecha_in) -> date:
    if isinstance(fecha_in, date):
        return fecha_in
    if isinstance(fecha_in, datetime):
        return fecha_in.date()
    if isinstance(fecha_in, str):
        s = fecha_in.strip()
        try:
            return datetime.strptime(s, "%Y-%m-%d").date()
        except ValueError:
            return datetime.strptime(s, "%d/%m/%Y").date()
    raise ValueError("Fecha no soportada")

def _ensure_time(hora_in) -> time:
    """Normaliza a time (acepta time | 'HH:MM')."""
    if isinstance(hora_in, time):
        return hora_in
    s = hhmm_str(hora_in)  # ya tienes este helper
    return datetime.strptime(s, "%H:%M").time()

# --- FUNCIÓN: chequeo de solape con soporte a peluquero ---
def hay_solape(session, peluqueria, fecha_in, hora_in, dur_min: int, peluquero_id: Optional[int] = None) -> bool:
    """
    Devuelve True si hay solape en BD:
      - Con selección de peluquero activada: solapa por peluquero concreto.
      - Sin selección activada: solape global (compatibilidad modo antiguo).
    """
    fecha = _ensure_date(fecha_in)
    hora = _ensure_time(hora_in)
    enable_sel = bool(getattr(peluqueria, "enable_peluquero_selection", False))
    target_peluquero_id = peluquero_id if enable_sel else None
    return check_overlap_for_peluquero(session, peluqueria.id, target_peluquero_id, fecha, hora, dur_min)

# --- FUNCIÓN: creación de reserva con peluquero (si aplica) ---
def crear_reserva(session, peluqueria, datos: dict):
    """
    datos esperados:
      'fecha': date | 'YYYY-MM-DD' | 'dd/mm/aaaa'
      'hora': time | 'HH:MM'
      'duracion_min': int (opcional; si no viene se intenta tomar de 'servicio' o 30)
      'telefono': str
      'nombre': str (opcional)
      'servicio_id': int (opcional si pasas 'servicio')
      'servicio': obj con .id y .duracion_min (opcional)
      'peluquero_id': Optional[int] (opcional)
    """
    # Normalización de entradas
    fecha = _ensure_date(datos.get("fecha"))
    hora = _ensure_time(datos.get("hora"))

    # Duración
    dur = datos.get("duracion_min")
    if dur is None:
        servicio = datos.get("servicio")
        if servicio is not None:
            try:
                dur = int(getattr(servicio, "duracion_min", 30) or 30)
            except Exception:
                dur = 30
        else:
            try:
                dur = int(datos.get("duracion_min", 30) or 30)
            except Exception:
                dur = 30

    # Peluquero (si procede)
    peluquero_id = datos.get("peluquero_id")
    enable_sel = bool(getattr(peluqueria, "enable_peluquero_selection", False))
    required_sel = bool(getattr(peluqueria, "peluquero_selection_required", False))

    if enable_sel and peluquero_id is None:
        if required_sel:
            raise ValueError("Debes elegir peluquero.")
        # No es requerido: intentamos asignar automáticamente uno libre
        asignado = pick_any_available(session, peluqueria.id, fecha, hora, dur)
        peluquero_id = asignado.id if asignado else None

    # Chequeo de solape (por peluquero si aplica)
    if hay_solape(session, peluqueria, fecha, hora, dur, peluquero_id):
        if enable_sel:
            raise ValueError("La hora seleccionada ya no está disponible para ese peluquero.")
        else:
            raise ValueError("La hora seleccionada ya no está disponible.")

    # Construcción de la reserva
    servicio_id = datos.get("servicio_id")
    if servicio_id is None and datos.get("servicio") is not None:
        try:
            servicio_id = int(getattr(datos["servicio"], "id"))
        except Exception:
            servicio_id = None

    r = Reserva(
        peluqueria_id=getattr(peluqueria, "id"),
        fecha=fecha,
        hora=hora,
        telefono=datos.get("telefono"),
        nombre=datos.get("nombre"),
        servicio_id=servicio_id,
        peluquero_id=peluquero_id,
    )
    try:
        session.flush()  # asegura r.id
    except IntegrityError:
        session.rollback()
        # Mensaje claro si alguien cogió el hueco justo antes
        if peluquero_id:
            raise ValueError("Esa hora ya se ha ocupado para ese profesional. Elige otra hora.")
        else:
            raise ValueError("Esa hora acaba de ocuparse. Prueba con otra.")
    return r