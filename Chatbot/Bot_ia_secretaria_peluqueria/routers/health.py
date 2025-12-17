"""Blueprint con endpoints de salud y readiness."""

import os

import sentry_sdk
from flask import Blueprint, jsonify
from sqlalchemy import text

from db import SessionLocal

try:
    from google_calendar_utils import get_calendar_service  # type: ignore
except Exception as import_error:  # pragma: no cover - import guard
    sentry_sdk.capture_exception(import_error)
    get_calendar_service = None  # type: ignore

bp = Blueprint("health", __name__)


@bp.get("/live")
def live():
    """Liveness simple: proceso responde."""
    return jsonify({"status": "alive"}), 200


@bp.get("/health")
def health():
    """Health simple: OK si la app responde."""
    return jsonify({"status": "OK"}), 200


@bp.get("/ready")
def ready():
    """Readiness: DB + Redis + GCal."""
    payload = {"status": "OK"}
    status_code = 200

    # Base de datos: SELECT 1 con SessionLocal para verificar conectividad crítica.
    db_session = None
    try:
        db_session = SessionLocal()
        db_session.execute(text("SELECT 1"))
        payload["db"] = True
    except Exception as exc:  # pragma: no cover - rutas de error
        sentry_sdk.capture_exception(exc)
        payload["db"] = False
        payload["status"] = "DEGRADED"
        status_code = 503
    finally:
        if db_session is not None:
            try:
                db_session.close()
            except Exception as exc:  # pragma: no cover - rutas de error
                sentry_sdk.capture_exception(exc)

    # Redis: setex "probe" 5 segundos solo si REDIS_URL está definido.
    redis_url = os.getenv("REDIS_URL")
    if redis_url:
        try:
            import redis  # type: ignore

            client = redis.Redis.from_url(redis_url)
            client.setex("probe", 5, "1")
            payload["redis"] = True
        except Exception as exc:  # pragma: no cover - rutas de error
            sentry_sdk.capture_exception(exc)
            payload["redis"] = False
            payload["status"] = "DEGRADED"
            status_code = 503
    else:
        payload["redis"] = None

    # Google Calendar: intenta obtener el servicio si la utilidad existe.
    if get_calendar_service is not None:
        try:
            service = get_calendar_service()
            service.calendarList().list(maxResults=1).execute()
            payload["gcal"] = True
        except Exception as exc:  # pragma: no cover - rutas de error
            sentry_sdk.capture_exception(exc)
            payload["gcal"] = False
            payload["status"] = "DEGRADED"
            status_code = 503
    else:
        payload["gcal"] = None

    return jsonify(payload), status_code