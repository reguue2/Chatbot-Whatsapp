# Configuración de Sentry: segura en dev/Windows, con scrub de PII
import os, json, re
import sentry_sdk
from sentry_sdk.integrations.flask import FlaskIntegration
from sentry_sdk.integrations.logging import LoggingIntegration

TEL_RE = re.compile(r"\+?\d{6,15}")
BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9\-\._~+/=]+", re.IGNORECASE)

def _scrub_event_dict(ev: dict) -> dict:
    # Scrub directo en request.headers/body sin serializar todo el evento
    try:
        req = ev.get("request") or {}
        # Headers
        headers = req.get("headers") or {}
        if isinstance(headers, dict):
            # Borra Authorization y Cookies; evita tokens en claro
            headers.pop("Authorization", None)
            headers.pop("authorization", None)
            headers.pop("Cookie", None)
            headers.pop("cookie", None)
        # Datos del body como texto si existen
        data = req.get("data")
        if isinstance(data, str):
            data = TEL_RE.sub("[TEL]", data)
            data = BEARER_RE.sub("Bearer [REDACTED]", data)
            req["data"] = data
        ev["request"] = req
    except Exception:
        # Si algo falla, seguimos; no capturamos aquí para evitar bucles
        pass
    return ev

def before_send(event, hint):
    # Filtra errores ruidosos del reloader de Flask/Windows
    try:
        exc = (hint or {}).get("exc_info")
        if exc:
            etype, evalue, _ = exc
            # TypeError por FrameLocalsProxy durante autoreload
            if isinstance(evalue, TypeError) and "FrameLocalsProxy" in str(evalue):
                return None
            # OSError de sockets en cierre del servidor dev (Windows)
            if isinstance(evalue, OSError) and getattr(evalue, "winerror", None) == 10038:
                return None
    except Exception:
        pass

    # Scrub conservador: teléfonos y bearer tokens en todo el evento
    try:
        event = _scrub_event_dict(event)
        dumped = json.dumps(event, default=str)
        dumped = TEL_RE.sub("[TEL]", dumped)
        dumped = BEARER_RE.sub("Bearer [REDACTED]", dumped)
        return json.loads(dumped)
    except Exception:
        return event  # no re-intentar ni capturar aquí

def init_sentry():
    dsn = os.getenv("SENTRY_DSN")
    if not dsn:
        return

    env = os.getenv("SENTRY_ENV", "development")
    # En prod, habilita algo de tracing; en dev, mejor 0
    traces_rate = float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.2" if env == "production" else "0.0"))
    profiles_rate = float(os.getenv("SENTRY_PROFILES_SAMPLE_RATE", "0.1" if env == "production" else "0.0"))

    sentry_sdk.init(
        dsn=dsn,
        integrations=[
            FlaskIntegration(),
            LoggingIntegration(level=None, event_level=None),
        ],
        environment=env,
        release=os.getenv("SENTRY_RELEASE"),   # opcional: añade versión/commit
        send_default_pii=False,                 # no enviar PII por defecto
        with_locals=(env == "production"),      # evita FrameLocalsProxy en dev/Windows
        max_breadcrumbs=50,
        traces_sample_rate=traces_rate,
        profiles_sample_rate=profiles_rate,
        before_send=before_send,
        debug=False,
    )
