# ================================================
# Bot IA Secretaria Peluquería (PROD-READY)
# ================================================
# Endurecido para producción:
# - API key solo por cabecera
# - Logging con rotación
# - /ready real con SQLAlchemy 2.x (text)
# - Flujos robustos + comandos globales
# - Corrección: flujos "modificar" y "cancelar" NO anidados
# ================================================
import hashlib
import math
import re
import hmac
import logging
import os
import json
import time as _time
from zoneinfo import ZoneInfo

import requests
import sentry_sdk
import unicodedata
from hashlib import sha256
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone, timedelta, date
from typing import Any, Optional

import openai
from flask import Flask, request, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from requests.exceptions import ReadTimeout
from sqlalchemy.orm import selectinload

from Bot_ia_secretaria_peluqueria.peluqueros_utils import get_active_peluqueros
# --- Dominio ---
from models import Reserva, Peluqueria
from interpretador_ia import interpreta_ia, interpreta_telefono, interpreta_hora, interpreta_fecha
from bd_utils import (
    guardar_reserva_db,
    cancelar_reserva_db, set_event_id_db
)
from reserva_utils import horas_disponibles, formatea_fecha_es, horas_disponibles_para_peluquero
from google_calendar_utils import (
    cancelar_reserva_google, crear_reserva_google_idempotente,
)
from db import SessionLocal
from settings import settings
from storage import get_storage
from routers.health import bp as health_bp
from time_utils import now_local
from concurrent.futures import ThreadPoolExecutor


# ================================================
# Configuración base
# ================================================
app = Flask(__name__)
app.register_blueprint(health_bp)

limiter = Limiter(
    get_remote_address,
    app=app,
    storage_uri=settings.REDIS_URL,
    default_limits=[],
)

try:
    import sentry_sdk
    from sentry_sdk.integrations.flask import FlaskIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration

    _dsn = os.environ.get("SENTRY_DSN")
    if _dsn:
        sentry_sdk.init(
            dsn=_dsn,
            integrations=[FlaskIntegration(), LoggingIntegration(level=None, event_level=None)],
            traces_sample_rate=0.1,       # métricas de rendimiento (puedes bajar a 0.0 si no quieres)
            profiles_sample_rate=0.0,     # perfiles desactivados
            environment=os.environ.get("FLASK_ENV", "production")
        )
except Exception as e:
    sentry_sdk.capture_exception(e)
    pass

app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # Tamaño de petición: 2 MB

storage = get_storage(settings)

# Ejecuta el loopback al core fuera del hilo del webhook para no bloquear WhatsApp.
CORE_EXECUTOR = ThreadPoolExecutor(max_workers=2)

@app.errorhandler(429)
def handle_rate_limit(_):
    return jsonify({"ok": False, "error": "rate_limited"}), 429

# Logging producción: fichero rotado + consola (evita PII en INFO)
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

file_handler = RotatingFileHandler("bot_peluqueria.log", maxBytes=2_000_000, backupCount=5)
file_handler.setFormatter(fmt)
root_logger.handlers.clear()
root_logger.addHandler(file_handler)

console_handler = logging.StreamHandler()
console_handler.setFormatter(fmt)
root_logger.addHandler(console_handler)

# OpenAI (puede usarse en módulos importados)
openai.api_key = settings.OPENAI_API_KEY

# Idempotencia
IDEMPOTENCY_TTL = 600  # 10 min


# =======================
# helpers Sentry
# =======================
from contextlib import contextmanager

def sentry_bind(pelu=None, session_id: str | None = None):
    # Tags estables por conversación/tenant (sin PII)
    with sentry_sdk.configure_scope() as scope:
        if session_id:
            scope.set_tag("session.id", session_id)
        if pelu is not None:
            scope.set_tag("tenant.id", getattr(pelu, "id", None))
            scope.set_tag("tenant.name", getattr(pelu, "nombre", None))

def sentry_event(event: str, level: str = "info", **extras):
    # Evento con extras sin romper el scope global
    with sentry_sdk.push_scope() as scope:
        for k, v in (extras or {}).items():
            scope.set_extra(k, v)
        sentry_sdk.capture_message(event, level=level)

@contextmanager
def sentry_span(op: str, description: str = ""):
    span = sentry_sdk.start_span(op=op, description=description)
    try:
        yield span
        span.set_status("ok")
    except Exception:
        span.set_status("internal_error")
        raise
    finally:
        span.finish()

# ================================================
# Helpers: Idempotencia
# ================================================
def _idem_key(action: str, pelu_id: int, payload: dict, explicit_key: Optional[str]) -> str:
    base = explicit_key or (
        f"{action}:{pelu_id}:"
        f"{payload.get('fecha')}:{payload.get('hora')}:"
        f"{payload.get('servicio_id')}:{payload.get('telefono')}:{payload.get('reserva_id')}"
    )
    return sha256(base.encode("utf-8")).hexdigest()


def idem_get(action: str, pelu_id: int, payload: dict, explicit_key: Optional[str] = None):
    k = _idem_key(action, pelu_id, payload, explicit_key)
    raw = storage.get(f"idemp:{k}")
    if not raw:
        return k, None
    try:
        cached = json.loads(raw)  # {"status": int, "json": {...}}
    except Exception as e:
        sentry_sdk.capture_exception(e)
        cached = None
    return k, cached


def idem_set(key: str, status: int, json_body: dict) -> None:
    payload = json.dumps({"status": status, "json": json_body}, ensure_ascii=False)
    storage.setex(f"idemp:{key}", payload, ttl=IDEMPOTENCY_TTL)


# ================================================
# Constantes / léxico conversación
# ================================================
AM_WORDS = {"am", "mañana", "de la mañana", "por la mañana", "mñna", "mañna", "mñn"}
PM_WORDS = {"pm", "tarde", "noche", "de la tarde", "por la tarde", "de la noche"}
MIN_WORDS = {"cinco": 5, "diez": 10, "quince": 15, "veinte": 20, "veinticinco": 25, "cuarto": 15, "media": 30}

DIAS_EN_ES = {
    "monday": "lunes", "tuesday": "martes", "wednesday": "miércoles",
    "thursday": "jueves", "friday": "viernes", "saturday": "sábado", "sunday": "domingo"
}
AFFIRM_WORDS = {
    "si", "sí", "vale", "ok", "okay", "okey", "de acuerdo",
    "correcto", "confirmo", "perfecto", "claro", "por supuesto",
    "obvio", "así es", "exacto", "cierto", "seguro", "afirmativo",
    "dale", "hecho", "va", "venga", "eso es"
}
DENIAL_WORDS = {
    "no", "nunca", "negativo", "para nada", "en absoluto",
    "que va", "de ninguna manera", "imposible"
}

# Comandos globales
CMD_MENU = {"menu", "inicio", "start", "empezar", "home"}
CMD_RESET = {"reiniciar", "reset", "empezar de cero", "empezar de nuevo"}
CMD_SALIR = {"salir", "parar", "stop", "abortar", "cancelar flujo", "cancelar operacion"}
CMD_VOLVER = {"volver", "atras", "atrás", "back"}

_INTENT_MAP = {
    "reservar": "reservar",
    "reserva": "reservar",
    "quiero reservar": "reservar",
    "pedir cita": "reservar",
    "sacar cita": "reservar",
    "cancelar": "cancelar",
    "cancelar cita": "cancelar",
    "anular": "cancelar",
    "anular cita": "cancelar",
    "quiero cancelar": "cancelar",
    "quiero cancelar una reserva": "cancelar",
    "cancelar una reserva": "cancelar",
    "anular reserva": "cancelar",
    "duda": "duda",
    "ayuda": "duda",
    "pregunta": "duda",
    "consulta": "duda",
}

# ================================================
# Helpers varios
# ================================================
def hhmm_str(value) -> str:
    """Normaliza objetos/strings/tuplas a 'HH:MM'."""
    if value is None:
        return ""
    try:
        if hasattr(value, "strftime"):
            return value.strftime("%H:%M")
    except Exception as e:
        sentry_sdk.capture_exception(e)
        pass
    if isinstance(value, str):
        s = value.strip()
        m = re.match(r"^(\d{1,2})(?::(\d{1,2}))?(?::\d{1,2})?$", s)
        if m:
            h = int(m.group(1))
            mm = int(m.group(2) or 0)
            if 0 <= h <= 23 and 0 <= mm <= 59:
                return f"{h:02d}:{mm:02d}"
        return s
    try:
        h, m = value
        h = int(h)
        m = int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return f"{h:02d}:{m:02d}"
    except Exception as e:
        sentry_sdk.capture_exception(e)
        pass
    return str(value)


def _format_hm(h: int, m: int) -> str:
    return f"{h:02d}:{m:02d}"


def _has_am_pm(texto: str) -> str | None:
    t = (texto or "").lower()
    if any(w in t for w in AM_WORDS):
        return "am"
    if any(w in t for w in PM_WORDS):
        return "pm"
    return None

def _infer_step(horas_libres: list[str]) -> int | None:
    """Infiero el paso entre horas (ej. 15/30 min) a partir de las dos primeras horas libres."""
    try:
        if len(horas_libres) >= 2:
            t1 = datetime.strptime(horas_libres[0], "%H:%M")
            t2 = datetime.strptime(horas_libres[1], "%H:%M")
            return int((t2 - t1).total_seconds() // 60)
    except Exception as e:
        sentry_sdk.capture_exception(e)
        pass
    return None


def _suggestions(horas_libres: list[str], ref: str, motivo: str, n: int = 4) -> dict:
    """Devuelve sugerencias cercanas a 'ref' dentro de 'horas_libres'."""
    try:
        def mins(hhmm: str) -> int:
            h, m = map(int, hhmm.split(":"))
            return h * 60 + m

        ordenadas = sorted(horas_libres, key=lambda x: abs(mins(x) - mins(ref)))
        suger = ordenadas[:n]
        if horas_libres:
            first = horas_libres[0]
            last = horas_libres[-1]
            _ = _infer_step(horas_libres) or 15
            if ref < first:
                motivo = f"A esa hora no hay huecos. La primera disponible es {first}."
            elif ref > last:
                motivo = f"A esa hora no damos citas ese día. La última disponible es {last}."
            else:
                motivo = "Por favor, elige una hora de las disponibles."
        return {"ok": False, "sugerencias": suger, "motivo": motivo}
    except Exception as e:
        sentry_sdk.capture_exception(e)
        return {"ok": False, "sugerencias": horas_libres[:n], "motivo": motivo or "Esa hora no está disponible."}


def _parse_ambiguous_text(texto: str):
    """Parsea horas '12h' ambiguas en español ('y cuarto', 'menos', etc.)."""
    t = (texto or "").lower().strip()
    if _has_am_pm(t):
        return None
    m = re.search(
        r"\b(?:a\s+las?\s+|las?\s+)?(\d{1,2})\s*menos\s*(\d{1,2}|media|cuarto|veinticinco|veinte|quince|diez|cinco)\b", t
    )
    if m:
        h = int(m.group(1))
        if 1 <= h <= 12:
            part = m.group(2)
            sub = int(part) if part.isdigit() else MIN_WORDS[part]
            if 1 <= sub <= 59:
                minutes = (60 - sub) % 60
                h = 12 if h == 1 else h - 1
                return h, minutes
    m = re.search(
        r"\b(?:a\s+las?\s+|las?\s+)?(\d{1,2})\s*y\s*(\d{1,2}|media|cuarto|veinticinco|veinte|quince|diez|cinco)\b", t
    )
    if m:
        h = int(m.group(1))
        if 1 <= h <= 12:
            part = m.group(2)
            minutes = int(part) if part.isdigit() else MIN_WORDS[part]
            if 0 <= minutes <= 59:
                return h, minutes
    m = re.search(r"\b(?:a\s+las?\s+|las?\s+)?(\d{1,2})\s*[:h.]\s*(\d{1,2})\b", t)
    if m:
        h = int(m.group(1))
        mm = int(m.group(2))
        if 1 <= h <= 12 and 0 <= mm <= 59:
            return h, mm
    m = re.search(r"\b(?:a\s+las?\s+|las?\s+)?(\d{1,2})\b", t)
    if m:
        h = int(m.group(1))
        if 1 <= h <= 12:
            return h, 0
    return None


def normaliza_hora_ia(mensaje: str):
    """Normaliza una entrada natural a una hora en 24h, detectando ambigüedad AM/PM."""
    amb_guess = _parse_ambiguous_text(mensaje)
    if amb_guess:
        h, m = amb_guess
        return {"h": 12 if h == 12 else h, "m": m, "clue": None, "ambigua": True}
    try:
        ai = interpreta_hora(mensaje)
    except Exception as ex:
        sentry_sdk.capture_exception(ex)
        ai = None
    if isinstance(ai, str) and ":" in ai:
        try:
            h, m = map(int, ai.split(":", 1))
            if 0 <= h <= 23 and 0 <= m <= 59:
                return {"h": h, "m": m, "clue": None, "ambigua": False}
        except Exception as ex:
            sentry_sdk.capture_exception(ex)
            pass
    if hasattr(ai, "hour") and hasattr(ai, "minute"):
        h = int(ai.hour)
        m = int(ai.minute)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return {"h": h, "m": m, "clue": None, "ambigua": False}
    h = m = None
    clue = None
    if isinstance(ai, dict):
        h = ai.get("h") if isinstance(ai.get("h"), int) else ai.get("hour")
        m = ai.get("m") if isinstance(ai.get("m"), int) else ai.get("minute")
        ampm = ai.get("ampm") or ai.get("am_pm") or ai.get("clue")
        if isinstance(ampm, str) and ampm.lower().strip() in {"am", "pm"}:
            clue = ampm.lower().strip()
        if (h is None or m is None) and isinstance(ai.get("hora"), str) and ":" in ai["hora"]:
            try:
                h2, m2 = map(int, ai["hora"].split(":", 1))
                if 0 <= h2 <= 23 and 0 <= m2 <= 59:
                    return {"h": h2, "m": m2, "clue": None, "ambigua": False}
            except Exception as e:
                sentry_sdk.capture_exception(e)
                pass
    t = (mensaje or "").lower().strip()
    if re.search(r"\b\d{1,2}\s*,\s*\d{1,2}\b", t):
        return None
    m_try = re.search(r"\b(\d{1,2})\s*[:h.]\s*(\d{1,2})\b", t)
    if m_try:
        hh = int(m_try.group(1))
        mm = int(m_try.group(2))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return None
    if h is None or m is None:
        if not m_try and "menos" not in t and not re.search(r"\by\b", t) and not re.search(r"\d\s*,\s*\d", t):
            mm = re.search(r"\b(\d{1,2})\b", (mensaje or ""))
            if mm:
                val = int(mm.group(1))
                if 0 <= val <= 23:
                    h, m = val, 0
    if h is None:
        return None
    if 13 <= h <= 23 or h == 0:
        return {"h": h, "m": (m or 0), "clue": clue, "ambigua": False}
    if 1 <= h <= 12:
        clue = clue or _has_am_pm(mensaje)
        return {"h": 12 if h == 12 else h, "m": (m or 0), "clue": clue, "ambigua": clue is None}
    return None


def elegir_hora_final(horas_libres: list[str], parsed: dict) -> dict:
    """Elige la hora final en base a 'parsed' y la lista 'horas_libres'."""
    h, m, clue, amb = parsed["h"], parsed["m"], parsed["clue"], parsed["ambigua"]
    if not amb:
        if clue == "am" and h == 12:
            h = 0
        if clue == "pm" and 1 <= h <= 11:
            h = h + 12
        hhmm = _format_hm(h, m)
        if hhmm in horas_libres:
            return {"ok": True, "hora": hhmm}
        return _suggestions(horas_libres, hhmm, "Esa hora no está disponible.")
    am_h = 0 if h == 12 else h
    pm_h = 12 if h == 12 else h + 12
    am = _format_hm(am_h, m)
    pm = _format_hm(pm_h, m)
    am_ok = am in horas_libres
    pm_ok = pm in horas_libres
    if am_ok and pm_ok:
        return {"ok": False, "need_am_pm": True, "candidatas": [am, pm]}
    if am_ok:
        return {"ok": True, "hora": am}
    if pm_ok:
        return {"ok": True, "hora": pm}
    prefer = pm if any(x.startswith(("15", "16", "17", "18", "19", "20", "21")) for x in horas_libres) else am
    return _suggestions(horas_libres, prefer, "No hay hueco exacto a esa hora.")


def filtra_horas_desde_ahora(pelu, horas: list[str], fecha_str: str) -> list[str]:
    """Quita horas pasadas si la fecha es HOY (en la TZ de la peluquería)."""
    try:
        fecha = datetime.strptime(fecha_str, "%Y-%m-%d").date()
    except Exception as e:
        sentry_sdk.capture_exception(e)
        return horas

    ahora = now_local(pelu)
    hoy_local = ahora.date()
    if fecha != hoy_local:
        return horas

    filtradas = []
    for h in horas:
        try:
            t = datetime.strptime(h, "%H:%M").time()
            if datetime.combine(fecha, t, tzinfo=ahora.tzinfo) > ahora:
                filtradas.append(h)
        except Exception as e:
            sentry_sdk.capture_exception(e)
            continue
    return filtradas


def _extract_hhmm_from_text(texto: str) -> Optional[str]:
    t = (texto or "").strip()
    m = re.search(r"(\d{1,2})\s*:\s*(\d{2})", t)
    if not m:
        return None
    h = int(m.group(1))
    mm = int(m.group(2))
    if 0 <= h <= 23 and 0 <= mm <= 59:
        return f"{h:02d}:{mm:02d}"
    return None


def es_reserva_futura(fecha, hora, pelu=None) -> bool:
    try:
        f = ymd_str(fecha)
        h = hhmm_str(hora)
        now = now_local(pelu)
        dt = datetime.strptime(f"{f} {h}", "%Y-%m-%d %H:%M").replace(tzinfo=now.tzinfo)
        return dt > now
    except Exception as e:
        sentry_sdk.capture_exception(e)
        return False


def set_servicio_en_datos(datos: dict, servicio) -> None:
    datos["servicio_id"] = getattr(servicio, "id", None)
    datos["servicio_nombre"] = getattr(servicio, "nombre", "")
    datos["servicio_duracion"] = getattr(servicio, "duracion_min", None)


def get_servicio_from_datos(pelu, datos: dict):
    sid = datos.get("servicio_id")
    if sid is None:
        return None
    return next((s for s in pelu.servicios if s.id == sid), None)


def ymd_str(posible_fecha: Any) -> Optional[str]:
    if posible_fecha is None:
        return None
    if isinstance(posible_fecha, str):
        return posible_fecha
    try:
        return posible_fecha.strftime("%Y-%m-%d")
    except Exception as e:
        sentry_sdk.capture_exception(e)
        return str(posible_fecha)


def guardar_estado(session_id: str, estado: dict) -> None:
    storage.setex(f"state:{session_id}", json.dumps(estado, ensure_ascii=False), ttl=60 * 60 * 5)


def cargar_estado(session_id: str) -> Optional[dict]:
    raw = storage.get(f"state:{session_id}")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception as e:
        sentry_sdk.capture_exception(e)
        return None


def get_peluqueria_by_api_key(api_key: str) -> Optional[Peluqueria]:
    db = SessionLocal()
    try:
        return (
            db.query(Peluqueria)
            .options(selectinload(Peluqueria.servicios))
            .filter_by(api_key=api_key)
            .first()
        )
    finally:
        db.close()


def _resumen_cancelacion_y_set_paso(pelu, estado, datos, session_id):
    """Muestra el resumen de la reserva a cancelar y deja el paso en confirmar_cancelar."""
    db = SessionLocal()
    try:
        reserva = (
            db.query(Reserva)
            .options(selectinload(Reserva.servicio))  # evitar Detached
            .filter_by(id=datos["reserva_id"])
            .first()
        )
    finally:
        db.close()

    if not reserva:
        reset_estado(session_id)
        return jsonify({"respuesta": "No encontré la reserva. ¿Te ayudo con otra cosa?"})

    # Stub de servicio para no depender de la sesión
    class _Srv:
        ...
    servicio_stub = _Srv()
    if getattr(reserva, "servicio", None):
        servicio_stub.id = getattr(reserva.servicio, "id", None)
        servicio_stub.nombre = getattr(reserva.servicio, "nombre", "")
        servicio_stub.duracion_min = getattr(reserva.servicio, "duracion_min", None)
    else:
        servicio_stub.id = None
        servicio_stub.nombre = ""
        servicio_stub.duracion_min = None

    # Guardar info en estado y avanzar paso
    datos["__servicio_stub"] = servicio_stub
    datos["__fecha_reserva"] = ymd_str(reserva.fecha)
    datos["__hora_reserva"] = hhmm_str(reserva.hora)
    datos["resumen_mostrado"] = True
    estado["paso"] = "confirmar_cancelar"
    guardar_estado(session_id, estado)

    # Resumen
    resumen = "Vas a cancelar esta reserva:\n"
    if len(pelu.servicios) > 1 and servicio_stub.nombre:
        resumen += f"Servicio: {servicio_stub.nombre}\n"
    resumen += (
        f"Fecha: {formatea_fecha_es(reserva.fecha)}\n"
        f"Hora: {hhmm_str(reserva.hora)}\n"
        f"Nombre: {reserva.nombre_cliente}\n"
        f"Teléfono: {reserva.telefono}\n"
        "¿Confirmas la cancelación? (si/no)"
    )
    return jsonify({"respuesta": resumen})


def _norm_txt(s: str) -> str:
    """Normaliza texto para matching de servicios."""
    s = (s or "").strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")  # quita tildes
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _elegir_servicio_desde_texto(pelu, mensaje: str, sugerido_por_ia: Optional[str] = None):
    """
    Intenta elegir servicio por:
    1) número (1..N)
    2) coincidencia exacta normalizada
    3) empieza por / contiene (normalizado)
    4) sugerencia de la IA como aguja adicional
    """
    servicios = list(pelu.servicios or [])
    if not servicios:
        return None

    txt = (mensaje or "").strip()
    n = re.fullmatch(r"\d{1,2}", txt)
    if n:
        idx = int(n.group()) - 1
        if 0 <= idx < len(servicios):
            return servicios[idx]

    aguja = _norm_txt(txt)
    agujas = [aguja]
    if sugerido_por_ia:
        agujas.append(_norm_txt(sugerido_por_ia))

    cand_norm = [(_norm_txt(s.nombre), s) for s in servicios]

    for a in agujas:
        for cn, s in cand_norm:
            if a and a == cn:
                return s

    for a in agujas:
        if not a:
            continue
        for cn, s in cand_norm:
            if cn.startswith(a):
                return s
        for cn, s in cand_norm:
            if a in cn:
                return s

    return None


def _norm(s: str) -> str:
    s = (s or "").strip()
    s = unicodedata.normalize("NFKD", s)
    s = s.encode("ascii", "ignore").decode("ascii")
    return s.lower()


def detect_global_command(texto: str) -> Optional[str]:
    """Detecta comandos como /menu, /reset, /salir, /volver (o sin /)."""
    t = _norm(texto)
    if t.startswith("/"):
        t = t[1:]
    if t in CMD_MENU:
        return "menu"
    if t in CMD_RESET:
        return "reset"
    if t in CMD_SALIR:
        return "salir"
    if t in CMD_VOLVER:
        return "volver"
    return None

def _norm_min(s: str) -> str:
    import unicodedata, re
    s = (s or "").strip()
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", s).lower()

def intencion_desde_texto_o_ia(mensaje: str, pelu, origin: str = "text") -> str | None:
    """
    1) Si el texto coincide con un botón o sinónimo claro → intención directa.
    2) Solo llama a interpreta_ia si el origen es texto libre.
    """
    t = _norm_min(mensaje)

    if any(k in t for k in ("cancelar", "anular", "anular cita", "cancelar cita", "cancelar reserva", "anular reserva")):
        return "cancelar"

    if t in _INTENT_MAP:
        return _INTENT_MAP[t]
    if origin != "text":
        return _INTENT_MAP.get(t)  # o None
    try:
        return interpreta_ia(mensaje, "intencion", pelu)
    except Exception as e:
        sentry_sdk.capture_exception(e)
        return None


def welcome_text(pelu_nombre: str, tipo_negocio: str) -> str:
    return (
        f"¡Hola! Soy la secretaria virtual de la {tipo_negocio} {pelu_nombre} ✂️✨\n"
        "(Escribe «menu» en cualquier momento para volver aquí)"
    )

def return_text(pelu_nombre: str, tipo_negocio: str) -> str:
    return (
        f"Menú principal de la {tipo_negocio} {pelu_nombre} ✂️✨\n"
        "(Escribe «menu» en cualquier momento para volver aquí)"
    )

DEFAULT_STATE = {"paso": "inicio", "datos": {}, "tipo_accion": None}

def reset_estado(session_id: str):
    guardar_estado(session_id, DEFAULT_STATE)

def purge_horas_cache(pelu, fecha_str: str):
    """Invalidar todas las combinaciones de servicio para (pelu, fecha)."""
    try:
        # Borramos claves para cada servicio y también el caso None
        for s in (pelu.servicios or []):
            storage.delete(get_horas_cache_key(pelu.id, getattr(s, "id", None), fecha_str))
        storage.delete(get_horas_cache_key(pelu.id, None, fecha_str))
    except Exception as e:
        sentry_sdk.capture_exception(e)

def get_horas_cache_key(pelu_id, servicio_id, fecha):
    return f"horas:{pelu_id}:{servicio_id or 'None'}:{fecha}"

def horas_disponibles_cached(db, pelu, servicio, fecha):
    key = get_horas_cache_key(pelu.id, getattr(servicio, "id", None), fecha)
    cached = storage.get(key)
    if cached:
        try:
            return json.loads(cached)
        except Exception as e:
            sentry_sdk.capture_exception(e)
            pass
    horas = horas_disponibles(db, pelu, servicio, fecha)
    storage.setex(key, json.dumps(horas, ensure_ascii=False), ttl=120)  # 2 min
    return horas

def _fecha_fuera_de_rango(fecha_date, pelu) -> tuple[bool, date]:
    """
    Devuelve (fuera_de_rango, limite_incluido).
    - fuera_de_rango: True si 'fecha_date' es posterior al limite permitido.
    - limite_incluido: hoy_local + max_avance_dias (en TZ de la peluqueria).
    """
    try:
        max_dias = int(getattr(pelu, "max_avance_dias", 150) or 150)
    except Exception:
        max_dias = 150
    hoy_local = now_local(pelu).date()
    limite = hoy_local + timedelta(days=max_dias)
    return fecha_date > limite, limite

MAX_LOCK_RETRIES = 1

def _sleep_backoff(i: int):
    """
    Espera progresiva con un poco de aleatoriedad para evitar colisiones.
    i = 0,1,2...
    """
    import random as _rnd
    from random import SystemRandom
    base = 0.15 * (2 ** i)  # 150ms, 300ms...
    try:
        jitter = _rnd.uniform(0.0, 0.05)  # hasta 50ms extra
    except Exception:
        # Si alguien ha pisado el módulo random, usamos un fallback determinista
        try:
            jitter = SystemRandom().random() * 0.05
        except Exception:
            jitter = 0.0
    _time.sleep(base + jitter)

def _filtra_horas_por_horario_json(horas_list, fecha_obj, horario_json):
    """
    horas_list: lista de horas tipo 'HH:MM'
    fecha_obj:  date de la fecha elegida
    horario_json: dict o string JSON con keys por día:
                  { "mon": ["08:00-14:00","16:00-22:00"], "sat": ["08:00-14:00"], ... }
    Devuelve solo las horas que caen dentro de los rangos del día.
    Si el JSON no está o es inválido, devuelve horas_list sin tocar.
    """
    if not horas_list:
        return horas_list
    try:
        import json
        data = horario_json
        if isinstance(data, str) and data.strip():
            data = json.loads(data)
        if not isinstance(data, dict):
            return horas_list

        # map weekday -> clave JSON
        keys = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        key = keys[fecha_obj.weekday()]  # 0=lunes … 6=domingo

        day_ranges = data.get(key)
        if not day_ranges:
            # sin rangos para ese día => no hay horas
            return []

        def _in_any_range(h):
            # h 'HH:MM'
            for r in day_ranges:
                try:
                    ini, fin = [x.strip() for x in r.split("-", 1)]
                    # comparación lexicográfica funciona con HH:MM
                    if ini <= h <= fin:
                        return True
                except Exception:
                    continue
            return False

        return [h for h in horas_list if _in_any_range(h)]
    except Exception:
        # si algo falla, no rompas el flujo
        return horas_list

def _proximas_fechas_con_hueco(pelu, servicio, fecha_inicio_date, max_items=5, peluquero_id=None) -> list[str]:
    """
    Devuelve hasta 'max_items' fechas (formateadas dd/mm/YYYY) posteriores a 'fecha_inicio_date'
    que tengan al menos una hora disponible. Si 'peluquero_id' está presente, filtra por ese profesional.
    Aplica los mismos filtros que usas en el flujo: 'filtra_horas_desde_ahora' y '_filtra_horas_por_horario_json'.
    """
    sugeridas: list[str] = []
    now = now_local(pelu)
    hoy = now.date()
    max_dias = int(getattr(pelu, "max_avance_dias", 150) or 150)

    with SessionLocal() as db:
        for delta in range(1, max_dias + 1):
            if len(sugeridas) >= max_items:
                break
            f = fecha_inicio_date + timedelta(days=delta)
            f_str = f.strftime("%Y-%m-%d")

            # Horas “brutas” (según haya peluquero o no)
            if peluquero_id:
                horas = horas_disponibles_para_peluquero(db, pelu, servicio, peluquero_id, f_str)
            else:
                horas = horas_disponibles_cached(db, pelu, servicio, f_str)

            # Mismos filtros que en el flujo
            try:
                horas = filtra_horas_desde_ahora(pelu, horas, f_str)
            except Exception:
                pass
            try:
                horas = _filtra_horas_por_horario_json(horas, f, getattr(pelu, "horario", None))
            except Exception:
                pass

            if horas:  # hay al menos una hora libre
                sugeridas.append(f.strftime("%d/%m/%Y"))

    return sugeridas

# ================================================
# WhatsApp helpers
# ================================================
def _wa_outbound_allow(phone_number_id: str) -> bool:
    # lee el tope; si no existe, no limita
    try:
        limit = int(settings.RATE_LIMITS["OUTBOUND_WA_PER_PELU"])
    except Exception as ex:
        sentry_sdk.capture_exception(ex)
        return True
    if limit <= 0:
        return True

    # resuelve peluquería y acumula en Redis 1/minuto
    try:
        pelu = get_peluqueria_by_wa_phone_number_id(phone_number_id)
        pelu_id = getattr(pelu, "id", None)
    except Exception as ex:
        sentry_sdk.capture_exception(ex)
        pelu_id = None
    if pelu_id is None:
        return True

    minute = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    key = f"rl:wa:out:{pelu_id}:{minute}"
    try:
        count = storage.incr(key, ttl=60)  # ← mismo storage que ya usas
    except Exception as e:
        sentry_sdk.capture_exception(e)
        return True

    return count <= limit

def verify_waba_signature(app_secret: str, raw_body: bytes, header_sig: str) -> bool:
    try:
        if not header_sig or not header_sig.startswith("sha256="):
            return False
        recv = header_sig.split("=", 1)[1]
        mac = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(mac, recv)
    except Exception as e:
        sentry_sdk.capture_exception(e)
        return False


def wa_send_text(phone_number_id: str, to: str, body: str, session_id: Optional[str] = None) -> bool | dict:
    token, graph_ver = _wa_creds_for(phone_number_id)
    if not token:
        logging.error("WABA_TOKEN no configurado para %s", phone_number_id)
        return False
    if not _wa_outbound_allow(phone_number_id):
        return {"ok": False, "error": "wa_outbound_rate_limited"}
    url = f"https://graph.facebook.com/{graph_ver}/{phone_number_id}/messages"
    normalized_session = _wa_normalize_session_id(session_id, to)
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": body[:4096]},
    }
    headers = _wa_headers(token, normalized_session, payload)
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        if not r.ok:
            logging.warning(f"WA send failed {r.status_code}: {r.text[:200]}")
        return r.ok
    except Exception as e:
        sentry_sdk.capture_exception(e)
        logging.error(f"WA send exception: {e}", exc_info=True)
        return False


def wa_extract_text(message_obj: dict):
    t = message_obj.get("type")
    if t == "text":
        return (message_obj.get("text") or {}).get("body")
    if t == "button":
        return (message_obj.get("button") or {}).get("text")
    if t == "interactive":
        inter = message_obj.get("interactive") or {}
        if inter.get("type") == "button_reply":
            return (inter.get("button_reply") or {}).get("title")
        if inter.get("type") == "list_reply":
            return (inter.get("list_reply") or {}).get("title")
    return None


def _wa_normalize_session_id(session_id: Optional[str], to: str) -> str:
    sid = (session_id or "").strip()
    if sid:
        return sid
    to_str = str(to or "").strip()
    if to_str.startswith("wa_"):
        return to_str
    return f"wa_{to_str}" if to_str else "wa_unknown"


def _wa_idempotency_key(session_id: str, payload: dict) -> str:
    try:
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except TypeError as e:
        sentry_sdk.capture_exception(e)
        serialized = str(payload)
    raw = f"{session_id}:{serialized}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _wa_headers(token: str, session_id: str, payload: dict) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Idempotency-Key": _wa_idempotency_key(session_id, payload),
    }
    return headers


def get_peluqueria_by_wa_phone_number_id(phone_number_id: str):
    db = SessionLocal()
    try:
        return (
            db.query(Peluqueria)
            .options(selectinload(Peluqueria.servicios))
            .filter_by(wa_phone_number_id=phone_number_id)
            .first()
        )
    finally:
        db.close()


def wa_send_main_menu(phone_number_id: str, to: str, pelu_nombre: str, session_id: Optional[str] = None) -> bool:
    token, graph_ver = _wa_creds_for(phone_number_id)
    if not token:
        logging.error("WABA_TOKEN no configurado para %s", phone_number_id)
        return False
    url = f"https://graph.facebook.com/{graph_ver}/{phone_number_id}/messages"
    normalized_session = _wa_normalize_session_id(session_id, to)
    if not _wa_outbound_allow(phone_number_id):
        return False
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": "¿Qué quieres hacer?"},
            "footer": {"text": "Elige una opción ↓"},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "ACT_RESERVAR", "title": "Reservar cita"}},
                    {"type": "reply", "reply": {"id": "ACT_CAN", "title": "Cancelar cita"}},
                    {"type": "reply", "reply": {"id": "ACT_DUDA", "title": "Duda"}},
                ]
            },
        },
    }
    headers = _wa_headers(token, normalized_session, payload)
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        if r.ok:
            return True
        else:
            logging.warning(f"WA send main menu failed {r.status_code}: {r.text[:200]}")
            # Fallback en texto
            fallback = (
                "¿Qué quieres hacer?\n"
                "1) Reservar cita\n"
                "2) Cancelar cita\n"
                "3) Duda"
            )
            wa_send_text(phone_number_id, to, fallback, session_id=normalized_session)
            return False
    except Exception as e:
        sentry_sdk.capture_exception(e)
        logging.error(f"WA send main menu exception: {e}", exc_info=True)
        fallback = (
            "¿Qué quieres hacer?\n"
            "1) Reservar cita\n"
            "2) Cancelar cita\n"
            "3) Duda"
        )
        wa_send_text(phone_number_id, to, fallback, session_id=normalized_session)
        return False

def wa_send_service_list(
    phone_number_id: str,
    to: str,
    pelu,
    prompt_text: str = "Elige un servicio:",
    session_id: Optional[str] = None,
    page: int = 1,
    per_page: int = 10,
) -> bool:
    servicios = list(getattr(pelu, "servicios", []) or [])
    if not servicios:
        return False

    normalized_session = _wa_normalize_session_id(session_id, to)
    if not _wa_outbound_allow(phone_number_id):
        return False

    total = len(servicios)

    # WhatsApp: max 10 filas, reservamos 1 para "Ver mas"
    items_per_page = max(1, min(9, per_page - 1))
    max_page = max(1, math.ceil(total / items_per_page))
    page = max(1, min(page, max_page))

    start = (page - 1) * items_per_page
    end = min(start + items_per_page, total)
    subset = servicios[start:end]
    has_next = end < total

    rows = []
    for i, servicio in enumerate(subset):
        idx_global = start + i

        title = (servicio.nombre or "").strip()[:24] or f"Servicio {idx_global + 1}"
        desc = (servicio.descripcion or "").strip()[:72]

        row = {
            "id": f"SERV_P{page}_{idx_global}",
            "title": title
        }
        if desc:
            row["description"] = desc

        rows.append(row)

    if has_next:
        rows.append({
            "id": f"SERV_NEXT_{page + 1}",
            "title": "➡️ Ver mas servicios",
        })

    token, graph_ver = _wa_creds_for(phone_number_id)
    if not token:
        logging.error("WABA_TOKEN no configurado para %s", phone_number_id)
        listado = "\n".join(
            f"{i+1}) {s.nombre}" + (f" - {s.descripcion}" if s.descripcion else "")
            for i, s in enumerate(servicios)
        )
        wa_send_text(phone_number_id, to, f"{prompt_text}\n{listado}", session_id=normalized_session)
        return False

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": prompt_text},
            "footer": {"text": "Pulsa para ver opciones"},
            "action": {
                "button": "Ver servicios",
                "sections": [
                    {"title": "Servicios", "rows": rows}
                ],
            },
        },
    }

    headers = _wa_headers(token, normalized_session, payload)
    try:
        r = requests.post(
            f"https://graph.facebook.com/{graph_ver}/{phone_number_id}/messages",
            headers=headers,
            json=payload,
            timeout=10
        )
        if r.ok:
            return True

        logging.warning(f"WA send service list failed {r.status_code}: {r.text[:200]}")
    except Exception as e:
        sentry_sdk.capture_exception(e)

    listado = "\n".join(
        f"{i+1}) {s.nombre}" + (f" - {s.descripcion}" if s.descripcion else "")
        for i, s in enumerate(servicios)
    )
    wa_send_text(phone_number_id, to, f"{prompt_text}\n{listado}", session_id=normalized_session)
    return False


def wa_send_peluquero_list(
    phone_number_id: str,
    to: str,
    peluqueria,
    prompt_text: str = "Elige un profesional:",
    include_any_option: bool = True,
    session_id: Optional[str] = None,
    page: int = 1,
    per_page: int = 10,
) -> bool:
    with SessionLocal() as db:
        activos = get_active_peluqueros(db, peluqueria_id=peluqueria.id)

    if not activos:  # nada que listar
        return False

    nombres = [p.nombre for p in activos]
    total_real = len(nombres)

    normalized_session = _wa_normalize_session_id(session_id, to)
    if not _wa_outbound_allow(phone_number_id):
        return False

    per_page = max(1, min(10, per_page))
    base_items_per_page = max(1, min(9, per_page - 1))  # 9 + "ver más"
    items_per_page = base_items_per_page - (1 if include_any_option else 0)
    items_per_page = max(1, items_per_page)

    max_page = max(1, math.ceil(total_real / items_per_page))
    page = max(1, min(page, max_page))

    start = (page - 1) * items_per_page
    end = min(start + items_per_page, total_real)
    subset = nombres[start:end]
    has_next = end < total_real

    rows = []
    if include_any_option:
        rows.append({
            "id": "PEL_ANY",
            "title": "Sin preferencia",
            "description": "Asignaremos a quien esté disponible"
        })

    for i, nombre in enumerate(subset):
        idx_global = start + i
        title = (nombre or "").strip()[:24] or f"Peluquero {idx_global+1}"
        desc = (nombre or "").strip()[24:80] if len(nombre or "") > 24 else ""
        rows.append({
            "id": f"PEL_P{page}_{idx_global}",
            "title": title,
            "description": desc
        })

    if has_next:
        rows.append({
            "id": f"PEL_NEXT_{page + 1}",
            "title": "➡️ Ver más opciones",
        })

    # ✅ mapping antes del POST
    try:
        mapping = [{"id": p.id, "nombre": p.nombre} for p in activos]
        storage.setex(f"pelulist:{normalized_session}", json.dumps(mapping, ensure_ascii=False), ttl=300)
    except Exception as e:
        sentry_sdk.capture_exception(e)

    token, graph_ver = _wa_creds_for(phone_number_id)
    if not token:
        logging.error("WABA_TOKEN no configurado para %s", phone_number_id)
        listado = []
        if include_any_option:
            listado.append("1) Sin preferencia")
            base = 2
        else:
            base = 1
        for i, nombre in enumerate(nombres):
            listado.append(f"{base + i}) {nombre}")
        wa_send_text(phone_number_id, to, f"{prompt_text}\n" + "\n".join(listado), session_id=normalized_session)
        return False

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": prompt_text},
            "footer": {"text": "Pulsa para ver opciones"},
            "action": {
                "button": "Ver peluqueros",
                "sections": [
                    {"title": "Peluqueros", "rows": rows}
                ],
            },
        },
    }

    headers = _wa_headers(token, normalized_session, payload)
    try:
        r = requests.post(
            f"https://graph.facebook.com/{graph_ver}/{phone_number_id}/messages",
            headers=headers, json=payload, timeout=10
        )
        if r.ok:
            return True
        logging.warning(f"WA send peluquero list failed {r.status_code}: {r.text[:200]}")
    except Exception as e:
        sentry_sdk.capture_exception(e)
        logging.error(f"WA send peluquero list exception: {e}", exc_info=True)

    # Fallback texto
    listado = []
    if include_any_option:
        listado.append("1) Sin preferencia")
        base = 2
    else:
        base = 1
    for i, nombre in enumerate(nombres):
        listado.append(f"{base + i}) {nombre}")

    wa_send_text(phone_number_id, to, f"{prompt_text}\n" + "\n".join(listado), session_id=normalized_session)
    return False


def wa_send_hours_page(phone_number_id, to, session_id, horas, page=1, per_page=10):
    """
    Envía lista paginada de horas. Si hay siguiente página, usamos (per_page-1) filas + 1 "Ver más"
    para no superar el límite de 10. Si falla, fallback en texto con TODAS las horas numeradas.
    """
    normalized_session = _wa_normalize_session_id(session_id, to)
    if not _wa_outbound_allow(phone_number_id):
        return False

    total = len(horas)
    if total == 0:
        wa_send_text(phone_number_id, to, "No hay horas disponibles.", session_id=normalized_session)
        return False

    # Siempre reservamos 1 para "Ver más" (si aplica)
    items_per_page = max(1, per_page - 1)

    # Clampea page por si llega fuera de rango
    import math
    max_page = max(1, math.ceil(total / items_per_page))
    page = max(1, min(page, max_page))

    start = (page - 1) * items_per_page
    end = min(start + items_per_page, total)
    subset = horas[start:end]

    # ¿Hay siguiente página?
    has_next = end < total

    rows = []
    for i, h in enumerate(subset):
        idx_global = start + i  # índice absoluto en la lista total (ya correcto)
        rows.append({
            "id": f"HORA_P{page}_{idx_global}",
            "title": h[:24],
        })

    if has_next:
        rows.append({
            "id": f"HORA_NEXT_{page + 1}",
            "title": "➡️ Ver más horas",
        })

    token, graph_ver = _wa_creds_for(phone_number_id)
    if not token:
        logging.error("WABA_TOKEN no configurado. Envío fallback en texto.")
        listado = "\n".join(horas)  # todas
        wa_send_text(phone_number_id, to, f"Elige una hora:\n{listado}", session_id=normalized_session)
        return False

    body = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": "Horas disponibles"},
            "footer": {"text": "Elige una hora"},
            "action": {
                "button": "Selecciona hora",
                "sections": [
                    {"title": "Horas", "rows": rows}
                ]
            }
        }
    }

    headers = _wa_headers(token, normalized_session, body)
    try:
        r = requests.post(
            f"https://graph.facebook.com/{graph_ver}/{phone_number_id}/messages",
            headers=headers,
            json=body,
            timeout=10
        )
        if not r.ok:
            logging.warning(f"WA send hours page failed {r.status_code}: {r.text[:200]}")
            # 🔁 Fallback: TODAS las horas en texto
            listado = "\n".join(horas)
            wa_send_text(phone_number_id, to, f"Elige una hora:\n{listado}", session_id=normalized_session)
            return False
        return True
    except Exception as e:
        sentry_sdk.capture_exception(e)
        logging.error(f"Error enviando lista de horas: {e}", exc_info=True)
        listado = "\n".join(horas)
        wa_send_text(phone_number_id, to, f"Elige una hora:\n{listado}", session_id=normalized_session)
        return False

def wa_send_reservas_list(
    phone_number_id: str,
    to: str,
    items: list[dict],
    prompt_text: str = "Selecciona la reserva:",
    session_id: Optional[str] = None,
    page: int = 1,
    per_page: int = 10,  # WhatsApp lista máx 10 filas. Usamos 9 + "Ver más".
) -> bool:
    """
    Lista paginada de reservas. Estructura esperada de cada item:
      {"id": "RID_123", "title": "15 sep · 16:30", "description": "Corte - Juan Pérez"}

    - 9 filas reales + 1 "➡️ Ver más reservas" (cuando aplica).
    - Ids:
        * Reserva: RES_P<page>_<idx_global> (ej: RES_P2_17)
        * Ver más: RES_NEXT_<page+1> (ej: RES_NEXT_3)
    - Fallback: envía TODAS las reservas en texto si falla el interactivo.
    """
    normalized_session = _wa_normalize_session_id(session_id, to)
    if not _wa_outbound_allow(phone_number_id):
        return False

    if not items:
        wa_send_text(phone_number_id, to, "No he encontrado reservas.", session_id=normalized_session)
        return False

    total = len(items)
    # 9 reales + 1 "Ver más"
    items_per_page = max(1, min(9, per_page - 1))

    import math
    max_page = max(1, math.ceil(total / items_per_page))
    page = max(1, min(page, max_page))

    start = (page - 1) * items_per_page
    end = min(start + items_per_page, total)
    subset = items[start:end]
    has_next = end < total

    # Construcción de filas
    rows = []
    for i, it in enumerate(subset):
        idx_global = start + i
        title = (str(it.get("title", "")) or "Reserva")[:24]
        desc = str(it.get("description", ""))[:72]
        rid = str(it.get("id", ""))[:200] or f"RID_{idx_global}"
        row = {"id": rid, "title": title}
        if desc:
            row["description"] = desc
        rows.append(row)

    if has_next:
        rows.append({
            "id": f"RID_NEXT_{page + 1}",
            "title": "➡️ Ver más reservas",
        })

    token, graph_ver = _wa_creds_for(phone_number_id)
    if not token:
        logging.error("WABA_TOKEN no configurado. Envío fallback en texto.")
        listado = "\n".join([f"- {it.get('title','')}" for it in items])
        wa_send_text(phone_number_id, to, f"{prompt_text}\n{listado}", session_id=normalized_session)
        return False

    body = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": prompt_text},
            "footer": {"text": "Pulsa para ver reservas"},
            "action": {
                "button": "Ver reservas",
                "sections": [
                    {"title": f"Reservas", "rows": rows}
                ],
            },
        },
    }

    headers = _wa_headers(token, normalized_session, body)
    try:
        r = requests.post(
            f"https://graph.facebook.com/{graph_ver}/{phone_number_id}/messages",
            headers=headers,
            json=body,
            timeout=10
        )
        if r.ok:
            return True
        logging.warning(f"WA send reservas list failed {r.status_code}: {r.text[:200]}")
    except Exception as e:
        sentry_sdk.capture_exception(e)
        logging.error(f"WA send reservas list exception: {e}", exc_info=True)

    # Fallback texto con TODAS
    listado = "\n".join([f"- {it.get('title','')}" for it in items])
    wa_send_text(phone_number_id, to, f"{prompt_text}\n{listado}", session_id=normalized_session)
    return False

def _wa_creds_for(phone_number_id: str):
    pelu = get_peluqueria_by_wa_phone_number_id(phone_number_id)
    if not pelu:
        raise ValueError(f"No se encontró {getattr(pelu, 'tipo_negocio', 'negocio')} con phone_number_id={phone_number_id}")
    token = pelu.wa_token
    graph_ver = settings.GRAPH_API_VERSION
    return token, graph_ver


def _msg_ts(msg) -> int:
    try:
        return int((msg or {}).get("timestamp") or 0)
    except Exception as e:
        sentry_sdk.capture_exception(e)
        return 0

def should_process_by_ts(session_id: str, ts: int) -> bool:
    """
    Solo procesa si este ts es estrictamente mayor que el último procesado.
    Evita re-ordenamientos y reintentos tardíos.
    """
    key = f"last_ts:{session_id}"
    try:
        last = storage.get(key)
        last_i = int(float(last)) if last is not None else 0
        if ts < last_i:
            return False
        # aceptamos y dejamos guardado
        storage.setex(key, str(ts), ttl=60*60*24)
        return True
    except Exception as e:
        sentry_sdk.capture_exception(e)
        # si storage falla, preferimos procesar
        return True

def is_current(session_id: str, ts: int) -> bool:
    last = storage.get(f"last_ts:{session_id}")
    try:
        return int(float(last)) == int(ts)
    except Exception as e:
        sentry_sdk.capture_exception(e)
        return True

# ================================================
# Webhook WhatsApp (verify + receive)
# ================================================


def _pelu_rate_scope(endpoint: str) -> str:
    try:
        payload = request.get_json(silent=True) or {}
    except Exception as e:
        sentry_sdk.capture_exception(e)
        payload = {}

    entries = payload.get("entry") or []
    for entry in entries:
        for change in (entry.get("changes") or []):
            value = change.get("value") or {}
            metadata = value.get("metadata") or {}
            phone_number_id = metadata.get("phone_number_id")
            if phone_number_id:
                try:
                    pelu = get_peluqueria_by_wa_phone_number_id(phone_number_id)
                    pelu_id = getattr(pelu, "id", None)
                    if pelu_id is not None:
                        return f"pelu:{pelu_id}"
                except Exception as e:
                    sentry_sdk.capture_exception(e)
                    continue
    return "pelu:unknown"

def _process_core_and_reply(
    phone_number_id: str,
    from_msisdn: str,
    session_id: str,
    texto: str,
    origin: str,
    idem: str,
):
    try:
        # Refetch pelu aqui (evita objetos SQLAlchemy detached fuera del request)
        pelu = get_peluqueria_by_wa_phone_number_id(phone_number_id)
        if not pelu:
            logging.warning("phone_number_id desconocido (async): %s", phone_number_id)
            return

        headers = {
            "Content-Type": "application/json",
            "X-API-KEY": pelu.api_key,
            "Idempotency-Key": idem,
        }
        body = {"session_id": session_id, "mensaje": texto, "origin": origin}
        base = os.getenv("BOT_INTERNAL_URL", "http://127.0.0.1:5000")
        timeout_tuple = (3.05, settings.LOOPBACK_TIMEOUT_SECONDS)

        try:
            r = requests.post(
                f"{base}/webhook",
                headers=headers,
                json=body,
                timeout=timeout_tuple,
            )
        except ReadTimeout:
            # No rompemos UX: el webhook ya respondio 200 antes
            sentry_sdk.capture_message(
                "Loopback timeout /webhook (async)",
                level="warning",
            )
            return

        if not r.ok:
            logging.warning(
                "loopback /webhook fallo %s: %s",
                r.status_code,
                (r.text or "")[:200],
            )
            return

        # ---- Respuesta del core ----
        data = r.json() or {}
        resp = data.get("respuesta")
        ui = data.get("ui")
        resp2 = data.get("respuesta2")

        # 🔒 BLINDAJE CLAVE:
        # Si el core devuelve texto, SE ENVIA SIEMPRE
        # (esto replica el comportamiento previo al async)
        if resp:
            wa_send_text(
                phone_number_id,
                from_msisdn,
                resp,
                session_id=session_id,
            )

        # ---- UIs especificas ----
        if ui == "main_menu":
            wa_send_main_menu(
                phone_number_id,
                from_msisdn,
                getattr(pelu, "nombre", "Peluquería"),
                session_id=session_id,
            )
            return

        if ui == "services":
            wa_send_service_list(
                phone_number_id,
                from_msisdn,
                pelu,
                session_id=session_id,
            )
            return

        if ui == "hours":
            horas = data.get("choices") or []
            try:
                storage.setex(
                    f"hours:{session_id}",
                    json.dumps(horas, ensure_ascii=False),
                    ttl=300,
                )
            except Exception as e:
                sentry_sdk.capture_exception(e)

            wa_send_hours_page(
                phone_number_id,
                from_msisdn,
                session_id,
                horas,
                page=1,
            )
            return

        if ui == "res_list":
            items = data.get("choices") or []
            try:
                storage.setex(
                    f"reslist:{session_id}",
                    json.dumps(items, ensure_ascii=False),
                    ttl=300,
                )
            except Exception as e:
                sentry_sdk.capture_exception(e)

            wa_send_reservas_list(
                phone_number_id,
                from_msisdn,
                items,
                prompt_text="¿Qué reserva quieres cancelar?",
                session_id=session_id,
                page=1,
            )

            if resp2:
                wa_send_text(
                    phone_number_id,
                    from_msisdn,
                    resp2,
                    session_id=session_id,
                )
            return

        if ui == "peluqueros":
            wa_send_peluquero_list(
                phone_number_id,
                from_msisdn,
                peluqueria=pelu,
                include_any_option=True,
                session_id=session_id,
                page=1,
            )
            return

        # ---- Texto secundario (si existe) ----
        if resp2:
            wa_send_text(
                phone_number_id,
                from_msisdn,
                resp2,
                session_id=session_id,
            )

        # ---- Ultimo blindaje ----
        # Si el core respondio pero no habia UI ni textos, dejamos traza
        if not ui and not resp and not resp2:
            logging.warning(
                "Core responded without ui/resp. session=%s data=%s",
                session_id,
                {k: data.get(k) for k in ("ui", "respuesta", "respuesta2")},
            )

    except Exception as e:
        sentry_sdk.capture_exception(e)
        logging.error(
            "Error loopback webhook (async): %s",
            e,
            exc_info=True,
        )



@app.route("/webhook/whatsapp", methods=["GET"])
def whatsapp_verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == settings.WABA_VERIFY_TOKEN and challenge:
        return challenge, 200
    return "", 403


@app.route("/webhook/whatsapp", methods=["POST"])
@limiter.shared_limit(
    settings.RATE_LIMITS["WEBHOOK_PER_PELU"],
    scope=_pelu_rate_scope,)
def whatsapp_receive():
    # Verifica firma
    raw = request.get_data() or b""
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not verify_waba_signature(settings.WABA_APP_SECRET, raw, sig):
        return "", 403

    try:
        payload = request.get_json(force=True) or {}
    except Exception as e:
        sentry_sdk.capture_exception(e)
        return "", 200

    entries = payload.get("entry", [])
    for entry in entries:
        for change in entry.get("changes", []):
            value = change.get("value", {}) or {}
            metadata = value.get("metadata", {}) or {}
            phone_number_id = metadata.get("phone_number_id")
            messages = value.get("messages", []) or []
            if not phone_number_id or not messages:
                continue

            pelu = get_peluqueria_by_wa_phone_number_id(phone_number_id)
            if not pelu:
                logging.warning(f"phone_number_id desconocido: {phone_number_id}")
                continue

            for msg in messages:
                ts = _msg_ts(msg)
                if not should_process_by_ts(session_id=f"wa_{msg.get('from')}", ts=ts):
                    continue

                from_msisdn = msg.get("from")
                wamid = msg.get("id") or msg.get("wamid")
                if not from_msisdn:
                    continue

                if wamid:
                    seen_key = f"seen_wamid:{wamid}"
                    if storage.get(seen_key):
                        continue
                    storage.setex(seen_key, "1", ttl=60*60*24)

                origin = "text"
                msg_type = (msg.get("type") or "").strip().lower()

                texto = wa_extract_text(msg) or ""
                t = (texto or "").strip().lower()

                btn_id = None
                list_id = None
                if msg_type == "interactive":
                    inter = msg.get("interactive") or {}
                    itype = (inter.get("type") or "").strip().lower()
                    if itype == "button_reply":
                        btn = inter.get("button_reply") or {}
                        btn_id = (btn.get("id") or "").strip()
                        origin = "button"
                    elif itype == "list_reply":
                        lr = inter.get("list_reply") or {}
                        list_id = (lr.get("id") or "").strip()
                        origin = "list"
                elif msg_type == "button":
                    origin = "button"

                session_id = f"wa_{from_msisdn}"

                if list_id and list_id.startswith("RID_NEXT_"):
                    try:
                        page = int(list_id.split("_")[-1])
                    except Exception:
                        page = 1
                    raw = storage.get(f"reslist:{session_id}")
                    items = json.loads(raw) if raw else []
                    wa_send_reservas_list(
                        phone_number_id,
                        from_msisdn,
                        items,
                        prompt_text="¿Qué reserva quieres cancelar?",
                        session_id=session_id,
                        page=page
                    )
                    continue

                # Paginación y selección de SERVICIOS
                if list_id and list_id.startswith("SERV_"):
                    # 1) "Ver más servicios"
                    if re.fullmatch(r"SERV_NEXT_\d+", list_id):
                        try:
                            next_page = int(list_id.split("_")[2])
                        except Exception:
                            next_page = 1
                        wa_send_service_list(
                            phone_number_id,
                            from_msisdn,
                            pelu,
                            session_id=session_id,
                            page=next_page
                        )
                        continue  # ya hemos enviado la siguiente página

                    # 2) Fila paginada: SERV_P<page>_<idx> → déjalo pasar al core
                    if re.fullmatch(r"SERV_P\d+_\d+", list_id):
                        texto = list_id
                        t = texto.strip().lower()
                        origin = "list"

                    # 3) Compat con formato antiguo: SERV_<idx> → convertir a nombre
                    elif re.fullmatch(r"SERV_\d+", list_id):
                        try:
                            idx = int(list_id.split("_", 1)[1])
                            servicios = [s.nombre for s in (pelu.servicios or [])]
                            if 0 <= idx < len(servicios):
                                texto = servicios[idx]
                                t = texto.strip().lower()
                                origin = "list"
                        except Exception as e:
                            sentry_sdk.capture_exception(e)


                if list_id and list_id.startswith("PEL_"):
                    # 1) Ver más profesionales
                    if re.fullmatch(r"PEL_NEXT_\d+", list_id):
                        try:
                            next_page = int(list_id.split("_")[2])
                        except Exception:
                            next_page = 1
                        wa_send_peluquero_list(
                            phone_number_id,
                            from_msisdn,
                            peluqueria=pelu,
                            include_any_option=True,
                            session_id=session_id,
                            page=next_page
                        )
                        continue

                    # 2) Sin preferencia
                    if list_id.upper() == "PEL_ANY":
                        texto = "PEL_ANY"
                        origin = "list"

                    # 3) Selección concreta: PEL_P<page>_<idx_global>
                    elif re.fullmatch(r"PEL_P\d+_\d+", list_id):
                        texto = list_id
                        origin = "list"

                # Mapear selección de hora
                if list_id and list_id.startswith("HORA_"):
                    raw = storage.get(f"hours:{session_id}")
                    horas = json.loads(raw) if raw else []

                    if list_id.startswith("HORA_NEXT_"):
                        # Usuario pidió siguiente página
                        try:
                            page = int(list_id.split("_")[2])
                            wa_send_hours_page(phone_number_id, from_msisdn, session_id, horas, page=page)
                            continue  # no mandes al core todavía
                        except Exception as e:
                            sentry_sdk.capture_exception(e)
                            pass

                    elif list_id.startswith("HORA_P"):
                        try:
                            # ej. HORA_P2_13 → page=2, idx=13
                            _, page_idx = list_id.split("_", 1)
                            _, idx = page_idx.split("_", 1)
                            idx = int(idx)
                            if 0 <= idx < len(horas):
                                texto = horas[idx]  # ⚡ usar hora elegida como mensaje al core
                                t = texto.strip().lower()
                        except Exception as e:
                            sentry_sdk.capture_exception(e)
                            pass

                if list_id and list_id.startswith("RID_"):
                    # El core entenderá mensaje "RID_123" como seleccionar la reserva 123
                    texto = list_id
                    t = texto.strip().lower()

                # --- Botones menú principal ---
                if btn_id == "ACT_RESERVAR":
                    texto = "reservar"

                elif btn_id == "ACT_DUDA":
                    texto = "duda"

                elif btn_id == "ACT_CAN":
                    texto = "cancelar"

                idem = wamid or f"{from_msisdn}:{ts}"

                estado_actual = cargar_estado(session_id)
                if estado_actual is None:
                    estado_inicial = {"paso": "inicio", "datos": {}, "tipo_accion": None}
                    guardar_estado(session_id, estado_inicial)
                    wa_send_text(
                        phone_number_id,
                        from_msisdn,
                        welcome_text(getattr(pelu, "nombre", "Peluquería"), getattr(pelu, "tipo_negocio", "Peluquería")),
                        session_id=session_id,
                    )
                    wa_send_main_menu(
                        phone_number_id,
                        from_msisdn,
                        getattr(pelu, "nombre", "Peluquería"),
                        session_id=session_id,
                    )
                    continue

                # Comandos globales
                cmd = detect_global_command(texto)
                if cmd in {"menu", "reset", "salir", "volver"}:
                    reset_estado(session_id)
                    wa_send_text(
                        phone_number_id,
                        from_msisdn,
                        return_text(getattr(pelu, "nombre", "Peluquería"), getattr(pelu, "tipo_negocio", "Peluquería")),
                        session_id=session_id,
                    )
                    wa_send_main_menu(
                        phone_number_id,
                        from_msisdn,
                        getattr(pelu, "nombre", "Peluquería"),
                        session_id=session_id,
                    )
                    continue

                # Reenvío al core
                CORE_EXECUTOR.submit(_process_core_and_reply, phone_number_id, from_msisdn, session_id, texto, origin, idem)
                continue

    return "", 200


# ================================================
# Webhook de negocio (chat core)
# ================================================
@app.route("/webhook", methods=["POST"])
def api_post():
    try:
        data = request.get_json(force=True) or {}

        # Seguridad: solo API key por cabecera
        api_key = request.headers.get("X-API-KEY")
        session_id = data.get("session_id")
        mensaje = data.get("mensaje", "")
        origin = (data.get("origin") or "text").lower().strip()

        if not api_key or not session_id or not mensaje:
            return jsonify({
                "respuesta": "No he podido procesar tu mensaje ahora mismo. Inténtalo más tarde.",
                "ui": "main_menu"
            }), 200

        if not re.match(r"^[A-Za-z0-9_\-]{4,40}$", session_id):
            return jsonify({
                "respuesta": "No he podido continuar con la conversación. Inténtalo de nuevo.",
                "ui": "main_menu"
            }), 200

        # Rate limit por sesión (60s de ventana, configurable en settings)
        count = storage.incr(f"rl:{session_id}", ttl=60)
        if count > settings.RATE_LIMIT_PER_MIN:
            # Opcional: resetea la sesión para no atascar al usuario
            try:
                guardar_estado(session_id, {"paso": "inicio", "datos": {}, "tipo_accion": None, "force_welcome": True})
            except Exception as e:
                sentry_sdk.capture_exception(e)
                pass
            return jsonify({
                "respuesta": "Estoy recibiendo muchos mensajes seguidos 😅. Espera unos segundos y seguimos.",
                "ui": "main_menu"
            }), 200

        pelu = get_peluqueria_by_api_key(api_key)
        if not pelu:
            return jsonify({
                "respuesta": f"No he podido identificar la {getattr(pelu, 'tipo_negocio', 'negocio')}. Inténtalo más tarde.",
                "ui": "main_menu"
            }), 200

        # Estado
        estado = cargar_estado(session_id)
        if not estado:
            estado = {"paso": "inicio", "datos": {}, "tipo_accion": None}
            guardar_estado(session_id, estado)
            return jsonify({"respuesta": welcome_text(pelu.nombre, pelu.tipo_negocio), "ui": "main_menu"})

        # Comandos globales → reset a menú
        cmd = detect_global_command(mensaje)
        if cmd in {"menu", "reset", "salir", "volver"}:
            guardar_estado(session_id, {"paso": "inicio", "datos": {}, "tipo_accion": None})
            return jsonify({"respuesta": return_text(pelu.nombre, pelu.tipo_negocio), "ui": "main_menu"})

        if estado.get("force_welcome"):
            estado["force_welcome"] = False
            guardar_estado(session_id, estado)
            return jsonify({"respuesta": welcome_text(pelu.nombre, pelu.tipo_negocio), "ui": "main_menu"})

        # ---------------------------------------------------------
        # INICIO → detecta intención
        # ---------------------------------------------------------
        if estado["paso"] == "inicio":
            if origin in ("button", "list"):
                tipo_accion = _INTENT_MAP.get(_norm_min(mensaje))
            else:
                tipo_accion = intencion_desde_texto_o_ia(mensaje, pelu, origin=origin)
            if tipo_accion == "reservar":
                estado["tipo_accion"] = "reservar"
                servicios_disp = [s.nombre for s in pelu.servicios]
                if len(servicios_disp) == 1:
                    estado["paso"] = "fecha"
                    set_servicio_en_datos(estado["datos"], pelu.servicios[0])
                    guardar_estado(session_id, estado)
                    return jsonify({"respuesta": "¿Para qué fecha quieres la cita?\n(dd/mm/aaaa)📅"})
                else:
                    estado["paso"] = "servicio"
                    guardar_estado(session_id, estado)
                    return jsonify({"respuesta": "¿Qué servicio deseas reservar?⬇️", "ui": "services"})

            if tipo_accion == "cancelar":
                estado.update({"tipo_accion": "cancelar", "paso": "buscar"})
                guardar_estado(session_id, estado)
                return jsonify({"respuesta": "Dime el teléfono📞 con el que hiciste la reserva que quieres cancelar."})

            if tipo_accion == "duda":
                estado.update({"tipo_accion": "duda", "paso": "duda"})
                guardar_estado(session_id, estado)
                return jsonify({"respuesta": "Escríbeme tu duda y te ayudo con lo que necesites.❓"})

            guardar_estado(session_id, estado)
            return jsonify({
                "respuesta": "Por favor, elige una opción de las disponibles:",
                "ui": "main_menu"
            })
        # ---------------------------------------------------------
        # FLUJO: RESERVAR
        # ---------------------------------------------------------
        if estado["tipo_accion"] == "reservar":
            datos = estado["datos"]

            # (1) Servicio
            if estado["paso"] == "servicio":
                servicio = None
                msg_norm = (mensaje or "").strip()

                if origin == "list":
                    # A) Fila paginada: SERV_P<page>_<idx>
                    if re.fullmatch(r"SERV_P\d+_\d+", msg_norm, flags=re.IGNORECASE):
                        try:
                            idx = int(msg_norm.split("_")[-1])
                            servicios = list(pelu.servicios or [])
                            if 0 <= idx < len(servicios):
                                servicio = servicios[idx]
                        except Exception:
                            servicio = None

                    # B) Formato legacy: SERV_<idx>  (compatibilidad)
                    elif re.fullmatch(r"SERV_\d+", msg_norm, flags=re.IGNORECASE):
                        try:
                            idx = int(msg_norm.split("_", 1)[1])
                            servicios = list(pelu.servicios or [])
                            if 0 <= idx < len(servicios):
                                servicio = servicios[idx]
                        except Exception:
                            servicio = None

                    # C) Cualquier otro id/texto: intenta resolver por nombre
                    else:
                        servicio = _elegir_servicio_desde_texto(pelu, msg_norm, None)

                else:
                    # Texto libre: IA + matching por nombre
                    try:
                        servicio_nombre_ai = interpreta_ia(msg_norm, "servicio", pelu)
                    except Exception as e:
                        sentry_sdk.capture_exception(e)
                        servicio_nombre_ai = None

                    servicio = _elegir_servicio_desde_texto(pelu, msg_norm, servicio_nombre_ai)

                if not servicio:
                    return jsonify({
                        "respuesta": "Por favor, selecciona un servicio de la lista o escribe el nombre.",
                        "ui": "services"
                    })

                set_servicio_en_datos(datos, servicio)

                if getattr(pelu, "enable_peluquero_selection", True):
                    estado["paso"] = "peluquero"
                    guardar_estado(session_id, estado)
                    return jsonify({"respuesta": "¿Con quién te gustaría reservar?", "ui": "peluqueros"})
                else:
                    estado["paso"] = "fecha"
                    guardar_estado(session_id, estado)
                    return jsonify({"respuesta": "¿Para qué fecha quieres la cita?\n(dd/mm/aaaa)📅"})

            #(1,5) Peluquero
            if estado["paso"] == "peluquero":
                msg = (mensaje or "").strip()

                # Si viene de lista interactiva
                if origin == "list":
                    # "Sin preferencia"
                    if msg.upper() == "PEL_ANY":
                        datos["peluquero_id"] = None
                        datos["peluquero_nombre"] = None
                        estado["paso"] = "fecha"
                        guardar_estado(session_id, estado)
                        return jsonify({"respuesta": "¿Para qué fecha quieres la cita?\n(dd/mm/aaaa)📅"})

                    # Fila paginada: PEL_P<page>_<idx_global>
                    m = re.fullmatch(r"PEL_P\d+_(\d+)", msg, flags=re.IGNORECASE)
                    if m:
                        idx = int(m.group(1))
                        try:
                            # obtenemos el listado ordenado de activos
                            with SessionLocal() as _db:
                                activos = get_active_peluqueros(_db, pelu.id) or []
                            if 0 <= idx < len(activos):
                                sel = activos[idx]
                                datos["peluquero_id"] = getattr(sel, "id", None)
                                datos["peluquero_nombre"] = getattr(sel, "nombre", "")
                                estado["paso"] = "fecha"
                                guardar_estado(session_id, estado)
                                return jsonify(
                                    {"respuesta": "¿Para qué fecha quieres la cita?\n(dd/mm/aaaa)📅"})
                        except Exception:
                            pass

                    # Cualquier otro id: re-mostrar lista
                    guardar_estado(session_id, estado)
                    return jsonify({"respuesta": "Elige un profesional de la lista, por favor.", "ui": "peluqueros"})

                # Texto libre: intenta match por nombre
                try:
                    with SessionLocal() as _db:
                        activos = get_active_peluqueros(_db, pelu.id) or []
                    tnorm = (msg or "").strip().lower()
                    match = next((p for p in activos if (p.nombre or "").strip().lower() == tnorm), None)
                    if match:
                        datos["peluquero_id"] = getattr(match, "id", None)
                        datos["peluquero_nombre"] = getattr(match, "nombre", "")
                        estado["paso"] = "fecha"
                        guardar_estado(session_id, estado)
                        return jsonify({"respuesta": "¿Para qué fecha quieres la cita?\n(dd/mm/aaaa)📅"})
                except Exception:
                    pass

                # Si no entiendo, reenvío lista
                guardar_estado(session_id, estado)
                return jsonify({"respuesta": "¿Con quién te gustaría reservar?", "ui": "peluqueros"})

            # (2) Fecha
            if estado["paso"] == "fecha":
                fecha_str = interpreta_fecha(mensaje, pelu)

                if not fecha_str or fecha_str.upper() == "NO_ENTIENDO":
                    return jsonify({
                        "respuesta": f"Por favor, elige una fecha correcta (dd/mm/aaaa)."
                    })

                # Validación de negocio (no parsing): existencia, pasado, día cerrado
                try:
                    y, mo, d = map(int, re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", fecha_str).groups())
                    fecha_obj = datetime(y, mo, d).date()
                    fecha_str = fecha_obj.strftime("%Y-%m-%d")  # normalizado
                except Exception as e:
                    sentry_sdk.capture_exception(e)
                    return jsonify({"respuesta": "La fecha no existe, prueba con otra fecha (dd/mm/aaaa)."})

                now = now_local(pelu)
                hoy = now.date()
                if fecha_obj < hoy:
                    return jsonify({"respuesta": "No puedes reservar para una fecha pasada, elige otra fecha."})

                # 1) Validación de días de la semana cerrados
                dias_cerrados = [d.strip().lower() for d in (pelu.dias_cerrados or "").split(",") if d.strip()]
                nombre_dia = fecha_obj.strftime("%A").lower()
                nombre_dia_es = DIAS_EN_ES.get(nombre_dia, nombre_dia) if 'DIAS_EN_ES' in globals() else nombre_dia
                if nombre_dia_es in dias_cerrados:
                    return jsonify({"respuesta": f"La {getattr(pelu, 'tipo_negocio', 'negocio')} cierra el {nombre_dia_es}🔒, elige otra fecha."})

                # 2) Validación de días concretos y recurrentes (JSON dias_cerrados_anio)
                #    Estructura esperada: {"dates": ["YYYY-MM-DD", ...], "recurring": ["MM-DD", ...]}
                import json  # por si el campo viniera como string JSON

                closed_raw = getattr(pelu, "dias_cerrados_anio", None) or {}
                if isinstance(closed_raw, str):
                    try:
                        closed_json = json.loads(closed_raw) or {}
                    except Exception:
                        closed_json = {}
                elif isinstance(closed_raw, dict):
                    closed_json = closed_raw
                else:
                    closed_json = {}

                # Fechas puntuales (YYYY-MM-DD)
                festivos = closed_json.get("dates", []) if isinstance(closed_json, dict) else []
                festivos_set = {str(f).strip() for f in festivos if f}
                if fecha_str in festivos_set:
                    return jsonify(
                        {"respuesta": f"La {getattr(pelu, 'tipo_negocio', 'negocio')} está cerrada el {formatea_fecha_es(fecha_str)} (festivo) 🔒, elige otra fecha."})

                # Recurrentes (normalizar a MM-DD)
                def _norm_mmdd(x):
                    if not x:
                        return None
                    s = str(x).strip().replace("/", "-")
                    parts = s.split("-")
                    if len(parts) == 2:
                        mm = parts[0].zfill(2)
                        dd = parts[1].zfill(2)
                        return f"{mm}-{dd}"
                    if len(parts) == 3:
                        # si viniera como YYYY-MM-DD, extraemos MM-DD
                        try:
                            return f"{int(parts[1]):02d}-{int(parts[2]):02d}"
                        except Exception:
                            return None
                    return None

                recurrentes = closed_json.get("recurring", []) if isinstance(closed_json, dict) else []
                recurrentes_set = {r for r in (_norm_mmdd(x) for x in recurrentes) if r}

                mmdd = fecha_obj.strftime("%m-%d")
                if mmdd in recurrentes_set:
                    return jsonify({"respuesta": f"La {getattr(pelu, 'tipo_negocio', 'negocio')} cierra ese día (festivo) 🔒, elige otra fecha."})

                # 3) Validación de rango permitido
                fuera, _ = _fecha_fuera_de_rango(fecha_obj, pelu)
                if fuera:
                    return jsonify({
                        "respuesta": (
                            "No se admiten reservas tan futuras. Elige una fecha anterior, por favor."
                        )
                    })

                # ✅ Si pasa todas las validaciones, se guarda la fecha
                datos["fecha"] = fecha_str
                estado["paso"] = "hora"
                guardar_estado(session_id, estado)

                # Horas disponibles
                db = SessionLocal()
                try:
                    servicio_sel = get_servicio_from_datos(pelu, datos)
                    if datos.get("peluquero_id"):
                        horas = horas_disponibles_para_peluquero(db, pelu, servicio_sel, datos["peluquero_id"],datos["fecha"])
                    else:
                        horas = horas_disponibles_cached(db, pelu, servicio_sel, datos["fecha"])

                    horas = filtra_horas_desde_ahora(pelu, horas, datos["fecha"])
                    horas = _filtra_horas_por_horario_json(
                        horas, fecha_obj, getattr(pelu, "horario", None)
                    )
                finally:
                    db.close()

                if not horas:
                    estado["paso"] = "fecha"
                    guardar_estado(session_id, estado)

                    servicio_sel = get_servicio_from_datos(pelu, datos)
                    sugeridas = _proximas_fechas_con_hueco(
                        pelu, servicio_sel, fecha_obj, max_items=5, peluquero_id=datos.get("peluquero_id")
                    )

                    if fecha_obj == hoy:
                        msg = "Para hoy ya no quedan horas libres"
                    else:
                        msg = "Ese día está completo"

                    if sugeridas:
                        msg += ". Fechas próximas con hueco:\n" + "\n".join(sugeridas)
                    msg += "\n\nElige otra fecha📅."

                    return jsonify({"respuesta": msg})

                return jsonify({
                    "respuesta": "¿A qué hora quieres tu cita?🕔",
                    "ui": "hours",
                    "choices": horas
                })

            # (3) Hora
            if estado["paso"] == "hora":

                db = SessionLocal()
                try:
                    servicio_sel = get_servicio_from_datos(pelu, datos)
                    if datos.get("peluquero_id"):
                        horas = horas_disponibles_para_peluquero(db, pelu, servicio_sel, datos["peluquero_id"],datos["fecha"])
                    else:
                        horas = horas_disponibles_cached(db, pelu, servicio_sel, datos["fecha"])
                    horas = filtra_horas_desde_ahora(pelu, horas, datos["fecha"])
                    try:
                        y, mo, d = map(int, datos["fecha"].split("-"))
                        _fecha_obj = datetime(y, mo, d).date()
                    except Exception:
                        _fecha_obj = now_local(pelu).date()
                    horas = _filtra_horas_por_horario_json(
                        horas, _fecha_obj, getattr(pelu, "horario", None)
                    )
                finally:
                    db.close()

                if not horas:
                    estado["paso"] = "fecha"
                    guardar_estado(session_id, estado)

                    servicio_sel = get_servicio_from_datos(pelu, datos)
                    try:
                        y, mo, d = map(int, datos["fecha"].split("-"))
                        _fecha_obj = datetime(y, mo, d).date()
                    except Exception:
                        _fecha_obj = now_local(pelu).date()

                    sugeridas = _proximas_fechas_con_hueco(
                        pelu, servicio_sel, _fecha_obj, max_items=5, peluquero_id=datos.get("peluquero_id")
                    )

                    hoy_str = now_local(pelu).strftime("%Y-%m-%d")
                    if datos.get("fecha") == hoy_str:
                        msg = "Para hoy ya no quedan horas disponibles"
                    else:
                        msg = "No hay horas libres ese día"

                    if sugeridas:
                        msg += ". Fechas próximas con hueco:\n" + "\n".join(sugeridas)
                    msg += "\nElige otra fecha, por favor."

                    return jsonify({"respuesta": msg})

                raw_hours = storage.get(f"hours:{session_id}")
                horas_all = []
                import json
                if raw_hours:
                    try:
                        horas_all = json.loads(raw_hours) or []
                    except Exception as e:
                        sentry_sdk.capture_exception(e)
                        horas_all = []

                # Si el usuario responde con "N" y tenemos lista en storage → selecciona por índice
                mnum = re.fullmatch(r"\d{1,2}", (mensaje or "").strip())
                if mnum and horas_all:
                    idx = int(mnum.group()) - 1
                    if 0 <= idx < len(horas_all):
                        datos["hora"] = horas_all[idx]
                        estado["paso"] = "nombre"
                        guardar_estado(session_id, estado)
                        return jsonify({"respuesta": "¿A nombre de quién hacemos la reserva?"})

                # 🔹 Si viene de lista interactiva y el texto ya es la hora exacta → aceptar directo
                if origin == "list" and (mensaje in horas or mensaje in horas_all):
                    datos["hora"] = mensaje
                    estado["paso"] = "nombre"
                    guardar_estado(session_id, estado)
                    return jsonify({"respuesta": "¿A nombre de quién hacemos la reserva?"})

                # 🔹 Intentar interpretar una hora libre escrita por el usuario
                parsed = normaliza_hora_ia(mensaje)
                if not parsed:
                    guardar_estado(session_id, estado)
                    # devolvemos TODAS las horas; la capa WA las pagina (máx 10 por página) o hace fallback en texto si falla
                    return jsonify({
                        "respuesta": "No he entendido la hora, estas son las horas disponibles:",
                        "ui": "hours",
                        "choices": horas
                    })

                decision = elegir_hora_final(horas, parsed)

                if decision.get("need_am_pm"):
                    datos["hora_candidatas"] = decision["candidatas"]
                    estado["paso"] = "confirma_am_pm"
                    guardar_estado(session_id, estado)
                    return jsonify({
                        "respuesta": f"¿Es por la mañana ({decision['candidatas'][0]}) o por la tarde ({decision['candidatas'][1]})?"})

                if not decision.get("ok"):
                    guardar_estado(session_id, estado)
                    return jsonify({
                        "respuesta": f"Por favor, elige una hora de las disponibles.",
                        "ui": "hours",
                        "choices": horas
                    })

                hora_str = decision["hora"]
                datos["hora"] = hora_str
                estado["paso"] = "nombre"
                guardar_estado(session_id, estado)
                return jsonify({"respuesta": "¿A nombre de quién hacemos la reserva?"})

            # (3b) Aclaración am/pm
            if estado["paso"] == "confirma_am_pm":
                cands = datos.get("hora_candidatas") or []
                if len(cands) != 2:
                    estado["paso"] = "hora"
                    guardar_estado(session_id, estado)
                    return jsonify({"respuesta": "Vuelvo a preguntarte la hora. ¿Cuál te viene bien?"})

                t = (mensaje or "").lower().strip()

                db = SessionLocal()
                try:
                    servicio_sel = get_servicio_from_datos(pelu, datos)
                    if datos.get("peluquero_id"):
                        horas = horas_disponibles_para_peluquero(db, pelu, servicio_sel, datos["peluquero_id"], datos["fecha"])
                    else:
                        horas = horas_disponibles_cached(db, pelu, servicio_sel, datos["fecha"])
                    horas = filtra_horas_desde_ahora(pelu, horas, datos["fecha"])
                finally:
                    db.close()

                am_opt, pm_opt = cands[0], cands[1]
                am_ok = am_opt in horas
                pm_ok = pm_opt in horas

                if not am_ok and not pm_ok:
                    if not horas:
                        estado["paso"] = "fecha"
                        guardar_estado(session_id, estado)

                        servicio_sel = get_servicio_from_datos(pelu, datos)
                        try:
                            f_obj = datetime.strptime(datos["fecha"], "%Y-%m-%d").date()
                        except Exception:
                            f_obj = now_local(pelu).date()

                        sugeridas = _proximas_fechas_con_hueco(
                            pelu, servicio_sel, f_obj, max_items=5, peluquero_id=datos.get("peluquero_id")
                        )

                        msg = "Esa opción ya no está disponible"
                        if sugeridas:
                            msg += ". Fechas próximas con hueco:\n" + "\n".join(sugeridas)
                        msg += "\nElige otra fecha, por favor."

                        return jsonify({"respuesta": msg})
                    else:
                        estado["paso"] = "hora"
                        guardar_estado(session_id, estado)
                        return jsonify({
                            "respuesta": f"Esa opción ya no está disponible.",
                            "ui": "hours",
                            "choices": horas
                        })

                if am_ok and not pm_ok:
                    elegido = am_opt
                elif pm_ok and not am_ok:
                    elegido = pm_opt
                else:
                    if any(w in t for w in AM_WORDS):
                        elegido = am_opt
                    elif any(w in t for w in PM_WORDS):
                        elegido = pm_opt
                    elif t in cands:
                        elegido = t
                    else:
                        literal = _extract_hhmm_from_text(t)
                        if literal and literal in cands and literal in horas:
                            elegido = literal
                        else:
                            alt = normaliza_hora_ia(mensaje)
                            elegido = None
                            if alt and not alt["ambigua"]:
                                h, m, clue = alt["h"], alt["m"], alt["clue"]
                                if clue == "am" and h == 12:
                                    h = 0
                                if clue == "pm" and 1 <= h <= 11:
                                    h = h + 12
                                cand = _format_hm(h, m)
                                if cand in (am_opt, pm_opt) and cand in horas:
                                    elegido = cand
                            if not elegido:
                                guardar_estado(session_id, estado)
                                return jsonify({"respuesta": f"¿Por la mañana ({am_opt}) o por la tarde ({pm_opt})?"})

                now = now_local(pelu)
                hoy = now.date()
                if datetime.strptime(datos["fecha"], "%Y-%m-%d").date() == hoy:
                    hh, mm = map(int, elegido.split(":")[:2])
                    chosen = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                    if chosen <= now:
                        estado["paso"] = "fecha"
                        guardar_estado(session_id, estado)
                        return jsonify(
                            {"respuesta": "No puedes reservar para una hora que ya ha pasado, indica otra fecha."})

                datos["hora"] = elegido
                datos.pop("hora_candidatas", None)
                estado["paso"] = "nombre"
                guardar_estado(session_id, estado)
                return jsonify({"respuesta": "¿A nombre de quién hacemos la reserva?"})

            # (4) Nombre
            if estado["paso"] == "nombre":
                nombre = mensaje.strip()

                if not nombre or len(nombre) < 2:
                    return jsonify({"respuesta": "No entendí el nombre, ¿Puedes escribirlo de nuevo?"})

                if re.fullmatch(r"[0-9\W_]+", nombre):
                    return jsonify({"respuesta": "Ese nombre no es válido, ¿Puedes escribirlo de nuevo?"})

                datos["nombre"] = nombre
                estado["paso"] = "telefono"
                guardar_estado(session_id, estado)
                return jsonify({"respuesta": "¿Cuál es tu número de teléfono?"})

            # (5) Teléfono -> Resumen
            if estado["paso"] == "telefono":
                telefono = interpreta_telefono(mensaje, getattr(pelu, 'country_code', None) or 'ES')
                if not telefono:
                    return jsonify({"respuesta": "El teléfono no es válido, ¿Puedes escribirlo de nuevo con el prefijo del país?"})
                datos["telefono"] = telefono
                estado["paso"] = "confirmar"
                guardar_estado(session_id, estado)

                pel_line = f"Peluquero/a: {datos.get('peluquero_nombre')}\n" if datos.get("peluquero_nombre") else ""

                if len(pelu.servicios) == 1:
                    resumen = (
                        "Resumen de tu reserva:\n"
                        f"{pel_line}"
                        f"Fecha: {formatea_fecha_es(datos['fecha'])}\n"
                        f"Hora: {datos['hora']}\n"
                        f"Nombre: {datos['nombre']}\n"
                        f"Teléfono: {datos['telefono']}\n"
                        "¿Confirmas la reserva? (*si*/*no*)"
                    )
                else:
                    resumen = (
                        "Resumen de tu reserva:\n"
                        f"Servicio: {datos.get('servicio_nombre', '')}\n"
                        f"{pel_line}"
                        f"Fecha: {formatea_fecha_es(datos['fecha'])}\n"
                        f"Hora: {datos['hora']}\n"
                        f"Nombre: {datos['nombre']}\n"
                        f"Teléfono: {datos['telefono']}\n"
                        "¿Confirmas la reserva? (*si*/*no*)"
                    )
                return jsonify({"respuesta": resumen})

            # (6) Confirmación final (idempotente y robusta)
            if estado["paso"] == "confirmar":
                text_in = (mensaje or "").strip().lower()

                # --- Caso NEGACIÓN ---
                if any(w in text_in for w in DENIAL_WORDS):
                    reset_estado(session_id)
                    return jsonify({
                        "respuesta": "De acuerdo👌🏼, no confirmamos la reserva.",
                        "ui": "main_menu"
                    })

                # --- Caso AFIRMACIÓN ---
                elif any(w in text_in for w in AFFIRM_WORDS):
                    horas_fresh = None
                    explicit_key = request.headers.get("Idempotency-Key")
                    payload = {
                        "fecha": datos.get("fecha"),
                        "hora": datos.get("hora"),
                        "servicio_id": datos.get("servicio_id"),
                        "telefono": datos.get("telefono"),
                    }
                    idem_key, cached = idem_get("reservar_confirm", pelu.id, payload, explicit_key)
                    if cached:
                        sentry_event("idem.hit", action="reservar_confirm", key=idem_key)
                        return jsonify(cached["json"]), cached["status"]

                    try:
                        # (1) PRIMERO: asegurar hueco en BD (sin crear evento aún) con reintentos si hay lock
                        retries = 0
                        while True:
                            with sentry_span("db.reserva.create", "guardar_reserva_db"):
                                res = guardar_reserva_db(
                                    pelu.id,
                                    datos["servicio_id"],
                                    datos["nombre"],
                                    datos["telefono"],
                                    datos["fecha"],
                                    datos["hora"],
                                    event_id=None  # aún no hay evento en Calendar
                                )

                            # éxito o error real (no_slot) -> salimos del bucle
                            if not (isinstance(res, dict) and res.get("error") == "lock_timeout"):
                                break

                            retries += 1
                            sentry_event("db.lock_retry", level="warning", retries=retries,
                                         fecha=datos["fecha"], hora=datos["hora"],
                                         peluquero_id=datos.get("peluquero_id"))

                            if retries >= MAX_LOCK_RETRIES:
                                guardar_estado(session_id, estado)
                                body = {
                                    "respuesta": "Estoy terminando de reservar. Confirma de nuevo en unos segundos."}
                                # Importante: no cachear (no llamamos a idem_set)
                                return jsonify(body), 200

                            _sleep_backoff(retries)  # 150ms, 300ms...
                            retries += 1

                        if not isinstance(res, int) and not (
                                isinstance(res, dict) and res.get("error") in ("no_slot",)
                        ):
                            guardar_estado(session_id, estado)
                            body = {
                                "respuesta": "No he podido confirmar ahora mismo. Vuelve a intentarlo en unos segundos, por favor."
                            }
                            # No cacheamos este estado incierto
                            return jsonify(body), 200

                        # (1a) Manejo explícito de errores de BD
                        if isinstance(res, dict) and res.get("error") == "no_slot":
                            # Recalcular HORAS FRESCAS (SIN caché)
                            try:
                                sentry_event("db.no_slot_conflict", level="warning",
                                             fecha=datos["fecha"], hora=datos["hora"],
                                             peluquero_id=datos.get("peluquero_id"),
                                             servicio_id=datos.get("servicio_id"))
                                servicio_sel = get_servicio_from_datos(pelu, datos)
                                with SessionLocal() as _db2:
                                    if datos.get("peluquero_id"):
                                        horas_fresh = horas_disponibles_para_peluquero(_db2, pelu, servicio_sel,datos["peluquero_id"], datos["fecha"]) or []
                                    else:
                                        horas_fresh = horas_disponibles(_db2, pelu, servicio_sel, datos["fecha"]) or []
                                # Filtro 'desde ahora' con fallback (no dejar vacío)
                                try:
                                    filtradas = filtra_horas_desde_ahora(pelu, horas_fresh, datos["fecha"])
                                    horas_fresh = filtradas or horas_fresh
                                except Exception:
                                    pass
                            except Exception as _e:
                                horas_fresh = []
                                try:
                                    sentry_sdk.capture_exception(_e)
                                except Exception:
                                    pass

                            if not horas_fresh:
                                estado["paso"] = "fecha"
                                guardar_estado(session_id, estado)

                                servicio_sel = get_servicio_from_datos(pelu, datos)
                                try:
                                    f_obj = datetime.strptime(datos["fecha"], "%Y-%m-%d").date()
                                except Exception:
                                    f_obj = now_local(pelu).date()

                                sugeridas = _proximas_fechas_con_hueco(
                                    pelu, servicio_sel, f_obj, max_items=5, peluquero_id=datos.get("peluquero_id")
                                )

                                msg = "Esa hora se acaba de ocupar y ya no quedan huecos ese día 😬"
                                if sugeridas:
                                    msg += ". Fechas próximas con hueco:\n" + "\n".join(sugeridas)
                                msg += "\nElige otra fecha, por favor."

                                body = {"respuesta": msg}
                                idem_set(idem_key, 200, body)
                                return jsonify(body), 200
                            else:
                                estado["paso"] = "hora"
                                guardar_estado(session_id, estado)
                                body = {
                                    "respuesta": "Esa hora se acaba de ocupar 😬. Elige otra disponible:",
                                    "ui": "hours",
                                    "choices": horas_fresh
                                }
                                idem_set(idem_key, 200, body)
                                return jsonify(body), 200

                        # (1b) Éxito BD → tenemos ID de la reserva
                        reserva_id = int(res)

                        # (2) AHORA crear el evento en Google Calendar (idempotente por reserva)
                        gcal_key = f"{pelu.id}:{datos['fecha']}:{datos['hora']}:{reserva_id}"

                        servicio_sel = get_servicio_from_datos(pelu, datos)
                        with sentry_span("calendar.create", "crear_reserva_google_idempotente"):
                            gcal = crear_reserva_google_idempotente(
                                peluqueria=pelu,
                                datos={
                                    "fecha": datos["fecha"],
                                    "hora": datos["hora"],
                                    "servicio": servicio_sel,
                                    "nombre": datos["nombre"],
                                    "telefono": datos["telefono"],
                                    "peluquero": datos.get("peluquero_nombre"),
                                },
                                private_key=gcal_key
                            )
                        event_id = gcal.get("event_id") if gcal and gcal.get("success") else None

                        # --- NUEVO: si Calendar avisa que se superó la capacidad, revertimos la reserva en BD
                        if not (gcal and gcal.get("success")) and gcal and gcal.get(
                                "error") == "no_slot_calendar_capacity":
                            try:
                                # Cancela/compensa la reserva en BD, ya que Calendar no la acepta por concurrencia real.
                                sentry_event("calendar.capacity_exceeded", level="warning",
                                             fecha=datos["fecha"], hora=datos["hora"],
                                             capacidad=getattr(pelu, "num_peluqueros", 1))
                                cancelar_reserva_db(reserva_id)
                            except Exception as _e:
                                try:
                                    sentry_sdk.capture_exception(_e)
                                except Exception:
                                    pass

                            # Recalcular horas frescas directamente desde Calendar (sin caché)
                            horas_fresh = []
                            try:
                                servicio_sel = get_servicio_from_datos(pelu, datos)
                                with SessionLocal() as _db2:
                                    if datos.get("peluquero_id"):
                                        horas_fresh = horas_disponibles_para_peluquero(_db2, pelu, servicio_sel,datos["peluquero_id"],datos["fecha"]) or []
                                    else:
                                        horas_fresh = horas_disponibles(_db2, pelu, servicio_sel, datos["fecha"]) or []
                                try:
                                    filtradas = filtra_horas_desde_ahora(pelu, horas_fresh, datos["fecha"])
                                    horas_fresh = filtradas or horas_fresh
                                except Exception:
                                    pass
                            except Exception as _e:
                                try:
                                    sentry_sdk.capture_exception(_e)
                                except Exception:
                                    pass

                            # Purga de caché de horas por si acaso
                            try:
                                purge_horas_cache(pelu, datos["fecha"])
                            except Exception as _e:
                                try:
                                    sentry_sdk.capture_exception(_e)
                                except Exception:
                                    pass

                            if not horas_fresh:
                                estado["paso"] = "fecha"
                                guardar_estado(session_id, estado)

                                servicio_sel = get_servicio_from_datos(pelu, datos)
                                try:
                                    f_obj = datetime.strptime(datos["fecha"], "%Y-%m-%d").date()
                                except Exception:
                                    f_obj = now_local(pelu).date()

                                sugeridas = _proximas_fechas_con_hueco(
                                    pelu, servicio_sel, f_obj, max_items=5, peluquero_id=datos.get("peluquero_id")
                                )

                                msg = "Esa hora se acaba de ocupar y ya no quedan huecos ese día 😬"
                                if sugeridas:
                                    msg += ". Fechas próximas con hueco:\n" + "\n".join(sugeridas)
                                msg += "\nElige otra fecha, por favor."

                                body = {"respuesta": msg}
                                idem_set(idem_key, 200, body)
                                sentry_event("calendar.no_hours_after_conflict", level="warning",
                                             fecha=datos["fecha"], peluquero_id=datos.get("peluquero_id"))
                                return jsonify(body), 200
                            else:
                                estado["paso"] = "hora"
                                guardar_estado(session_id, estado)
                                body = {
                                    "respuesta": "Esa hora se acaba de ocupar 😬. Elige otra disponible:",
                                    "ui": "hours",
                                    "choices": horas_fresh
                                }
                                idem_set(idem_key, 200, body)
                                sentry_event("calendar.no_hours_after_conflict", level="warning",
                                             fecha=datos["fecha"], peluquero_id=datos.get("peluquero_id"))
                                return jsonify(body), 200

                        # (2b) Guardar event_id en BD si está disponible
                        try:
                            if event_id:
                                set_event_id_db(reserva_id, event_id)
                        except Exception as _e:
                            try:
                                sentry_sdk.capture_exception(_e)
                            except Exception:
                                pass

                        # (3) Purga de caché de horas del día afectado
                        try:
                            purge_horas_cache(pelu, datos["fecha"])
                        except Exception as _e:
                            try:
                                sentry_sdk.capture_exception(_e)
                            except Exception:
                                pass

                        # (4) Post-confirm OK
                        estado["paso"] = "post_confirm"
                        guardar_estado(session_id, estado)
                        body = {
                            "respuesta": (
                                f"✅ ¡Reserva confirmada en {pelu.nombre}! "
                                f"Te espero el {formatea_fecha_es(datos['fecha'])} a las {datos['hora']}."
                            ),
                            "respuesta2": "¿Quieres hacer algo más? (*si*/*no*)",
                        }
                        idem_set(idem_key, 200, body)
                        return jsonify(body), 200

                    except Exception as e:
                        try:
                            sentry_sdk.capture_exception(e)
                        except Exception:
                            pass
                        logging.error(f"Error confirmando reserva: {e}", exc_info=True)
                        guardar_estado(session_id,
                                       {"paso": "inicio", "datos": {}, "tipo_accion": None, "force_welcome": True})
                        body = {"respuesta": "Ocurrió un error al confirmar la reserva, inténtalo de nuevo.",
                                "ui": "main_menu"}
                        idem_set(idem_key, 500, body)
                        return jsonify(body), 500

                # --- Caso NEUTRO / Irrelevante ---
                guardar_estado(session_id, estado)
                return jsonify({
                    "respuesta": (
                        "👉 Responde *si* para confirmarla\n"
                        "👉 Responde *no* para cancelarla"
                    )
                })

            # (6b) Post confirmación: ¿algo más?
            if estado["paso"] == "post_confirm":
                text_in = (mensaje or "").strip().lower()

                if any(w in text_in for w in DENIAL_WORDS):
                    guardar_estado(session_id,{"paso": "inicio", "datos": {}, "tipo_accion": None, "force_welcome": True})
                    return jsonify({"respuesta": "¡Genial! Gracias por reservar. ¡Que tengas un buen día! 👋"})

                if any(w in text_in for w in AFFIRM_WORDS):
                    guardar_estado(session_id, {"paso": "inicio", "datos": {}, "tipo_accion": None})
                    return jsonify({
                        "respuesta": "Perfecto, te muestro el menú para continuar.",
                        "ui": "main_menu"
                    })

                guardar_estado(session_id, estado)
                return jsonify({"respuesta": "¿Quieres hacer algo más? (*si*/*no*)"})

        # ---------------------------------------------------------
        # FLUJO: CANCELAR
        # ---------------------------------------------------------
        elif estado["tipo_accion"] == "cancelar":
            datos = estado["datos"]

            # Fast-path selección desde lista
            if re.fullmatch(r"RID_\d+", (mensaje or "").strip()) and estado["paso"] in {
                "seleccionar_reserva",
                "seleccionar_reserva_cancelar",
                "buscar",
                "cancelar_seleccionar_fecha",
                "cancelar_seleccionar_hora",
            }:
                rid = int(mensaje.split("_", 1)[1])

                db = SessionLocal()
                try:
                    r = db.query(Reserva).options(selectinload(Reserva.servicio)).filter_by(id=rid).first()
                finally:
                    db.close()
                if not r:
                    estado["paso"] = "buscar"
                    guardar_estado(session_id, estado)
                    return jsonify(
                        {"respuesta": "No encontré esa reserva. Escribe el teléfono con el que hiciste la reserva."})

                if not es_reserva_futura(r.fecha, r.hora):
                    tel = getattr(r, "telefono", None) or datos.get("telefono")
                    if tel:
                        db = SessionLocal()
                        try:
                            reservas = (
                                db.query(Reserva)
                                .options(selectinload(Reserva.servicio))
                                .filter(
                                    Reserva.peluqueria_id == pelu.id,
                                    Reserva.telefono == tel
                                )
                                .order_by(Reserva.fecha.desc(), Reserva.hora.desc())
                                .all()
                            )
                        finally:
                            db.close()
                        reservas = [x for x in reservas if es_reserva_futura(x.fecha, x.hora, pelu)]
                        if reservas:
                            items = []
                            for rr in reservas:
                                f = ymd_str(rr.fecha)
                                h = hhmm_str(rr.hora)
                                title = f"{formatea_fecha_es(f)} · {h}"
                                desc = getattr(getattr(rr, "servicio", None), "nombre", "")
                                if rr.nombre_cliente:
                                    desc = (desc + " · " if desc else "") + rr.nombre_cliente
                                items.append({"id": f"RID_{rr.id}", "title": title, "description": desc})

                            datos["telefono"] = tel
                            datos["last_choices_cancel"] = items  # ⬅️ guarda la última lista
                            estado["paso"] = "seleccionar_reserva_cancelar"
                            guardar_estado(session_id, estado)
                            return jsonify({
                                "respuesta": "Esa reserva ya ha pasado. ¿Qué reserva quieres cancelar?",
                                "ui": "res_list",
                                "choices": items
                            })

                    estado["paso"] = "buscar"
                    guardar_estado(session_id, estado)
                    return jsonify(
                        {"respuesta": "Esa reserva ya ha pasado. Escribe el teléfono para ver tus reservas futuras."})

                datos["reserva_id"] = rid
                estado["paso"] = "confirmar_cancelar"
                guardar_estado(session_id, estado)
                return jsonify({"respuesta": "¿Confirmas la cancelación de esa reserva? (*si*/*no*)"})

            # (0bis) Estando en selección de reserva para cancelar, si NO pulsan una fila válida, re-muestra la lista
            if estado["paso"] == "seleccionar_reserva_cancelar":
                m = (mensaje or "").strip()

                # 0) Si el usuario pulsa "➡️ Ver más reservas", deja que lo maneje el webhook
                # (el core ya no debe enviar nada aquí)
                if re.fullmatch(r"RID_NEXT_\d+", m):
                    # simplemente no respondas; el webhook ya se encargará de paginar
                    return jsonify({"respuesta": None})

                # 1) Si viene un RID válido (selección de reserva), deja que lo capture el fast-path de arriba
                if re.fullmatch(r"RID_\d+", m):
                    pass
                else:
                    # 2) Acepta "no" / "volver" para salir sin romper UX
                    t = m.lower()
                    if any(w in t for w in DENIAL_WORDS) or t in {"volver", "cancelar"}:
                        guardar_estado(session_id, {"paso": "inicio", "datos": {}, "tipo_accion": None})
                        return jsonify({
                            "respuesta": "Cancelación detenida. Te muestro el menú para continuar.",
                            "ui": "main_menu"
                        })

                    # 3) Re-muestra la lista que tenías; si no está, reconstruye por teléfono
                    choices = (estado.get("datos") or {}).get("last_choices_cancel") or []
                    if not choices:
                        tel = (estado.get("datos") or {}).get("telefono")
                        if tel:
                            db = SessionLocal()
                            try:
                                reservas = (
                                    db.query(Reserva)
                                    .options(selectinload(Reserva.servicio))
                                    .filter(
                                        Reserva.peluqueria_id == pelu.id,
                                        Reserva.telefono == tel,
                                        Reserva.estado == "confirmada",
                                    )
                                    .order_by(Reserva.fecha.desc(), Reserva.hora.desc())
                                    .all()
                                )
                            finally:
                                db.close()

                            reservas = [x for x in reservas if es_reserva_futura(x.fecha, x.hora, pelu)]
                            items = []
                            for r in reservas:
                                f = ymd_str(r.fecha)
                                h = hhmm_str(r.hora)
                                title = f"{formatea_fecha_es(f)} · {h}"
                                desc = getattr(getattr(r, "servicio", None), "nombre", "")
                                if r.nombre_cliente:
                                    desc = (desc + " · " if desc else "") + r.nombre_cliente
                                items.append({"id": f"RID_{r.id}", "title": title, "description": desc})
                            choices = items
                            estado["datos"]["last_choices_cancel"] = choices

                    guardar_estado(session_id, estado)
                    return jsonify({
                        "respuesta": "Por favor, elige una reserva de las disponibles.",
                        "ui": "res_list",
                        "choices": choices
                    })

            # (1) Buscar por teléfono
            if estado["paso"] == "buscar":
                telefono = interpreta_telefono(mensaje, getattr(pelu, 'country_code', None) or 'ES')
                if not telefono:
                    return jsonify({"respuesta": "El teléfono no es válido, ¿Puedes escribirlo de nuevo?"})

                db = SessionLocal()
                try:
                    reservas = (
                        db.query(Reserva)
                        .options(selectinload(Reserva.servicio))  # 👈 evita lazy-load
                        .filter(
                            Reserva.peluqueria_id == pelu.id,
                            Reserva.telefono == telefono,
                            Reserva.estado == "confirmada"
                        )
                        .order_by(Reserva.fecha.desc(), Reserva.hora.desc())
                        .all()
                    )

                    reservas = [x for x in reservas if es_reserva_futura(x.fecha, x.hora, pelu)]

                    if not reservas:
                        estado["paso"] = "cancelar_confirmar_continuar"
                        guardar_estado(session_id, estado)
                        return jsonify({
                            "respuesta": "No encuentro reservas con ese teléfono. ¿Quieres intentar con otro número? (*si*/*no*)"})

                    datos["telefono"] = telefono

                    if len(reservas) == 1:
                        r = reservas[0]
                        datos["reserva_id"] = r.id
                        datos["nombre"] = r.nombre_cliente
                        set_servicio_en_datos(datos, getattr(r, "servicio", None) or (
                            pelu.servicios[0] if pelu.servicios else None))
                        estado["paso"] = "confirmar_cancelar"
                        guardar_estado(session_id, estado)
                        resumen = f"Vas a cancelar la reserva del {formatea_fecha_es(r.fecha)} a las {hhmm_str(r.hora)}."
                        return jsonify({"respuesta": resumen + " ¿Confirmas la cancelación? (*si*/*no*)"})

                    # Varias reservas → lista
                    items = []
                    for r in reservas:
                        f = ymd_str(r.fecha)
                        h = hhmm_str(r.hora)
                        title = f"{formatea_fecha_es(f)} · {h}"
                        desc = getattr(getattr(r, "servicio", None), "nombre", "")
                        if r.nombre_cliente:
                            desc = (desc + " · " if desc else "") + r.nombre_cliente
                        items.append({"id": f"RID_{r.id}", "title": title, "description": desc})

                finally:
                    db.close()

                datos["last_choices_cancel"] = items  # ⬅️ guarda la última lista
                estado["paso"] = "seleccionar_reserva_cancelar"
                guardar_estado(session_id, estado)
                return jsonify({
                    "respuesta": "Tienes más de una reserva",
                    "ui": "res_list",
                    "choices": items
                })

            # (1b) ¿Intentar con otro número?
            if estado["paso"] == "cancelar_confirmar_continuar":
                t = (mensaje or "").strip().lower()
                if any(w in t for w in DENIAL_WORDS):
                    reset_estado(session_id)
                    return jsonify({"respuesta": "De acuerdo👌🏽, te devuelvo al menú principal.", "ui": "main_menu"})
                if any(w in t for w in AFFIRM_WORDS):
                    estado["paso"] = "buscar"
                    guardar_estado(session_id, estado)
                    return jsonify({"respuesta": "Escribe el teléfono📞 con el que hiciste la reserva que quieres cancelar."})
                return jsonify({"respuesta": "¿Quieres intentar con otro número? (*si*/*no*)"})

            # (2) Confirmación final (idempotente) + resumen + respuesta2
            if estado["paso"] == "confirmar_cancelar":
                t = (mensaje or "").strip().lower()

                # Negación
                if any(w in t for w in DENIAL_WORDS):
                    reset_estado(session_id)
                    return jsonify({
                        "respuesta": "De acuerdo👌🏽, no la cancelamos, te devuelvo al menú principal.",
                        "ui": "main_menu"
                    })

                # Pide confirmación explícita
                if not any(w in t for w in AFFIRM_WORDS):
                    guardar_estado(session_id, estado)
                    return jsonify({
                        "respuesta": "👉 Responde *si* para confirmar la cancelación\n👉 Responde *no* para cancelar la cancelación"})

                explicit_key = request.headers.get("Idempotency-Key")
                payload = {"reserva_id": datos.get("reserva_id")}
                idem_key, cached = idem_get("cancelar_confirm", pelu.id, payload, explicit_key)
                if cached:
                    return jsonify(cached["json"]), cached["status"]

                try:
                    # Cargar datos de la reserva (para resumen/purga/event_id)
                    db = SessionLocal()
                    try:
                        r = (
                            db.query(Reserva)
                            .options(selectinload(Reserva.servicio))
                            .filter_by(id=datos["reserva_id"])
                            .first()
                        )
                        if not r:
                            body = {"respuesta": "No encontré la reserva a cancelar."}
                            idem_set(idem_key, 404, body)
                            return jsonify(body), 200  # 200 para que el usuario lo vea
                        f = ymd_str(r.fecha)
                        h = hhmm_str(r.hora)
                        srv_nombre = getattr(getattr(r, "servicio", None), "nombre", "")
                        nombre_cli = getattr(r, "nombre_cliente", "")
                        telefono_cli = getattr(r, "telefono", "")
                        event_id = getattr(r, "event_id", None)
                    finally:
                        db.close()

                    # (A) Primero: CANCELAR EN BD con reintentos si hay lock_timeout
                    retries = 0
                    while True:
                        ok_bd = cancelar_reserva_db(datos["reserva_id"])
                        # ok_bd True/False o dict{error}
                        if not (isinstance(ok_bd, dict) and ok_bd.get("error") == "lock_timeout"):
                            break
                        if retries >= MAX_LOCK_RETRIES:
                            reset_estado(session_id)
                            body = {
                                "respuesta": "Ahora mismo estoy terminando otra operación. Intenta cancelar de nuevo en unos segundos.",
                                "ui": "main_menu"}
                            # Importante: NO cacheamos fallos transitorios
                            return jsonify(body), 200
                        _sleep_backoff(retries)
                        retries += 1

                    # Si no existe o no pudo cancelarse
                    if ok_bd is False:
                        reset_estado(session_id)
                        body = {"respuesta": "No he podido cancelar en este momento, inténtalo más tarde.",
                                "ui": "main_menu"}
                        idem_set(idem_key, 200, body)  # cacheamos respuesta amable final
                        return jsonify(body), 200

                    # Si ya estaba cancelada, seguimos como OK (idempotente)
                    # ok_bd True => cancelación efectiva o ya cancelada

                    # (B) Después: intentar cancelar en Google Calendar (best-effort)
                    # Aunque falle, la reserva YA está cancelada en BD y devolvemos éxito al usuario.
                    try:
                        if event_id:
                            _ = cancelar_reserva_google(pelu, event_id)
                    except Exception as _e:
                        try:
                            sentry_sdk.capture_exception(_e)
                        except Exception:
                            pass

                    # (C) Purga de caché del día real de la reserva
                    try:
                        if f:
                            purge_horas_cache(pelu, f)
                    except Exception as _e:
                        try:
                            sentry_sdk.capture_exception(_e)
                        except Exception:
                            pass

                    # (D) OK → post_confirm con mini-resumen + segundo mensaje
                    estado["paso"] = "post_confirm"
                    guardar_estado(session_id, estado)

                    lineas = ["Reserva cancelada:"]
                    if len(getattr(pelu, "servicios", []) or []) > 1 and srv_nombre:
                        lineas.append(f"Servicio: {srv_nombre}")
                    if f: lineas.append(f"Fecha: {formatea_fecha_es(f)}")
                    if h: lineas.append(f"Hora: {h}")
                    if nombre_cli: lineas.append(f"Nombre: {nombre_cli}")
                    if telefono_cli: lineas.append(f"Teléfono: {telefono_cli}")
                    resumen = "\n".join(lineas)

                    body = {"respuesta": f"❌ {resumen}", "respuesta2": "¿Quieres hacer algo más? (*si*/*no*)"}
                    idem_set(idem_key, 200, body)
                    return jsonify(body), 200

                except Exception as e:
                    try:
                        sentry_sdk.capture_exception(e)
                    except Exception:
                        pass
                    logging.error(f"Error cancelando reserva: {e}", exc_info=True)
                    # Siempre 200 para que el usuario reciba mensaje
                    guardar_estado(session_id,
                                   {"paso": "inicio", "datos": {}, "tipo_accion": None, "force_welcome": True})
                    body = {"respuesta": "Ocurrió un error al cancelar la reserva, inténtalo de nuevo.",
                            "ui": "main_menu"}
                    # No lo cacheo si quieres permitir reintento inmediato; si prefieres, puedes idem_set con 200
                    return jsonify(body), 200

            # (3) Post confirm (sí → menú, no → despedida y force_welcome)
            if estado["paso"] == "post_confirm":
                t = (mensaje or "").strip().lower()
                if any(w in t for w in DENIAL_WORDS):
                    guardar_estado(session_id,
                                   {"paso": "inicio", "datos": {}, "tipo_accion": None, "force_welcome": True})
                    return jsonify({"respuesta": "Entendido. ¡Que tengas un buen día! 👋"})
                guardar_estado(session_id, {"paso": "inicio", "datos": {}, "tipo_accion": None})
                return jsonify({"respuesta": "Perfecto, te muestro el menú para continuar", "ui": "main_menu"})

        # ---------------------------------------------------------
        # FLUJO: DUDA
        # ---------------------------------------------------------
        if estado["tipo_accion"] == "duda":
            t = (mensaje or "").strip().lower()

            # Después de contestar una duda, solo preguntamos si tiene otra (sí/no)
            if estado["paso"] == "duda_confirmar":
                if any(w in t for w in AFFIRM_WORDS):
                    estado["paso"] = "duda"
                    guardar_estado(session_id, estado)
                    return jsonify({"respuesta": "¡Perfecto! Cuéntame tu otra duda.❓"})

                if any(w in t for w in DENIAL_WORDS):
                    guardar_estado(session_id,{"paso": "inicio", "datos": {}, "tipo_accion": None, "force_welcome": True})
                    return jsonify({"respuesta": "¡Perfecto! Si necesitas algo más, aquí estoy.👋"})

                # Entrada ambigua → repregunta
                guardar_estado(session_id, estado)
                return jsonify({"respuesta": "¿Tienes otra duda? (*si*/*no*)."})

            # Respuesta libre de IA a la duda
            if estado["paso"] == "duda":
                try:
                    respuesta_ia = interpreta_ia(mensaje, "duda", pelu)
                except Exception as e:
                    sentry_sdk.capture_exception(e)
                    logging.error(f"Error llamando a interpreta_ia: {e}", exc_info=True)
                    guardar_estado(session_id,{"paso": "inicio", "datos": {}, "tipo_accion": None, "force_welcome": True}
                    )
                    return jsonify({
                        "respuesta": "No he podido consultar la información. Intentalo más tarde."
                    })
                # Tras responder, pasamos a confirmar si quiere otra duda
                estado["paso"] = "duda_confirmar"
                guardar_estado(session_id, estado)
                return jsonify({"respuesta": f"{respuesta_ia}\n\n¿Tienes otra duda? (*si*/*no*)"})

        # Si llegamos aquí, el estado no encaja
        guardar_estado(session_id, {"paso": "inicio", "datos": {}, "tipo_accion": None})
        return jsonify({"respuesta": return_text(pelu.nombre, pelu.tipo_negocio), "ui": "main_menu"})


    except Exception as e:
        sentry_sdk.capture_exception(e)

        logging.error(f"Error en api_post: {e}", exc_info=True)

        guardar_estado(session_id,{"paso": "inicio", "datos": {}, "tipo_accion": None, "force_welcome": True})

        return jsonify({
            "respuesta": "Ha ocurrido un error interno. Por favor, inténtalo más tarde."
        }), 500

# ================================================
# Main (solo DEV)
# ================================================
if __name__ == "__main__":
    # Debug=True para recarga en caliente durante desarrollo (no usar en producción)
    app.run(host="0.0.0.0", port=5000, debug=True)



