#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
send_reminders.py — Recordatorios simples "día anterior a hora fija" vía cron.

Uso recomendado (cron a las 09:00 todos los días):
    0 9 * * * /opt/bot-pelu/.venv/bin/python /opt/bot-pelu/send_reminders.py >> /var/log/reminders.log 2>&1

Requisitos:
- Acceso a tu mismo entorno del bot (variables de entorno, settings, db, models, storage).
- No requiere Celery.
- Evita duplicados usando Redis (storage) con una marca por reserva (TTL ~72h).
"""

import logging
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Asegura que PYTHONPATH apunte al proyecto si ejecutas fuera
# sys.path.append("/opt/bot-pelu/Bot-Peluqueria-bueno")  # <-- ajusta si es necesario

from db import SessionLocal
from models import Reserva, Peluqueria  # asumiendo relación Reserva.peluqueria
from sqlalchemy.orm import selectinload
from settings import settings
from storage import get_storage

# Opcional: si tienes util para formato en español
try:
    from reserva_utils import formatea_fecha_es as _fmt_es
except Exception:
    _fmt_es = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("reminders")

storage = get_storage(settings)

def fmt_fecha_es(fecha_ymd: str) -> str:
    """Intenta usar formatea_fecha_es si está disponible; si no, fallback simple."""
    if _fmt_es:
        try:
            return _fmt_es(fecha_ymd)
        except Exception:
            pass
    try:
        d = datetime.strptime(fecha_ymd, "%Y-%m-%d").date()
        meses = ["enero","febrero","marzo","abril","mayo","junio","julio","agosto","septiembre","octubre","noviembre","diciembre"]
        return f"{d.day} de {meses[d.month-1]} de {d.year}"
    except Exception:
        return fecha_ymd

def hhmm_str(value) -> str:
    try:
        if hasattr(value, "strftime"):
            return value.strftime("%H:%M")
    except Exception:
        pass
    if isinstance(value, str):
        s = value.strip()
        return s[:5] if len(s) >= 5 else s
    try:
        h, m = value
        return f"{int(h):02d}:{int(m):02d}"
    except Exception:
        return str(value)

def wa_send_text(token: str, graph_ver: str, phone_number_id: str, to: str, body: str) -> bool:
    url = f"https://graph.facebook.com/{graph_ver}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": body[:4096]},
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        if not r.ok:
            log.warning("WA reminder failed %s: %s", r.status_code, r.text[:200])
        return r.ok
    except Exception as e:
        log.exception("WA reminder exception: %s", e)
        return False

def main():
    tz = ZoneInfo(getattr(settings, "APP_TZ", "Europe/Madrid"))
    today_local = datetime.now(tz).date()
    target_date = today_local + timedelta(days=1)  # mañana (no exacto 24h)
    target_str = target_date.strftime("%Y-%m-%d")

    log.info("Buscando reservas para mañana (%s)", target_str)

    sent = skipped = failed = 0

    with SessionLocal() as db:
        # Cargamos peluquería y (si existe) servicio para personalizar el texto
        qs = (
            db.query(Reserva)
            .options(
                selectinload(Reserva.peluqueria),
                selectinload(Reserva.servicio),
            )
            .filter(
                Reserva.fecha == target_date,
                Reserva.estado == "confirmada")
        )

        for r in qs.all():
            try:
                # Validaciones mínimas
                pelu = getattr(r, "peluqueria", None)
                if not pelu or not getattr(pelu, "wa_token", None) or not getattr(pelu, "wa_phone_number_id", None):
                    skipped += 1
                    continue
                if not getattr(r, "telefono", None):
                    skipped += 1
                    continue

                # Evita duplicados con una marca en Redis durante 72h
                seen_key = f"rem24:{r.id}"
                if storage.get(seen_key):
                    skipped += 1
                    continue

                nombre_pelu = getattr(pelu, "nombre", "")
                servicio = getattr(getattr(r, "servicio", None), "nombre", None)

                fecha_txt = fmt_fecha_es(target_str)
                hora_txt = hhmm_str(getattr(r, "hora", ""))

                if servicio:
                    body = (f"🔔 Recordatorio: tu cita en {nombre_pelu} es mañana.\n\n"
                            f"📅 {fecha_txt} a las {hora_txt}\n"
                            f"🧾 Servicio: {servicio}\n\n"
                            f"Si no puedes asistir, cancela tu cita.")
                else:
                    body = (f"🔔 Recordatorio: tu cita en {nombre_pelu} es mañana.\n\n"
                            f"📅 {fecha_txt} a las {hora_txt}\n\n"
                            f"Si no puedes asistir, cancela tu cita.")

                ok = wa_send_text(
                    token=pelu.wa_token,
                    graph_ver=settings.GRAPH_API_VERSION,
                    phone_number_id=pelu.wa_phone_number_id,
                    to=r.telefono,
                    body=body
                )
                if ok:
                    # Marca como enviado (TTL 72h para cubrir reintentos de cron)
                    storage.setex(seen_key, "1", ttl=72 * 3600)
                    sent += 1
                else:
                    failed += 1
            except Exception as e:
                log.exception("Error procesando reserva %s: %s", getattr(r, "id", "?"), e)
                failed += 1

    log.info("Resumen: enviados=%d, omitidos=%d, fallidos=%d", sent, skipped, failed)

if __name__ == "__main__":
    main()
