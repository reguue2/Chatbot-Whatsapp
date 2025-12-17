import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from openai import OpenAI
import dateparser
import sentry_sdk

from settings import settings

# -----------------------
# Cliente OpenAI (API moderna)
# -----------------------

client = OpenAI(api_key=settings.OPENAI_API_KEY)

# -----------------------
# Normalizadores y utilidades
# -----------------------

def _fmt_precio(p):
    if isinstance(p, (int, float)):
        s = f"{p:.2f}"
        return s.rstrip("0").rstrip(".")
    return str(p) if p is not None else None

def _clean_leading(texto: str) -> str:
    """Limpieza ligera para ayudar a la IA (sin lógica de negocio)."""
    if not texto:
        return ""
    t = (texto or "").strip()
    t = t.strip(" \t\n\r\"'¡!¿?.")
    return t

def _normalize_date_output(s: str) -> str | None:
    """
    Normaliza salida de la IA a YYYY-MM-DD aceptando:
      - '2025-8-3'
      - '2025-08-03'
      - '2025-08-03.'  -> quita puntuación final
    """
    if not s:
        return None
    s = s.strip().strip(" .,'\"")
    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", s)
    if not m:
        return None
    y, mo, d = m.groups()
    try:
        y, mo, d = int(y), int(mo), int(d)
        dt = datetime(y, mo, d)
        return dt.strftime("%Y-%m-%d")
    except Exception as e:
        sentry_sdk.capture_exception(e)
        return None

def _today_iso_for_pelu(pelu) -> str:
    """
    Devuelve 'YYYY-MM-DD' usando la TZ de la peluquería si existe;
    si no, usa settings.CAL_TZ como fallback.
    """
    tzname = getattr(pelu, "tz", None) or getattr(settings, "CAL_TZ", "Europe/Madrid")
    try:
        hoy = datetime.now(ZoneInfo(tzname)).date()
    except Exception:
        hoy = datetime.utcnow().date()
    return hoy.strftime("%Y-%m-%d")

# -----------------------
# Moneda por peluquería (para 'duda')
# -----------------------

def _currency_code(pelu) -> str:
    code = getattr(pelu, "currency_code", None)
    return (code or "EUR").upper()

def _currency_symbol(code: str) -> str:
    code = (code or "").upper()
    return {
        "EUR": "€",
        "USD": "$",
        "UYU": "$",
        "MXN": "$",
        "CLP": "$",
        "ARS": "$",
        "COP": "$",
        "PEN": "S/",
        "BRL": "R$",
        "GBP": "£",
        "DOP": "$",
        "CRC": "₡"
    }.get(code, code)

def _fmt_money_for_pelu(pelu, amount) -> str | None:
    if amount is None:
        return None
    try:
        val = float(amount)
    except Exception:
        return str(amount)

    code = _currency_code(pelu)
    sym = _currency_symbol(code)
    txt = _fmt_precio(val)
    return f"{sym} {txt} {code}"

# -----------------------
# IA principal
# -----------------------

def interpreta_ia(texto, paso, pelu):
    mensaje = _clean_leading(texto)

    if paso == "intencion":
        prompt = (
            "Clasifica la intención del usuario en un chatbot de una peluquería.\n"
            "Opciones visibles:\n"
            "1. Reservar cita\n"
            "2. Cancelar una cita\n"
            "3. Tengo una duda\n\n"
            "Devuelve exactamente una de estas palabras: 'reservar', 'cancelar', 'duda', 'NO_ENTIENDO'.\n"
            f"Mensaje: {mensaje}"
        )

    elif paso == "servicio":
        servicios = ", ".join([s.nombre for s in getattr(pelu, "servicios", [])])
        prompt = (
            "Eres el asistente de una peluquería. INTERPRETA el servicio que pide el cliente.\n"
            f"Servicios disponibles: {servicios}.\n"
            f"Mensaje: {mensaje}\n"
            "Devuelve solo el nombre exacto del servicio o 'NO_ENTIENDO'."
        )

    elif paso == "fecha":
        hoy_iso = _today_iso_for_pelu(pelu)
        prompt = (
            f"Hoy es {hoy_iso} (formato ISO YYYY-MM-DD).\n"
            f"El cliente dice: {mensaje}\n\n"
            "TAREA: Interpreta a qué FECHA concreta se refiere el cliente.\n"
            "Acepta SOLO estos formatos de entrada: "
            "'03/09/2025', '3/9/25', '3-9-25', '03-09-2025', '2025-09-03', '2025/9/3', "
            "'15 de octubre', '15 octubre 2025', 'oct 15', '15 oct', '15 oct 25', "
            "'octubre 15, 2025', 'oct 3, 2025', '3 oct', '3 oct 25', 'oct 3', "
            "'3 de octubre de 2025', '25 diciembre', '25 dic 25', 'dic 25', 'diciembre 25, 2025'.\n"
            "REGLAS IMPORTANTES:\n"
            "1) Si el texto es ambiguo y NO puedes estar 100% seguro, devuelve EXACTAMENTE 'NO_ENTIENDO'.\n"
            "2) La salida debe ser SOLO una fecha en formato EXACTO 'YYYY-MM-DD' (por ejemplo 2025-09-16), sin texto adicional.\n"
        )

    elif paso == "duda":
        servicios_detallados = []
        for s in getattr(pelu, "servicios", []):
            nombre = getattr(s, "nombre", "Servicio")
            precio = getattr(s, "precio", None)
            dur = getattr(s, "duracion_min", None)

            partes = [nombre]
            if precio is not None:
                partes.append(_fmt_money_for_pelu(pelu, precio))
            if dur is not None:
                partes.append(f"{dur} min")
            servicios_detallados.append(" - " + " · ".join(partes))

        servicios_txt = "\n".join(servicios_detallados) if servicios_detallados else "(sin servicios configurados)"
        prompt = (
            f"Eres la secretaria virtual de la peluquería {getattr(pelu, 'nombre', '(sin nombre)')}.\n"
            "Tu misión es contestar la duda del cliente usando EXCLUSIVAMENTE estos datos:\n"
            f"- Dirección: {getattr(pelu, 'direccion', '')}\n"
            f"- Días cerrados: {getattr(pelu, 'dias_cerrados', '')}\n"
            f"- Horarios: {getattr(pelu, 'horario', '')}\n"
            f"- Telefono de la Peluqueria: {getattr(pelu, 'telefono_peluqueria', '')}\n"
            f"- Servicios disponibles:\n{servicios_txt}\n"
            f"- Número de peluqueros: {getattr(pelu, 'num_peluqueros', '')}\n"
            f"- Información adicional: {getattr(pelu, 'info', '')}\n\n"
            "REGLAS ESTRICTAS:\n"
            "1. Si el cliente pregunta por un servicio que NO está en la lista, responde que no ofrece ese servicio.\n"
            "2. Si el cliente pregunta por horarios o días que no coinciden con los listados, responde que en esos horarios/días no se atiende.\n"
            f"3. Si el cliente pide información no incluida en los datos, responde exactamente: "
            f"'Lo siento, no dispongo de esa información. Por favor, contacta directamente con la peluquería en el número "
            f"{getattr(pelu, 'telefono_peluqueria', '')}'.\n"
            "4. Nunca inventes servicios, precios ni horarios.\n"
            "5. Si el cliente pide hablar con una persona, proporciona el teléfono de la peluquería.\n"
            f"Mensaje del cliente: {mensaje}"
        )

    else:
        return "NO_ENTIENDO"

    try:
        respuesta = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Eres un experto en interpretar texto para reservas de peluquería. Responde solo con lo pedido."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=60 if paso != "duda" else 120,
            temperature=0.2
        )
        salida = (respuesta.choices[0].message.content or "").strip()
        if not salida:
            return "NO_ENTIENDO"
        return salida
    except Exception as e:
        sentry_sdk.capture_exception(e)
        logging.error(f"Error en interpreta_ia: {e}", exc_info=True)
        return "NO_ENTIENDO"

def interpreta_hora(texto_usuario):
    txt = _clean_leading(texto_usuario).lower()

    if txt in {"mediodia", "mediodía"}:
        return datetime.strptime("12:00", "%H:%M").time()
    if txt == "medianoche":
        return datetime.strptime("00:00", "%H:%M").time()

    prompt = (
        f"Extrae una hora del mensaje: '{texto_usuario}'. "
        "Devuelve SOLO una hora en 24h HH:MM (con cero inicial). "
        "Acepta: '17:30', '5 pm', '5 y media', '5 menos cuarto', 'mediodía', 'medianoche'. "
        "Si no logras interpretarlo, responde 'NO_ENTIENDO'."
    )
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "Eres experto en interpretar horas en español. Responde solo con HH:MM o 'NO_ENTIENDO'."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=12,
            temperature=0.2
        )
        salida = (response.choices[0].message.content or "").strip()
        if not salida or salida.lower().startswith("no entiendo"):
            return None

        hora = dateparser.parse(salida, languages=["es"])
        if hora:
            return hora.time()

        if re.fullmatch(r"\d{2}:\d{2}", salida):
            try:
                return datetime.strptime(salida, "%H:%M").time()
            except Exception:
                pass

    except Exception as e:
        sentry_sdk.capture_exception(e)
        logging.error(f"Error en interpreta_hora: {e}", exc_info=True)
    return None

def interpreta_telefono(mensaje, default_region: str | None = None):
    from phone_utils import normalize_msisdn
    return normalize_msisdn(mensaje, default_region)

def interpreta_fecha(texto, pelu):
    dt = dateparser.parse(
        texto,
        languages=["es"],
        settings={
            "DATE_ORDER": "DMY",
            "PREFER_DAY_OF_MONTH": "first",
            "RELATIVE_BASE": datetime.now(
                ZoneInfo(getattr(pelu, "tz", None) or settings.CAL_TZ)
            ),
        },
    )
    if dt:
        return dt.date().strftime("%Y-%m-%d")

    out = interpreta_ia(texto, "fecha", pelu)
    norm = _normalize_date_output(out)
    return norm if norm else "NO_ENTIENDO"

