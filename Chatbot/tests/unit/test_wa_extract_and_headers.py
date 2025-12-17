# tests/unit/test_wa_extract_and_headers.py

def test_wa_extract_text_text():
    from Bot_ia_secretaria_peluqueria.app import wa_extract_text
    assert wa_extract_text({"type":"text","text":{"body":"hola"}}) == "hola"

def test_wa_extract_text_button():
    from Bot_ia_secretaria_peluqueria.app import wa_extract_text
    assert wa_extract_text({"type":"button","button":{"text":"Reservar"}}) == "Reservar"

def test_wa_extract_text_interactive_button_reply():
    from Bot_ia_secretaria_peluqueria.app import wa_extract_text
    obj = {"type":"interactive","interactive":{"type":"button_reply","button_reply":{"title":"Cancelar"}}}
    assert wa_extract_text(obj) == "Cancelar"

def test_wa_extract_text_interactive_list_reply():
    from Bot_ia_secretaria_peluqueria.app import wa_extract_text
    obj = {"type":"interactive","interactive":{"type":"list_reply","list_reply":{"title":"Corte"}}}
    assert wa_extract_text(obj) == "Corte"

def test_wa_extract_text_none():
    from Bot_ia_secretaria_peluqueria.app import wa_extract_text
    assert wa_extract_text({"type":"image"}) is None

def test_wa_headers_incluye_idempotency_key_estable():
    from Bot_ia_secretaria_peluqueria.app import _wa_headers
    h1 = _wa_headers("T","wa_1", {"a":1})
    h2 = _wa_headers("T","wa_1", {"a":1})
    assert h1["Authorization"].startswith("Bearer ")
    assert "X-Idempotency-Key" in h1
    assert h1["X-Idempotency-Key"] == h2["X-Idempotency-Key"]

def test_wa_headers_idempotency_key_cambia_si_payload_cambia():
    from Bot_ia_secretaria_peluqueria.app import _wa_headers
    h1 = _wa_headers("T","wa_1", {"a":1})
    h2 = _wa_headers("T","wa_1", {"a":2})
    assert h1["X-Idempotency-Key"] != h2["X-Idempotency-Key"]
