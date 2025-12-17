"""Utilidades para interactuar con Google Calendar (aware por peluquería)."""
from __future__ import annotations

import logging
from time import sleep
from typing import Optional, List, Tuple
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import sentry_sdk
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2 import service_account

from settings import settings

# === Config base ===
SCOPES = ['https://www.googleapis.com/auth/calendar']
SERVICE_ACCOUNT_FILE = settings.GOOGLE_SERVICE_ACCOUNT_FILE
FALLBACK_TZ = getattr(settings, "CAL_TZ", "Europe/Madrid")


# --- Helpers de TZ ---
def tz_of(peluqueria) -> str:
    """
    Devuelve la TZ a usar para la peluquería, con fallback a settings.CAL_TZ.
    """
    return getattr(peluqueria, "tz", None) or FALLBACK_TZ


def to_aware(dt: datetime, tz_name: str) -> datetime:
    """
    Asegura que 'dt' sea timezone-aware en la TZ indicada.
    Si ya es aware se respeta tal cual.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ZoneInfo(tz_name))
    return dt


def parse_iso_dt(s: str) -> Optional[datetime]:
    """
    Parsea un ISO-8601 devuelto por Google (suele venir con offset).
    Devuelve aware datetime o None.
    """
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


# --- Google Service ---
def get_calendar_service():
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    # cache_discovery=False evita warnings en serverless/containers
    return build('calendar', 'v3', credentials=creds, cache_discovery=False)


def _retry(callable_execute, retries: int = 3):
    for i in range(retries):
        try:
            return callable_execute()
        except HttpError as e:
            sentry_sdk.capture_exception(e)
            status = getattr(e, "status_code", None) or getattr(getattr(e, "resp", None), "status", None)
            try:
                status = int(status)
            except Exception as e2:
                sentry_sdk.capture_exception(e2)
                status = None
            if status in (429, 500, 502, 503, 504):
                sleep(0.5 * (2 ** i))
                continue
            logging.error(f"Calendar HttpError: {e}", exc_info=True)
            break
        except Exception as e:
            sentry_sdk.capture_exception(e)
            logging.error(f"Calendar error: {e}", exc_info=True)
            if i < retries - 1:
                sleep(0.5 * (2 ** i))
                continue
            break
    return None


# --- Búsquedas ---
def _find_event_by_reserva_id(service, calendar_id: str, reserva_id: str) -> Optional[dict]:
    """Busca un evento existente filtrando por la propiedad privada ``reserva_id``."""
    reserva_prop = f"reserva_id={reserva_id}"

    def _execute_list():
        return service.events().list(
            calendarId=calendar_id,
            privateExtendedProperty=reserva_prop,
            maxResults=1,
            singleEvents=True,
            showDeleted=False,
        ).execute()

    result = _retry(_execute_list)
    if not result:
        return None
    items = result.get("items") or []
    return items[0] if items else None


# === Lectura de ocupaciones por día ===
def list_event_ranges_for_day(peluqueria, fecha):
    """
    Devuelve [(HH:MM, HH:MM), ...] con los rangos OCUPADOS en 'fecha'
    para el calendar 'peluqueria.cal_id'.

    - Ignora eventos cancelados
    - Un evento all-day bloquea 00:00-23:59
    - Usa la zona horaria REAL de la peluquería para construir timeMin/timeMax (RFC3339 con offset)
    """
    try:
        # ✅ Validación de calendar id
        if not getattr(peluqueria, "cal_id", None):
            return []

        service = get_calendar_service()

        tzname = tz_of(peluqueria)
        tz = ZoneInfo(tzname)
        start_dt = datetime(fecha.year, fecha.month, fecha.day, 0, 0, tzinfo=tz)
        end_dt = start_dt + timedelta(days=1)

        resp = _retry(
            service.events()
            .list(
                calendarId=peluqueria.cal_id,
                timeMin=start_dt.isoformat(),  # ej. 2025-10-08T00:00:00-03:00
                timeMax=end_dt.isoformat(),    # ej. 2025-10-09T00:00:00-03:00
                singleEvents=True,
                orderBy="startTime",
                showDeleted=False,
                maxResults=2500,
            )
            .execute
        )
        items = (resp or {}).get("items", [])
        ranges: List[Tuple[str, str]] = []
        for ev in items:
            if ev.get("status") == "cancelled":
                continue
            start = ev.get("start", {})
            end = ev.get("end", {})
            s_dt = start.get("dateTime")
            e_dt = end.get("dateTime")
            if not s_dt or not e_dt:
                # Evento all-day → bloquea all day
                if start.get("date") and end.get("date"):
                    ranges.append(("00:00", "23:59"))
                continue

            # Mostramos horas HH:MM en la TZ del evento (Google ya da dateTime con offset)
            try:
                ranges.append((s_dt[11:16], e_dt[11:16]))
            except Exception:
                # fallback robusto por si cambia formato
                s_parsed = parse_iso_dt(s_dt)
                e_parsed = parse_iso_dt(e_dt)
                if s_parsed and e_parsed:
                    ranges.append((s_parsed.strftime("%H:%M"), e_parsed.strftime("%H:%M")))
        return ranges
    except Exception as e:
        sentry_sdk.capture_exception(e)
        logging.error("list_event_ranges_for_day error", exc_info=True)
        return []


# --- util de listado acotado ---
def _list_events_between(service, calendar_id: str, start_dt: datetime, end_dt: datetime):
    start_iso = start_dt.isoformat()
    end_iso = end_dt.isoformat()
    return _retry(
        service.events().list(
            calendarId=calendar_id,
            timeMin=start_iso,
            timeMax=end_iso,
            singleEvents=True,
            orderBy="startTime",
            showDeleted=False,
            maxResults=2500,
        ).execute
    ) or {"items": []}


# === Conteo de solapamientos (calendar capacity) ===
def _count_overlaps_calendar(service, calendar_id: str, start_dt_local: datetime, dur_min: int, tzname: str) -> int:
    """
    Cuenta eventos que solapan con [start_dt_local, start_dt_local+dur).
    start_dt_local debe ser AWARE en la TZ de la peluquería (tzname).
    Comparamos como datetime (no strings) para evitar errores por offsets distintos.
    """
    start_dt_local = to_aware(start_dt_local, tzname)
    end_dt_local = start_dt_local + timedelta(minutes=int(dur_min or 30))

    resp = _list_events_between(service, calendar_id, start_dt_local, end_dt_local)
    cnt = 0
    for ev in resp.get("items", []) or []:
        if ev.get("status") == "cancelled":
            continue
        st = ev.get("start", {})
        en = ev.get("end", {})
        s_dt = st.get("dateTime")
        e_dt = en.get("dateTime")

        if not s_dt or not e_dt:
            # All-day entre start/end → lo consideramos superpuesto
            if st.get("date") and en.get("date"):
                cnt += 1
            continue

        s_parsed = parse_iso_dt(s_dt)
        e_parsed = parse_iso_dt(e_dt)
        if not s_parsed or not e_parsed:
            # Si no podemos parsear, sé conservador y no cuentes
            continue

        # Solapamiento real:
        # [s_parsed, e_parsed) vs [start_dt_local, end_dt_local)
        if not (e_parsed <= start_dt_local or end_dt_local <= s_parsed):
            cnt += 1
    return cnt


# === CRUD de eventos ===
def crear_reserva_google_idempotente(peluqueria, datos, private_key: str):
    """
    Crea (o actualiza si ya existe por gkey) un evento en Google Calendar
    respetando la TZ de la peluquería. SOLO añade el NOMBRE del peluquero
    (si procede) al summary y a la description. No guarda IDs extra.
    """
    try:
        # ✅ Validación de calendar id
        if not getattr(peluqueria, "cal_id", None):
            return {"success": False, "error": "missing_calendar_id"}

        service = get_calendar_service()

        # --- Entradas esperadas en 'datos' ---
        # datos['fecha']: 'YYYY-MM-DD'
        # datos['hora'] : 'HH:MM'
        # datos['servicio']: objeto con .nombre y .duracion_min (opcional)
        # datos['nombre'] (cliente), datos['telefono']
        # datos['peluquero_nombre'] o datos['peluquero']  (opcional)
        # datos['reserva_id'] (opcional)  <-- NUEVO: si está, lo guardamos en propiedades privadas

        start_dt_str = f"{datos['fecha']}T{datos['hora']}:00"
        servicio_nombre = getattr(datos.get("servicio"), "nombre", "Servicio")
        dur_min = int(getattr(datos.get("servicio"), "duracion_min", 30) or 30)

        # Nombre del profesional si viene (no guardamos IDs)
        peluquero_nombre = datos.get("peluquero_nombre") or datos.get("peluquero")

        tzname = tz_of(peluqueria)
        tz = ZoneInfo(tzname)

        # Aware local para conteos/chequeos
        start_dt_local = datetime.fromisoformat(start_dt_str)
        if start_dt_local.tzinfo is None:
            start_dt_local = start_dt_local.replace(tzinfo=tz)
        end_dt_local = start_dt_local + timedelta(minutes=dur_min)

        # Construcción summary / description
        summary = f"{servicio_nombre} - {datos['nombre']}"
        if peluquero_nombre:
            summary += f" · {peluquero_nombre}"

        description = f"Teléfono: {datos['telefono']}"
        if peluquero_nombre:
            description += f"\nProfesional: {peluquero_nombre}"

        # 1) Buscar si ya existe evento por gkey (idempotencia)
        try:
            resp = service.events().list(
                calendarId=peluqueria.cal_id,
                privateExtendedProperty=f"gkey={private_key}",
                maxResults=1,
                singleEvents=True,
            ).execute()
            items = resp.get("items", [])
            if items:
                # Ya existe → actualizamos título/description y aseguramos propiedades privadas
                existing = items[0]
                # Merge de propiedades privadas existentes con gkey y (opcional) reserva_id
                existing_private = (existing.get("extendedProperties", {}) or {}).get("private", {}) or {}
                existing_private.setdefault("gkey", private_key)
                if datos.get("reserva_id"):
                    existing_private["reserva_id"] = str(datos["reserva_id"])

                patch_body = {
                    "summary": summary,
                    "description": description,
                    "extendedProperties": {
                        "private": existing_private
                    },
                }
                _retry(service.events().patch(
                    calendarId=peluqueria.cal_id,
                    eventId=existing["id"],
                    body=patch_body,
                    sendUpdates="none",
                ).execute)
                return {"success": True, "event_id": existing["id"]}
        except HttpError:
            # si falla la búsqueda, continuamos con inserción
            pass

        # 2) (Opcional) Pre-conteo de capacidad usando Calendar
        try:
            _ = _count_overlaps_calendar(service, peluqueria.cal_id, start_dt_local, dur_min, tzname)
        except Exception:
            pass  # sólo informativo

        # 3) Crear evento nuevo
        def _compute_end_iso(start_iso: str, minutes: int) -> str:
            start_tmp = datetime.fromisoformat(start_iso)
            return (start_tmp + timedelta(minutes=int(minutes))).isoformat()

        # Propiedades privadas (idempotencia gkey + opcional reserva_id)
        private_props = {"gkey": private_key}
        if datos.get("reserva_id"):
            private_props["reserva_id"] = str(datos["reserva_id"])

        body = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start_dt_str, "timeZone": tzname},
            "end":   {"dateTime": _compute_end_iso(start_dt_str, dur_min), "timeZone": tzname},
            "extendedProperties": {"private": private_props},
        }

        created = service.events().insert(
            calendarId=peluqueria.cal_id,
            body=body,
            sendUpdates="none",
        ).execute()
        ev_id = created.get("id")

        # 4) Post-chequeo de capacidad (capacidad = num_peluqueros)
        try:
            capacidad = max(1, int(getattr(peluqueria, "num_peluqueros", 1)))
        except Exception:
            capacidad = 1

        post_cnt = _count_overlaps_calendar(service, peluqueria.cal_id, start_dt_local, dur_min, tzname)
        if post_cnt > capacidad:
            # carrera: revertir
            try:
                _retry(service.events().delete(calendarId=peluqueria.cal_id, eventId=ev_id).execute)
            except Exception:
                pass
            return {"success": False, "error": "no_slot_calendar_capacity"}

        return {"success": True, "event_id": ev_id}

    except Exception as e:
        try:
            sentry_sdk.capture_exception(e)
        except Exception:
            pass
        logging.error(f"crear_reserva_google_idempotente error: {e}", exc_info=True)
        return {"success": False, "error": str(e)}



def cancelar_reserva_google(peluqueria, event_id: str):
    try:
        if not event_id:
            return {"success": False, "error": "missing_event_id"}
        # ✅ Validación de calendar id
        if not getattr(peluqueria, "cal_id", None):
            return {"success": False, "error": "missing_calendar_id"}
        service = get_calendar_service()
        resp = _retry(service.events().delete(calendarId=peluqueria.cal_id, eventId=event_id).execute)
        return {"success": resp is not None}
    except Exception as e:
        sentry_sdk.capture_exception(e)
        logging.error(f"cancelar_reserva_google error: {e}", exc_info=True)
        return {"success": False}
