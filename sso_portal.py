"""Entrada única desde el portal Ecommerce Inhumario (ecommerce.inhumario.com).

El portal manda a `https://seo-aromas.inhumario.com/?sso=<billete>`. El billete es
base64url(JSON) + "." + hex(HMAC-SHA256(SSO_KEY_ECOMMERCE, b64)), dura 60 s y
lleva `n` (aleatorio de un solo uso). Campos: email, nombre, cliente,
cliente_nombre, papel, admin, mfa, ir, exp, n.

Solo comprueba el billete; quién es el usuario lo decide app.py (por email).
El login normal (usuario + contraseña) no cambia.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
import time

_USADOS: dict[str, float] = {}  # n -> exp (la app corre en un solo proceso, 1 réplica)
_LOCK = threading.Lock()


def configurado() -> bool:
    return bool(os.environ.get("SSO_KEY_ECOMMERCE", "").strip())


def comprobar(tok: str) -> tuple[dict | None, str]:
    """Devuelve (datos, "") si el billete es bueno o (None, motivo) si no."""
    clave = os.environ.get("SSO_KEY_ECOMMERCE", "").strip()
    if not clave:
        return None, "La entrada desde el portal no está configurada en esta app."
    try:
        b, firma = str(tok).strip().rsplit(".", 1)
        esperada = hmac.new(clave.encode(), b.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(firma, esperada):
            return None, "El enlace del portal no es válido."
        datos = json.loads(base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)))
    except Exception:
        return None, "El enlace del portal no es válido."
    ahora = time.time()
    exp = float(datos.get("exp") or 0)
    if exp < ahora:
        return None, "El enlace del portal ha caducado. Vuelve a entrar desde el portal."
    n = str(datos.get("n") or "")
    if not n:
        return None, "El enlace del portal no es válido."
    with _LOCK:
        for k in [k for k, v in _USADOS.items() if v < ahora]:
            _USADOS.pop(k, None)
        if n in _USADOS:
            return None, "Ese enlace del portal ya se usó. Vuelve a entrar desde el portal."
        _USADOS[n] = exp
    if not str(datos.get("email") or "").strip():
        return None, "El enlace del portal no trae email."
    return datos, ""
