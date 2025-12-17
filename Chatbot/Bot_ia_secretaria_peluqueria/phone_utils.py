# phone_utils.py
import phonenumbers

def normalize_msisdn(raw: str, default_region: str | None = None) -> str | None:
    """
    Devuelve el número en formato E.164 (+<country><nsn>) o None si no es válido.
    default_region: código ISO-2 (ES, UY, MX, AR...) usado cuando el número no trae prefijo.
    """
    if not raw:
        return None
    s = "".join(ch for ch in str(raw) if ch.isdigit() or ch == "+")
    try:
        num = phonenumbers.parse(s, (default_region or "ES"))
        if not phonenumbers.is_valid_number(num):
            return None
        return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)
    except Exception:
        return None

def is_valid_msisdn(raw: str, default_region: str | None = None) -> bool:
    return normalize_msisdn(raw, default_region) is not None
