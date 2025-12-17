# tests/unit/test_whatsapp_helpers.py
import hmac, hashlib
from importlib import import_module


def test_verify_waba_signature_valida(monkeypatch):
    app =  __import__("Bot_ia_secretaria_peluqueria.app")
    secret = "abc123"
    body = b'{"hello":"world"}'
    mac = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    header = f"sha256={mac}"
    assert app.verify_waba_signature(secret, body, header) is True

def test_verify_waba_signature_invalida():
    from Bot_ia_secretaria_peluqueria.app import verify_waba_signature
    assert verify_waba_signature("abc", b"x", "sha256=00") is False
    assert verify_waba_signature("abc", b"x", "") is False

def test_wa_normalize_session_id():
    from Bot_ia_secretaria_peluqueria.app import _wa_normalize_session_id
    assert _wa_normalize_session_id("", "600000000") == "wa_600000000"
    assert _wa_normalize_session_id("wa_abc", "600000000") == "wa_abc"
    assert _wa_normalize_session_id(None, "") == "wa_unknown"

def test_idempotency_key_deterministico():
    from Bot_ia_secretaria_peluqueria.app import _wa_idempotency_key
    p1 = {"a": 1, "b": 2}
    p2 = {"b": 2, "a": 1}  # distinto orden â†’ misma hash por sort_keys
    k1 = _wa_idempotency_key("wa_600", p1)
    k2 = _wa_idempotency_key("wa_600", p2)
    assert k1 == k2

def test_should_process_by_ts_ok(fake_storage):
    from Bot_ia_secretaria_peluqueria.app import should_process_by_ts
    sid = "wa_600"
    assert should_process_by_ts(sid, 100) is True
    assert should_process_by_ts(sid, 99) is False
    assert should_process_by_ts(sid, 100) is False
    assert should_process_by_ts(sid, 101) is True

def test_is_current(fake_storage):
    from Bot_ia_secretaria_peluqueria.app import is_current
    fake_storage.setex("last_ts:wa_1", "123", ttl=600)
    assert is_current("wa_1", 123) is True
    assert is_current("wa_1", 124) is False
